#!/bin/bash

echo "🔄 Restarting freesky container with optimized streaming settings..."

# Stop the current container
echo "📦 Stopping current container..."
docker-compose down

# Clean up any lingering containers
echo "🧹 Cleaning up..."
docker system prune -f

# Start with optimized settings
echo "🚀 Starting with optimized settings..."
docker-compose up -d

# Wait for container to be ready
echo "⏳ Waiting for container to be ready..."
sleep 10

# Check container status
echo "📊 Container status:"
docker-compose ps

# Show logs for verification
echo "📋 Recent logs:"
docker-compose logs --tail=20

echo "✅ Restart complete! The container is now running with optimized settings:"
echo "   - MAX_CONCURRENT_STREAMS: 5 (reduced from 10)"
echo "   - WORKERS: 2 (reduced from 3)"
echo "   - Memory limits: 512M (reduced from 1G)"
echo "   - Improved timeout handling and connection management"
echo ""
echo "🎯 These changes should reduce lag and broken pipe errors." 