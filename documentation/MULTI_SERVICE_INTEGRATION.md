# Multi-Service Streaming Integration

This document explains how to integrate multiple upstream streaming services into your application, inspired by the Kodi addons from [https://github.com/LoopAddon/repository.the-loop](https://github.com/LoopAddon/repository.the-loop).

## Overview

The multi-service streaming architecture allows your application to:
- Connect to multiple streaming services simultaneously
- Automatically fallback between services when one fails
- Search for content across all services
- Enable/disable services dynamically
- Handle different authentication methods and APIs

## Architecture

### Core Components

1. **`MultiServiceStreamer`** - Main coordinator class
2. **`BaseStreamer`** - Abstract base class for all services
3. **Service Implementations** - Concrete classes for each streaming service
4. **Configuration System** - Centralized service configuration

### Service Types

Based on the Kodi repository, we support:

- **DLHD** - DaddyLive HD (your existing service)
- **StreamsPro** - Live streaming service
- **TheLoop** - General entertainment streaming
- **Plexus** - P2P streaming service
- **IPTV Providers** - Generic IPTV services
- **Sports Streams** - Sports-focused services

## Adding a New Streaming Service

### Step 1: Create Service Class

Create a new class that inherits from `BaseStreamer`:

```python
class MyNewServiceStreamer(BaseStreamer):
    """My New Streaming Service"""
    
    def __init__(self):
        super().__init__("MyNewService")
        self.base_url = "https://myservice.com"
    
    async def get_stream_url(self, channel_id: str) -> Optional[str]:
        """Get stream URL for a channel"""
        try:
            # Implement your stream extraction logic here
            url = f"{self.base_url}/stream/{channel_id}"
            async with self.session.get(url, headers=self._headers()) as response:
                if response.status_code == 200:
                    # Extract stream URL from response
                    content = response.text
                    # Look for stream patterns
                    stream_patterns = [
                        r'https://[^"\']*\.m3u8[^"\']*',
                        r'https://[^"\']*\.mp4[^"\']*',
                    ]
                    for pattern in stream_patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            return matches[0]
            return None
        except Exception as e:
            logger.error(f"MyNewService stream error: {str(e)}")
            return None
    
    async def get_channels(self) -> List[Dict[str, Any]]:
        """Get list of available channels"""
        try:
            url = f"{self.base_url}/channels"
            async with self.session.get(url, headers=self._headers()) as response:
                if response.status_code == 200:
                    data = response.json()
                    return [
                        {
                            "id": channel["id"],
                            "name": channel["name"],
                            "logo": channel.get("logo", ""),
                            "tags": channel.get("tags", []),
                            "service": "MyNewService"
                        }
                        for channel in data.get("channels", [])
                    ]
            return []
        except Exception as e:
            logger.error(f"MyNewService channels error: {str(e)}")
            return []
```

### Step 2: Add to MultiServiceStreamer

Update `freesky/multi_service_streamer.py`:

```python
class MultiServiceStreamer:
    def __init__(self):
        self.services = {
            "DLHD": DLHDStreamer(),
            "StreamsPro": StreamsProStreamer(),
            "TheLoop": TheLoopStreamer(),
            "Plexus": PlexusStreamer(),
            "MyNewService": MyNewServiceStreamer(),  # Add your service
        }
        self.enabled_services = ["DLHD", "MyNewService"]  # Enable it
```

### Step 3: Add Configuration

Update `freesky/streaming_services_config.py`:

```python
STREAMING_SERVICES = {
    # ... existing services ...
    
    "MyNewService": {
        "name": "My New Service",
        "base_url": "https://myservice.com",
        "enabled": True,
        "priority": 7,
        "description": "My custom streaming service",
        "features": ["live_tv", "movies"],
        "api_endpoints": {
            "channels": "/api/channels",
            "stream": "/api/stream/{channel_id}"
        },
        "auth_required": False
    }
}
```

## API Endpoints

### Service Management

- `GET /api/services/status` - Get status of all services
- `POST /api/services/{service_name}/enable` - Enable a service
- `POST /api/services/{service_name}/disable` - Disable a service

### Channel Management

- `GET /api/channels/all` - Get all channels from all services
- `GET /api/channels/search?query=news` - Search channels across services

### Streaming

- `GET /api/stream/{channel_id}.m3u8` - Get stream (tries all enabled services)

## Usage Examples

### Python Usage

```python
from freesky.multi_service_streamer import multi_streamer

# Enable a service
multi_streamer.enable_service("StreamsPro")

# Get stream from specific service
stream_url = await multi_streamer.get_stream("123", "StreamsPro")

# Get stream (tries all enabled services)
stream_url = await multi_streamer.get_stream("123")

# Search for channels
channels = await multi_streamer.search_channels("news")

# Get all channels
all_channels = await multi_streamer.get_all_channels()
```

### API Usage

```bash
# Enable StreamsPro service
curl -X POST http://localhost:8005/api/services/StreamsPro/enable

# Get service status
curl http://localhost:8005/api/services/status

# Search for news channels
curl "http://localhost:8005/api/channels/search?query=news"

# Get stream (will try all enabled services)
curl http://localhost:8005/api/stream/123.m3u8
```

## Authentication

Some services require authentication. Handle this in your service class:

```python
class AuthenticatedServiceStreamer(BaseStreamer):
    def __init__(self, api_key: str = None):
        super().__init__("AuthenticatedService")
        self.api_key = api_key or os.environ.get("AUTHENTICATED_SERVICE_API_KEY")
    
    def _headers(self, referer: str = None) -> Dict[str, str]:
        headers = super()._headers(referer)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
```

## Error Handling

The multi-service system includes robust error handling:

- **Service Failures**: If one service fails, it automatically tries the next
- **Timeout Protection**: Each service has configurable timeouts
- **Logging**: Comprehensive logging for debugging
- **Fallback**: Automatic fallback to working services

## Testing

Use the test script to verify your integration:

```bash
python test_multi_service.py
```

This will test:
- Service status endpoints
- Channel retrieval
- Stream generation
- Service management

## Best Practices

1. **Graceful Degradation**: Always handle service failures gracefully
2. **Rate Limiting**: Respect service rate limits
3. **Caching**: Cache channel lists and stream URLs when possible
4. **Monitoring**: Log service performance and failures
5. **Configuration**: Use environment variables for sensitive data

## Troubleshooting

### Common Issues

1. **Service Not Found**: Check if service is enabled in configuration
2. **Authentication Errors**: Verify API keys and credentials
3. **Timeout Errors**: Increase timeout values for slow services
4. **Rate Limiting**: Implement delays between requests

### Debug Mode

Enable debug logging:

```python
import logging
logging.getLogger("freesky.multi_service_streamer").setLevel(logging.DEBUG)
```

## Future Enhancements

- **Service Health Monitoring**: Track service uptime and performance
- **Load Balancing**: Distribute requests across multiple services
- **Content Categorization**: Better organization of content by type
- **User Preferences**: Allow users to prioritize certain services
- **Service Discovery**: Automatically discover and test new services

## References

- [Kodi Addon Repository](https://github.com/LoopAddon/repository.the-loop)
- [Streaming Architecture Documentation](./STREAMING_ARCHITECTURE.md)
- [Backend API Documentation](./BACKEND_API.md) 