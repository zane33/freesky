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

# Get number of workers from environment or use default
WORKERS=${WORKERS:-6}
# Get backend port from environment or use default
BACKEND_PORT=${BACKEND_PORT:-8005}
echo "Starting Reflex backend with $WORKERS workers on port $BACKEND_PORT..."

# Start the Reflex backend (which includes the FastAPI backend via api_transformer)
cd /app && reflex run --backend-only --backend-host 0.0.0.0 --backend-port $BACKEND_PORT &

# Wait for backend to be ready
echo "Waiting for backend..."
until curl -s http://localhost:$BACKEND_PORT/health &>/dev/null; do
    sleep 1
done
echo "Backend started successfully"

# Start Caddy in the foreground with explicit configuration
echo "Starting Caddy..."
exec caddy run --config "$CADDYFILE" --adapter caddyfile