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
if ! curl -s --connect-timeout 10 "${DADDYLIVE_URI:-https://thedaddy.click}" >/dev/null; then
    echo "Warning: Cannot connect to content provider at ${DADDYLIVE_URI:-https://thedaddy.click}"
fi

FRONTEND_CONFIG_FILE="/srv/.config.json"

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
    WS_URL=$(echo "$CURRENT_API_URL" | sed 's|^http://|ws://|')/_event
    echo "Injecting WebSocket URL: $WS_URL"
    find /srv -name "*.js" -type f -exec sed -i "s|ws://[^/]*:[0-9]*/_event|$WS_URL|g" {} \; 2>/dev/null || true
    echo "{\"api_url\":\"$CURRENT_API_URL\"}" > "$FRONTEND_CONFIG_FILE"
    echo "Frontend ready with WebSocket URL: $WS_URL"
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
exec caddy run --config /app/Caddyfile --adapter caddyfile 