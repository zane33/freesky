# Troubleshooting Guide

## Common Issues and Solutions

### 1. Connection Refused Error (Port 8005)

**Error**: `dial tcp 127.0.0.1:8005: connect: connection refused`

**Cause**: The backend is not running on port 8005 as expected by Caddy.

**Solutions**:
- Check if the container is running: `docker ps`
- Check container logs: `docker logs <container_name>`
- Verify the backend started properly by looking for "Backend initialized" in logs
- Test the backend directly: `curl http://localhost:8005/ping`

### 2. Curl Error in Channel Loading

**Error**: `curl_cffi.requests.exceptions.HTTPError: Failed to perform, curl: (16)`

**Cause**: Network connectivity issues or external API being unavailable.

**Solutions**:
- Check internet connectivity from within the container
- Verify the external API is accessible: `curl https://thedaddy.click/24-7-channels.php`
- Check if SOCKS5 proxy is configured correctly (if using)
- The app will now use fallback channels if the external API fails

### 3. Backend Not Starting

**Symptoms**: No backend logs, connection refused errors

**Solutions**:
- Check if all required files are present
- Verify Python dependencies are installed
- Check if the startup script has execute permissions
- Look for any Python import errors in logs

### 4. Poor Performance with Multiple Connections

**Symptoms**: Slow response times, timeouts, connection errors under load

**Causes**:
- Single-threaded backend
- No connection pooling
- No caching
- Resource limitations

**Solutions**:
- Increase `WORKERS` environment variable (default: 6, recommended: 4-8)
- Monitor system resources (CPU, memory)
- Check cache hit rates using `/health` endpoint
- Use the performance monitoring script: `python monitor_performance.py http://localhost:${PORT:-3000}`

### 5. Stream drops with `Error proxying content for session ...` (empty message)

**Symptom**: playback stops; the log shows a 500 on `/api/content/...` and an
error line with no cause after it, e.g.

```
ERROR - Error proxying content for session content_...:
... status=500 duration=3.5
```

**Cause**: a transient upstream CDN failure — usually a connect timeout (the
streaming client has a 3s connect budget) or a 5xx — on a playlist or segment
fetch. httpx timeout exceptions carry an empty message, which is why the log
line looked blank. The proxy returned 500, and every player reads a 500 on a
playlist as "channel is dead" rather than retrying.

**Fix (already applied)**: `/api/content` now retries transient upstream
failures up to 3 times with a short backoff (`_UPSTREAM_ATTEMPTS` in
`freesky/backend.py`). Segment streams are retried only before the first byte
reaches the client, since restarting mid-body would corrupt the segment. Hard
failures (403/404) are still surfaced immediately, and errors are logged with
their exception type so empty-message failures are identifiable.

**If it persists**: repeated `ConnectTimeout` after 3 attempts means the CDN
edge is genuinely unreachable from the host — check DNS/IPv6 and any SOCKS5
proxy configuration.

## Performance Optimizations

### Environment Variables for Performance

To check current environment variables in a running container:
```bash
docker exec <container_name> env | grep -E "(PORT|API_URL|DADDYLIVE_URI|PROXY_CONTENT|SOCKS5|WORKERS|BACKEND_PORT)"
```

### Core Variables

- `PORT`: The frontend port (default: 3000)
- `BACKEND_PORT`: The backend service port (default: 8005)
- `BACKEND_URI`: The backend URI without port i.e. localhost or ip address
- `API_URL`: The public URL for accessing the service
- `DADDYLIVE_URI`: The daddylive service endpoint
- `PROXY_CONTENT`: Whether to proxy video content
- `SOCKS5`: Optional SOCKS5 proxy configuration
- `WORKERS`: Number of backend worker processes

### Example Usage

```bash
PORT=3000 BACKEND_PORT=8005 API_URL=http://192.168.1.100:3000 docker-compose up
```

### WebSocket Connections

The application uses WebSocket for real-time updates. The connection flow is:
1. Frontend connects to `ws://{API_URL}/_event` (API_URL is your frontend interface, e.g. http://192.168.4.5:3000)
2. Caddy proxies the WebSocket connection from frontend port (3000) to backend port (8005)
3. Backend handles the WebSocket connection on port 8005

Note: The API_URL should point to your frontend interface (port 3000) where clients connect. Caddy handles proxying these connections to the backend service (port 8005). Reflex automatically converts http:// to ws:// for WebSocket connections.

If you're having WebSocket connection issues:
1. Check that both ports (3000 and 8005) are exposed
2. Verify Caddy is properly proxying WebSocket connections
3. Check browser console for connection errors
4. Ensure `API_URL` is correctly set to your server's address 

# Check environment variables in the container
To see what environment variables are set in the running container:

```bash
docker exec freesky-step-daddy-live-hd-1 env | findstr "PORT API_URL BACKEND_URI DADDYLIVE_URI PROXY_CONTENT SOCKS5 WORKERS BACKEND_PORT WEBSOCKET_URL REFLEX_ENV REFLEX_FRONTEND_ONLY REFLEX_SKIP_COMPILE REDIS_URL PYTHONUNBUFFERED"
```

Example output:
```
API_URL=http://localhost:8005
BACKEND_URI=http://localhost:8005
DADDYLIVE_URI=https://thedaddy.click
SOCKS5=
PROXY_CONTENT=TRUE
PORT=3000
WORKERS=1
BACKEND_PORT=8005
WEBSOCKET_URL=ws://localhost:8005
REFLEX_ENV=prod
REFLEX_FRONTEND_ONLY=true
REFLEX_SKIP_COMPILE=1
REDIS_URL=redis://localhost
PYTHONUNBUFFERED=1
``` 
## HTTPS: playlist URL stopped working after enabling TLS

### Symptom
`http://<host>:3000/playlist.m3u8?token=...` returns `400 Client sent an HTTP
request to an HTTPS server`, and the `https://` form is rejected by Dispatcharr,
ffmpeg, VLC or curl (`curl: (60) SSL certificate problem`).

### Cause
Two independent faults, both introduced by the first HTTPS implementation:

1. **HTTPS replaced HTTP on the same port.** One TCP port cannot serve both
   protocols. Every existing `http://` playlist URL — including the one already
   configured in Dispatcharr — began answering 400.
2. **The self-signed certificate did not cover the host's IP.** Its SAN was
   `DNS:localhost, DNS:*, IP:127.0.0.1, IP:0.0.0.0`. A TLS client connecting by
   IP address matches **only `iPAddress` SAN entries** — a DNS wildcard never
   covers an IP, and `0.0.0.0` is not the host's address. Verified: with the cert
   explicitly trusted as a CA, `curl` still failed with exit 60; the same request
   against a cert carrying `IP:192.168.3.148` returned 200.

### Fix
`ENABLE_HTTPS=true` now **adds** a TLS listener rather than replacing HTTP:

| Setting | Result |
|---|---|
| `ENABLE_HTTPS=false` (default) | HTTP on `PORT` only |
| `ENABLE_HTTPS=true` | HTTP on `PORT`, **HTTPS on `HTTPS_PORT`** (default 3443) |
| `ENABLE_HTTPS=true`, `HTTPS_PORT=$PORT` | HTTPS only — the old behaviour |

The certificate's SAN is built from `DOCKER_HOST_IP` plus every entry in
`TLS_HOSTS` (comma separated), classified into `IP:` or `DNS:` automatically.
The SAN list is recorded in `data/certs/.san`; if it changes, the self-signed
cert is regenerated on next start. A cert you supplied yourself (any subject
other than `CN=freesky`) is never touched.

### Which URL to give each client
- **Dispatcharr, ffmpeg, VLC, any IPTV player** → use `http://`. They validate
  certificates and will reject a self-signed one no matter what its SAN says.
  Fixing that needs a certificate from a real CA, not a SAN change.
- **Browsers** → `https://` works after clicking through the untrusted-issuer
  warning once.

### Caddyfile structure
The server config lives in an `(app)` snippet imported by each site block. A
`tls` directive cannot appear in a block whose address is plain `http://` —
Caddy rejects it with *"server listening on [:3000] is HTTP, but attempts to
configure TLS connection policies"* — so the HTTP and HTTPS listeners must be
separate blocks. `start.sh` appends the HTTPS block to a copy of the Caddyfile
at `/tmp/Caddyfile` (a copy, so a `docker restart` cannot stack duplicates).

---

## Virtual Channels (web page → HLS)

Full detail in [VIRTUAL_CHANNELS.md](VIRTUAL_CHANNELS.md#troubleshooting). The
short version:

### "Virtual channels need these to be installed: Xvfb, ffmpeg, …"

**Cause**: the running image predates the virtual-channels feature.

**Solution**: rebuild the image. The Dockerfile installs `xvfb`, `x11-utils`,
`pulseaudio`, `ffmpeg`, `dbus`, `dumb-init` and the font packages. Confirm with:

```bash
curl http://localhost:8005/api/virtual-sessions/status
```

`missing_binaries` should be `[]`.

### Virtual channel plays as a black screen

**Cause**: the page loaded but never started playing — almost always autoplay,
a consent dialog, or a login.

**Solutions**:
- Add the play button's CSS selector to **Click these** in the channel's settings.
- Add any cookie/consent overlay to **Hide these**.
- Open **Control** on the channel to see what the browser is actually showing,
  and click through it by hand.
- Raise **Warm-up seconds** if the page is slow to paint.

### Virtual channel has no audio

**Solutions**:
- Confirm **Capture audio** is enabled on the channel.
- Use **Control** to check the page's own player is not muted.
- Audio that worked and then stopped usually means a stalled session — stop it
  in Settings and let the next request rebuild it.

### Virtual channel is choppy, stutters, or looks like a slideshow

**Symptom.** The channel plays, audio is continuous, but the picture updates a
few times a second or moves in bursts. On the x11grab capture path, ffmpeg's
`-progress` counters (visible in Settings → Sessions) show `dup` and `drop`
both climbing into the thousands while `fps` reads a healthy 30 and `speed`
1.0x. A real case: 5,741 frames out in three minutes, 5,087 of them duplicates
and 1,990 dropped — about two distinct pictures a second — while the page
itself was presenting 44fps.

**Cause.** x11grab samples the screen on ffmpeg's wall-clock timer. When the
container is short of CPU, or is being paused by its CFS quota, the timer slips:
several grabs then land within milliseconds of each other (dropped, timestamps
collide) after a long gap (filled with duplicates of the last frame). The
browser was painting fine; the *sampling* was starved. This cannot be tuned
away with presets, thread caps or bitrates.

**Fix.**

1. Use **tab capture** (the default for new channels since this was found;
   existing channels pick it up on their next session, or set **Capture** in
   the channel form). Frames then come from Chromium's compositor with their
   own timestamps and Chromium encodes the H.264; ffmpeg only remuxes, and
   its timestamps are snapped to the channel's frame grid. Measured: 33ms every
   frame at 30fps, 40ms at 25fps, zero duplicates or drops.
2. Press **Sessions** and read the **Host CPU** line. `throttled N% of periods`
   above a few percent means the container's `cpus:` quota is pausing it; set
   `CPUSET=0-3` (pin cores) in `.env` instead, or raise `CPU_LIMIT`. See
   [VIRTUAL_CHANNELS.md](VIRTUAL_CHANNELS.md#running-many-channels-at-once).
3. Read the session's `cpu browser … enc …` figures. A browser at several
   hundred percent is a page too heavy for the host at that resolution: lower
   the resolution before the frame rate.
4. Pick a frame rate that divides 60 (20, 30, 60) for tab capture; the
   compositor runs at 60Hz and those give even intervals without relying on the
   snap.
5. At **1080p**, do not expect more than 30fps from a CPU-only host: the
   software compositor is single-threaded and saturates one core there. See
   [VIRTUAL_CHANNELS.md](VIRTUAL_CHANNELS.md#1080p-and-high-frame-rates) for
   the GPU option.

`/api/virtual-control/<name>/diagnostics` (admin token) reports all of the
above in one JSON document: encoder counters, the capture feed's state and the
recorder's own error list, per-process CPU, host load and throttling, and what
the page's `<video>` is presenting.

### Container is OOM-killed once several virtual channels run

**Cause**: concurrent sessions are uncapped by default and each 720p30 session
costs roughly 900 MB. The container memory limit is the real ceiling, and when
it is hit **every** channel dies, not just the newest.

**Solutions**:
- Raise `MEMORY_LIMIT` (budget ~1 GB base + ~900 MB per concurrent session).
- Drop channels to 480p, or set `MAX_VIRTUAL_SESSIONS` to a hard cap.

### Chromium crashes with blank pages / "page crash"

**Cause**: `/dev/shm` too small (Docker's default is 64 MB).

**Solution**: `shm_size: "1gb"` in `docker-compose.yml` — already the shipped
value; check it was not overridden.

### First tune-in times out in the player

**Cause**: a cold start launches a browser and an encoder and waits for the
first segments, which can take ~45s.

**Solutions**: request the channel once in a browser to warm it, lower
**Warm-up seconds**, or raise the player's own timeout. Caddy's `@api_virtual`
block already allows 180s.

### Control panel returns 502 about 2.2 seconds after Start

**Symptom**: `POST /api/virtual-control/<name>/start` returns 502 with an empty
body after a very consistent ~2.2s, while `/health`, the panel HTML and
`/api/virtual-sessions/status` all answer in milliseconds. The same `/start`
route answers instantly (503/401) for an unknown channel or a bad token, so
routing is clearly fine. Hitting the backend port directly, bypassing the
reverse proxy, fails identically -- so it is not the proxy.

Often paired with: "settings reload the page every time they are saved".

**Cause**: the backend was running in DEVELOPMENT mode, which enables granian's
file watcher. `reflex run` defaults to `--env dev`
(`reflex/reflex.py`: `env: constants.Env = constants.Env.DEV`), and neither
`REFLEX_ENV=prod` nor `env=rx.Env.PROD` in `rxconfig.py` overrides that CLI
default. Dev mode starts granian with `reload=True`, `reload_tick=100` and
`reload_paths=[Path.cwd()]` -- and `start.sh` runs from `/app`, so the entire
app tree is watched, **including the `./data` volume**.

Starting a virtual channel makes Chromium write its profile into
`/app/data/virtual-profiles`. The watcher fires, granian restarts the worker,
and `workers_kill_timeout=2` drops the in-flight request about 2.2 seconds in.
Saving settings does the same, because `users.json`, `app_settings.json` and
`virtual_channels.json` all live under `/app/data`.

Note `HOTRELOAD_IGNORE_EXTENSIONS` covers `json` and extension-less files, so
the `.json` stores and files like `Preferences` are ignored -- but a Chromium
profile also contains `.ldb` files, which are not.

**Fix**: `start.sh` now runs `reflex run --env prod`. Production mode has no
file watcher at all. As a backstop it also exports
`REFLEX_HOT_RELOAD_OVERRIDE_PATHS=/app/freesky`, so even in dev the watcher
points at source rather than the data volume.

**Verified empirically**: writing a single `.ldb` file into
`data/virtual-profiles/` took a dev-mode backend down for 2.2s
(`200 -> 000 -> 200`); the identical write under `--env prod` produced no
interruption at all.

### Control panel returns 502 and the backend looks like it never starts

**Symptom**: `POST /api/virtual-control/<name>/start` returns HTTP 502 with an
empty body after ~2s, while `/health`, `/api/virtual-sessions/status` and the
panel HTML itself all return 200. Polling `/health` every 100ms during the
request shows it flipping 200 -> 502 -> 200, sometimes twice, and occasionally
hanging instead of refusing.

**Cause**: more than one backend worker process. `reflex run` does not read
`WORKERS`; it reads `GRANIAN_WORKERS`, and
`reflex.utils.processes.get_num_workers()` returns `(os.cpu_count() * 2) + 1`
as soon as it can ping Redis -- which `start.sh` starts. An 8-core host
therefore ran 17 backends instead of 1.

That is merely wasteful for a stateless proxy, but fatal for virtual channels.
Each worker imports the app separately, so each has its **own**
`virtual_session.manager`, its own display counter starting at `:99`, and its
own copy of the autostart lifespan task. Every worker raced to start the same
channel: unlinking each other's `/tmp/.X99-lock`, launching competing Xvfb
servers on one display, opening the single persistent Chrome profile N times,
and pointing several encoders at one playlist. Workers died; granian's shared
listener then answered some connections with a reset, which Caddy reported as
502 while other requests were still served normally by surviving workers.

**Fix**: run exactly one worker. `start.sh` now does
`export GRANIAN_WORKERS="$WORKERS"` with `WORKERS` defaulting to 1, and
`VirtualSession` takes an exclusive `flock` per channel under
`VIRTUAL_LOCK_ROOT` so a second process fails fast with a clear message
instead of corrupting the profile.

**Do not raise `WORKERS`** while virtual channels are in use. A single async
worker is I/O-bound and ample for this load. It is also required for
`/api/content` URLs, which are encrypted with a per-process key.

## Dispatcharr / ffmpeg: stream drops with `ValueError: Upstream returned HTTP 403` traceback

The CDN's segment URLs carry a path-embedded expiry. When one 403s, the proxy
used to raise *inside* the response body after the `200` headers were already
sent, so the client saw a dropped connection and a traceback filled the log.
`/api/content/...` now opens the upstream before answering: a hard upstream
status (403/404) is relayed as that status, transient ones (5xx/timeouts) are
retried, and all cached `/api/stream` playlists are dropped so the player's
next playlist request re-resolves a fresh feed.

## Channels missing after importing the playlist into Dispatcharr

Every `#EXTINF` now carries `tvg-id="<channel id>"` and `tvg-name`. Without them
importers de-duplicate same-named feeds (e.g. two "SEE Denmark", the event
"Backup Stream" feeds) into a single stream. Re-import the M3U after upgrading.

## Channels re-enable themselves in Settings

Toggling used to write the browser tab's snapshot of the disabled list over the
file. A second tab or a socket reconnect racing `on_load` could push a stale
list and re-enable channels turned off elsewhere. Toggles now read-modify-write
the prefs file, so the file is the only source of truth.

## New upstream channels appear enabled in Settings

Refresh replaces the channel list with a fresh scrape (channels gone upstream
drop off, new ones appear). Ids never scraped before are now recorded in
`channel_seen.json` (next to `channel_prefs.json`) and added to the disabled
set on first sight, so additions start OFF. The first run after upgrading only
seeds the seen-file; nothing already listed is touched.

## Schedule page: dates / filters

Times are parsed as Europe/London wall-clock (upstream says "UK GMT" but means
local UK time, so summer listings were an hour off). The filter card has
From/To date inputs, All/None tag buttons and a shown/total count. Upstream
only publishes the current day's schedule, so the range is usually one day.

## Enabling a channel from a schedule event

Settings → **Schedule** → *Load schedule* lists every upstream event with its
channels, disabled ones included (the public /schedule page hides those). Grey
chip = off, click to enable; green = already on, click to disable; *Enable all*
switches on every listed channel for that event. "(not in list)" means upstream
cites an id that is not in the channel list, so it cannot be played or enabled.
