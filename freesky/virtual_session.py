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
* **Tab capture by default, x11grab as the fallback.** x11grab samples the X
  display on ffmpeg's wall-clock timer. When the host is busy the timer slips,
  grabs bunch up, and ffmpeg both drops the bunched frames and duplicates the
  last one to fill the gap: the stream turns into a slideshow with bursts even
  though the browser is painting fine. Tab capture (freesky/virtual_capture_ext)
  takes frames from Chromium's own compositor with the timestamp of the frame
  they are, muxes the tab's audio on the same clock, and has Chromium encode
  the H.264 itself, so ffmpeg only remuxes. Frame timing is then a property of
  the browser, not of scheduler luck. x11grab remains per channel for pages tab
  capture cannot see.
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
import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
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

# Crash-proof breadcrumb trail for session startup.
#
# Session start spawns an X server, an audio daemon, a browser and an encoder.
# When one of those takes the whole backend process down with it -- an OOM kill,
# a signal, a segfault -- Python never gets to run an `except`, log a traceback,
# or return a response. The reverse proxy just reports 502 and the log holds
# nothing, which is exactly the dead end this was written for.
#
# So each stage records where it got to BEFORE attempting the next one, and the
# line is fsync'd so it survives a SIGKILL. Whatever the last line says is the
# stage that killed the process. Served back by GET /api/virtual-sessions/trace,
# because on a container you cannot get a shell into, an HTTP endpoint is the
# only way to read it.
TRACE_PATH = os.environ.get("VIRTUAL_TRACE_PATH", "/tmp/freesky-virtual-trace.log")
_TRACE_LIMIT = 400


def trace(stage: str, detail: str = "") -> None:
    """Append one fsync'd breadcrumb. Never raises: diagnostics must not break
    the thing they are diagnosing."""
    try:
        os.makedirs(os.path.dirname(TRACE_PATH) or "/tmp", exist_ok=True)
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {stage}"
        if detail:
            line += f" | {detail}"
        with open(TRACE_PATH, "a") as f:
            f.write(line + "\n")
            f.flush()
            # The whole point: without this the last line before a SIGKILL is
            # still sitting in the page cache and is lost.
            os.fsync(f.fileno())
    except Exception:
        pass


def read_trace(limit: int = _TRACE_LIMIT) -> list:
    """The most recent breadcrumbs, oldest first."""
    try:
        with open(TRACE_PATH, "r") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-limit:]]
    except OSError:
        return []


def _own_orphan(cmdline: str) -> bool:
    """True if this command line is a virtual-channel process we spawned.

    Deliberately narrow. Each pattern names something only this feature creates,
    so nothing else in the container can match: the X displays we allocate, an
    x11grab encoder, our per-channel PulseAudio sink, and a browser opened on one
    of our profiles.
    """
    if not cmdline:
        return False
    if cmdline.startswith("Xvfb"):
        # Only displays from our allocation range.
        match = re.search(r"Xvfb\s+:(\d+)", cmdline)
        return bool(match) and int(match.group(1)) >= _DISPLAY_BASE
    if "x11grab" in cmdline and cmdline.startswith("ffmpeg"):
        return True
    if PROFILE_ROOT and f"--user-data-dir={PROFILE_ROOT}" in cmdline:
        return True
    if "freesky-" in cmdline and "pulse" in cmdline.lower():
        return True
    return False


def reap_orphans() -> list:
    """Kill virtual-channel processes left over from a previous backend.

    This exists because the backend can die *during* session startup -- an OOM
    kill or a signal gives Python no chance to run `stop()`. The Xvfb, browser
    and encoder it had already spawned are then orphaned but still RUNNING, and
    the supervisor restarts only the backend, not the container.

    Without this they accumulate: every failed attempt adds another X server and
    another browser, memory pressure rises, and the next attempt dies sooner.
    The feature appears to "stop working permanently" when in fact the container
    is full of processes nobody owns any more. Only a container restart cleared
    it, which is not something a stream should ever need.

    Safe to run at startup precisely because the session manager is empty then:
    any matching process necessarily belongs to a previous life of this backend.
    Scans /proc rather than shelling out to pkill, so it needs no extra binary
    and cannot match on a truncated pattern.
    """
    killed = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return killed
    me = os.getpid()
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue  # process exited, or not ours to read
        if not _own_orphan(cmdline):
            continue
        try:
            os.kill(int(pid), signal.SIGKILL)
            killed.append(f"{pid}: {cmdline[:100]}")
        except OSError:
            continue
    if killed:
        logger.warning(
            "virtual: reaped %d orphaned process(es) from a previous backend; "
            "these accumulate when the backend dies mid-startup", len(killed))
        trace("reaped", f"{len(killed)} orphan(s)")
    # Stale X locks belong to the servers we just killed; leaving them behind
    # makes the next Xvfb refuse its display number.
    for entry in list(killed):
        match = re.search(r"Xvfb\s+:(\d+)", entry)
        if match:
            with contextlib.suppress(OSError):
                os.unlink(f"/tmp/.X{match.group(1)}-lock")
    return killed


def reset_trace(name: str) -> None:
    """Start a fresh trail for one attempt, so the last line is unambiguous."""
    with contextlib.suppress(OSError):
        with open(TRACE_PATH, "w") as f:
            f.write("")
    trace("attempt", f"channel={name} pid={os.getpid()}")

# X display numbers are allocated from here upward. 99 by convention, and well
# clear of anything a desktop session would claim.
_DISPLAY_BASE = int(os.environ.get("VIRTUAL_DISPLAY_BASE", "99"))

# Development knob: talk to Xvfb over TCP loopback instead of the unix socket.
# In the container the socket is the right thing. On a dev machine where
# /tmp/.X11-unix is not writable (WSLg mounts it read-only for the user), it is
# the only way to run the pipeline at all. Never needed in Docker.
_DISPLAY_TCP = os.environ.get("VIRTUAL_DISPLAY_TCP", "") == "1"


def display_name(display: int) -> str:
    """The DISPLAY string for a session's X server."""
    return f"127.0.0.1:{display}" if _DISPLAY_TCP else f":{display}"

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

# Opt-in GPU acceleration for the browser (EXPERIMENTAL).
#
# In software mode Chromium composites and captures on ONE thread in its GPU
# process. Measured at 1080p that thread saturates a core at about 30 presented
# frames a second, whatever the core count: 1080p50/60 is out of reach of
# software compositing on a serial thread. With a DRM render node mapped into
# the container (/dev/dri) and the VA-API drivers installed (Dockerfile), this
# switches Chromium to EGL on that device for compositing and to VA-API for
# video decode. Requires `devices: [/dev/dri:/dev/dri]` in docker-compose and
# an Intel or AMD iGPU. Not verified on this project's hardware; the status
# line reports whether /dev/dri is visible so it can be tried deliberately.
GPU = os.environ.get("VIRTUAL_GPU", "").strip() == "1"
GPU_DEVICE = "/dev/dri/renderD128"
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

# --- tab capture --------------------------------------------------------------
# The unpacked extension that does the capturing. It ships with the app; its id
# is pinned by the `key` in its manifest so Chromium can be told to trust it.
EXT_DIR = os.environ.get(
    "VIRTUAL_CAPTURE_EXT_DIR",
    os.path.join(os.path.dirname(__file__), "virtual_capture_ext"),
)
# Where the extension delivers the recording. Loopback straight to the backend,
# bypassing the reverse proxy: this is an internal pipe, not a client request.
# `or`, not a get() default: docker-compose passes the variable through as an
# empty string when it is unset in .env, and an empty base produced a relative
# URL that the extension rejected ("scheme must be ws or wss").
CAPTURE_WS_BASE = (
    os.environ.get("VIRTUAL_CAPTURE_WS", "").strip()
    or f"ws://127.0.0.1:{os.environ.get('BACKEND_PORT', '').strip() or '8005'}"
).rstrip("/")
# MediaRecorder emits a chunk this often. Smaller means less latency between
# the compositor and ffmpeg; 250ms is well under one segment and keeps the
# message rate trivial.
CAPTURE_TIMESLICE_MS = int(os.environ.get("VIRTUAL_CAPTURE_TIMESLICE_MS", "250"))
# H.264 in WebM is what Playwright's Chromium can record, and what lets ffmpeg
# remux with -c:v copy instead of encoding a second time.
CAPTURE_MIME_AV = "video/webm;codecs=h264,opus"
CAPTURE_MIME_V = "video/webm;codecs=h264"


def extension_id() -> str:
    """The extension id Chromium derives from the manifest's `key`.

    Chromium ids an unpacked extension by the SHA-256 of its public key when a
    key is present (else by its path). Computing it here from the same manifest
    keeps the --allowlisted-extension-id flag from silently drifting away from
    the extension it names, which would surface only as "Extension has not been
    invoked for the current page" at capture time.
    """
    cached = getattr(extension_id, "_cached", None)
    if cached:
        return cached
    with open(os.path.join(EXT_DIR, "manifest.json")) as f:
        key = json.load(f)["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    ext_id = "".join(chr(ord("a") + int(c, 16)) for c in digest)
    extension_id._cached = ext_id
    return ext_id


# Sessions currently accepting a capture feed, keyed by their one-time secret.
# Registered before the extension is told to connect and removed on stop, so
# the WebSocket route can admit a feed for a session that is still starting
# (it is not in manager._sessions until start() returns).
_capture_targets: Dict[str, "VirtualSession"] = {}


def capture_target(name: str, key: str) -> Optional["VirtualSession"]:
    """The session a capture feed belongs to, or None if the key is wrong."""
    session = _capture_targets.get(key or "")
    if session is None or session.name != name:
        return None
    return session

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


def _ffmpeg_has_bsf(name: str) -> bool:
    """True when the installed ffmpeg lists `name` in `ffmpeg -bsfs`.

    Cached per name; shells out once. Used to skip the timestamp snap on an
    ffmpeg too old to have `setts` (added in 5.0) instead of failing to start.
    """
    cache = getattr(_ffmpeg_has_bsf, "_cache", None)
    if cache is None:
        cache = _ffmpeg_has_bsf._cache = {}
    if name in cache:
        return cache[name]
    result = False
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-bsfs"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        result = any(line.strip() == name for line in out.splitlines())
    except (OSError, subprocess.SubprocessError):
        result = False
    cache[name] = result
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
        # Tab capture. The secret is what admits the extension's WebSocket feed
        # and nothing else: it is minted per session and never leaves the
        # container, so a client that can reach the backend port still cannot
        # inject video into a channel.
        self.capture = record.get("capture", virtual_channels.DEFAULT_CAPTURE)
        self.capture_secret = secrets.token_urlsafe(24)
        self.capture_connected = False
        self.capture_bytes = 0
        self.capture_chunks = 0
        self._capture_ready = asyncio.Event()
        self._capture_started = False
        # CPU accounting, filled in by the manager's sampler. Percent of one
        # core over the last sample interval, per process group.
        self.cpu: dict = {}
        self._cpu_sample: Optional[tuple] = None
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
        # Every stage is recorded before the next is attempted, so a crash that
        # kills the interpreter still names the stage that did it. See trace().
        reset_trace(self.name)
        trace("config", f"display=:{self.display} {self.width}x{self.height} "
                        f"audio={self.record['audio']} url={self.record['url'][:120]}")
        try:
            # Before anything is spawned: a second process must not get as far
            # as unlinking the X lock or opening the shared Chrome profile.
            trace("lock:begin")
            self._acquire_channel_lock()
            trace("lock:ok")

            trace("xvfb:begin")
            await self._start_display()
            trace("xvfb:ok")

            if self.record["audio"]:
                trace("pulse:begin")
                await _ensure_pulse()
                trace("pulse:ok")
                trace("sink:begin")
                await self._start_sink()
                trace("sink:ok")

            trace("browser:begin")
            await self._start_browser()
            trace("browser:ok")

            trace("ffmpeg:begin")
            await self._start_ffmpeg()
            trace("ffmpeg:ok")

            if wait_for_stream:
                trace("segments:begin")
                await self._await_first_segments()
                trace("segments:ok")
            trace("start:complete")
        except BaseException as exc:
            # BaseException, not Exception: asyncio.CancelledError inherits from
            # BaseException, so a cancelled start would otherwise skip both the
            # trace line and the teardown, leaking a browser and an X server.
            trace("start:failed", f"{type(exc).__name__}: {exc}")
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
            "-ac", "-dpi", "96", "+extension", "RANDR",
        ] + (["-listen", "tcp", "-nolisten", "unix"] if _DISPLAY_TCP else ["-nolisten", "tcp"])
        trace("xvfb:spawn", " ".join(argv))
        self._xvfb = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        trace("xvfb:spawned", f"pid={self._xvfb.pid}")

        # Poll rather than sleep: racing Chromium against a not-yet-listening
        # Xvfb is the single most common cause of flaky startup.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._xvfb.poll() is not None:
                raise VirtualSessionError(f"Xvfb exited immediately on display :{self.display}")
            try:
                await _run(["xdpyinfo", "-display", display_name(self.display)], timeout=5)
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
            # Frees the display scheduler from a synthetic 60Hz vblank timer.
            # NOT --disable-frame-rate-limit: unbounded frame production has
            # been reported to starve video decode, and this container is
            # CPU-bound, so it would compete with the encoder for the cores the
            # stream actually needs.
            "--disable-gpu-vsync",
        ] + self._render_args() + [
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
        ] + self._capture_args()

    def _render_args(self) -> list:
        """Software rendering by default; GPU via EGL/VA-API when opted in.

        Software: SwiftShader was removed here deliberately. It is a WebGL/GLES
        emulator, and routing a plain <video> page's 2D composites through an
        emulated GL driver is the expensive path. With no GPU in the container,
        Skia's CPU raster is both cheaper and what Chromium's own docs point at.
        --disable-software-rasterizer stops it falling back into SwiftShader.

        GPU (VIRTUAL_GPU=1): ANGLE on EGL talks to the render node directly, so
        no GLX and no X compositor is needed under Xvfb; the composited frame is
        still handed to X as pixels. VA-API decode moves the H.264/HEVC decode
        of the page's video off the CPU. Both need /dev/dri in the container.
        """
        if not GPU:
            return ["--disable-gpu", "--disable-software-rasterizer"]
        return [
            "--use-gl=angle", "--use-angle=gl-egl",
            "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization", "--enable-zero-copy",
            # A second --enable-features occurrence replaces Playwright's
            # (CDPScreenshotNewSurface, screenshot-only), which is acceptable
            # here and only here.
            "--enable-features=VaapiVideoDecoder,VaapiVideoDecodeLinuxGL,"
            "VaapiIgnoreDriverChecks,AcceleratedVideoDecodeLinuxGL",
            "--disable-features=UseChromeOSDirectVideoDecoder",
        ]

    def _capture_args(self) -> list:
        """Switches that load and trust the tab-capture extension.

        Only for tab capture: an x11grab session has no use for the extension
        and should not carry a capture-capable extension it never drives.

        --allowlisted-extension-id is what lets the extension call
        chrome.tabCapture.getMediaStreamId without the user having "invoked" it
        on the tab; without it capture fails at start. The id is derived from
        the manifest at runtime so the flag and the extension cannot disagree.
        """
        if self.capture != "tab":
            return []
        ext_id = extension_id()
        return [
            f"--load-extension={EXT_DIR}",
            f"--disable-extensions-except={EXT_DIR}",
            f"--allowlisted-extension-id={ext_id}",
            # The pre-M110 spelling, harmless on builds that ignore it.
            f"--whitelisted-extension-id={ext_id}",
        ]

    async def _start_browser(self) -> None:
        """Headful Chromium on our display, playing into our sink."""
        from playwright.async_api import async_playwright

        env = dict(os.environ)
        env["DISPLAY"] = display_name(self.display)
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
        trace("browser:profile", self.profile_dir)
        os.makedirs(self.profile_dir, exist_ok=True)
        self._seed_profile()
        trace("browser:profile-ok")

        args = self._browser_args()

        # launch_persistent_context, NOT launch(): Playwright rejects a
        # --user-data-dir in args outright ("Pass user_data_dir parameter to
        # browser_type.launch_persistent_context instead"), and that error is
        # raised before the browser starts. A persistent profile is what we want
        # anyway — it is how a login an admin performs through the control panel
        # survives for the life of the session.
        trace("browser:playwright-start")
        self._playwright = await async_playwright().start()
        trace("browser:playwright-ok")
        try:
            trace("browser:launch", " ".join(args[:6]) + f" ... ({len(args)} args)")
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
                # Playwright disables extensions by default. Tab capture IS an
                # extension, so that one default has to go for the tab path.
                ignore_default_args=(
                    ["--disable-extensions"] if self.capture == "tab" else []
                ),
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
        """The encoder command for this session's capture path."""
        if self.capture == "tab":
            return self._ffmpeg_argv_tab()
        return self._ffmpeg_argv_x11grab()

    def _hls_output_argv(self) -> list:
        """The HLS muxer half of the command, shared by both capture paths."""
        return [
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

    def _ffmpeg_argv_tab(self) -> list:
        """Remux the extension's WebM feed (H.264 + Opus) into HLS.

        The video is normally passed through untouched: Chromium already encoded
        it, with a keyframe every SEGMENT_SECONDS (offscreen.js asks for one via
        videoKeyFrameIntervalDuration), so every segment still starts on an IDR
        and the encode costs ffmpeg nothing. Only audio is transcoded, Opus to
        AAC, because MPEG-TS players do not take Opus.

        A crop is the one thing that forces a re-encode: cutting pixels out of a
        compressed stream needs decoding it first. That path uses the same
        libx264 settings as x11grab does.
        """
        record = self.record
        fps = record["framerate"]
        gop = fps * SEGMENT_SECONDS
        vb = record["video_bitrate"]

        argv = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:1",
            # The feed arrives on stdin as it is recorded; do not wait to probe
            # more of it than the first cluster before starting.
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", "pipe:0",
            "-map", "0:v",
        ]
        if record["audio"]:
            argv += ["-map", "0:a?"]
        box = virtual_channels.crop_box(record)
        if box:
            crop_x, crop_y, crop_w, crop_h = box
            argv += [
                "-vf", f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}",
                "-c:v", "libx264",
                "-preset", record["preset"],
                "-threads", str(ENCODER_THREADS),
                "-tune", "zerolatency",
                "-profile:v", "main", "-pix_fmt", "yuv420p",
                "-b:v", f"{vb}k", "-maxrate", f"{vb}k", "-bufsize", f"{vb * 2}k",
                "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
                "-x264-params", "repeat-headers=1",
            ]
            argv += ["-fps_mode", "cfr"] if _ffmpeg_supports_fps_mode() else ["-vsync", "cfr"]
        else:
            argv += ["-c:v", "copy"] + self._snap_bsf(fps)
        if record["audio"]:
            argv += [
                # The recorder's audio and video share a clock, so there is no
                # capture-clock drift to correct; a gentle async only absorbs
                # the odd gap Opus leaves when the tab briefly produces nothing.
                "-af", "aresample=async=1000:first_pts=0",
                "-c:a", "aac", "-b:a", f"{record['audio_bitrate']}k",
                "-ar", "48000", "-ac", "2",
            ]
        else:
            argv += ["-an"]
        return argv + self._hls_output_argv()

    @staticmethod
    def _snap_bsf(fps: int) -> list:
        """Snap copied video timestamps onto the channel's frame grid.

        Chromium's compositor in a container runs on a fixed 60Hz timer, so
        the frames the capturer hands over carry timestamps on a 16.7ms grid.
        Captured at 30fps a 50fps page comes out as 17/33/50ms intervals, and a
        25fps page captured at 25 alternates 33/50ms: the average rate is right
        but every third frame is early or late, which reads as a wobble on
        slow pans. Each timestamp is rounded to the nearest 1/fps slot, but
        never to a slot earlier than one past the previous frame's: two grid
        frames 17ms apart at 30fps would otherwise round into the same slot,
        and the second is pushed to the next one instead. Because the capturer
        is rate-limited to fps on average, a pushed frame is followed by a gap
        that rounding pulls back onto the true grid, so the shift stays within
        a frame and cannot accumulate over a long session. Measured: a 50fps
        page captured at 30 goes from 17/33/50ms intervals to 33ms every frame,
        and at 25 from 33/50 to 40ms every frame.

        Only meaningful with -c:v copy; the re-encode path already runs CFR.
        """
        if not _ffmpeg_has_bsf("setts"):
            # ffmpeg < 5.0 has no setts filter. The stream is still correct,
            # just on the compositor's grid rather than the channel's.
            return []
        # The comma inside the expression must be escaped, or ffmpeg reads it
        # as the separator between bsf options. argv goes straight to exec, so
        # exactly one backslash reaches ffmpeg. TB is the input timebase, so
        # 1/(TB*fps) is one frame slot in timestamp units.
        expr = f"max(PREV_OUTPTS+1/(TB*{fps})\\,round(TS*TB*{fps})/(TB*{fps}))"
        return ["-bsf:v", f"setts=ts={expr}"]

    def _ffmpeg_argv_x11grab(self) -> list:
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
        ]

        # A crop is applied at the INPUT, via x11grab's own geometry, not with a
        # -vf crop filter. x11grab then reads only those pixels off the X server
        # each frame, so a small region is cheaper to capture and cheaper to
        # encode; a filter would pull the whole screen across and throw most of
        # it away. The offset is the `+X,Y` suffix on the display specifier.
        box = virtual_channels.crop_box(record)
        if box:
            crop_x, crop_y, crop_w, crop_h = box
            argv += [
                "-video_size", f"{crop_w}x{crop_h}",
                "-i", f"{display_name(self.display)}.0+{crop_x},{crop_y}",
            ]
        else:
            argv += [
                "-video_size", f"{self.width}x{self.height}",
                "-i", f"{display_name(self.display)}.0",
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
        return argv + self._hls_output_argv()

    async def _start_ffmpeg(self) -> None:
        # Warm the ffmpeg version probe OFF the event loop. It shells out with a
        # 10s timeout and _ffmpeg_argv() calls it synchronously, so on the first
        # session after a restart it would otherwise stall the whole server --
        # every other request, /health included, just hangs. It caches on the
        # function, so this costs nothing on later starts.
        await asyncio.to_thread(_ffmpeg_supports_fps_mode)
        await asyncio.to_thread(_ffmpeg_has_bsf, "setts")

        # Wipe first, not just on teardown: append_list will happily resume onto
        # a playlist left behind by a crashed run and reference segments that no
        # longer exist.
        shutil.rmtree(self.out_dir, ignore_errors=True)
        os.makedirs(self.out_dir, exist_ok=True)

        env = dict(os.environ)
        env["DISPLAY"] = display_name(self.display)
        if self.record["audio"]:
            env.update(_pulse_env())

        self._ffmpeg = await asyncio.create_subprocess_exec(
            *self._ffmpeg_argv(),
            # Tab capture feeds the recording in on stdin; x11grab reads the
            # display itself and gets no stdin at all (-nostdin).
            stdin=asyncio.subprocess.PIPE if self.capture == "tab" else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        asyncio.create_task(self._drain_ffmpeg_log())
        asyncio.create_task(self._drain_ffmpeg_progress())

        if self.capture == "tab":
            trace("capture:begin")
            await self._start_tab_capture()
            trace("capture:ok")

    # -- tab capture -----------------------------------------------------------

    async def _start_tab_capture(self) -> None:
        """Tell the extension to record the page tab and stream it to us.

        The extension's service worker is driven directly through Playwright,
        which is what makes the whole thing need no user gesture and no UI. The
        feed lands on /api/virtual-capture/<name> (backend.py) and is written
        into ffmpeg's stdin by feed_capture().
        """
        assert self._context is not None
        try:
            worker = await self._extension_worker()
        except Exception as exc:
            raise VirtualSessionError(
                "Tab capture extension did not start (is freesky/virtual_capture_ext "
                f"present in the image?): {exc}"
            ) from exc

        _capture_targets[self.capture_secret] = self
        opts = {
            "ws": f"{CAPTURE_WS_BASE}/api/virtual-capture/{self.name}?key={self.capture_secret}",
            "fps": self.record["framerate"],
            "width": self.width, "height": self.height,
            "audio": bool(self.record["audio"]),
            "mime": CAPTURE_MIME_AV if self.record["audio"] else CAPTURE_MIME_V,
            "vbps": self.record["video_bitrate"] * 1000,
            "abps": self.record["audio_bitrate"] * 1000,
            "keyMs": SEGMENT_SECONDS * 1000,
            "timeslice": CAPTURE_TIMESLICE_MS,
        }
        try:
            result = await asyncio.wait_for(
                worker.evaluate("opts => startCapture(opts)", opts), timeout=30
            )
        except Exception as exc:
            raise VirtualSessionError(f"Tab capture failed to start: {exc}") from exc
        self._capture_started = True
        trace("capture:started", json.dumps(result)[:200])

        # The feed connecting is the proof that the whole path works end to end;
        # a start that returned but never connects is a dead session, and it is
        # cheaper to say so now than to time out waiting for segments.
        try:
            await asyncio.wait_for(self._capture_ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            raise VirtualSessionError(
                "Tab capture started but its feed never reached the backend at "
                f"{CAPTURE_WS_BASE} (VIRTUAL_CAPTURE_WS)"
            ) from None

    async def _extension_worker(self, timeout: float = 20.0):
        """The capture extension's service worker, waiting for it if needed.

        Not `context.service_workers[0]`: a site can register a service worker
        of its own (Sky Sport does), and it is often first in the list. Picking
        it produced "ReferenceError: startCapture is not defined" -- the
        function was being looked for in the wrong worker. Match on the
        extension's origin instead, and retry briefly in case the worker exists
        but its script has not finished evaluating its globals.
        """
        prefix = f"chrome-extension://{extension_id()}/"
        deadline = time.monotonic() + timeout
        while True:
            for worker in self._context.service_workers:
                if worker.url.startswith(prefix):
                    try:
                        if await worker.evaluate("() => typeof startCapture === 'function'"):
                            return worker
                    except Exception:
                        pass  # still evaluating its script; retry below
            if time.monotonic() >= deadline:
                seen = [w.url for w in self._context.service_workers]
                raise VirtualSessionError(
                    f"no service worker for extension {extension_id()} "
                    f"(workers present: {seen or 'none'})"
                )
            await asyncio.sleep(0.25)

    def accepts_capture(self, key: str) -> bool:
        """True when `key` is this session's live capture secret."""
        return bool(key) and secrets.compare_digest(key, self.capture_secret) \
            and self._ffmpeg is not None

    def capture_opened(self) -> None:
        self.capture_connected = True
        self._capture_ready.set()

    def capture_closed(self) -> None:
        self.capture_connected = False

    async def feed_capture(self, data: bytes) -> None:
        """Write one recorder chunk into ffmpeg. Raises when ffmpeg is gone."""
        proc = self._ffmpeg
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise VirtualSessionError("encoder is not running")
        proc.stdin.write(data)
        # Back-pressure: if ffmpeg falls behind, this waits, the WebSocket read
        # loop waits, and the extension's bufferedAmount grows where the
        # diagnostics endpoint can see it, rather than memory growing here.
        await proc.stdin.drain()
        self.capture_bytes += len(data)
        self.capture_chunks += 1

    async def capture_status(self) -> dict:
        """What the extension reports about its recording, for diagnostics."""
        if self.capture != "tab":
            return {"mode": self.capture}
        out = {"mode": "tab", "connected": self.capture_connected,
               "bytes": self.capture_bytes, "chunks": self.capture_chunks}
        try:
            if self._context is not None:
                worker = await self._extension_worker(timeout=2)
                out["recorder"] = await asyncio.wait_for(
                    worker.evaluate("() => captureStatus()"), timeout=5
                )
        except Exception as exc:
            out["recorder"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

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
                # WebM packets carry no duration, and the hls muxer says so on
                # every one when copying. It is harmless (EXTINF comes out
                # right) but would push every real warning out of the tail.
                if "pkt->duration = 0" in line or line.startswith("Last message repeated"):
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
        _capture_targets.pop(self.capture_secret, None)
        # ffmpeg first, and gracefully: SIGKILL leaves a playlist with no
        # ENDLIST, which players poll forever.
        if self._ffmpeg is not None and self._ffmpeg.returncode is None:
            # EOF on stdin is the clean end for the remux path.
            if self._ffmpeg.stdin is not None:
                with contextlib.suppress(Exception):
                    self._ffmpeg.stdin.close()
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
            "-i", f"{display_name(self.display)}.0",
            # -2 keeps the height even, which the encoder requires.
            "-vf", f"scale={width}:-2",
            "-q:v", str(CONTROL_QUALITY), "-f", "mjpeg", "pipe:1",
        ]
        env = dict(os.environ)
        env["DISPLAY"] = display_name(self.display)
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
        display = display_name(self.display)
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
            # Stream copy reports no encoder fps; the recorder's rate is the
            # channel's, so fall back to the configured value there.
            "fps": self.metrics.get("fps") or (str(self.record["framerate"]) if self.capture == "tab" else "-"),
            "speed": self.metrics.get("speed", "-"),
            "dup": self.metrics.get("dup", "-"),
            "drop": self.metrics.get("drop", "-"),
            "capture": self.capture,
            "capture_connected": self.capture_connected,
            # Percent of one core over the last sample (see SessionManager.
            # _sample_cpu). "-" until the first two samples exist.
            "cpu_browser": self.cpu.get("browser", "-"),
            "cpu_encoder": self.cpu.get("encoder", "-"),
            "cpu_display": self.cpu.get("display", "-"),
            "error": self.error,
            "log": self._log_tail[-5:],
        }

    def _pids(self) -> dict:
        """Process ids per group: the browser tree, the encoder, the display.

        Chromium's helper processes do not repeat --user-data-dir, so the tree
        is found by parent links from the one process that does.
        """
        out = {"browser": [], "encoder": [], "display": []}
        if self._ffmpeg is not None and self._ffmpeg.returncode is None:
            out["encoder"].append(self._ffmpeg.pid)
        if self._xvfb is not None and self._xvfb.poll() is None:
            out["display"].append(self._xvfb.pid)
        marker = f"--user-data-dir={self.profile_dir}"
        parents: Dict[int, int] = {}
        root = None
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                try:
                    with open(f"/proc/{pid}/stat") as f:
                        parents[pid] = int(f.read().rsplit(")", 1)[1].split()[1])
                    if root is None:
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            cmd = f.read()
                        if marker.encode() in cmd and b"--type=" not in cmd:
                            root = pid
                except OSError:
                    continue
        except OSError:
            return out
        if root is None:
            return out
        tree = {root}
        # A few passes are enough: Chromium's tree is at most a few levels deep.
        for _ in range(4):
            tree |= {pid for pid, ppid in parents.items() if ppid in tree}
        out["browser"] = sorted(tree)
        return out


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

    def _sample_cpu(self) -> None:
        """Refresh every session's per-group CPU percentages.

        Reads /proc directly rather than shelling out to ps, and keeps one prior
        sample per session so the number is a rate over the last interval rather
        than a lifetime average that hides a stall.
        """
        clk = os.sysconf("SC_CLK_TCK")
        now = time.monotonic()
        for session in list(self._sessions.values()):
            ticks = {}
            for group, pids in session._pids().items():
                total = 0
                for pid in pids:
                    try:
                        with open(f"/proc/{pid}/stat") as f:
                            parts = f.read().rsplit(")", 1)[1].split()
                        total += int(parts[11]) + int(parts[12])
                    except (OSError, IndexError, ValueError):
                        continue
                ticks[group] = total
            prev = session._cpu_sample
            session._cpu_sample = (now, ticks)
            if prev is None:
                continue
            elapsed = now - prev[0]
            if elapsed <= 0:
                continue
            session.cpu = {
                group: round((ticks.get(group, 0) - prev[1].get(group, 0)) / clk / elapsed * 100)
                for group in ticks
            }

    async def _reap_loop(self) -> None:
        """Tear down idle and dead sessions.

        A browser and an encoder per channel is expensive; nothing should stay up
        because someone opened a tab yesterday.
        """
        try:
            while self._sessions:
                await asyncio.sleep(5)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._sample_cpu)
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


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


_throttle_sample: Optional[tuple] = None


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return ""


def host_load() -> dict:
    """The CPU picture a stuttering stream is usually explained by.

    `quota` is the container's CPU limit in cores (0 = none). `throttled_pct`
    is the share of CFS periods in which the container was stopped for having
    used its quota, over the interval since this was last called. That number
    is the one to look at first: a `cpus:` limit is enforced by pausing every
    thread once the quota for the current 100ms period is spent, and Chromium,
    with dozens of threads, can spend a 4-core quota in 40ms on an 8-core host
    and then sit frozen for the remaining 60ms. That shows up as a stream that
    stutters at ~10Hz while average CPU looks fine. Pinning cores (cpuset)
    instead of a quota has no such pause; see docker-compose.yml.
    """
    global _throttle_sample
    out: dict = {"cpus": os.cpu_count() or 0, "quota": 0.0, "load": None,
                 "throttled_pct": None, "cgroup": "",
                 # What the box is, and whether a GPU render node is visible
                 # inside the container. Decides whether VIRTUAL_GPU=1 can work.
                 "cpu_model": _cpu_model(),
                 "gpu_device": os.path.exists(GPU_DEVICE),
                 "gpu_mode": GPU}
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    # cgroup v2, then v1.
    periods = throttled = None
    cpu_max = _read("/sys/fs/cgroup/cpu.max")
    if cpu_max:
        out["cgroup"] = "v2"
        quota, _, period = cpu_max.partition(" ")
        if quota != "max" and period.isdigit() and int(period):
            out["quota"] = round(int(quota) / int(period), 2)
        stat = dict(line.split(" ", 1) for line in _read("/sys/fs/cgroup/cpu.stat").splitlines() if " " in line)
        periods, throttled = stat.get("nr_periods"), stat.get("nr_throttled")
    else:
        quota = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota:
            out["cgroup"] = "v1"
            if quota.lstrip("-").isdigit() and int(quota) > 0 and period.isdigit() and int(period):
                out["quota"] = round(int(quota) / int(period), 2)
            stat = dict(line.split(" ", 1) for line in _read("/sys/fs/cgroup/cpu/cpu.stat").splitlines() if " " in line)
            periods, throttled = stat.get("nr_periods"), stat.get("nr_throttled")
    if periods is not None and throttled is not None and periods.isdigit() and throttled.isdigit():
        sample = (int(periods), int(throttled))
        prev = _throttle_sample
        _throttle_sample = sample
        if prev is not None and sample[0] > prev[0]:
            out["throttled_pct"] = round((sample[1] - prev[1]) / (sample[0] - prev[0]) * 100)
        elif prev is None and sample[0]:
            out["throttled_pct"] = round(sample[1] / sample[0] * 100)
    return out


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
         "framerate": 30, "video_bitrate": 3000, "audio_bitrate": 96,
         "capture": "x11grab"}
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

    # Tab capture: Chromium encodes, ffmpeg only remuxes.
    tab = VirtualSession(virtual_channels.validate_channel(
        {"name": "tab", "url": "https://example.com", "framerate": 30}), 99)
    targv = tab._ffmpeg_argv()
    tjoined = " ".join(targv)
    assert "pipe:0" in targv and "x11grab" not in tjoined and "pulse" not in tjoined
    assert targv[targv.index("-c:v") + 1] == "copy", "no second encode without a crop"
    assert "-c:a aac" in tjoined, "MPEG-TS players do not take Opus"
    assert "-nostdin" not in targv, "stdin IS the input on this path"
    assert targv[-1].endswith("index.m3u8") and "delete_segments" in tjoined
    assert any(a.startswith("--load-extension=") for a in tab._browser_args())
    assert f"--allowlisted-extension-id={extension_id()}" in tab._browser_args()
    assert not any(a.startswith("--load-extension=") for a in session._browser_args()), \
        "x11grab sessions carry no capture extension"
    assert re.fullmatch(r"[a-p]{32}", extension_id()), extension_id()
    cropped = VirtualSession(virtual_channels.validate_channel(
        {"name": "crop", "url": "https://example.com", "crop_x": 10, "crop_y": 10,
         "crop_w": 640, "crop_h": 360}), 99)
    cjoined = " ".join(cropped._ffmpeg_argv())
    assert "crop=640:360:10:10" in cjoined and "libx264" in cjoined, "a crop needs a re-encode"
    assert not tab.accepts_capture(tab.capture_secret), "no encoder yet, no feed"
    assert capture_target("tab", tab.capture_secret) is None, "not registered until capture starts"

    silent = VirtualSession(
        virtual_channels.validate_channel(
            {"name": "silent", "url": "https://e.com", "audio": False, "capture": "x11grab"}
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
