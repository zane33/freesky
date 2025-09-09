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

# Check if frontend exists and matches current configuration
FRONTEND_CONFIG_FILE="/srv/.config.json"
CURRENT_API_URL="${API_URL:-http://localhost:${PORT:-3001}}"
NEEDS_REBUILD=false

if [ ! -f "$FRONTEND_CONFIG_FILE" ]; then
    echo "Frontend configuration not found, will compile frontend..."
    NEEDS_REBUILD=true
else
    STORED_API_URL=$(cat "$FRONTEND_CONFIG_FILE" 2>/dev/null | grep -o '"api_url":"[^"]*"' | cut -d'"' -f4)
    if [ "$STORED_API_URL" != "$CURRENT_API_URL" ]; then
        echo "API URL changed from $STORED_API_URL to $CURRENT_API_URL, will recompile frontend..."
        NEEDS_REBUILD=true
    fi
fi

# Compile frontend if needed
if [ "$NEEDS_REBUILD" = true ]; then
    echo "Compiling frontend with API_URL=$CURRENT_API_URL..."
    cd /app
    
    # Set environment for compilation
    export REFLEX_ENV=prod
    export REFLEX_SKIP_COMPILE=0
    
    # Try to compile frontend
    if timeout 120 reflex export --no-zip --frontend-only 2>&1 | tee /tmp/reflex_export.log; then
        # Move compiled frontend to serving directory
        if [ -d ".web/_static" ]; then
            echo "Moving compiled frontend to /srv..."
            rm -rf /srv/*
            cp -r .web/_static/* /srv/
            # Save configuration for future comparison
            echo "{\"api_url\":\"$CURRENT_API_URL\"}" > "$FRONTEND_CONFIG_FILE"
            echo "Frontend compiled and deployed successfully"
        else
            echo "Warning: Frontend compilation completed but no static files found"
        fi
    else
        echo "Warning: Frontend compilation failed or timed out"
        echo "Check /tmp/reflex_export.log for details"
        # Create a fallback HTML page
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
else
    echo "Frontend already compiled with correct configuration"
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