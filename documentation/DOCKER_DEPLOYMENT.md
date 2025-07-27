# Docker Deployment Configuration

## API URL Configuration Issue

When deploying the application via Docker, the copy link functionality on the watch page may not work because the generated URLs use internal container IPs that are not accessible from outside the container.

## Problem

The application generates stream URLs like:
- **Local**: `http://localhost:3000/api/stream/588.m3u8` ✅ (works)
- **Docker (broken)**: `http://0.0.0.0:3000/api/stream/588.m3u8` ❌ (not accessible)

## Solution

### Option 1: Set DOCKER_HOST_IP Environment Variable

Set the `DOCKER_HOST_IP` environment variable to your server's public IP or hostname:

```bash
# In your .env file or docker-compose command
DOCKER_HOST_IP=your-server-ip.com
# or
DOCKER_HOST_IP=192.168.1.100
```

### Option 2: Set API_URL Directly

Override the API_URL completely:

```bash
# In your .env file or docker-compose command
API_URL=http://your-server-ip.com:3000
```

### Option 3: Use Docker Host Networking

If using Docker host networking, you can use the host's IP:

```yaml
# In docker-compose.yml
services:
  freesky:
    network_mode: "host"
    environment:
      - DOCKER_HOST_IP=localhost
```

## Testing the Configuration

Run the test script inside the Docker container to verify the configuration:

```bash
# Build and run the container
docker-compose up -d

# Execute the test script
docker-compose exec freesky python test_api_url.py
```

## Example .env File

```env
# Application ports
PORT=3000
BACKEND_PORT=8005

# Docker deployment
DOCKER_HOST_IP=your-server-ip.com
# or
API_URL=http://your-server-ip.com:3000

# Other settings
DADDYLIVE_URI=https://dlhd.click
PROXY_CONTENT=TRUE
```

## Verification

After deploying with the correct configuration:

1. Navigate to a channel page (e.g., `/watch/588`)
2. Click the copy link button
3. The copied URL should be accessible from outside the container
4. Test the URL in VLC or another media player

## Troubleshooting

### URLs still showing 0.0.0.0

1. Check that `DOCKER_HOST_IP` or `API_URL` is set correctly
2. Restart the Docker container after changing environment variables
3. Verify the configuration with the test script

### URLs showing localhost in production

1. Make sure `DOCKER_HOST_IP` is set to your server's public IP
2. Check that the port is accessible from outside the container
3. Verify firewall settings allow access to the port

### Copy button not working at all

1. Check browser console for JavaScript errors
2. Verify the frontend is loading correctly
3. Check that the API endpoints are accessible 