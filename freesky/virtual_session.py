"""Runs a web page as a live HLS channel.

One "session" is a private X display, a headful Chromium showing the page, a
PulseAudio null-sink that Chromium plays into, and an ffmpeg that grabs the
display and that sink's monitor and writes a rolling HLS playlist. Sessions are
started on demand when a player asks for the channel and torn down after
`idle_timeout` seconds with no request.

Why this shape rather than the obvious alternatives
---------------------------------------------------
* **Headful Chromium under Xvfb, not `--headless=new`.** Headless Chrome has no
  reliable audio output path in a container — this is a long-standing, still-open
  problem (puppeteer-stream#189), and the production writeups that do this at
  scale (Mux) all run a real browser against a virtual display. With Xvfb we get
  an ordinary X display and an ordinary PulseAudio client, and `x11grab` gives
  real frames with no CDP round-trip.
* **x11grab rather than CDP `Page.startScreencast`.** The screencast API delivers
  base64 JPEG frames at a variable rate and carries no audio at all, so we would
  have to re-time frames ourselves. It also couples stream liveness to Chromium:
  with x11grab, Chromium can crash and be relaunched into the same display while
  ffmpeg keeps running and viewers see only a few black frames.
* **Playwright rather than raw Chromium.** The browser and its matching Chromium
  build are already a dependency of this repo, and we need real page control
  anyway to dismiss consent dialogs and click a play button.
* **Plain HLS, not LL-HLS.** ffmpeg's `hls` muxer does not implement Apple
  LL-HLS — there is no `EXT-X-PART`, no partial segments, and no `hls_part_size`
  option, despite what several guides claim (`-lhls` is a *dash* muxer option
  implementing the abandoned 2019 draft). Real LL-HLS needs a separate packager.
  2s segments give ~6s latency, which is right for a "watch what the browser is
  showing" channel.

Nothing here runs until a virtual channel is actually requested, so the cost on
an install that never uses the feature is one import.
"""
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Dict, Optional

from freesky import virtual_channels

logger = logging.getLogger(__name__)

# Where HLS segments are written. Should be a tmpfs: segments are write-heavy,
# live for seconds, and are never worth touching disk.
HLS_ROOT = os.environ.get("VIRTUAL_HLS_ROOT", "/tmp/freesky-hls")

# Per-channel flock files (see VirtualSession._acquire_channel_lock). Kept out of
# HLS_ROOT because stop() rmtree's a channel's HLS directory, and deleting the
# file an flock is held on drops the exclusion silently. /tmp is container-local
# and cleared on restart, which is exactly the lifetime a runtime lock wants.
LOCK_ROOT = os.environ.get("VIRTUAL_LOCK_ROOT", "/tmp/freesky-locks")

# X display numbers are allocated from here upward. 99 by convention, and well
# clear of anything a desktop session would claim.
_DISPLAY_BASE = int(os.environ.get("VIRTUAL_DISPLAY_BASE", "99"))

# Runtime dir for the container-local PulseAudio daemon. Deliberately not the
# host's socket: we want our own daemon at the same UID as Chromium and ffmpeg.
# Browser profiles. These live on the DATA VOLUME, not in /tmp, because they
# hold the cookies and stored logins that let a channel come back after a
# restart already signed in instead of at a login page. One directory per
# channel, kept across sessions and across container restarts.
PROFILE_ROOT = os.environ.get(
    "VIRTUAL_PROFILE_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "virtual-profiles"),
)

_PULSE_DIR = os.environ.get("VIRTUAL_PULSE_DIR", "/tmp/freesky-pulse")
_PULSE_SOCKET = os.path.join(_PULSE_DIR, "native")

# Segment length. Also the GOP length — they must match or `independent_segments`
# is a lie and players stall at segment boundaries.
# x264 thread cap and Chromium raster threads. Both exist to stop the two
# fighting over a small container's cores: the encode is cheap (a fraction of a
# core at 720p30), the browser is not, and the usual failure is Chromium being
# starved until it stops painting — which shows up as ffmpeg duplicating frames.
# HTTP disk cache for each channel's persistent profile.
DISK_CACHE_BYTES = int(os.environ.get("VIRTUAL_DISK_CACHE_BYTES", str(256 * 1024 * 1024)))

ENCODER_THREADS = int(os.environ.get("VIRTUAL_ENCODER_THREADS", "2"))
RASTER_THREADS = int(os.environ.get("VIRTUAL_RASTER_THREADS", "2"))

SEGMENT_SECONDS = int(os.environ.get("VIRTUAL_SEGMENT_SECONDS", "2"))
PLAYLIST_SIZE = int(os.environ.get("VIRTUAL_PLAYLIST_SIZE", "6"))

# Optional ceiling on concurrent sessions. 0 (the default) means no limit: run
# as many channels at once as the host can carry.
#
# There is no limit by default because the natural use of this feature is
# several channels running side by side, and a cap that silently evicts the
# channel someone is watching is a worse failure than a slow host. Sizing is
# therefore the operator's call, and the honest numbers are: roughly 1.5-2 CPU
# cores and ~900MB per 720p30 session (a headful Chromium plus an x264 encoder),
# about double that at 1080p30. Set MAX_VIRTUAL_SESSIONS to a positive number to
# reinstate a cap, in which case the least recently watched session is evicted.
MAX_SESSIONS = int(os.environ.get("MAX_VIRTUAL_SESSIONS", "0"))

# How long to wait for the first segments after the browser is ready before
# giving up on a session.
_FIRST_SEGMENT_TIMEOUT = float(os.environ.get("VIRTUAL_START_TIMEOUT", "45"))

# Live-preview settings for the admin control panel.
#
# The preview captures the SAME X display the encoder does, so what the admin
# sees is exactly what viewers see — including Chromium's own UI (a save-password
# bubble, an autofill dropdown, a permission prompt), which is drawn by the
# browser into its X window. A CDP screencast was tried here and rejected: it
# captures only the page's compositor surface, so browser dialogs were invisible
# in the panel while being plainly visible in the stream.
#
# Only the resolution and frame rate are reduced, never the content.
CONTROL_MAX_WIDTH = int(os.environ.get("VIRTUAL_CONTROL_MAX_WIDTH", "960"))
# ffmpeg mjpeg quality scale: 2 is best, 31 is worst.
CONTROL_QUALITY = int(os.environ.get("VIRTUAL_CONTROL_QUALITY", "7"))
CONTROL_FPS = float(os.environ.get("VIRTUAL_CONTROL_FPS", "10"))

# A stream is considered dead when its playlist has not been rewritten in this
# many segment durations. Process liveness is NOT a sufficient check: ffmpeg can
# sit there holding a dead X connection, and Chromium can die while ffmpeg
# happily keeps capturing a blank root window.
_STALL_FACTOR = 4

# A session is still considered healthy for this long after it starts, even
# before the playlist exists. Without it, a session started for the control
# panel (which does not wait for segments) would immediately look "stalled" and
# be torn down and rebuilt in a loop.
_STARTUP_GRACE = float(os.environ.get("VIRTUAL_STARTUP_GRACE", "90"))

_SEGMENT_RE = re.compile(r"^seg_\d{6}\.ts$")


# Browser key names (KeyboardEvent.key) mapped to the X keysym names xdotool
# expects. Anything not listed is passed through, which is correct for plain
# letters and digits.
_XDOTOOL_KEYS = {
    "Enter": "Return", "Escape": "Escape", "Backspace": "BackSpace",
    "Tab": "Tab", "Delete": "Delete", " ": "space",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "Control": "ctrl", "Shift": "shift", "Alt": "alt", "Meta": "super",
}


def _xdotool_key(key: str) -> str:
    return _XDOTOOL_KEYS.get(key, key)


class VirtualSessionError(RuntimeError):
    """A session could not be started. Message is shown to the admin."""


def _ffmpeg_supports_fps_mode() -> bool:
    """True on ffmpeg >= 5.1, where `-fps_mode` replaced `-vsync`.

    Cached on the function because it shells out. The container ships ffmpeg 7,
    but a developer running this on an older distro would otherwise get an
    `Unrecognized option` failure that looks like a broken feature.
    """
    cached = getattr(_ffmpeg_supports_fps_mode, "_cached", None)
    if cached is not None:
        return cached
    result = False
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        match = re.search(r"ffmpeg version n?(\d+)\.(\d+)", out)
        if match:
            major, minor = int(match.group(1)), int(match.group(2))
            result = (major, minor) >= (5, 1)
    except (OSError, subprocess.SubprocessError, ValueError):
        result = False
    _ffmpeg_supports_fps_mode._cached = result
    return result


def preflight() -> list:
    """Names of required binaries that are missing.

    Surfaced in the settings UI so "the stream won't start" turns into "ffmpeg is
    not installed" rather than a log dive.
    """
    # xdotool is included because the control panel's input goes through it.
    # Its absence degrades rather than breaks (input falls back to page-level
    # CDP, which cannot reach browser UI), but an admin should be told.
    return [b for b in ("Xvfb", "ffmpeg", "pulseaudio", "pactl", "xdotool")
            if not shutil.which(b)]


# --- PulseAudio -------------------------------------------------------------
# One daemon for the whole process, one null-sink per session. Chromium is
# pointed at its own sink via PULSE_SINK so two sessions cannot bleed into each
# other's audio.

_pulse_proc: Optional[subprocess.Popen] = None
_pulse_lock = asyncio.Lock()


def _pulse_env() -> dict:
    return {
        "PULSE_SERVER": f"unix:{_PULSE_SOCKET}",
        "PULSE_RUNTIME_PATH": _PULSE_DIR,
        "XDG_RUNTIME_DIR": _PULSE_DIR,
    }


async def _run(argv, timeout=15.0, env_extra=None) -> str:
    """Run a command, return stdout, raise VirtualSessionError on failure."""
    env = dict(os.environ)
    env.update(env_extra or {})
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise VirtualSessionError(f"{argv[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        raise VirtualSessionError(
            f"{argv[0]} failed ({proc.returncode}): {err.decode(errors='replace').strip()[:300]}"
        )
    return out.decode(errors="replace")


async def _ensure_pulse() -> None:
    """Start the container-local PulseAudio daemon if it isn't up.

    `--exit-idle-time=-1` matters: without it the daemon quits the moment the
    last Chromium disconnects, and the next session start races a dying daemon.
    `--disable-shm` avoids the `shm_open() failed` spam containers produce with a
    small /dev/shm.
    """
    global _pulse_proc
    async with _pulse_lock:
        if _pulse_proc is not None and _pulse_proc.poll() is None:
            return
        os.makedirs(_PULSE_DIR, mode=0o700, exist_ok=True)
        argv = [
            "pulseaudio",
            "--exit-idle-time=-1",
            "--disallow-exit",
            "--disable-shm=true",
            "-n",  # do not read the default config; everything is explicit below
            f"--load=module-native-protocol-unix socket={_PULSE_SOCKET} auth-anonymous=1",
            "--load=module-always-sink",
            "--log-target=stderr",
        ]
        logger.info("virtual: starting PulseAudio daemon")
        _pulse_proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, **_pulse_env()},
        )
        # Wait for the socket to answer rather than sleeping a fixed amount.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _pulse_proc.poll() is not None:
                raise VirtualSessionError("PulseAudio exited immediately on startup")
            try:
                await _run(["pactl", "info"], timeout=5, env_extra=_pulse_env())
                logger.info("virtual: PulseAudio ready")
                return
            except VirtualSessionError:
                await asyncio.sleep(0.4)
        raise VirtualSessionError("PulseAudio did not become ready within 15s")


# --- one session ------------------------------------------------------------


class VirtualSession:
    """A running browser + encoder pair for one virtual channel."""

    def __init__(self, record: dict, display: int):
        self.record = record
        self.name = record["name"]
        self.display = display
        self.width, self.height = virtual_channels.geometry(record)
        self.sink = f"fsk_{self.name}".replace("-", "_")[:60]
        self.out_dir = os.path.join(HLS_ROOT, self.name)
        self.playlist_path = os.path.join(self.out_dir, "index.m3u8")
        self.profile_dir = os.path.join(PROFILE_ROOT, self.name)

        self.last_access = time.monotonic()
        self.started_at = time.monotonic()
        self.error = ""
        # Live encoder metrics, parsed from ffmpeg's -progress stream. This is
        # the only way to tell "the browser is not painting" (dup climbing,
        # speed ~1.0) apart from "we cannot encode fast enough" (speed < 1.0).
        self.metrics: dict = {}

        self._xvfb: Optional[subprocess.Popen] = None
        self._ffmpeg: Optional[asyncio.subprocess.Process] = None
        self._sink_module = ""
        self._playwright = None
        self._context = None
        self._page = None
        self._log_tail: list = []
        # Serialises page operations. Playwright's API is not safe against two
        # coroutines driving the same page at once.
        self._page_lock = asyncio.Lock()
        # Cross-PROCESS guard; see _acquire_channel_lock().
        self._lock_fd: Optional[int] = None

    # -- cross-process exclusion ---------------------------------------------

    def _acquire_channel_lock(self) -> None:
        """Claim this channel for this process, or refuse to start.

        _page_lock only orders coroutines inside one interpreter, and `manager`
        is a per-process singleton, so nothing above this stops two BACKEND
        PROCESSES from starting the same channel. That is not a theoretical
        race: reflex runs granian with `(cpu_count * 2) + 1` workers whenever it
        can reach Redis, and every one of them ran the autostart lifespan task.
        The result was competing Xvfb servers on the same display, N Chromiums
        sharing one persistent profile, and two encoders writing one playlist.

        An flock on a file keyed by channel name makes that impossible to do
        silently: the second process fails fast with a message naming the cause
        instead of corrupting the profile. The lock is advisory and held by an
        open fd, so it is released automatically if the process dies -- a stale
        lock cannot outlive a crash the way a lock *file* would.
        """
        os.makedirs(LOCK_ROOT, exist_ok=True)
        path = os.path.join(LOCK_ROOT, f"{self.name}.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise VirtualSessionError(
                f"Another process is already running virtual channel '{self.name}'. "
                "Run the backend with a single worker (GRANIAN_WORKERS=1)."
            ) from None
        with contextlib.suppress(OSError):
            os.truncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def _release_channel_lock(self) -> None:
        """Drop the channel lock. Safe on a session that never took one."""
        if self._lock_fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._lock_fd)
        self._lock_fd = None

    # -- lifecycle --

    async def start(self, wait_for_stream: bool = True) -> None:
        """Bring the whole pipeline up, or raise and leave nothing behind.

        `wait_for_stream=False` returns as soon as the browser and encoder are
        running, without waiting for the first HLS segments. The admin control
        panel only needs the browser — making it wait for segments too pushed a
        cold start past a minute, which the reverse proxy answered with a 502
        long before the session was ready.
        """
        try:
            # Before anything is spawned: a second process must not get as far
            # as unlinking the X lock or opening the shared Chrome profile.
            self._acquire_channel_lock()
            await self._start_display()
            if self.record["audio"]:
                await _ensure_pulse()
                await self._start_sink()
            await self._start_browser()
            await self._start_ffmpeg()
            if wait_for_stream:
                await self._await_first_segments()
        except Exception as exc:
            self.error = str(exc)
            logger.error("virtual[%s]: start failed: %s", self.name, exc)
            await self.stop()
            raise

    async def _start_display(self) -> None:
        """Xvfb at exactly the capture geometry.

        24-bit depth is not optional — x11grab expects x24 and a 16-bit screen
        produces colour-mangled output.
        """
        # A crashed previous run leaves the lock behind and Xvfb then refuses the
        # display number, which looked like "the feature stopped working".
        with contextlib.suppress(OSError):
            os.unlink(f"/tmp/.X{self.display}-lock")
        argv = [
            "Xvfb", f":{self.display}",
            "-screen", "0", f"{self.width}x{self.height}x24",
            "-ac", "-nolisten", "tcp", "-dpi", "96", "+extension", "RANDR",
        ]
        self._xvfb = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Poll rather than sleep: racing Chromium against a not-yet-listening
        # Xvfb is the single most common cause of flaky startup.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._xvfb.poll() is not None:
                raise VirtualSessionError(f"Xvfb exited immediately on display :{self.display}")
            try:
                await _run(["xdpyinfo", "-display", f":{self.display}"], timeout=5)
                return
            except VirtualSessionError:
                await asyncio.sleep(0.3)
        raise VirtualSessionError(f"Xvfb :{self.display} did not become ready within 15s")

    async def _start_sink(self) -> None:
        """A null-sink whose `.monitor` source is what ffmpeg records.

        Per session rather than one shared sink, so two channels playing at once
        do not record each other's audio.
        """
        out = await _run(
            ["pactl", "load-module", "module-null-sink",
             f"sink_name={self.sink}",
             f"sink_properties=device.description={self.sink}"],
            env_extra=_pulse_env(),
        )
        self._sink_module = out.strip()

    def _seed_profile(self) -> None:
        """Write the profile preferences that suppress Chromium's own dialogs.

        This is not cosmetic: browser UI is drawn into the browser's X window,
        so a "Save password?" bubble lands in the captured stream that viewers
        watch. There is no command-line switch for it in modern Chromium —
        `--disable-save-password-bubble` was removed, as was the
        `profile.password_manager_enabled` pref that most guides still
        recommend. `credentials_enable_service` is the surviving control, and
        its upstream comment describes exactly this behaviour ("when it is
        false, it doesn't ask if you want to save passwords").

        Two details make the difference between this working and silently doing
        nothing:

        * **The "First Run" sentinel.** Without it Chromium treats the profile
          as brand new and overwrites the Preferences file we just wrote. This
          is the same thing ChromeDriver does, for the same reason.
        * **`profile.exit_type: "Normal"`.** Chromium writes "Crashed" at
          startup and only rewrites it on a clean shutdown. We stop sessions
          abruptly, so without resetting this every launch the next session
          shows a "Restore pages?" bubble — on the stream.

        Only untracked prefs are written here. Chromium HMAC-protects a set of
        tracked prefs (homepage, startup/session restore, search providers) and
        silently resets any unsigned value, so those cannot be seeded this way.
        """
        default_dir = os.path.join(self.profile_dir, "Default")
        os.makedirs(default_dir, exist_ok=True)
        prefs_path = os.path.join(default_dir, "Preferences")

        # Merge into whatever Chromium last wrote rather than replacing it. The
        # profile is persistent, so this file also carries session state we want
        # to keep; overwriting it wholesale would quietly discard that on every
        # launch and defeat the point of a persistent profile.
        existing = {}
        try:
            with open(prefs_path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (FileNotFoundError, ValueError, OSError):
            existing = {}

        prefs = {
            # The save-password bubble.
            "credentials_enable_service": False,
            # The auto-sign-in toast.
            "credentials_enable_autosignin": False,
            "profile": {
                # "Your password was found in a data breach" bubble.
                "password_manager_leak_detection": False,
                # See the docstring: stops the "Restore pages?" bubble.
                "exit_type": "Normal",
                # 2 == block. Belt and braces alongside --deny-permission-prompts.
                "default_content_setting_values": {
                    "notifications": 2,
                    "geolocation": 2,
                    "media_stream_mic": 2,
                    "media_stream_camera": 2,
                },
            },
            # Address- and card-save bubbles, and the autofill dropdown.
            "autofill": {"profile_enabled": False, "credit_card_enabled": False},
            "bookmark_bar": {"show_on_all_tabs": False},
            "translate": {"enabled": False},
            "download": {"prompt_for_download": False},
            "signin": {"allowed": False},
        }
        merged = dict(existing)
        for key, value in prefs.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                nested = dict(merged[key])
                nested.update(value)
                merged[key] = nested
            else:
                merged[key] = value

        tmp = f"{prefs_path}.tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f)
        os.replace(tmp, prefs_path)

        # Empty file, and the space in the name is part of it.
        first_run = os.path.join(self.profile_dir, "First Run")
        if not os.path.exists(first_run):
            with open(first_run, "w"):
                pass

        # Chromium refuses to start on a profile that another instance appears
        # to hold. When a session is killed rather than closed, these are left
        # behind and the NEXT start fails — which, with a persistent profile,
        # would be permanent rather than self-healing.
        for stale in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(self.profile_dir, stale))

    def _browser_args(self) -> list:
        """Chromium switches for this session.

        Split out so the flag set can be asserted on directly, the same
        way _ffmpeg_argv() is — a wrong switch here shows up only as a
        stuttering stream, which is an expensive way to find it.
        """
        return [
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            # Without this, a <video> with sound simply never starts. It is the
            # single most important flag in this list.
            "--autoplay-policy=no-user-gesture-required",
            "--kiosk",  # no tab strip, no omnibox: the page fills the capture
            f"--window-size={self.width},{self.height}",
            "--window-position=0,0",
            "--force-device-scale-factor=1",
            "--start-fullscreen",
            # Native prompts that would otherwise be drawn over the stream.
            # Playwright already passes --disable-infobars, --no-first-run,
            # --no-default-browser-check, --disable-popup-blocking,
            # --disable-component-update, --disable-sync and a comma-joined
            # --disable-features list, so those are not repeated here.
            # Deliberately NOT passing our own --disable-features either: a
            # second occurrence of that switch is a merge hazard.
            "--deny-permission-prompts",
            "--disable-notifications",
            "--noerrdialogs",
            "--disable-print-preview",
            # Suppresses in-product-help promo bubbles.
            "--propagate-iph-for-testing",
            # Rendering. SwiftShader was removed here deliberately: it is a
            # WebGL/GLES emulator, and routing a plain <video> page's 2D
            # composites through an emulated GL driver is the expensive path.
            # With no GPU in the container, Skia's CPU raster is both cheaper
            # and what Chromium's own docs point at. --disable-software-rasterizer
            # stops it falling back into SwiftShader anyway.
            "--disable-gpu",
            "--disable-software-rasterizer",
            # Frees the display scheduler from a synthetic 60Hz vblank timer.
            # NOT --disable-frame-rate-limit: unbounded frame production has
            # been reported to starve video decode, and this container is
            # CPU-bound, so it would compete with the encoder for the cores the
            # stream actually needs.
            "--disable-gpu-vsync",
            # Keeps raster from taking every core away from x11grab and x264.
            f"--num-raster-threads={RASTER_THREADS}",
            # Without this Chromium blanks its output when a navigation stalls,
            # and we would capture white frames.
            "--disable-new-content-rendering-timeout",
            "--hide-scrollbars",
            # A defined HTTP disk cache in the persistent profile, so a restart
            # re-uses the site's assets instead of re-downloading them. Only
            # --disable-back-forward-cache is passed by Playwright, and that is
            # the in-memory bfcache, not this.
            f"--disk-cache-size={DISK_CACHE_BYTES}",
            # Playwright already passes --disable-background-timer-throttling,
            # --disable-backgrounding-occluded-windows and
            # --disable-renderer-backgrounding, which matter here (a kiosk
            # window under Xvfb can look "occluded", and a throttled renderer
            # stops presenting frames), so they are not repeated.
        ]

    async def _start_browser(self) -> None:
        """Headful Chromium on our display, playing into our sink."""
        from playwright.async_api import async_playwright

        env = dict(os.environ)
        env["DISPLAY"] = f":{self.display}"
        if self.record["audio"]:
            env.update(_pulse_env())
            env["PULSE_SINK"] = self.sink
        else:
            # No sink for this channel: make sure Chromium cannot grab whatever
            # the host default happens to be.
            env["PULSE_SERVER"] = "/nonexistent"

        # The profile is PERSISTENT and deliberately not wiped: it carries the
        # cookies and stored logins that let this channel resume after a restart
        # without an admin signing in again. Only one session per channel ever
        # runs, so there is no contention over it.
        os.makedirs(self.profile_dir, exist_ok=True)
        self._seed_profile()

        args = self._browser_args()

        # launch_persistent_context, NOT launch(): Playwright rejects a
        # --user-data-dir in args outright ("Pass user_data_dir parameter to
        # browser_type.launch_persistent_context instead"), and that error is
        # raised before the browser starts. A persistent profile is what we want
        # anyway — it is how a login an admin performs through the control panel
        # survives for the life of the session.
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                self.profile_dir,
                headless=False,
                args=args,
                env=env,
                # no_viewport: let the page fill the real window instead of
                # applying a device-metrics override on top of it. The window is
                # already exactly the Xvfb screen size, so the screenshot the
                # control panel shows is pixel-identical to what x11grab records
                # — an emulated viewport would be a second, redundant geometry
                # that can silently disagree with the capture.
                no_viewport=True,
                ignore_https_errors=True,
            )
        except Exception as exc:
            # Playwright raises its own error types. Wrap them so callers get a
            # VirtualSessionError with a readable message instead of a 500.
            raise VirtualSessionError(f"Could not start Chromium: {exc}") from exc

        # A persistent context opens with one page already present.
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        try:
            await self._page.goto(self.record["url"], wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            raise VirtualSessionError(f"Could not load {self.record['url']}: {exc}") from exc

        await self._apply_page_tweaks()

        # Let the page settle (fonts, player bootstrap, consent dialogs) before
        # the encoder starts, so viewers don't join on a half-painted page.
        await asyncio.sleep(self.record["warmup"])

    async def _apply_page_tweaks(self) -> None:
        """Hide chrome the admin listed, then click whatever starts playback.

        Both lists are best-effort: a selector that matches nothing is normal
        (a consent banner only appears on the first visit), so nothing here is
        allowed to fail the session start.
        """
        hide = self.record["hide_selectors"]
        if hide:
            # Selectors are validated to contain no quotes or backslashes
            # (virtual_channels._validate_selectors), which is what makes this
            # interpolation safe.
            css = ", ".join(hide) + " { display: none !important; }"
            with contextlib.suppress(Exception):
                await self._page.add_style_tag(content=css)

        for selector in self.record["click_selectors"]:
            try:
                await self._page.click(selector, timeout=5000)
            except Exception:
                logger.debug("virtual[%s]: click selector %r matched nothing", self.name, selector)

        # A synthetic gesture as a belt-and-braces fallback: some Chromium builds
        # have been reported to ignore --autoplay-policy, and a real click
        # satisfies the autoplay gate unconditionally.
        with contextlib.suppress(Exception):
            await self._page.mouse.click(self.width // 2, self.height // 2)

    def _ffmpeg_argv(self) -> list:
        record = self.record
        fps = record["framerate"]
        gop = fps * SEGMENT_SECONDS
        vb = record["video_bitrate"]

        argv = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            # Machine-readable progress on stdout, leaving stderr for warnings.
            # -nostats suppresses the human progress line, which is carriage-
            # return delimited and would never terminate a readline().
            "-nostats", "-progress", "pipe:1",
            # Trim input-side buffering; this is a live capture, not a file.
            "-fflags", "nobuffer", "-flags", "low_delay",
            # Both inputs need a deep queue. With the tiny default, a momentary
            # x11grab stall drops pulse packets and the stream desyncs for good.
            "-thread_queue_size", "1024",
            "-f", "x11grab", "-draw_mouse", "0",
            "-framerate", str(fps),
            "-video_size", f"{self.width}x{self.height}",
            "-i", f":{self.display}.0",
        ]
        if record["audio"]:
            argv += [
                "-thread_queue_size", "1024",
                "-f", "pulse", "-name", f"freesky-{self.name}",
                "-sample_rate", "48000", "-channels", "2", "-fragment_size", "4096",
                "-i", f"{self.sink}.monitor",
                # async=1000, not async=1. Per ffmpeg's resampler docs, async=1
                # enables only "filling and trimming" — it inserts silence or
                # hard-cuts samples, which over a long session is audible as
                # clicks and micro-gaps. A larger value is the maximum samples
                # per second it may stretch or squeeze instead, which is the
                # documented idiom for an independent capture clock (PulseAudio
                # at a fixed 48kHz) against a wall-clock video timeline.
                # 1000/48000 is about 2% maximum correction, applied smoothly.
                "-filter_complex", "[1:a]aresample=async=1000:first_pts=0[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:a", "aac", "-b:a", f"{record['audio_bitrate']}k", "-ar", "48000", "-ac", "2",
            ]
        else:
            argv += ["-map", "0:v", "-an"]

        argv += [
            "-c:v", "libx264",
            "-preset", record["preset"],
            # Pinned so x264 does not spawn ~1.5x ncpu threads and starve the
            # browser it is capturing. Measured cost of the encode itself is
            # only a fraction of a core at 720p30, so threads buy little here
            # and contention costs a lot.
            "-threads", str(ENCODER_THREADS),
            # Disables lookahead and B-frames: sub-frame encoder delay, which is
            # the right trade for live capture.
            "-tune", "zerolatency",
            "-profile:v", "main", "-pix_fmt", "yuv420p",
            "-b:v", f"{vb}k", "-maxrate", f"{vb}k", "-bufsize", f"{vb * 2}k",
            # GOP == segment length, and no scene-change keyframes, so every
            # segment starts on an IDR and independent_segments holds.
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            # SPS/PPS in-band on every IDR, so each segment really is
            # independently decodable for a client joining mid-stream — which
            # is what EXT-X-INDEPENDENT-SEGMENTS promises.
            "-x264-params", "repeat-headers=1",
        ]
        # x11grab drops frames under load; libx264 with a fixed GOP is much
        # happier with a constant frame rate.
        argv += ["-fps_mode", "cfr"] if _ffmpeg_supports_fps_mode() else ["-vsync", "cfr"]

        argv += [
            # Drops the MPEG-TS muxer's default 0.7s PCR preload, a fixed offset
            # between the container timeline and real time.
            "-muxdelay", "0", "-muxpreload", "0",
            "-f", "hls",
            "-hls_time", str(SEGMENT_SECONDS),
            "-hls_list_size", str(PLAYLIST_SIZE),
            # Keep a few segments past the window so a client holding a slightly
            # stale playlist does not 404.
            "-hls_delete_threshold", "3",
            "-hls_segment_type", "mpegts",
            # temp_file writes .tmp then renames, so we never serve a
            # half-written segment. NOTE: no hls_playlist_type — "event" forbids
            # removing segments and would silently defeat delete_segments,
            # growing the disk without bound.
            "-hls_flags",
            "delete_segments+append_list+independent_segments+program_date_time+temp_file",
            "-hls_segment_filename", os.path.join(self.out_dir, "seg_%06d.ts"),
            self.playlist_path,
        ]
        return argv

    async def _start_ffmpeg(self) -> None:
        # Warm the ffmpeg version probe OFF the event loop. It shells out with a
        # 10s timeout and _ffmpeg_argv() calls it synchronously, so on the first
        # session after a restart it would otherwise stall the whole server --
        # every other request, /health included, just hangs. It caches on the
        # function, so this costs nothing on later starts.
        await asyncio.to_thread(_ffmpeg_supports_fps_mode)

        # Wipe first, not just on teardown: append_list will happily resume onto
        # a playlist left behind by a crashed run and reference segments that no
        # longer exist.
        shutil.rmtree(self.out_dir, ignore_errors=True)
        os.makedirs(self.out_dir, exist_ok=True)

        env = dict(os.environ)
        env["DISPLAY"] = f":{self.display}"
        if self.record["audio"]:
            env.update(_pulse_env())

        self._ffmpeg = await asyncio.create_subprocess_exec(
            *self._ffmpeg_argv(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        asyncio.create_task(self._drain_ffmpeg_log())
        asyncio.create_task(self._drain_ffmpeg_progress())

    async def _drain_ffmpeg_log(self) -> None:
        """Keep the last few stderr lines so a failure has a diagnosis.

        Without this the pipe fills, ffmpeg blocks on write, and the stream
        stops for a reason that leaves no trace anywhere.
        """
        assert self._ffmpeg is not None and self._ffmpeg.stderr is not None
        try:
            async for raw in self._ffmpeg.stderr:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                self._log_tail.append(line)
                del self._log_tail[:-40]
                logger.debug("virtual[%s] ffmpeg: %s", self.name, line)
        except (asyncio.CancelledError, ValueError):
            pass

    async def _drain_ffmpeg_progress(self) -> None:
        """Parse ffmpeg's -progress stream into self.metrics.

        ffmpeg writes a block of key=value lines terminated by `progress=`.
        The interesting ones:

          fps         frames actually encoded per second
          speed       encode speed relative to realtime; below 1.0x means the
                      encoder cannot keep up
          dup_frames  frames ffmpeg REPEATED to hold the constant rate, i.e.
                      frames the capture never delivered
          drop_frames frames discarded because their timestamps bunched up

        dup climbing while speed stays at ~1.0x is the signature of a browser
        that is not painting; speed below 1.0x is the encoder falling behind.
        Without this the two are indistinguishable from the outside.
        """
        assert self._ffmpeg is not None and self._ffmpeg.stdout is not None
        block: dict = {}
        try:
            async for raw in self._ffmpeg.stdout:
                line = raw.decode(errors="replace").strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                block[key.strip()] = value.strip()
                if key.strip() == "progress":
                    self.metrics = progress_metrics(block)
                    block = {}
        except (asyncio.CancelledError, ValueError):
            pass

    async def _await_first_segments(self) -> None:
        """Block until the playlist lists at least one segment.

        Returning a playlist with no segments makes players give up immediately
        and report the channel as dead, so the first request has to wait.
        """
        deadline = time.monotonic() + _FIRST_SEGMENT_TIMEOUT
        while time.monotonic() < deadline:
            if self._ffmpeg is not None and self._ffmpeg.returncode is not None:
                raise VirtualSessionError(
                    "ffmpeg exited during startup: " + (self._log_tail[-1] if self._log_tail else "no output")
                )
            try:
                with open(self.playlist_path, "r") as f:
                    if ".ts" in f.read():
                        return
            except (FileNotFoundError, OSError):
                pass
            await asyncio.sleep(0.5)
        raise VirtualSessionError(
            "No video was produced within "
            f"{int(_FIRST_SEGMENT_TIMEOUT)}s: "
            + (self._log_tail[-1] if self._log_tail else "ffmpeg produced no output")
        )

    async def stop(self) -> None:
        """Tear everything down. Safe to call twice, and on a half-built session."""
        # ffmpeg first, and gracefully: SIGKILL leaves a playlist with no
        # ENDLIST, which players poll forever.
        if self._ffmpeg is not None and self._ffmpeg.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._ffmpeg.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._ffmpeg.wait(), timeout=5)
            if self._ffmpeg.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._ffmpeg.kill()
        self._ffmpeg = None

        for closer in (
            getattr(self._context, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(closer(), timeout=10)
        self._context = self._playwright = self._page = None

        # Leaked null-sinks accumulate and eventually exhaust module slots.
        if self._sink_module:
            with contextlib.suppress(VirtualSessionError):
                await _run(["pactl", "unload-module", self._sink_module], env_extra=_pulse_env())
            self._sink_module = ""

        if self._xvfb is not None and self._xvfb.poll() is None:
            self._xvfb.terminate()
            # In a thread: Popen.wait() is synchronous, and blocking the loop for
            # up to 5s here stalls every other request in this process while a
            # session is torn down.
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(self._xvfb.wait, 5)
            if self._xvfb.poll() is None:
                self._xvfb.kill()
        self._xvfb = None

        # The profile deliberately survives: it is what makes the channel come
        # back signed in. Only the HLS output is transient.
        shutil.rmtree(self.out_dir, ignore_errors=True)

        # Released last, so the channel stays claimed until every process it
        # owned is gone. Releasing earlier would let another starter race this
        # teardown for the same display and profile.
        self._release_channel_lock()

    # -- interactive control --------------------------------------------
    # The admin panel drives the live page: a stream of screenshots out, mouse
    # and keyboard events in. Screenshots come from Playwright rather than from
    # the x11grab capture because they are already in page coordinates, so a
    # click at (x, y) on the panel lands at (x, y) in the page with no mapping.

    # -- live preview: exactly what viewers see --------------------------------
    # Grabs the same X display the encoder captures, so the panel and the stream
    # cannot diverge. This matters for more than tidiness: Chromium's own UI —
    # a save-password bubble, an autofill dropdown, an infobar — is drawn by the
    # browser into its X window, so it appears in the stream. Capturing the page
    # instead (CDP screencast) hid exactly those dialogs from the admin who
    # needed to dismiss them.

    async def screen_frames(self, max_width: int = 0):
        """Yield JPEG frames of the whole X display."""
        width = max_width or CONTROL_MAX_WIDTH
        argv = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "x11grab",
            # Unlike the stream, draw the pointer: an admin aiming a mouse needs
            # to see where it is.
            "-draw_mouse", "1",
            "-framerate", str(int(max(CONTROL_FPS, 1))),
            "-video_size", f"{self.width}x{self.height}",
            "-i", f":{self.display}.0",
            # -2 keeps the height even, which the encoder requires.
            "-vf", f"scale={width}:-2",
            "-q:v", str(CONTROL_QUALITY), "-f", "mjpeg", "pipe:1",
        ]
        env = dict(os.environ)
        env["DISPLAY"] = f":{self.display}"
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        try:
            buf = b""
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    return
                buf += chunk
                # ffmpeg's mjpeg muxer writes complete JPEGs back to back, so
                # split on the end-of-image marker rather than guessing lengths.
                while True:
                    end = buf.find(b"\xff\xd9")
                    if end == -1:
                        break
                    frame, buf = buf[:end + 2], buf[end + 2:]
                    start = frame.find(b"\xff\xd8")
                    if start != -1:
                        yield frame[start:]
        finally:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)

    async def dispatch_screen_input(self, event: dict) -> None:
        """Apply an input event at the X level, via xdotool.

        Needed for anything that is browser UI rather than page content: a
        CDP/Playwright click cannot reach a save-password bubble, because as far
        as the page is concerned it does not exist. Falls back to the page-level
        path when xdotool is not installed.
        """
        if not shutil.which("xdotool"):
            await self.dispatch_input(event)
            return

        kind = str(event.get("type", ""))
        display = f":{self.display}"
        x, y = int(event.get("x", 0) or 0), int(event.get("y", 0) or 0)
        button = {"left": "1", "middle": "2", "right": "3"}.get(
            str(event.get("button", "left")), "1"
        )
        argv = None
        if kind == "move":
            argv = ["xdotool", "mousemove", "--sync", str(x), str(y)]
        elif kind == "click":
            clicks = max(1, int(event.get("clicks", 1) or 1))
            argv = ["xdotool", "mousemove", "--sync", str(x), str(y),
                    "click", "--repeat", str(clicks), button]
        elif kind == "down":
            argv = ["xdotool", "mousemove", "--sync", str(x), str(y),
                    "mousedown", button]
        elif kind == "up":
            argv = ["xdotool", "mousemove", "--sync", str(x), str(y),
                    "mouseup", button]
        elif kind == "wheel":
            # X wheel buttons: 4 = up, 5 = down.
            argv = ["xdotool", "mousemove", "--sync", str(x), str(y),
                    "click", "5" if float(event.get("dy", 0) or 0) > 0 else "4"]
        elif kind == "text":
            argv = ["xdotool", "type", "--delay", "12", "--", str(event.get("text", ""))]
        elif kind == "key":
            argv = ["xdotool", "key", "--", _xdotool_key(str(event.get("key", "")))]
        else:
            # back/forward/reload are page-level operations with no X equivalent.
            await self.dispatch_input(event)
            return

        with contextlib.suppress(VirtualSessionError):
            await _run(argv, timeout=10, env_extra={"DISPLAY": display})
        self.last_access = time.monotonic()

    async def diagnostics(self) -> dict:
        """What the page thinks it is doing, measured in the page.

        This is the other half of the ffmpeg metrics: those say how many frames
        arrived, this says how many the site actually rendered and at what size.
        A site that quietly picked a 480p/15fps rendition, or whose decoder is
        dropping frames, looks identical from outside the browser.
        """
        if self._page is None:
            raise VirtualSessionError("Session has no page")
        script = """
        async () => {
          const out = {
            url: location.href,
            visibility: document.visibilityState,
            hidden: document.hidden,
            secureContext: window.isSecureContext,
            devicePixelRatio: window.devicePixelRatio,
            inner: [window.innerWidth, window.innerHeight],
            videos: [],
          };
          for (const v of document.querySelectorAll('video')) {
            const q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
            const info = {
              size: [v.videoWidth, v.videoHeight],
              paused: v.paused, muted: v.muted, readyState: v.readyState,
              currentTime: Math.round(v.currentTime * 10) / 10,
              dropped: q ? q.droppedVideoFrames : null,
              total: q ? q.totalVideoFrames : null,
            };
            // Measure the real presented frame rate over ~1s. This is the
            // number that decides whether the capture can ever be smooth.
            if (v.requestVideoFrameCallback && !v.paused) {
              info.fps = await new Promise(res => {
                let n = 0; const t0 = performance.now();
                const tick = () => {
                  n++;
                  if (performance.now() - t0 < 1000) v.requestVideoFrameCallback(tick);
                  else res(Math.round(n * 1000 / (performance.now() - t0) * 10) / 10);
                };
                v.requestVideoFrameCallback(tick);
                setTimeout(() => res(n), 1500);
              });
            }
            out.videos.push(info);
          }
          return out;
        }
        """
        async with self._page_lock:
            return await self._page.evaluate(script)

    async def page_size(self) -> tuple:
        """The live page's CSS pixel size.

        The control panel needs this to map a click, and it must come from the
        page rather than from the configured geometry: the preview is
        deliberately downscaled, so the frame's own dimensions are NOT the
        coordinate space, and a kiosk window is often a pixel or two off the
        nominal screen size.
        """
        if self._page is None:
            return (self.width, self.height)
        try:
            size = await self._page.evaluate(
                "() => [window.innerWidth, window.innerHeight]"
            )
            if size and size[0] and size[1]:
                return (int(size[0]), int(size[1]))
        except Exception:
            pass
        return (self.width, self.height)

    @property
    def page_url(self) -> str:
        """Where the live page currently is, for the control panel's address bar."""
        try:
            return self._page.url if self._page is not None else ""
        except Exception:
            return ""

    # -- health --

    def is_alive(self) -> bool:
        """Liveness is playlist freshness, not process liveness.

        ffmpeg can hold a dead X connection and Chromium can crash while ffmpeg
        keeps capturing a blank root window — in both cases the process table
        looks perfect. A playlist that has stopped being rewritten is the only
        signal that actually correlates with a viewer seeing video.
        """
        if self._ffmpeg is None or self._ffmpeg.returncode is not None:
            return False
        try:
            age = time.time() - os.path.getmtime(self.playlist_path)
        except OSError:
            # No playlist yet. That is expected while the page loads and the
            # encoder fills its first segment, so a freshly started session is
            # given a grace period rather than being reaped as stalled.
            return (time.monotonic() - self.started_at) < _STARTUP_GRACE
        return age < SEGMENT_SECONDS * _STALL_FACTOR

    def status(self) -> dict:
        return {
            "name": self.name,
            "display": self.display,
            "resolution": f"{self.width}x{self.height}",
            "uptime": int(time.monotonic() - self.started_at),
            "idle": int(time.monotonic() - self.last_access),
            "alive": self.is_alive(),
            "url": self.page_url,
            # Flat, not nested: Reflex cannot index a nested dict inside an
            # rx.foreach over List[dict], so the row would fail to render.
            "fps": self.metrics.get("fps", "-"),
            "speed": self.metrics.get("speed", "-"),
            "dup": self.metrics.get("dup", "-"),
            "drop": self.metrics.get("drop", "-"),
            "error": self.error,
            "log": self._log_tail[-5:],
        }


# --- manager ----------------------------------------------------------------


class SessionManager:
    """Owns every running session. One instance per process (see `manager`)."""

    def __init__(self):
        self._sessions: Dict[str, VirtualSession] = {}
        # Per-channel locks, so two players asking for the same channel at once
        # start one session rather than racing to build two on the same display.
        self._locks: Dict[str, asyncio.Lock] = {}
        # Display numbers handed out but whose session has not finished starting.
        # The per-channel locks above do NOT cover this: two DIFFERENT channels
        # starting concurrently would both scan for a free display, both pick the
        # same one, and the second Xvfb would fail on an already-bound display.
        self._reserved: set = set()
        self._alloc_lock = asyncio.Lock()
        self._janitor: Optional[asyncio.Task] = None

    def _lock_for(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    def _free_display(self) -> int:
        """Pick an unused display number. Call only while holding _alloc_lock."""
        used = {s.display for s in self._sessions.values()} | self._reserved
        display = _DISPLAY_BASE
        # Wide range: sessions are unlimited by default, and an X display number
        # costs nothing. The bound exists only so a filesystem full of stale
        # lock files cannot spin here forever.
        while display in used or os.path.exists(f"/tmp/.X{display}-lock"):
            display += 1
            if display > _DISPLAY_BASE + 512:
                raise VirtualSessionError("No free X display")
        return display

    async def acquire(self, name: str, wait_for_stream: bool = True) -> VirtualSession:
        """Return a live session for `name`, starting or restarting as needed.

        `wait_for_stream=False` is for callers that only need the browser (the
        control panel); it skips waiting for the encoder's first segments.
        """
        record = virtual_channels.get_channel(name)
        if record is None:
            raise VirtualSessionError(f"No virtual channel named {name!r}.")
        if not record["enabled"]:
            raise VirtualSessionError(f"Virtual channel {name!r} is disabled.")

        missing = preflight()
        if missing:
            raise VirtualSessionError(
                "Virtual channels need these to be installed: " + ", ".join(missing)
            )

        async with self._lock_for(name):
            session = self._sessions.get(name)
            if session is not None:
                if session.is_alive():
                    session.last_access = time.monotonic()
                    # The session may have been started by the control panel,
                    # which does not wait for segments. A player asking for the
                    # playlist still has to, or it would be handed a session
                    # whose playlist file does not exist yet.
                    if wait_for_stream and not os.path.exists(session.playlist_path):
                        await session._await_first_segments()
                    return session
                # Dead but still registered — clean up before rebuilding, or the
                # display and sink leak.
                logger.warning("virtual[%s]: session went stale, restarting", name)
                await session.stop()
                self._sessions.pop(name, None)

            if MAX_SESSIONS > 0 and len(self._sessions) >= MAX_SESSIONS:
                # Only when a cap is configured. Evict the least recently watched
                # rather than refusing: a player that just asked matters more
                # than one that stopped watching.
                victim = min(self._sessions.values(), key=lambda s: s.last_access)
                logger.info("virtual: at capacity, evicting %s for %s", victim.name, name)
                await victim.stop()
                self._sessions.pop(victim.name, None)

            async with self._alloc_lock:
                display = self._free_display()
                self._reserved.add(display)
            try:
                session = VirtualSession(record, display)
                await session.start(wait_for_stream=wait_for_stream)
                self._sessions[name] = session
            finally:
                # Released either way: once registered the session's own
                # `display` keeps the number out of the pool, and on failure it
                # must go back rather than leaking.
                async with self._alloc_lock:
                    self._reserved.discard(display)
            self._ensure_janitor()
            return session

    def touch(self, name: str) -> Optional[VirtualSession]:
        """Mark a session as still watched. Called on every segment request."""
        session = self._sessions.get(name)
        if session is not None:
            session.last_access = time.monotonic()
        return session

    def get(self, name: str) -> Optional[VirtualSession]:
        return self._sessions.get(name)

    def statuses(self) -> list:
        return [s.status() for s in self._sessions.values()]

    async def stop(self, name: str) -> bool:
        session = self._sessions.pop(name, None)
        if session is None:
            return False
        await session.stop()
        return True

    async def stop_all(self) -> None:
        for name in list(self._sessions):
            with contextlib.suppress(Exception):
                await self.stop(name)

    def _ensure_janitor(self) -> None:
        if self._janitor is None or self._janitor.done():
            self._janitor = asyncio.create_task(self._reap_loop())

    async def start_autostart_channels(self) -> None:
        """Bring up every channel marked autostart.

        Called from the app's lifespan, so a container restart puts these back
        exactly as they were — and because the browser profile is persistent,
        they come back signed in rather than at a login page.

        Failures are logged and skipped: one channel whose site is down must not
        stop the others, and the normal on-demand path will retry when someone
        tunes in.
        """
        for record in virtual_channels.list_channels():
            if not (record.get("autostart") and record.get("enabled")):
                continue
            name = record["name"]
            try:
                logger.info("virtual[%s]: autostart", name)
                await self.acquire(name)
            except Exception as exc:
                logger.error("virtual[%s]: autostart failed: %s", name, exc)

    def apply_live_settings(self, record: dict) -> bool:
        """Push a non-capture settings change onto a running session.

        Title, logo, tags, idle timeout and autostart do not affect how the
        browser or the encoder were started, so they can take effect without
        tearing the session down. Returns True if a session was updated.
        """
        session = self._sessions.get(record.get("name", ""))
        if session is None:
            return False
        session.record = record
        return True

    async def _reap_loop(self) -> None:
        """Tear down idle and dead sessions.

        A browser and an encoder per channel is expensive; nothing should stay up
        because someone opened a tab yesterday.
        """
        try:
            while self._sessions:
                await asyncio.sleep(5)
                now = time.monotonic()
                for name, session in list(self._sessions.items()):
                    idle = now - session.last_access
                    keep_hot = session.record.get("autostart")
                    if not session.is_alive():
                        # A stalled session is restarted rather than left dead,
                        # for an autostart channel that is the whole point.
                        logger.warning("virtual[%s]: stream stalled, stopping", name)
                        with contextlib.suppress(Exception):
                            await self.stop(name)
                        if keep_hot:
                            with contextlib.suppress(Exception):
                                await self.acquire(name)
                        continue
                    if keep_hot:
                        continue  # kept running on purpose
                    if idle > session.record["idle_timeout"]:
                        logger.info("virtual[%s]: idle %ds, stopping", name, int(idle))
                        with contextlib.suppress(Exception):
                            await self.stop(name)
        except asyncio.CancelledError:
            pass


manager = SessionManager()


def progress_metrics(block: dict) -> dict:
    """Project one ffmpeg -progress block onto the fields worth showing.

    Separated from the reader so it can be tested against real ffmpeg output
    without running a capture.
    """
    return {
        "fps": block.get("fps", ""),
        "speed": block.get("speed", ""),
        "frames": block.get("frame", ""),
        "dup": block.get("dup_frames", "0"),
        "drop": block.get("drop_frames", "0"),
        "bitrate": block.get("bitrate", ""),
        "out_time": block.get("out_time", ""),
    }


def segment_is_safe(segment: str) -> bool:
    """True for a name ffmpeg actually generates.

    The segment name arrives in a URL path and is joined onto a directory, so
    this is the check that stops `../../etc/passwd` from being served. An
    allowlist pattern rather than a traversal blocklist, because the set of
    legal names here is exactly one shape.
    """
    return bool(_SEGMENT_RE.match(segment or ""))


def rewrite_playlist(text: str, base: str, token: str = "") -> str:
    """Point segment URIs at our proxy route instead of bare filenames.

    ffmpeg writes `seg_000001.ts`, which is relative to wherever the playlist was
    fetched from. Players fetch our playlist from /api/stream/virt-x.m3u8, so a
    relative name would resolve to /api/stream/seg_000001.ts. Rewriting to an
    absolute path also lets us attach the stream token, which external players
    have no other way to carry.
    """
    suffix = f"?token={token}" if token else ""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(f"{base}/{stripped}{suffix}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    # Command construction is the part worth testing without a display: it is
    # long, order-sensitive, and a wrong flag only shows up as a dead stream.
    rec = virtual_channels.validate_channel(
        {"name": "demo", "url": "https://example.com", "resolution": "720p",
         "framerate": 30, "video_bitrate": 3000, "audio_bitrate": 96}
    )
    session = VirtualSession(rec, 99)
    argv = session._ffmpeg_argv()
    joined = " ".join(argv)

    assert "-f x11grab" in joined and ":99.0" in argv
    assert "1280x720" in argv, "geometry comes from the resolution preset"
    assert argv.count("-thread_queue_size") == 2, "both inputs need a deep queue"
    assert "fsk_demo.monitor" in argv, "records the session's own sink monitor"
    assert "-tune" in argv and argv[argv.index("-tune") + 1] == "zerolatency"
    # GOP must equal fps * segment seconds or independent_segments is a lie.
    assert argv[argv.index("-g") + 1] == str(30 * SEGMENT_SECONDS)
    assert argv[argv.index("-keyint_min") + 1] == argv[argv.index("-g") + 1]
    assert "3000k" in argv and "6000k" in argv, "bufsize is 2x bitrate"
    assert "96k" in argv
    # These two are the flags the fabricated LL-HLS guides get wrong.
    assert "-hls_part_size" not in joined and "-lhls" not in joined
    assert "-hls_playlist_type" not in joined, "event would defeat delete_segments"
    assert "delete_segments" in joined and "temp_file" in joined
    assert argv[-1].endswith("index.m3u8") and argv[-2].endswith("seg_%06d.ts")
    assert ("-fps_mode" in argv) != ("-vsync" in argv), "exactly one CFR flag"

    silent = VirtualSession(
        virtual_channels.validate_channel(
            {"name": "silent", "url": "https://e.com", "audio": False}
        ), 99,
    )
    sargv = silent._ffmpeg_argv()
    assert "-an" in sargv and "-f" in sargv
    assert "pulse" not in sargv and "aresample" not in " ".join(sargv)
    assert sargv.count("-thread_queue_size") == 1, "no audio input to queue"

    # Path traversal guard on the segment route.
    assert segment_is_safe("seg_000123.ts")
    for bad in ("../../etc/passwd", "index.m3u8", "seg_1.ts", "seg_000001.ts/../x", "", "a.ts"):
        assert not segment_is_safe(bad), bad

    rewritten = rewrite_playlist(
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg_000001.ts\n",
        "/api/virtual/demo", "tok",
    )
    assert "/api/virtual/demo/seg_000001.ts?token=tok" in rewritten
    assert "#EXT-X-TARGETDURATION:2" in rewritten, "tags pass through untouched"
    assert "#EXTM3U" in rewritten
    assert "/api/virtual/demo/#" not in rewritten, "comments must not be rewritten"

    print("virtual_session ok  (missing binaries: %s)" % (preflight() or "none"))
