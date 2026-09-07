# Virtual Channels — restreaming a web page as live HLS

A **virtual channel** turns any web page into a live TV channel. FreeSky opens
the page in a real browser on a private virtual display inside the container,
records the screen *and* the browser's audio, encodes it to H.264/AAC, and
serves it as a rolling HLS playlist. To a player it is indistinguishable from
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
5. [Configuration reference](#configuration-reference)
6. [API reference](#api-reference)
7. [Architecture and design decisions](#architecture-and-design-decisions)
8. [Security model](#security-model)
9. [Troubleshooting](#troubleshooting)

---

## How it works

```
                    ┌──────────────── one session, per channel ─────────────────┐
                    │                                                            │
 GET /api/stream/   │   Xvfb :99          Chromium (headful, --kiosk)            │
 virt-<name>.m3u8 ──┼─► 1280x720x24  ◄──── renders the page into the display     │
                    │        │                      │                            │
                    │        │ x11grab              │ PulseAudio null-sink       │
                    │        ▼                      ▼   (fsk_<name>)             │
                    │      ffmpeg ◄──────── fsk_<name>.monitor                   │
                    │        │  libx264 zerolatency + AAC                        │
                    │        ▼                                                   │
                    │   /streams/<name>/index.m3u8 + seg_%06d.ts   (tmpfs)       │
                    └────────────────────────────┬───────────────────────────────┘
                                                 │
 player ◄── rewritten playlist ◄─────────────────┘
        ◄── GET /api/virtual/<name>/seg_000123.ts?token=…
```

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
| **Frame rate** | 15, 24, 25 or 30. |
| **Encoder preset** | `ultrafast`, `superfast` or `veryfast`. Slower presets cannot keep up with realtime capture, so they are not offered. |
| **Video kbps** | Target and max bitrate. 2500 is right for 720p30; 4500–6000 for 1080p30. |
| **Capture audio** | Off drops the audio input entirely — worth it for a silent dashboard, since a silent sink still costs an encoder and can desync a long session. |
| **Audio kbps** | 128 is fine for most things. |
| **Warm-up seconds** | How long to let the page settle (fonts, player bootstrap, consent dialogs) before the encoder starts, so viewers don't join on a half-painted page. |
| **Idle timeout (s)** | Seconds with no request before the session is torn down. |
| **Hide these** | CSS selectors, one per line, hidden with `display: none` after load. Cookie banners, overlays, headers. |
| **Click these** | CSS selectors, one per line, clicked after load to start playback. A selector that matches nothing is normal and is not an error. |
| **Logo / Tags** | As for any other channel. |

Changes take effect on the next session — saving a channel stops any running
session for it so the next tune-in picks up the new settings.

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

The practical ceiling is the container's memory limit, not a code constant — see
`MEMORY_LIMIT` below. If the container is OOM-killed, **every** channel goes
down, not just the newest, so size it deliberately.

Set `MAX_VIRTUAL_SESSIONS` to a positive number only if you want a hard cap; in
that mode the least recently watched session is evicted to make room.

---

## Configuration reference

All optional. Defaults are in `docker-compose.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `VIRTUAL_CHANNELS_FILE` | `/app/data/virtual_channels.json` | Where channel records are stored. On the `./data` volume so they survive a rebuild. |
| `VIRTUAL_HLS_ROOT` | `/streams` | Where HLS output is written. Should be a tmpfs. |
| `MAX_VIRTUAL_SESSIONS` | `0` | `0` = unlimited. A positive number caps concurrency with LRU eviction. |
| `VIRTUAL_SEGMENT_SECONDS` | `2` | Segment length, and therefore the GOP length. |
| `VIRTUAL_PLAYLIST_SIZE` | `6` | Segments kept in the playlist window. |
| `VIRTUAL_START_TIMEOUT` | `45` | Seconds to wait for the first segments before giving up. |
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
| `GET` | `/api/virtual-sessions/status` | Running sessions plus a preflight check for missing binaries. |
| `POST` | `/api/virtual-sessions/<name>/stop` | Stop one session. |

### Remote control (**admin token required**)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/virtual-control/<name>/start` | Start a session without a viewer. |
| `GET` | `/api/virtual-control/<name>/stream.mjpeg` | Live preview as multipart JPEG. |
| `POST` | `/api/virtual-control/<name>/input` | One input event. |
| `POST` | `/api/virtual-control/<name>/navigate` | Transient navigation. |
| `GET` | `/api/virtual-control/<name>/panel` | The control panel HTML. |

Input event shapes: `{"type":"click","x":100,"y":200,"button":"left","clicks":1}`,
`{"type":"move"|"down"|"up",…}`, `{"type":"wheel","x":…,"y":…,"dx":0,"dy":120}`,
`{"type":"key","key":"Enter"}`, `{"type":"text","text":"hello"}`,
`{"type":"back"|"forward"|"reload"}`.

---

## Architecture and design decisions

**Headful Chromium under Xvfb, not `--headless=new`.** Headless Chrome has no
reliable audio output path in a container — a long-standing, still-open problem
([puppeteer-stream#189](https://github.com/samuelscheit/puppeteer-stream/issues/189))
— and the production writeups that do this at scale
([Mux](https://www.mux.com/blog/lessons-learned-building-headless-chrome-as-a-service))
all run a real browser against a virtual display.

**`x11grab`, not CDP `Page.startScreencast`.** The screencast API delivers
base64 JPEG frames at a variable rate and carries no audio at all, so frames
would have to be re-timed by hand. It also couples stream liveness to Chromium:
with `x11grab`, Chromium can crash and be relaunched into the same display while
ffmpeg keeps running and viewers see only a few black frames.

**Playwright, not raw Chromium.** The browser and a matching Chromium build were
already a dependency of this repo, and real page control is needed anyway for
consent dialogs, play buttons and the remote-control panel.

**PulseAudio null-sink per session.** A null-sink automatically exposes a
`.monitor` source, which is what ffmpeg records. One per session, with Chromium
pointed at it via `PULSE_SINK`, is what keeps two concurrent channels from
recording each other's audio. Sinks are unloaded on teardown — leaked null-sinks
accumulate and eventually exhaust module slots.

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

**ffmpeg logs "More than 1000 frames duplicated".**
The page is repainting more slowly than the channel's configured frame rate, so
the encoder duplicates frames to hold a constant rate. It is informational, not
an error, and is normal for a mostly-static page. If it coincides with a choppy
stream the container is CPU bound: drop the channel to 720p or 480p, set the
frame rate to match the source (25 for most broadcast content), or use a faster
x264 preset. Rendering is software (`--use-gl=swiftshader`) as there is no GPU
in the container, and that is the dominant cost for a video-heavy page.

**Sessions do not appear in Settings.**
Press **Sessions** to refresh — the panel is not polled, since each entry costs
a browser and an encoder and there are never many.

---

## See also

- [STREAMING_ARCHITECTURE.md](STREAMING_ARCHITECTURE.md) — how ordinary proxied channels work
- [SECURITY.md](SECURITY.md)
- [DOCKER_DEPLOYMENT.md](DOCKER_DEPLOYMENT.md)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
