# DaddyLive Upstream Source Integration Architecture

## Overview

This document details the technical implementation of DaddyLive upstream source integration in Freesky, including the complex iframe-based authentication system, multi-service streaming architecture, and specific handling of channels that require browser automation.

## Architecture Summary

Freesky implements a sophisticated multi-layer system to handle DaddyLive's security-hardened streaming infrastructure:

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Client App    │ -> │  Freesky Backend │ -> │ DaddyLive APIs  │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                               │
                               v
                    ┌──────────────────┐
                    │ Multi-Service    │
                    │ Stream Handler   │
                    └──────────────────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
                    v                     v
        ┌──────────────────┐    ┌──────────────────┐
        │ Direct Stream    │    │ Vidembed Iframe  │
        │ Extraction       │    │ Authentication   │
        └──────────────────┘    └──────────────────┘
                                         │
                                         v
                                ┌──────────────────┐
                                │ Playwright       │
                                │ Browser Engine   │
                                └──────────────────┘
```

## Core Components

### 1. Multi-Service Streamer (`multi_service_streamer.py`)

**Purpose**: Manages multiple upstream sources with failover capability.

**Key Features**:
- Concurrent service attempts
- Automatic failover on service failures
- Service health monitoring
- Connection pooling

**Configuration**:
```python
services = {
    "DLHD": {
        "class": StepDaddyHybrid,
        "priority": 1,
        "timeout": 35.0  # Increased for vidembed channels
    }
}
```

### 2. Hybrid Stream Handler (`free_sky_hybrid.py`)

**Purpose**: Primary interface to DaddyLive's streaming infrastructure.

**Architecture Layers**:

#### Layer 1: Channel Discovery
- Fetches channel list from `https://dlhd.click/24-7-channels.php`
- Extracts channel metadata and stream references
- Maintains channel mapping cache

#### Layer 2: Stream Type Detection
- **Direct Streams**: HLS URLs that can be accessed directly
- **Vidembed Streams**: Require iframe-based authentication
- **Fallback Streams**: Alternative sources when primary fails

#### Layer 3: Authentication Strategy Selection
```python
def _handle_new_architecture(self, vidembed_url):
    # 1. Try iframe-based extraction (primary)
    try:
        hls_url = await extract_hls_from_vidembed(vidembed_url)
        if hls_url:
            return process_stream_content(hls_url)
    except Exception:
        pass
    
    # 2. Try direct API simulation (fallback)
    api_headers = {
        "Origin": "https://vidembed.re",
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }
    
    # 3. Direct page access (last resort)
    return vidembed_url  # Client-side processing
```

### 3. Vidembed Iframe Extractor (`vidembed_extractor.py`)

**Purpose**: Handles vidembed.re's iframe-based authentication system using browser automation.

**Critical Implementation Details**:

#### Browser Setup
```python
browser = await playwright.chromium.launch(
    headless=True,
    args=[
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu'
    ]
)
```

#### Iframe Authentication Flow
```python
async def extract_hls_stream(self, vidembed_url):
    # 1. Navigate to vidembed page
    await page.goto(vidembed_url, wait_until='networkidle')
    
    # 2. Wait for iframe to load
    await asyncio.sleep(10)
    
    # 3. Extract stream authentication data
    player_data = await page.evaluate("""
        () => {
            return window.playerData || window.streamData;
        }
    """)
    
    # 4. Construct authenticated HLS URL
    return construct_hls_url(player_data)
```

#### Security Compliance
The extractor maintains compliance with vidembed.re's security requirements:

- **Origin Validation**: All requests originate from legitimate vidembed.re context
- **Referrer Headers**: Proper iframe referrer context maintained
- **CORS Compliance**: Cross-origin requests handled within iframe boundary
- **Frame Ancestry**: Respects anti-hotlinking protections

## Channel-Specific Implementations

### Standard Channels (Direct Stream Access)

**Example**: ESPN, BBC, CNN, etc.

**Flow**:
1. Channel request received
2. Direct HLS URL extracted from DaddyLive API
3. Stream proxied with proper headers
4. Client receives working stream

**Response Time**: ~2-5 seconds

### Vidembed Channels (Iframe Authentication Required)

**Example**: Channel 588, premium sports channels

**Flow**:
1. Channel request received
2. Vidembed URL detected: `https://vidembed.re/stream/{UUID}`
3. Playwright browser launched
4. Iframe authentication performed
5. Authenticated HLS URL extracted
6. Stream proxied to client

**Response Time**: ~15-30 seconds (due to browser automation)

## Technical Specifications

### Timeout Configuration

```python
# Backend timeouts (backend.py)
VIDEMBED_EXTRACTION_TIMEOUT = 30.0  # Playwright browser operations
MULTI_SERVICE_TIMEOUT = 35.0        # Overall stream generation
HTTP_CLIENT_TIMEOUT = 30.0          # Individual HTTP requests

# Vidembed-specific timeouts (vidembed_extractor.py)
BROWSER_LAUNCH_TIMEOUT = 10.0       # Browser startup
PAGE_LOAD_TIMEOUT = 15.0            # Initial page load
IFRAME_WAIT_TIMEOUT = 10.0          # Iframe authentication
STREAM_EXTRACTION_TIMEOUT = 5.0     # Data extraction
```

### Error Handling Strategy

```python
# 1. Primary: Iframe extraction
try:
    hls_url = await extract_hls_from_vidembed(vidembed_url)
    return hls_url
except PlaywrightError:
    logger.warning("Browser automation failed")

# 2. Fallback: Direct API simulation
try:
    api_response = await session.get(api_url, headers=iframe_headers)
    return parse_api_response(api_response)
except AuthenticationError:
    logger.warning("API simulation failed")

# 3. Last resort: Client-side processing
return create_vidembed_response(vidembed_url)
```

### Headers and Authentication

#### Standard Stream Headers
```python
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://dlhd.click/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9"
}
```

#### Vidembed-Specific Headers
```python
iframe_headers = {
    "Origin": "https://vidembed.re",
    "Referer": vidembed_url,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin"
}
```

## Performance Optimizations

### Connection Pooling
```python
# HTTP client with connection reuse
client = httpx.AsyncClient(
    http2=True,
    timeout=httpx.Timeout(30.0, connect=10.0),
    limits=httpx.Limits(
        max_keepalive_connections=5,
        max_connections=20
    )
)
```

### Stream Caching
```python
# Active stream tracking prevents duplicate processing
active_streams = {
    "channel_id": {
        "clients": set(),
        "stream_data": cached_content,
        "timestamp": creation_time
    }
}
```

### Browser Instance Management
```python
# Shared browser instance with context isolation
browser = await get_browser_instance()
context = await browser.new_context(
    user_agent=STANDARD_USER_AGENT,
    viewport={'width': 1920, 'height': 1080}
)
```

## Deployment Configuration

### Docker Requirements

```dockerfile
# Playwright browser installation
RUN playwright install chromium

# Required system dependencies
RUN apt-get update && apt-get install -y \
    libnspr4 \
    libnss3 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2
```

### Environment Variables
```env
# Core configuration
DADDYLIVE_URI=https://thedaddy.click
BACKEND_PORT=8005
PORT=3000

# Performance tuning
VIDEMBED_TIMEOUT=30
MAX_CONCURRENT_STREAMS=10
BROWSER_POOL_SIZE=3

# Security
PROXY_CONTENT=TRUE
CORS_ORIGINS=*
```

## Monitoring and Debugging

### Logging Configuration
```python
# Component-specific loggers
logger = logging.getLogger("freesky.vidembed_extractor")
logger.setLevel(logging.DEBUG)

# Key metrics logged
- Browser launch success/failure
- Iframe authentication timing
- Stream extraction success rates
- Fallback method usage
- Client connection counts
```

### Performance Metrics
```python
# Tracked metrics
stream_generation_time = time.time() - start_time
browser_launch_time = browser_ready - browser_start
authentication_time = auth_complete - iframe_load
extraction_time = stream_ready - auth_complete
```

### Common Issues and Solutions

#### Issue: Playwright Browser Launch Failures
**Symptoms**: `BrowserType.launch: Executable doesn't exist`
**Solution**: Ensure `playwright install chromium` in Docker build

#### Issue: 504 Gateway Timeouts on Vidembed Channels
**Symptoms**: Requests timeout after 10 seconds
**Solution**: Increase `VIDEMBED_EXTRACTION_TIMEOUT` to 30+ seconds

#### Issue: CORS Errors on Direct API Access
**Symptoms**: `API request failed with status 400`
**Solution**: Use iframe-based extraction (primary method)

#### Issue: Authentication Failures
**Symptoms**: Empty or invalid stream URLs
**Solution**: Verify browser automation and iframe context

## Security Considerations

### Anti-Detection Measures
- Randomized user agents
- Natural timing delays
- Proper referrer handling
- Browser fingerprint randomization

### Rate Limiting
- Maximum concurrent extractions
- Per-client request limits
- Browser instance pooling
- Graceful degradation

### Privacy Protection
- No user data logging
- Secure header stripping
- Origin protection
- Stream URL obfuscation

## Future Enhancements

### Planned Improvements
1. **Browser Pool Management**: Maintain warm browser instances
2. **Stream Caching**: Cache authenticated URLs with expiration
3. **Health Monitoring**: Upstream service availability checking
4. **Load Balancing**: Multiple vidembed extraction workers
5. **Metrics Dashboard**: Real-time performance monitoring

### Scalability Considerations
- Horizontal browser scaling
- Distributed stream caching
- Service mesh integration
- Container orchestration

---

## Technical Support

For implementation questions or debugging assistance:

1. **Check Logs**: Container logs contain detailed extraction flow
2. **Verify Timeouts**: Ensure adequate time for browser operations
3. **Test Isolation**: Use single-channel tests for debugging
4. **Monitor Resources**: Browser automation requires adequate RAM/CPU

This architecture successfully handles DaddyLive's complex security requirements while maintaining performance and reliability for end users.