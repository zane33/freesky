# Deployment Guide for Remote Servers

This guide covers deploying freesky to remote servers, including Portainer, and configuring it for network access.

## Problem: Connection Refused When Accessing from Remote Machines

When deploying to a remote server (like Portainer), you might encounter errors like:
```
localhost:3000/api/stream/588.m3u8: Failed to load resource: net::ERR_CONNECTION_REFUSED
```

This happens because the application defaults to `localhost` URLs, which work locally but fail when accessed from other machines on the network.

## Solution: Configure API_URL for Remote Access

### 1. For Portainer Deployment

Create a `.env` file or set environment variables in Portainer:

```bash
# Replace 192.168.1.100 with your server's actual IP address
API_URL=http://192.168.1.100:3000
BACKEND_URI=http://localhost:8005
PORT=3000
BACKEND_PORT=8005
DADDYLIVE_URI=https://thedaddy.click
PROXY_CONTENT=TRUE
WORKERS=3
```

### 2. For Docker Compose (Manual Deployment)

```bash
# Option 1: Using environment variables
API_URL=http://192.168.1.100:3000 docker-compose up -d

# Option 2: Using .env file
echo "API_URL=http://192.168.1.100:3000" > .env
echo "BACKEND_URI=http://localhost:8005" >> .env
docker-compose up -d
```

### 3. For Domain-based Deployment

If you have a domain name:
```bash
API_URL=https://your-domain.com
BACKEND_URI=http://localhost:8005
# Add SSL/TLS configuration if needed
```

## Key Configuration Parameters

### API_URL
- **Purpose**: The public URL that clients use to access the service
- **Local**: `http://localhost:3000`
- **Remote**: `http://YOUR_SERVER_IP:3000`
- **Domain**: `https://your-domain.com`

### BACKEND_URI
- **Purpose**: Internal communication between containers
- **Value**: Almost always `http://localhost:8005`
- **Note**: This should remain as localhost since it's container-internal communication

### Port Configuration
- **PORT**: Frontend port (default: 3000)
- **BACKEND_PORT**: Backend service port (default: 8005)
- **Note**: Both ports must be exposed in your container configuration

## Network Security Considerations

The application is configured with permissive CORS settings for streaming compatibility:
- `Access-Control-Allow-Origin: "*"` for streaming endpoints
- WebSocket connections allowed from any origin
- This is necessary for video streaming functionality

## Troubleshooting

### 1. Connection Refused Error
**Symptoms**: `ERR_CONNECTION_REFUSED` when accessing streams
**Solution**: Ensure API_URL is set to your server's IP, not localhost

### 2. Port Not Accessible
**Symptoms**: Cannot reach the application at all
**Solution**: 
- Check that ports 3000 and 8005 are exposed in your container configuration
- Verify firewall settings on your server
- Test with: `curl http://YOUR_SERVER_IP:3000/health`

### 3. Streaming Issues
**Symptoms**: Videos won't play or HLS streams fail
**Solution**:
- Set `PROXY_CONTENT=TRUE` to proxy all content through the backend
- Check that your server can reach external content sources
- Verify no proxy/firewall is blocking streaming

### 4. WebSocket Connection Issues
**Symptoms**: "Socket is reconnected" messages in console
**Solution**:
- Ensure WebSocket connections are allowed through your proxy/firewall
- Check that both HTTP and WebSocket traffic can reach the backend
- Verify the API_URL is correctly configured

## Example Portainer Stack Configuration

```yaml
version: '3.8'
services:
  freesky:
    image: your-registry/freesky:latest
    ports:
      - "3000:3000"
      - "8005:8005"
    environment:
      - API_URL=http://192.168.1.100:3000  # Replace with your server IP
      - BACKEND_URI=http://localhost:8005
      - PORT=3000
      - BACKEND_PORT=8005
      - DADDYLIVE_URI=https://thedaddy.click
      - PROXY_CONTENT=TRUE
      - WORKERS=3
      - REFLEX_ENV=prod
      - REFLEX_SKIP_COMPILE=1
    restart: unless-stopped
    networks:
      - freesky-network

networks:
  freesky-network:
    driver: bridge
```

## Testing Your Deployment

1. **Health Check**: `curl http://YOUR_SERVER_IP:3000/health`
2. **Backend Check**: `curl http://YOUR_SERVER_IP:8005/health`
3. **Stream Test**: `curl -I http://YOUR_SERVER_IP:3000/api/stream/1.m3u8`
4. **WebSocket Test**: Check browser console for WebSocket connections

## Security Notes

- The application uses permissive CORS settings required for streaming functionality
- Consider placing behind a reverse proxy (nginx, Cloudflare) for additional security
- Use HTTPS in production environments
- Regularly update the container image for security patches

## Performance Optimization

- Set `WORKERS=3` or higher for better concurrent performance
- Use `PROXY_CONTENT=TRUE` for better stream reliability
- Monitor resource usage and adjust worker count accordingly
- Consider using a CDN for static assets in high-traffic scenarios 