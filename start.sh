#!/bin/bash

echo "Starting freesky services..."

# Check internet connectivity
echo "Checking internet connectivity..."
if ! curl -s --connect-timeout 5 https://8.8.8.8 >/dev/null; then
    echo "Warning: Cannot reach internet (IP connectivity test failed)"
fi

if ! curl -s --connect-timeout 5 https://www.google.com >/dev/null; then
    echo "Warning: Cannot resolve DNS (DNS resolution test failed)"
fi

# Test connection to DADDYLIVE_URI
echo "Testing connection to content provider..."
if ! curl -s --connect-timeout 10 "${DADDYLIVE_URI:-https://dlhd.st}" >/dev/null; then
    echo "Warning: Cannot connect to content provider at ${DADDYLIVE_URI:-https://dlhd.st}"
fi

FRONTEND_CONFIG_FILE="/srv/.config.json"

# --- TLS -------------------------------------------------------------------
# ENABLE_HTTPS=true ADDS an HTTPS listener on HTTPS_PORT alongside the plain HTTP
# one on PORT. It deliberately does not replace it: one TCP port cannot speak both
# protocols, so serving TLS on PORT made every existing http:// URL answer
# "400 Client sent an HTTP request to an HTTPS server" — which is how Dispatcharr
# and every other player lost the playlist. Set HTTPS_PORT=$PORT to get the old
# HTTPS-only behaviour on a single port.
#
# Drop your own cert.pem/key.pem into the certs dir to use a real certificate;
# otherwise a self-signed one is generated. A cert FILE (rather than Caddy's `tls
# internal`) is used deliberately: internal issuance needs a hostname up front,
# and this app is reached by LAN IP, NAT'd port and hostname alike.
CERT_DIR="${CERT_DIR:-/app/data/certs}"
HTTPS_PORT="${HTTPS_PORT:-3443}"
# Work on a fresh copy: appending the TLS block to /app/Caddyfile in place would
# stack a duplicate block on every `docker restart` of the same container.
CADDYFILE=/tmp/Caddyfile
cp /app/Caddyfile "$CADDYFILE"
if [ "$(echo "${ENABLE_HTTPS:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
    mkdir -p "$CERT_DIR"

    # A TLS client connecting by IP matches ONLY iPAddress SANs — a DNS wildcard
    # does not cover 192.168.3.148, and neither does IP:0.0.0.0. The old cert had
    # exactly that, so every verifying client (ffmpeg, VLC, Dispatcharr, curl)
    # rejected it. Build the SAN list from the addresses this box is actually
    # reached on: DOCKER_HOST_IP plus anything in TLS_HOSTS (comma separated).
    SAN="DNS:localhost,DNS:*,IP:127.0.0.1"
    for h in ${DOCKER_HOST_IP:-} ${TLS_HOSTS//,/ }; do
        [ -n "$h" ] || continue
        if [[ "$h" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; then SAN="$SAN,IP:$h"; else SAN="$SAN,DNS:$h"; fi
    done

    # Regenerate when the SAN set changes, otherwise a stale cert on the data
    # volume silently outlives the config that was supposed to fix it.
    if [ -f "$CERT_DIR/cert.pem" ] && [ -f "$CERT_DIR/key.pem" ] \
       && [ "$(cat "$CERT_DIR/.san" 2>/dev/null)" != "$SAN" ] \
       && openssl x509 -in "$CERT_DIR/cert.pem" -noout -subject 2>/dev/null | grep -q "CN *= *${TLS_CN:-freesky}"; then
        echo "Self-signed certificate does not match current SAN list - regenerating"
        rm -f "$CERT_DIR/cert.pem" "$CERT_DIR/key.pem"
    fi

    if [ ! -f "$CERT_DIR/cert.pem" ] || [ ! -f "$CERT_DIR/key.pem" ]; then
        echo "Generating self-signed certificate for $SAN"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
            -subj "/CN=${TLS_CN:-freesky}" -addext "subjectAltName=$SAN" \
            >/dev/null 2>&1 \
            && { echo "$SAN" > "$CERT_DIR/.san"; echo "Self-signed certificate created"; } \
            || echo "WARNING: certificate generation failed; HTTPS may not start"
    else
        echo "Using certificate from $CERT_DIR"
    fi

    if [ "$HTTPS_PORT" = "$PORT" ]; then
        export SITE_ADDRESS="https://:${PORT}"
        export CADDY_TLS="tls $CERT_DIR/cert.pem $CERT_DIR/key.pem"
        echo "HTTPS on port ${PORT} (HTTP disabled - HTTPS_PORT equals PORT)"
    else
        # Second listener as its own block: `tls` in a block whose address is
        # plain http:// is a hard config error, so they cannot share one.
        export SITE_ADDRESS="http://:${PORT}"
        export CADDY_TLS=""
        cat >> "$CADDYFILE" <<EOF

https://:${HTTPS_PORT} {
	tls $CERT_DIR/cert.pem $CERT_DIR/key.pem
	import app
}
EOF
        echo "HTTP on port ${PORT}, HTTPS on port ${HTTPS_PORT}"
    fi
else
    export SITE_ADDRESS="${SITE_ADDRESS:-:${PORT}}"
    export CADDY_TLS="${CADDY_TLS:-}"
fi

# Use the exact API_URL from environment, this is critical for container networking
CURRENT_API_URL="${API_URL}"

echo "Environment variables:"
echo "  API_URL=${API_URL}"
echo "  DOCKER_HOST_IP=${DOCKER_HOST_IP}"
echo "  PORT=${PORT}"
echo "  BACKEND_PORT=${BACKEND_PORT}"

# ponytail: no runtime rebuild. The Dockerfile already builds the frontend into
# /srv and then deletes .web (node_modules), so `reflex export` here can only
# fail — and it used to wipe /srv first, leaving Caddy with nothing to serve.
# Runtime API_URL is handled by the sed injection below.
if [ -f /srv/index.html ]; then
    # Make the client's URLs ORIGIN-RELATIVE instead of pinning them to API_URL.
    # reflex-env-*.js holds them as backtick template literals, so swapping the
    # scheme+host for a ${location...} expression stays valid JS and lets the app
    # be reached on any address — LAN IP, NAT'd port, hostname, http or https —
    # without a rebuild or a matching API_URL. There is no wildcard form of
    # API_URL; this is the equivalent.
    for f in /srv/assets/reflex-env-*.js; do
        [ -e "$f" ] || continue
        sed -i -E \
            -e 's#`ws://[^/`]+#`${location.origin.replace(/^http/,"ws")}#g' \
            -e 's#`wss://[^/`]+#`${location.origin.replace(/^http/,"ws")}#g' \
            -e 's#`http://[^/`]+#`${location.origin}#g' \
            -e 's#`https://[^/`]+#`${location.origin}#g' \
            "$f"
        echo "Rewrote $f to use the browser's own origin"
    done
    echo "{\"api_url\":\"$CURRENT_API_URL\"}" > "$FRONTEND_CONFIG_FILE"
    echo "Frontend will connect back to whatever host it was loaded from"
else
    echo "ERROR: /srv/index.html missing - frontend build failed at image build time"
    cat > /srv/index.html << EOF
<!DOCTYPE html>
<html>
<head>
    <title>FreeSky - Backend Running</title>
    <style>
        body { font-family: Arial, sans-serif; padding: 20px; }
        .info { background: #f0f0f0; padding: 10px; margin: 10px 0; }
    </style>
</head>
<body>
    <h1>FreeSky Backend is Running</h1>
    <p>Frontend compilation failed, but the backend API is available.</p>
    <div class="info">
        <strong>API Endpoints:</strong><br>
        - Channels: <a href="/api/channels">/api/channels</a><br>
        - Playlist: <a href="/playlist.m3u8">/playlist.m3u8</a><br>
        - Health: <a href="/health">/health</a>
    </div>
    <p>Check container logs for compilation errors.</p>
</body>
</html>
EOF
fi

# Set environment variables to prevent recompilation at runtime
export REFLEX_ENV=prod
export REFLEX_SKIP_COMPILE=1

# Start Redis in the background
echo "Starting Redis..."
redis-server --daemonize yes

# Wait for Redis to start
echo "Waiting for Redis..."
until redis-cli ping &>/dev/null; do
    sleep 1
done
echo "Redis started successfully"

# Number of granian worker PROCESSES.
#
# This MUST be exported as GRANIAN_WORKERS. Setting only WORKERS did nothing:
# `reflex run` never reads it, and reflex.utils.processes.get_num_workers()
# returns `(os.cpu_count() or 1) * 2 + 1` as soon as it can ping Redis -- which
# it always can, because this script starts Redis a few lines above. On an
# 8-core host that silently produced 17 backend processes instead of the one
# configured here.
#
# For virtual channels that was fatal, not merely wasteful. Each worker imports
# the app afresh, so each gets its OWN virtual_session.manager, its own display
# counter starting at :99, and its own copy of the autostart lifespan task. Every
# worker therefore tried to start the SAME channel: unlinking each other's
# /tmp/.X99-lock, launching competing Xvfb servers on :99, and opening the one
# persistent Chrome profile N times over. Workers died, granian's shared listener
# then answered some connections with RST, and Caddy turned those into the
# intermittent 502s that made the backend look like it would not start.
#
# One worker is also still the right answer for the original reason recorded in
# docker-compose.yml: utils.py mints an in-memory encryption key per process, so
# a content URL issued by one worker cannot be decrypted by another.
WORKERS=${WORKERS:-1}

# Clamped, not merely defaulted. The default was already 1 in docker-compose.yml
# and this file, and a stray `WORKERS=4` left in .env still beat both of them --
# compose interpolates ${WORKERS:-1} from .env, and `:-` only applies when the
# variable is UNSET. That one line silently restored the multi-process failure.
#
# More than one worker is not a supported tuning knob here, it is a broken
# configuration: /api/content URLs are encrypted with a per-process key, and each
# worker gets its own virtual-channel session manager, display counter and
# autostart task. Refuse it loudly rather than starting something that half works.
if [ "$WORKERS" != "1" ]; then
    echo "[$(date -Is)] WARNING: WORKERS=$WORKERS is not a supported configuration."
    echo "  The backend is async and I/O-bound; extra worker PROCESSES break"
    echo "  /api/content URLs (per-process encryption key) and virtual channels"
    echo "  (per-process session manager, display counter and autostart task)."
    if [ "${ALLOW_MULTIPLE_WORKERS:-false}" = "true" ]; then
        echo "  ALLOW_MULTIPLE_WORKERS=true is set -- honouring $WORKERS anyway."
    else
        echo "  Clamping to 1. Set ALLOW_MULTIPLE_WORKERS=true to override."
        WORKERS=1
    fi
fi
export GRANIAN_WORKERS="$WORKERS"

# Belt and braces for the hot-reload trap above. If anyone ever drops --env prod
# from the run line, this keeps the file watcher pointed at SOURCE only, instead
# of Path.cwd() (= /app), which sweeps in the ./data volume that Chromium
# profiles, session files and settings are all written to. Ignored entirely in
# production mode, where there is no watcher.
export REFLEX_HOT_RELOAD_OVERRIDE_PATHS="${REFLEX_HOT_RELOAD_OVERRIDE_PATHS:-/app/freesky}"
# Get backend port from environment or use default
BACKEND_PORT=${BACKEND_PORT:-8005}
echo "Starting Reflex backend with $WORKERS granian worker(s) on port $BACKEND_PORT..."

# Start the Reflex backend (which includes the FastAPI backend via api_transformer)
#
# Supervised, not fire-and-forget. Caddy is exec'd below and keeps running
# whatever happens to the backend, so a backend that dies later leaves the whole
# site answering 502 until someone restarts the container by hand. The most
# likely way for that to happen here is the kernel OOM-killer picking off the
# Python process when a virtual channel's Chromium spikes -- a container-level
# event the app itself cannot catch or report.
#
# Restarting keeps the exit code and a timestamp in the log so the cause is
# still visible afterwards.
backend_supervisor() {
    local attempt=0
    while true; do
        attempt=$((attempt + 1))
        echo "[$(date -Is)] Starting Reflex backend (attempt ${attempt})..."
        # --env prod is LOAD-BEARING, not tidiness.
        #
        # `reflex run` defaults to DEV (reflex/reflex.py: `env: constants.Env =
        # constants.Env.DEV`), and the CLI default wins over `env=rx.Env.PROD`
        # in rxconfig.py. Exporting REFLEX_ENV=prod above does not change it
        # either. Dev mode starts granian with reload=True, reload_tick=100 and
        # reload_paths=[Path.cwd()] -- and cwd is /app, so the ENTIRE app tree is
        # watched, including the ./data volume.
        #
        # That made virtual channels impossible: Chromium writes its profile
        # into /app/data/virtual-profiles, the watcher fired, granian restarted
        # the worker, and workers_kill_timeout=2 meant the in-flight request was
        # dropped about 2.2 seconds in -- which is exactly the "HTTP 502 after
        # 2.2s" the control panel reported on every single start. Saving any
        # setting did the same thing, because users.json, app_settings.json and
        # virtual_channels.json all live under /app/data too; that is the "it
        # reloads the page every time settings change" symptom.
        #
        # Production mode has no file watcher at all.
        cd /app && reflex run --env prod --backend-only --backend-host 0.0.0.0 --backend-port "$BACKEND_PORT"
        local code=$?
        echo "[$(date -Is)] Backend exited with code ${code}."
        if [ "$code" -eq 137 ] || [ "$code" -eq 139 ]; then
            echo "  Exit ${code} means the process was killed (137 = SIGKILL, usually the"
            echo "  OOM-killer). Check the container memory limit and how many virtual"
            echo "  channels are running -- a 1080p session costs roughly 1.6GB."
        elif [ "$code" -eq 0 ]; then
            # Do not read this as a clean shutdown. Granian is configured with
            # respawn_failed_workers=False (its default; reflex never overrides
            # it), so when ANY worker dies -- including by SIGKILL -- the master
            # tears down the remaining workers and exits 0. The kill is therefore
            # invisible in this exit code; granian logs the real cause just above
            # as "[ERROR] Unexpected exit from worker-N".
            echo "  Exit 0 here is not necessarily a clean stop: granian also exits 0"
            echo "  after a worker dies unexpectedly. Look for \"Unexpected exit from"
            echo "  worker-N\" above this line before assuming a normal shutdown."
        fi
        echo "[$(date -Is)] Restarting backend in 3s..."
        sleep 3
    done
}
backend_supervisor &

# Wait for backend to be ready
echo "Waiting for backend..."
until curl -s http://localhost:$BACKEND_PORT/health &>/dev/null; do
    sleep 1
done
echo "Backend started successfully"

# Start Caddy in the foreground with explicit configuration
echo "Starting Caddy..."
exec caddy run --config "$CADDYFILE" --adapter caddyfile