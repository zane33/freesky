# Virtual Channels — restreaming a web page as live HLS

A **virtual channel** turns any web page into a live TV channel. FreeSky opens
the page in a real browser on a private virtual display inside the container,
has the browser record its own tab (video *and* audio, H.264-encoded by the
browser itself), remuxes that to HLS, and serves it as a rolling playlist. To a player it is indistinguishable from
any other channel: it appears in `/playlist.m3u8`, in the web UI, and works in
VLC, Jellyfin and Dispatcharr.

This exists for sources that have no stream URL to proxy — a site that only
plays inside its own player, a dashboard, a webcam page, a scoreboard.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Creating a channel](#creating-a-channel)
3. [Controlling a live session](#controlling-a-live-session)
4. [Running many channels at once](#running-many-channels-at-once)
5. [1080p and high frame rates](#1080p-and-high-frame-rates)
6. [One worker, always](#one-worker-always)
6. [Cropping: streaming part of the screen](#cropping-streaming-part-of-the-screen)
7. [Configuration reference](#configuration-reference)
8. [API reference](#api-reference)
9. [Architecture and design decisions](#architecture-and-design-decisions)
10. [Security model](#security-model)
11. [Troubleshooting](#troubleshooting)

---

## How it works

```
                    ┌──────────────── one session, per channel ─────────────────┐
                    │                                                            │
 GET /api/stream/   │   Xvfb :99          Chromium (headful, --kiosk)            │
 virt-<name>.m3u8 ──┼─► 1280x720x24  ◄──── renders the page into the display     │
                    │   (control-panel        │                                  │
                    │    preview only)        │ tab capture extension            │
                    │                         │ (freesky/virtual_capture_ext):   │
                    │                         │ compositor frames + tab audio,   │
                    │                         │ H.264/Opus in WebM, one keyframe │
                    │                         │ per segment                      │
                    │                         ▼                                  │
                    │   ws://127.0.0.1:8005/api/virtual-capture/<name>?key=…     │
                    │                         │                                  │
                    │                         ▼                                  │
                    │      ffmpeg  -c:v copy (timestamps snapped to 1/fps),      │
                    │              Opus → AAC                                    │
                    │                         ▼                                  │
                    │   /streams/<name>/index.m3u8 + seg_%06d.ts   (tmpfs)       │
                    └────────────────────────────┬───────────────────────────────┘
                                                 │
 player ◄── rewritten playlist ◄─────────────────┘
        ◄── GET /api/virtual/<name>/seg_000123.ts?token=…
```

That is the default, **tab capture**. The older path, **x11grab**, is still
available per channel: ffmpeg samples the X display on its own timer and
records a PulseAudio null-sink that Chromium plays into, then encodes with
libx264. See [Capture paths](#capture-paths-tab-vs-x11grab) for why the
default changed and when to pick the other one.

**Lifecycle.** A session starts when the first player requests the channel and
stops after `idle_timeout` seconds with no request. Every segment request is a
heartbeat. The first request blocks while the browser loads and the encoder
produces its first segments (up to ~45s on a slow page) — deliberately, because
a playlist with no segments makes players conclude the channel is dead and stop
retrying.

**Liveness is playlist freshness, not process liveness.** ffmpeg can sit holding
a dead X connection, and Chromium can crash while ffmpeg happily keeps capturing
a blank root window — in both cases the process table looks perfect. A session
whose `index.m3u8` has not been rewritten in 4 segment durations is considered
stalled and is rebuilt on the next request.

Code: [`freesky/virtual_channels.py`](../freesky/virtual_channels.py) (the
record store) and [`freesky/virtual_session.py`](../freesky/virtual_session.py)
(the capture pipeline).

---

## Creating a channel

**Settings → Virtual Channels → Add a virtual channel.** Settings is admin-only.

| Field | What it does |
|---|---|
| **Name (id)** | Slug. Becomes the channel id `virt-<name>`, the M3U `tvg-id`, and the segment directory name. Lowercase letters, digits, `-`, `_`. |
| **Display name** | What appears in the guide and the M3U. |
| **Page URL** | The page to restream. `http://` or `https://` only. |
| **Resolution** | `480p`, `720p` or `1080p`. This is the Xvfb screen size *and* the browser window size — nothing is scaled. |
| **Frame rate** | 15, 20, 24, 25, 30, 50 or 60. Default 30. With tab capture, prefer a rate that divides 60 (20, 30, 60): the browser's compositor runs on a 60Hz timer and those give even frame intervals. 50/60 double the capture and encode cost. |
| **Capture** | `tab` (default): the browser records its own tab and encodes the H.264; ffmpeg only remuxes. `x11grab`: ffmpeg samples the X display and encodes. See [Capture paths](#capture-paths-tab-vs-x11grab). |
| **Encoder preset** | `ultrafast`, `superfast` or `veryfast`. Slower presets cannot keep up with realtime capture, so they are not offered. Used by x11grab, and by tab capture only when a crop forces a re-encode. |
| **Video kbps** | Target bitrate. 2500 is right for 720p30; 4500–6000 for 1080p30. Tab capture hands this to the browser's encoder (Constrained Baseline, which is a little less efficient than x264's Main, so lean higher). |
| **Capture audio** | Off drops the audio track entirely — worth it for a silent dashboard. |
| **Audio kbps** | 128 is fine for most things. |
| **Warm-up seconds** | How long to let the page settle (fonts, player bootstrap, consent dialogs) before the encoder starts, so viewers don't join on a half-painted page. |
| **Idle timeout (s)** | Seconds with no request before the session is torn down. |
| **Hide these** | CSS selectors, one per line, hidden with `display: none` after load. Cookie banners, overlays, headers. |
| **Click these** | CSS selectors, one per line, clicked after load to start playback. A selector that matches nothing is normal and is not an error. |
| **Logo / Tags** | As for any other channel. |

Changes take effect on the next session — saving a channel stops any running
session for it so the next tune-in picks up the new settings.

### Capture paths: tab vs x11grab

**Why tab capture is the default.** x11grab records what is on the X display
by grabbing it on ffmpeg's wall-clock timer. That works when the host is idle
and fails in a specific, ugly way when it is not: the timer slips, grabs bunch
up, and ffmpeg both *drops* the bunched frames (their timestamps collide) and
*duplicates* the last frame to fill the gap before them. Measured on a real
deployment, a 720p30 channel whose page was presenting 44fps produced about
**two distinct pictures a second** — 5000 duplicated and 2000 dropped frames in
three minutes — while ffmpeg reported a healthy 30fps at speed 1.0x. Wall-clock
sampling cannot be made smooth under load, because the picture's timing is
decided by scheduler luck, not by the content.

Tab capture takes the frames from Chromium's own compositor, each stamped with
the time of the frame it *is*, and the tab's audio on the same clock. Under load
it degrades to fewer frames at the right times instead of bursts of the same
frame. Chromium also encodes the H.264 itself, so ffmpeg's job shrinks from
"grab, colour-convert, encode" to "remux": measured, the ffmpeg process went
from ~25% of a core to ~4%. The browser's own cost goes up by about the same
amount for the capture copy and the encode, so the total is similar — but none
of it is timing-sensitive any more.

Two side effects worth knowing:

- **Browser UI is no longer in the stream.** A "Save password?" bubble or an
  autofill dropdown is drawn by the browser, not the page, so tab capture does
  not see it. The control panel still does (it previews the X display), so an
  admin can dismiss it; viewers never saw it. With x11grab such bubbles were
  visible in the stream.
- **The stream is Constrained Baseline H.264** (what Chromium's encoder
  produces) rather than Main. Every player takes it; it needs a slightly higher
  bitrate for the same quality.

**When to pick x11grab.** A page that blanks protected (DRM) video under
capture — tab capture then records black — or a host whose Chromium build will
not load the extension (the session fails to start with "Tab capture extension
did not start"). It remains fully supported and is exercised by the same tests.

**Timestamps.** Chromium's compositor in a container has no monitor to sync to
and runs on a fixed 60Hz timer, so captured frames arrive on a 16.7ms grid. A
30fps capture of a 50fps page comes out as 17/33/50ms intervals and a 25fps
capture alternates 33/50ms: right on average, wobbling frame to frame. ffmpeg
snaps each copied frame to the nearest 1/fps slot (never earlier than one past
the previous frame), which measured as **33ms every frame at 30fps and 40ms
every frame at 25fps**, with zero duplicated or dropped frames. Each frame is
snapped on its own, so nothing accumulates over a long session.

### Getting a page to actually play

Most "it's just a black screen" problems are one of three things:

1. **Autoplay.** FreeSky already passes
   `--autoplay-policy=no-user-gesture-required` and issues a synthetic click at
   the centre of the page, which satisfies the autoplay gate unconditionally.
   If the site needs a specific button, add its selector to **Click these**.
2. **A consent dialog covering the page.** Add its selector to **Hide these**,
   or its accept button to **Click these**.
3. **A login.** Use **Control** (below) to sign in once — the browser profile
   lives for the life of the session.

---

## Controlling a live session

**Settings → Virtual Channels → Control** opens a remote-control panel for that
channel's browser in a new tab. It shows a live view of the page and, once you
press **Take control**, forwards your mouse and keyboard to it:

- click, right-click, double-click, scroll
- typing, including Enter/Tab/arrows
- back / forward / reload
- a temporary **Go** to another URL (transient — the channel returns to its
  configured URL on the next session; edit the channel to change it for good)

Use it to sign into a site, dismiss a dialog, choose a quality setting, or
scroll something into place before the channel goes out. The panel starts the
session if it is not already running, and keeping the panel open keeps the
session alive.

### How the live view works

**The panel shows exactly what viewers see.** It captures the same X display the
encoder captures, at reduced resolution (`VIRTUAL_CONTROL_MAX_WIDTH`, default
960px) and frame rate (`VIRTUAL_CONTROL_FPS`, default 10) — only the size and
smoothness are reduced, never the content.

This is deliberate and was not the first design. The panel originally used a CDP
screencast, which captures the page's compositor surface. That is cheaper and
smoother, but it captures **only the page**: Chromium's own UI — a "Save
password?" bubble, an autofill dropdown, an infobar, a permission prompt — is
drawn by the browser into its X window, not by the page. Those dialogs therefore
appeared in the stream, where viewers saw them, while being invisible in the
panel where an admin might have dismissed them. Two capture layers meant two
different pictures, so there is now only one.

For the same reason, **input is injected at the X level** (via `xdotool`) rather
than through the page. A page-level click cannot reach a save-password bubble,
because as far as the page is concerned that bubble does not exist. Coordinates
are therefore X display coordinates, which are the channel's configured
geometry. If `xdotool` is missing the panel degrades to page-level input, which
still works for page content; the preflight in Settings reports its absence.

The panel also **drops pointer-move events while one is still in flight**. A
move is only useful if it is the latest one; sending them regardless builds a
queue the server works through long after the pointer has moved on. Clicks and
keystrokes are never dropped.

The stream itself is unaffected and stays at the channel's configured
resolution and frame rate.

The panel is a plain HTML page served by the backend rather than a Reflex route,
because faithful remote control needs raw pointer and keyboard events with exact
coordinates and modifier state. Pointer coordinates are scaled from the rendered
preview back into page coordinates, so clicks land correctly on any screen size.

---

## Running many channels at once

There is **no limit** by default: run as many virtual channels simultaneously as
the host can carry. Each is fully independent — its own X display, its own
browser profile, its own PulseAudio null-sink (so two channels never bleed into
each other's audio), its own encoder and its own segment directory.

Budget, per concurrent session:

| Resolution | CPU | Memory |
|---|---|---|
| 720p30 | ~1.5–2 cores | ~900 MB |
| 1080p30 | ~3–4 cores | ~1.6 GB |

720p is the single biggest lever for density and is the default for that reason.

**How the CPU is limited matters as much as how much there is.** The default
`cpus:` limit in `docker-compose.yml` is a CFS *quota*, enforced by pausing every
thread in the container once the quota for the current 100ms period is spent.
Chromium runs dozens of threads and, on a host with more cores than the quota,
can spend a 4-core allowance in 40ms and then sit frozen for 60ms — ten times a
second. Average CPU looks fine; the stream stutters. **Settings → Virtual
Channels → Sessions** shows this as `throttled N% of periods`. If that number
is above a few percent, set `CPUSET` (e.g. `CPUSET=0-3`) to pin whole cores
instead, or raise `CPU_LIMIT`. Each running session also shows its own CPU
split (browser / encoder / display) so a starved browser is visible as such.

The practical ceiling is the container's memory limit, not a code constant — see
`MEMORY_LIMIT` below. If the container is OOM-killed, **every** channel goes
down, not just the newest, so size it deliberately.

Set `MAX_VIRTUAL_SESSIONS` to a positive number only if you want a hard cap; in
that mode the least recently watched session is evicted to make room.

---

## 1080p and high frame rates

720p30 is comfortable on a modern 4-core budget. 1080p30 works with about
twice the browser CPU. **1080p50/60 does not work under software rendering,
and no amount of cores fixes it.** Measured: with a 60fps capture at 1080p the
browser's GPU process sits at ~100% of *one* core while the page presents only
~16 frames a second and the capture yields ~30 distinct pictures — Chromium's
software compositor and the capture copy run on a single thread, so the
ceiling is the speed of one core, not the count. On the 8-core deployment
host that thread is slower still.

What helps, in order:

1. **Match the source, don't exceed it.** Sky Sport is 50fps; capturing at 60
   encodes the compositor's repeat frames for nothing. Use 50, or 25 if the
   host cannot sustain 50 (a clean 2:1 of the source, 40ms every frame).
2. **Give the container real cores** (`CPUSET`), not a quota. Throttling shows
   up as stutter before it shows up as load.
3. **GPU mode** (`VIRTUAL_GPU=1` plus `/dev/dri` mapped in; drivers are in the
   image). This moves compositing and video decode to the iGPU and is the only
   route to 1080p50/60. The Sessions line reports the CPU model and whether
   `/dev/dri` is visible inside the container, so you can tell whether it is
   worth trying before touching anything. It is experimental: it was not
   verified on this project's hardware. If the session fails to start with it
   on, the startup log in the control panel names the stage; turn it back off.
4. Otherwise, 1080p30 or 720p50 are the honest options for a CPU-only host.

## One worker, always

The backend must run as a **single** granian worker process. `start.sh` enforces
this with `export GRANIAN_WORKERS="$WORKERS"`, where `WORKERS` defaults to 1.

This is not a tuning preference. Each worker imports the app separately, so each
gets its own `virtual_session.manager`, its own X display counter starting at
`:99`, and its own copy of the autostart lifespan task. Several workers then race
to start the *same* channel: unlinking each other's `/tmp/.X99-lock`, launching
competing Xvfb servers on one display, opening the single persistent Chrome
profile N times over, and pointing several encoders at one playlist. Workers die,
and granian's shared listener resets some connections, which the reverse proxy
reports as intermittent 502s from the control panel.

`start.sh` also *clamps* the value rather than merely defaulting it: both
`docker-compose.yml` and `start.sh` already defaulted to 1, and a leftover
`WORKERS=4` in `.env` still won, because compose's `${WORKERS:-1}` applies only
when the variable is **unset**. A non-1 value is now warned about and forced to
1 unless `ALLOW_MULTIPLE_WORKERS=true` is set.

Setting `WORKERS` alone does not achieve this. `reflex run` never reads it, and
`reflex.utils.processes.get_num_workers()` returns `(os.cpu_count() * 2) + 1`
whenever it can ping Redis -- which `start.sh` always starts. Only
`GRANIAN_WORKERS` has any effect.

As a backstop, `VirtualSession.start()` takes an exclusive `flock` on
`$VIRTUAL_LOCK_ROOT/<channel>.lock`. A second process is refused with a readable
error instead of silently corrupting the browser profile. The lock is held by an
open file descriptor, so it is released automatically if a process dies.

## Cropping: streaming part of the screen

By default a virtual channel streams the whole browser screen. Often only part
of it is worth sending -- a video player inside a page full of navigation,
headers and related-content rails.

Open **Control** for the channel, click **Crop**, and drag a rectangle over the
live view. The view is a capture of the same X display the encoder records, so
the rectangle you draw is exactly what viewers get; there is no second preview
geometry that can disagree with the stream. **Apply crop** saves it, and
**Full screen** clears it again. An existing crop is outlined in green when you
open the panel.

Taking control and selecting a crop are mutually exclusive: arming the selector
releases control, so a drag cannot click through and follow a link on the page.

Three things worth knowing:

- **The resolution setting sizes the browser window; the crop selects the part
  of that window which is streamed.** A 720p channel cropped to 800x450 gives
  viewers an 800x450 stream of a 1280x720 browser. The region is sent at its
  native pixels rather than scaled back up, so it stays sharp and costs less to
  encode.
- **Width and height are rounded down to even numbers.** H.264 with `yuv420p`
  subsamples chroma 2x2 and rejects an odd dimension outright.
- **Changing the crop restarts the session.** It is part of ffmpeg's input
  geometry, so a new encoder is required. Re-applying an identical crop does
  nothing, and so does not interrupt anyone watching.

With x11grab the crop is applied to the **input** (`-video_size WxH -i
:99.0+X,Y`), not with a `-vf crop` filter: x11grab then reads only those pixels
off the X server each frame. With tab capture a crop is the one thing that
forces ffmpeg to re-encode (`-vf crop` + libx264 with the channel's preset and
bitrate), because pixels cannot be cut out of an already-compressed stream. A
cropped tab-capture channel therefore costs about what an x11grab channel does;
an uncropped one costs ffmpeg almost nothing.

The settings form has no crop fields -- a crop is something you pick by looking
at the page -- but saving that form preserves whatever the panel set.

## Configuration reference

All optional. Defaults are in `docker-compose.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `VIRTUAL_CHANNELS_FILE` | `/app/data/virtual_channels.json` | Where channel records are stored. On the `./data` volume so they survive a rebuild. |
| `VIRTUAL_HLS_ROOT` | `/streams` | Where HLS output is written. Should be a tmpfs. |
| `VIRTUAL_LOCK_ROOT` | `/tmp/freesky-locks` | Per-channel `flock` files that stop two processes running one channel. Must not live under `VIRTUAL_HLS_ROOT`, which is deleted on stop. |
| `MAX_VIRTUAL_SESSIONS` | `0` | `0` = unlimited. A positive number caps concurrency with LRU eviction. |
| `VIRTUAL_SEGMENT_SECONDS` | `2` | Segment length, and therefore the GOP length. |
| `VIRTUAL_PLAYLIST_SIZE` | `6` | Segments kept in the playlist window. |
| `VIRTUAL_START_TIMEOUT` | `45` | Seconds to wait for the first segments before giving up. |
| `VIRTUAL_ENCODER_THREADS` | `2` | x264 thread cap. Unpinned, x264 spawns ~1.5x ncpu threads and starves the browser it is capturing. |
| `VIRTUAL_RASTER_THREADS` | `2` | Chromium raster threads, for the same reason. |
| `VIRTUAL_CAPTURE_WS` | `ws://127.0.0.1:$BACKEND_PORT` | Where the tab-capture extension delivers its recording. Loopback to the backend, bypassing the proxy. |
| `VIRTUAL_CAPTURE_TIMESLICE_MS` | `250` | How often the recorder emits a chunk. |
| `VIRTUAL_CAPTURE_EXT_DIR` | `freesky/virtual_capture_ext` | The unpacked extension. |
| `VIRTUAL_DISPLAY_TCP` | unset | Development only: talk to Xvfb over TCP loopback instead of the unix socket, for machines where `/tmp/.X11-unix` is not writable (WSL). Never needed in Docker. |
| `VIRTUAL_GPU` | unset | `1` switches Chromium to EGL compositing and VA-API decode on `/dev/dri` (must be mapped into the container). Experimental; see [1080p and high frame rates](#1080p-and-high-frame-rates). |
| `CPU_LIMIT` | `4` | Container CPU quota. See `CPUSET`. |
| `CPUSET` | unset | Pin the container to these cores (e.g. `0-3`) instead of metering it with a quota. Prefer this: a quota pauses the whole container when spent, which stutters the stream. |
| `VIRTUAL_CONTROL_FPS` | `10` | Preview frame rate. |
| `VIRTUAL_CONTROL_MAX_WIDTH` | `960` | Preview is downscaled to this width. Lower it first if the panel feels slow. |
| `VIRTUAL_CONTROL_QUALITY` | `7` | Preview JPEG quality on ffmpeg's mjpeg scale — 2 is best, 31 is worst. |
| `VIRTUAL_DISPLAY_BASE` | `99` | First X display number to allocate. |
| `VIRTUAL_PULSE_DIR` | `/tmp/freesky-pulse` | Runtime dir for the container-local PulseAudio daemon. |
| `MEMORY_LIMIT` | `8G` | Container memory limit. **This is what actually decides how many channels you can run.** |

### Container requirements

The Dockerfile installs everything needed: `xvfb`, `x11-utils`, `pulseaudio`,
`pulseaudio-utils`, `ffmpeg`, `dbus`, `dumb-init`, the `fonts-*` set (without
which pages render as tofu boxes), and the extra Chromium libraries needed to
run headful.

Two compose settings are load-bearing:

- **`shm_size: "1gb"`** — Chromium uses `/dev/shm` to share render surfaces
  between its processes and crashes with blank pages on Docker's 64 MB default.
  The `--disable-dev-shm-usage` flag is also set, but that only falls back to
  `/tmp`; raising `shm_size` is the real fix.
- **`tmpfs: /streams`** — segments are write-heavy, live for seconds, and are
  never worth touching disk.

`dumb-init` is PID 1. Chromium forks a tree of renderer/GPU/utility processes,
and with a shell as PID 1 none of them are reaped — the container accumulates
zombies until it hits the PID limit.

---

## API reference

### Playback (token-authenticated, same as every other channel)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/stream/virt-<name>.m3u8` | Media playlist. Starts the session on demand. |
| `GET` | `/api/virtual/<name>/seg_NNNNNN.ts` | One segment. Also the session's heartbeat. |

Segment names are matched against an allowlist (`seg_` + 6 digits + `.ts`), which
is what prevents path traversal out of the output directory.

### Status

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/virtual-sessions/status` | Running sessions plus a preflight check for missing binaries, and `host`: `{cpus, quota, load, throttled_pct}`. Each session carries `capture`, `capture_connected` and `cpu_browser` / `cpu_encoder` / `cpu_display` (percent of one core over the last sample). |
| `POST` | `/api/virtual-sessions/<name>/stop` | Stop one session. |

### Internal

| Method | Path | Purpose |
|---|---|---|
| `WS` | `/api/virtual-capture/<name>?key=…` | The tab-capture extension's recording, written straight into ffmpeg's stdin. Admitted only by the session's one-time secret, minted per session and never sent to a client — a valid user token is deliberately *not* enough to push video into a channel. Wrong key: closed before accept. |

### Remote control (**admin token required**)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/virtual-control/<name>/start` | Start a session without a viewer. |
| `GET` | `/api/virtual-control/<name>/stream.mjpeg` | Live preview as multipart JPEG. |
| `POST` | `/api/virtual-control/<name>/input` | One input event. |
| `POST` | `/api/virtual-control/<name>/navigate` | Transient navigation. |
| `GET` | `/api/virtual-control/<name>/panel` | The control panel HTML. |
| `POST` | `/api/virtual-control/<name>/crop` | Set or clear the streamed region. |

Input event shapes: `{"type":"click","x":100,"y":200,"button":"left","clicks":1}`,
`{"type":"move"|"down"|"up",…}`, `{"type":"wheel","x":…,"y":…,"dx":0,"dy":120}`,
`{"type":"key","key":"Enter"}`, `{"type":"text","text":"hello"}`,
`{"type":"back"|"forward"|"reload"}`.

Crop body: `{"x":40,"y":20,"w":800,"h":450}`, in screen pixels. All zeros clears
the crop. The reply reports the resulting stream size, whether a crop is in
effect, and whether the session was restarted:
`{"saved":true,"cropped":true,"crop":{…},"output":{"width":800,"height":450},"restarted":true}`.
An out-of-bounds or undersized region comes back as `400` with a readable
message, and nothing is written.

---

## Architecture and design decisions

**Headful Chromium under Xvfb, not `--headless=new`.** Headless Chrome has no
reliable audio output path in a container — a long-standing, still-open problem
([puppeteer-stream#189](https://github.com/samuelscheit/puppeteer-stream/issues/189))
— and the production writeups that do this at scale
([Mux](https://www.mux.com/blog/lessons-learned-building-headless-chrome-as-a-service))
all run a real browser against a virtual display.

**Tab capture (`chrome.tabCapture` + `MediaRecorder`), not `x11grab` and not
CDP `Page.startScreencast`.** The screencast API delivers base64 JPEG frames at
a variable rate and carries no audio, so it was never a candidate. `x11grab` was
the original design and is kept as an option; it lost the default because it
samples on a wall-clock timer and turns into bursts of duplicate frames the
moment the host is busy (numbers in [Capture paths](#capture-paths-tab-vs-x11grab)).
Tab capture is what the browser-to-video tools that work well in practice
(puppeteer-stream and its descendants) use: frames come from the compositor
with their own timestamps, audio from the same tab on the same clock, and
Chromium's MediaRecorder can emit H.264 directly — `MediaRecorder.isTypeSupported
('video/webm;codecs=h264,opus')` is true in Playwright's Chromium — so ffmpeg
remuxes with `-c:v copy` instead of encoding a second time. The extension is
MV3 (MV2 no longer loads in current Chromium): a service worker that the
backend drives through Playwright's `context.service_workers[0].evaluate()`, and
an offscreen document that holds the `MediaStream` and the recorder, because a
service worker has no DOM. `--allowlisted-extension-id=<id>` lifts tabCapture's
"the user must have invoked the extension" rule; the id is derived at runtime
from the SHA-256 of the manifest's `key`, so the flag cannot drift from the
extension it names. `videoKeyFrameIntervalDuration` asks the recorder for one
keyframe per segment, which is what keeps `EXT-X-INDEPENDENT-SEGMENTS` true
without a re-encode.

Rejected along the way: VP8/VP9 from MediaRecorder (Chromium's VP8 encode cost
~2 cores at 720p30 in measurement, versus ~0.3 for its H.264) and re-timing
frames to CFR in ffmpeg (needs a decode; the `setts` bitstream filter does the
snap on the copied packets instead).

**Playwright, not raw Chromium.** The browser and a matching Chromium build were
already a dependency of this repo, and real page control is needed anyway for
consent dialogs, play buttons and the remote-control panel.

**PulseAudio null-sink per session.** A null-sink automatically exposes a
`.monitor` source, which is what ffmpeg records on the x11grab path. One per
session, with Chromium pointed at it via `PULSE_SINK`, is what keeps two
concurrent channels from recording each other's audio. Tab capture takes the
audio from the tab itself and no longer records the sink, but the sink is still
created: Chromium needs an audio output to run a `<video>`'s clock against, and
a missing device is a stalled player. Sinks are unloaded on teardown — leaked
null-sinks accumulate and eventually exhaust module slots.

**Plain HLS, not LL-HLS.** ffmpeg's `hls` muxer **does not implement Apple
LL-HLS**: there is no `EXT-X-PART`, no partial segments, and no `hls_part_size`
option, despite what several 2025–2026 guides claim. (`-lhls` is a *dash* muxer
option implementing the abandoned 2019 `EXT-X-PREFETCH` draft, which Safari does
not support.) Passing those options makes ffmpeg exit with `Option not found`
and the channel never starts — there is a regression test for this. 2s segments
give ~6s latency, which is right for this use case. If sub-3s ever matters, the
upgrade path is to insert a real packager (e.g. OvenMediaEngine) downstream and
leave the capture stage unchanged.

**GOP == segment length.** `-g` is set to `framerate × segment_seconds` with
`-sc_threshold 0`, so every segment starts on an IDR frame and
`EXT-X-INDEPENDENT-SEGMENTS` is true rather than a lie that stalls players.

**No `hls_playlist_type`.** `event` forbids removing segments, which silently
defeats `delete_segments` and grows the output directory without bound.

**A/V sync.** Both inputs get `-thread_queue_size 1024` — with the tiny default,
a momentary `x11grab` stall drops audio packets and the stream desyncs
permanently. `aresample=async=1` stretches/pads audio onto the video timeline,
which is what keeps a multi-hour session from drifting. Output is forced to CFR
(`-fps_mode cfr`, or `-vsync cfr` on ffmpeg < 5.1) because `x11grab` drops frames
under load and libx264 with a fixed GOP is much happier with a constant rate.

---

## Performance and latency

**Where the CPU goes (tab capture, 720p30, measured on a desktop core with the
container's flags).** Chromium ~1.1 cores in total: about half in the renderer
(decode, raster, and the H.264 encode in the offscreen document) and half in
the GPU process (software compositing plus the capture copy). ffmpeg ~0.04 of
a core (remux + AAC). Xvfb ~0.04. On x11grab the split was Chromium ~0.4,
ffmpeg ~0.25 (libx264 + colour conversion), Xvfb ~0.05 — cheaper in total, but
every one of those parts sat on the timing-critical path. If a tab-capture
channel stutters, the cause is CPU starvation or quota throttling of the
browser (see the Host CPU line in Settings), not the codec.

Notable choices, all of which were measured or checked against source rather
than taken from guides:

- **No SwiftShader.** `--use-gl=swiftshader` was removed. SwiftShader is a
  WebGL/GLES emulator; for a page that is essentially a `<video>` element, Skia's
  CPU raster (`--disable-gpu --disable-software-rasterizer`) is both cheaper and
  what Chromium's own documentation points to.
- **`--disable-gpu-vsync`, but *not* `--disable-frame-rate-limit`.** The first
  frees the display scheduler from a synthetic 60Hz timer. The second uncaps
  frame production entirely, which on a CPU-bound host has been reported to
  starve video decode — it would compete for the cores the stream needs.
- **Thread caps on both sides** (`VIRTUAL_ENCODER_THREADS`,
  `VIRTUAL_RASTER_THREADS`), so x264 and Chromium's rasteriser do not fight.
- **`-tune zerolatency` is kept** — besides latency it is measurably *cheaper*
  than the untuned preset, because it disables B-frames and lookahead.
- **`aresample=async=1000`, not `async=1`.** `async=1` only fills and trims: it
  inserts silence or hard-cuts samples, audible as clicks over a long session.
  A larger value stretches and squeezes instead, which is the documented idiom
  for an independent capture clock.
- **`repeat-headers=1`** puts SPS/PPS in-band on every IDR, so each segment
  really is independently decodable for a client joining mid-stream.
- **`-muxdelay 0 -muxpreload 0`** removes the MPEG-TS muxer's default 0.7s
  preload.

### Latency

With 2s segments a player sits about 3 segments back from the live edge, so
expect **6-10s** end to end. That floor is set by HLS itself, not by this code.
FFmpeg's HLS muxer cannot do LL-HLS (see the note above), so breaking below it
means putting a real packager downstream — send `-f flv rtmp://...` to
MediaMTX or OvenMediaEngine and let it produce LL-HLS. The capture stage would
not change.

The web player's hls.js config previously set `liveSyncDuration: 2`, which
overrides `liveSyncDurationCount` and pinned playback to less than one segment
of headroom — the player permanently chased a fragment that had barely been
written, and stalled its way through playback. It now uses the documented floor
of 3 segments plus `maxLiveSyncPlaybackRate`, which corrects drift by speeding
up slightly rather than by a visible seek.

---

## Security model

The trust boundary is **admin**, and Settings is admin-gated.

- **URL scheme is restricted to `http`/`https`.** A browser would otherwise
  happily open `file:///etc/passwd` or `chrome://gpu` and restream it. This is
  the single check that keeps a virtual channel from becoming a filesystem
  viewer, and it applies to live navigation as well as stored records.
- **RFC1918 destinations are deliberately *not* blocked.** This is a self-hosted
  LAN app whose whole point may be restreaming something on the same network.
  Be aware this means an admin can make the server fetch hosts the client cannot
  reach directly.
- **CSS selectors reject quotes and backslashes**, because they are interpolated
  into injected page CSS.
- **Segment names are allowlisted**, not traversal-blocklisted.
- **Remote control requires an admin token specifically** — a valid stream token
  is not enough, and the trusted-subnet bypass does not apply. Being on the LAN
  lets you watch; it does not let you drive the server's browser.
- Chromium runs with `--no-sandbox` (as it already did for the vidembed
  extractor), so treat the container as the security boundary.

---

## Troubleshooting

**"Virtual channels need these to be installed: …"**
The image predates this feature. Rebuild it — the Dockerfile installs
`xvfb`, `ffmpeg`, `pulseaudio` and the rest. Check with
`GET /api/virtual-sessions/status`.

**The channel is a black screen.**
The page probably has not started playing. In order: add the play button to
**Click these**; add any consent dialog to **Hide these**; open **Control** and
see what the page actually looks like. Increase **Warm-up seconds** for a slow
page.

**The stream has no sound.**
Confirm **Capture audio** is on. Check the page is not muted in its own player —
use **Control** to look. If audio worked and then stopped, the session is
probably stalled; stop it and let it restart.

**Audio drifts out of sync over hours.**
Known characteristic of the PulseAudio timestamp path. `aresample=async=1`
bounds it. If it is still unacceptable, shorten `idle_timeout` so sessions
recycle more often, or move to PipeWire (`pipewire-pulse` keeps the same
protocol, so no code changes here).

**The first tune-in times out in the player.**
Cold start can take ~45s. Caddy's `@api_virtual` block already allows 60s. Some
players give up sooner — request the channel once in a browser to warm it, or
lower **Warm-up seconds**.

**The container is OOM-killed when several channels run.**
Raise `MEMORY_LIMIT`. Budget ~1 GB base plus ~900 MB per concurrent 720p30
session. Alternatively lower channels to 480p, or set `MAX_VIRTUAL_SESSIONS`.

**Chromium crashes with blank pages.**
`shm_size` is too small. It must be well above Docker's 64 MB default; the
shipped value is 1 GB.

**The control panel feels laggy.**
Lower `VIRTUAL_CONTROL_MAX_WIDTH` (e.g. 640) and/or `VIRTUAL_CONTROL_FPS`. The
preview runs a second capture of the display alongside the encoder, so it is not
free. If input specifically is slow while the picture is fine, the container is
CPU bound — see the next entry.

**A "Save password?" or other browser dialog appears on the stream.**
It is Chromium's own UI, drawn into the browser window, so it is captured like
anything else on screen. Open **Control** and dismiss it — the panel captures
the same display and injects input at the X level, so it can click browser UI,
not just page content. To stop it recurring, the container launches Chromium
with the password manager and other prompt-generating features disabled; if a
new dialog type appears, add its suppression flag in `_start_browser`.

**The stream is choppy / a slideshow.**
Open **Settings → Virtual Channels** and press **Sessions**. Three things to
read there, in order:

1. The **Host CPU** line. `throttled N% of periods` above a few percent means
   the container's CPU quota is pausing it (see
   [Running many channels at once](#running-many-channels-at-once)); set
   `CPUSET` or raise `CPU_LIMIT`. A load well above the quota means the host is
   simply full.
2. The session's **cpu browser / enc** split. A browser at several hundred
   percent with a low frame rate is a page that is too expensive to render at
   that resolution: drop to 480p/720p or lower the frame rate.
3. The session's capture badge. `tab capture: no feed` means the extension is
   not delivering; the session will be rebuilt by the janitor, and
   `/api/virtual-control/<name>/diagnostics` shows the recorder's own error
   list under `feed`.

If the channel is on **x11grab**, switch it to **tab** capture: x11grab cannot
be made smooth on a busy host, for the reason in
[Capture paths](#capture-paths-tab-vs-x11grab).

**ffmpeg logs "More than 1000 frames duplicated".** (x11grab only)
This is a *timestamp* message, not a picture-quality one: ffmpeg received frames
whose wall-clock times were further apart than 1/framerate, and padded the
constant-rate grid by repeating the last one. x11grab grabs unconditionally on a
timer, so this does **not** mean "the page didn't change" — it means the grab
loop was late, which on this container almost always means **Chromium was
starved of CPU and stopped painting**, or the container was being throttled by
its CPU quota. The fix is tab capture; the rest of this entry is for the case
where x11grab has to stay.

Things that do *not* fix it: a faster x264 preset, or a different muxer. The
encode costs only a fraction of a core at 720p30 — it is not the bottleneck.

Things that do:
- Give the container more CPU (`CPU_LIMIT`; budget ~2 cores per concurrent 720p
  channel).
- Lower the channel's **frame rate** to something the browser can actually
  sustain. Asking for 30 when it can paint 20 does not make the stream smoother;
  it just burns CPU duplicating frames.
- Lower the **resolution**. This cuts Chromium's raster cost roughly linearly,
  which matters far more than the encoder saving.

To confirm which it is, run the same capture against a static page: if the
duplicate count stays near zero there, the capture path is fine and the problem
is browser CPU contention.

**Sessions do not appear in Settings.**
Press **Sessions** to refresh — the panel is not polled, since each entry costs
a browser and an encoder and there are never many.

---

## See also

- [STREAMING_ARCHITECTURE.md](STREAMING_ARCHITECTURE.md) — how ordinary proxied channels work
- [SECURITY.md](SECURITY.md)
- [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
