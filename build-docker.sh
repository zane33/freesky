#!/bin/bash

# Optimized Docker build script for freesky
set -e

echo "🐳 Building optimized freesky Docker container..."

# Build arguments with defaults
PORT=${PORT:-3000}
BACKEND_PORT=${BACKEND_PORT:-8005}
API_URL=${API_URL:-"http://0.0.0.0:${PORT}"}
DADDYLIVE_URI=${DADDYLIVE_URI:-"https://thedaddy.click"}
PROXY_CONTENT=${PROXY_CONTENT:-TRUE}
SOCKS5=${SOCKS5:-""}

# Build the optimized container
echo "📦 Building with arguments:"
echo "  PORT: $PORT"
echo "  BACKEND_PORT: $BACKEND_PORT"
echo "  API_URL: $API_URL"
echo "  DADDYLIVE_URI: $DADDYLIVE_URI"
echo "  PROXY_CONTENT: $PROXY_CONTENT"
echo "  SOCKS5: $SOCKS5"

# Build with BuildKit for better performance
DOCKER_BUILDKIT=1 docker build \
    --file Dockerfile.optimized \
    --tag freesky:optimized \
    --build-arg PORT=$PORT \
    --build-arg BACKEND_PORT=$BACKEND_PORT \
    --build-arg API_URL=$API_URL \
    --build-arg DADDYLIVE_URI=$DADDYLIVE_URI \
    --build-arg PROXY_CONTENT=$PROXY_CONTENT \
    --build-arg SOCKS5=$SOCKS5 \
    --progress=plain \
    .

# Show image size
echo "📊 Build completed! Image size:"
docker images freesky:optimized --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"

# Show layer information
echo "🔍 Layer information:"
docker history freesky:optimized --format "table {{.CreatedBy}}\t{{.Size}}"

echo "✅ Optimized container build completed successfully!"
echo "🚀 To run the container:"
echo "   docker run -p $PORT:$PORT -p $BACKEND_PORT:$BACKEND_PORT freesky:optimized" 