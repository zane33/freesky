# freesky 🚀

A self-hosted IPTV proxy built with [Reflex](https://reflex.dev), enabling you to watch over 1,000 📺 TV channels and search for live events or sports matches ⚽🏀. Stream directly in your browser 🌐 or through any media player client 🎶. You can also download the entire playlist (`playlist.m3u8`) and integrate it with platforms like Jellyfin 🍇 or other IPTV media players.

---

## ✨ Features

- **📱 Stream Anywhere**: Watch TV channels on any device via the web or media players.
- **🔎 Event Search**: Quickly find the right channel for live events or sports.
- **📄 Playlist Integration**: Download the `playlist.m3u8` and use it with Jellyfin or any IPTV client.
- **⚙️ Customizable Hosting**: Host the application locally or deploy it via Docker with various configuration options.

---

## 📦 Dependencies

freesky relies on several key Python packages, each serving a specific purpose in the application:

### Core Dependencies

- **`reflex==0.8.0`**
  - Primary web framework for building the full-stack application
  - Handles both frontend and backend, compiling Python to React
  - Provides real-time state management and WebSocket communication
  - Used for: UI components, state management, routing, and real-time updates

- **`curl-cffi==0.11.4`**
  - High-performance HTTP client with CFFI bindings
  - Supports advanced features like HTTP/2 and connection pooling
  - Used for: Fetching channel data and stream information with optimal performance

- **`httpx[http2]==0.28.1`**
  - Modern HTTP client with HTTP/2 support
  - Provides both sync and async APIs
  - Used for: Making HTTP requests to external services and handling stream proxying

### Backend Server

- **`uvicorn[standard]==0.32.1`**
  - Lightning-fast ASGI server implementation
  - Provides production-ready features with the [standard] extras
  - Used for: Serving the backend application with WebSocket support

- **`fastapi==0.115.6`**
  - Modern, fast web framework for building APIs
  - Provides automatic OpenAPI documentation
  - Used for: Backend API endpoints and WebSocket handling

### Utilities

- **`python-dateutil==2.9.0`**
  - Powerful extensions to Python's datetime module
  - Used for: Handling timestamps and schedule information

- **`aiohttp==3.10.11`**
  - Asynchronous HTTP client/server framework
  - Used for: Async HTTP requests and WebSocket communication

## 🐳 Docker Configuration

> **⚠️ Remote Deployment Note**: If you're deploying to a remote server (like Portainer) and accessing from other machines, you may encounter connection errors. See [DEPLOYMENT.md](DEPLOYMENT.md) for configuration details.

The application uses a multi-stage Docker build for optimal image size and security. Here's a breakdown of the Docker setup:

### Dockerfile Structure

```dockerfile
# Build stage
FROM python:3.13 AS builder
# Install build dependencies
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    gnupg
# Install Node.js
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs
# Set up Python virtual environment
RUN python -m venv /app/.venv
# Install Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt
# Build frontend
COPY . .
RUN npm install && npm run build

# Runtime stage
FROM python:3.13-slim
# Install runtime dependencies
RUN apt-get update -y && apt-get install -y \
    caddy \
    redis-server
# Copy built application
COPY --from=builder /app /app
COPY --from=builder /srv /srv
# Configure entrypoint
RUN chmod +x /app/start.sh
CMD ["/app/start.sh"]
```

### Docker Compose Configuration

The `docker-compose.yml` file provides a production-ready setup with:

- **Network Configuration**:
  - Exposed ports for frontend and backend
  - Internal Docker network for service communication
  - DNS configuration for reliable name resolution

- **Environment Variables**:
  - Comprehensive configuration through environment variables
  - Production-ready defaults for quick deployment
  - Support for concurrent streaming, proxy settings, and scaling

- **Resource Management**:
  - Automatic container restart
  - Network access control
  - Host machine access when needed

Example `docker-compose.yml`:
```yaml
services:
  freesky:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "${PORT:-3000}:${PORT:-3000}"
      - "${BACKEND_PORT:-8005}:${BACKEND_PORT:-8005}"
      - "2019:2019"  # Caddy admin port for debugging
    environment:
      - PORT=${PORT:-3000}
      - BACKEND_PORT=${BACKEND_PORT:-8005}
      - HOST_IP=${HOST_IP:-}  # Dynamic host IP, will use 0.0.0.0 if not set
      - BACKEND_URI=${BACKEND_URI:-}  # Let rxconfig determine this dynamically
      - API_URL=${API_URL:-}  # Let rxconfig determine this dynamically
      - DADDYLIVE_URI=${DADDYLIVE_URI:-https://thedaddy.click}
      - PROXY_CONTENT=${PROXY_CONTENT:-TRUE}
      - SOCKS5=${SOCKS5:-}
      - WORKERS=${WORKERS:-3}  # Backend worker processes
      - MAX_CONCURRENT_STREAMS=${MAX_CONCURRENT_STREAMS:-10}  # Concurrent streaming limit
      - REFLEX_ENV=prod
      - REFLEX_SKIP_COMPILE=1
    networks:
      freesky-network:
        driver: bridge
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:${PORT:-3000}/health", "||", "exit", "1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
```

---

## ⚙️ Environment Variables

freesky provides comprehensive configuration through environment variables, allowing you to customize the application for different deployment scenarios. All variables have sensible defaults for quick deployment.

### 🌐 Network Configuration

| Variable | Default | Description | Example |
|----------|---------|-------------|---------|
| `PORT` | `3000` | Frontend port for web interface | `PORT=8080` |
| `BACKEND_PORT` | `8005` | Backend API server port | `BACKEND_PORT=9000` |
| `HOST_IP` | `0.0.0.0` | Host IP for binding services | `HOST_IP=192.168.1.100` |
| `API_URL` | *auto-detected* | External API URL for client connections | `API_URL=https://mydomain.com` |
| `BACKEND_URI` | *auto-detected* | Internal backend service URL | `BACKEND_URI=http://localhost:8005` |

### 🚀 Performance & Scaling

| Variable | Default | Description | Recommendations |
|----------|---------|-------------|-----------------|
| `WORKERS` | `3` | Number of backend worker processes | `1-2` for small deployments<br>`3-6` for medium traffic<br>`6-12` for high traffic |
| `MAX_CONCURRENT_STREAMS` | `10` | Maximum concurrent streaming connections | `5-10` for basic use<br>`15-25` for medium traffic<br>`50+` for high-capacity servers |

### 📺 Content Configuration

| Variable | Default | Description | Usage |
|----------|---------|-------------|-------|
| `DADDYLIVE_URI` | `https://thedaddy.click` | Content provider URL | Change if using alternative provider |
| `PROXY_CONTENT` | `TRUE` | Enable content proxying through backend | `TRUE` for better CORS handling<br>`FALSE` for lower server load |
| `SOCKS5` | *(empty)* | SOCKS5 proxy for external requests | `SOCKS5=127.0.0.1:1080` |

### 🔧 Development & Production

| Variable | Default | Description | When to Use |
|----------|---------|-------------|-------------|
| `REFLEX_ENV` | `prod` | Reflex environment mode | `dev` for development<br>`prod` for production |
| `REFLEX_SKIP_COMPILE` | `1` | Skip frontend compilation | `1` for Docker builds<br>`0` for development |

### 📊 Configuration Examples

#### **Basic Home Setup**
```bash
# Simple home deployment
PORT=3000
BACKEND_PORT=8005
WORKERS=2
MAX_CONCURRENT_STREAMS=5
PROXY_CONTENT=TRUE
```

#### **Medium Traffic Server**
```bash
# Office or small business
PORT=3000
BACKEND_PORT=8005
WORKERS=4
MAX_CONCURRENT_STREAMS=20
PROXY_CONTENT=TRUE
API_URL=https://tv.yourdomain.com
```

#### **High-Capacity Production**
```bash
# Large-scale deployment
PORT=3000
BACKEND_PORT=8005
WORKERS=8
MAX_CONCURRENT_STREAMS=50
PROXY_CONTENT=FALSE  # Direct streaming for lower server load
API_URL=https://iptv.yourdomain.com
HOST_IP=10.0.1.100
```

#### **Development Environment**
```bash
# Local development
PORT=3000
BACKEND_PORT=8005
WORKERS=1
MAX_CONCURRENT_STREAMS=3
PROXY_CONTENT=TRUE
REFLEX_ENV=dev
REFLEX_SKIP_COMPILE=0
```

### 🛠️ Environment File Setup

Create a `.env` file in your project root:

```bash
# Create environment file
cat > .env << EOF
PORT=3000
BACKEND_PORT=8005
WORKERS=4
MAX_CONCURRENT_STREAMS=15
PROXY_CONTENT=TRUE
DADDYLIVE_URI=https://thedaddy.click
API_URL=http://192.168.1.100:3000
REFLEX_ENV=prod
REFLEX_SKIP_COMPILE=1
EOF

# Deploy with environment file
docker-compose up -d
```

### 📈 Performance Impact

#### **WORKERS vs MAX_CONCURRENT_STREAMS**

- **WORKERS**: Controls **process-level** concurrency
  - Each worker is a separate Python process
  - Bypasses Python GIL limitations
  - Recommended: 1 worker per CPU core

- **MAX_CONCURRENT_STREAMS**: Controls **thread-level** concurrency per worker
  - Handles async I/O operations within each process
  - Each stream gets isolated HTTP client and TCP connection
  - Memory usage scales with this limit
  - Recommended: 5-10 streams per worker

#### **Optimal Scaling Formula**
```
Total Capacity = WORKERS × MAX_CONCURRENT_STREAMS

Examples:
- 4 workers × 10 streams = 40 concurrent streams
- 6 workers × 15 streams = 90 concurrent streams
- 8 workers × 20 streams = 160 concurrent streams
```

### 🔍 Monitoring Configuration

Check your current configuration:
```bash
# View environment in running container
docker exec <container_name> env | grep -E "(PORT|WORKERS|MAX_CONCURRENT|API_URL|PROXY_CONTENT)"

# Check streaming capacity
curl http://localhost:3000/api/health
```

Expected health response:
```json
{
  "status": "healthy",
  "channels_count": 688,
  "max_concurrent_streams": 15,
  "total_active_streams": 3,
  "stream_utilization": "20.0%",
  "active_channels": 2
}
```

### ⚠️ Important Notes

1. **Memory Usage**: Each concurrent stream uses ~10-50MB RAM
2. **CPU Usage**: Workers should not exceed available CPU cores
3. **Network**: Higher `MAX_CONCURRENT_STREAMS` requires more bandwidth
4. **Storage**: Logs and cache scale with concurrent connections

---

## 🔄 Frontend-Backend Architecture

freesky uses a modern architecture built with **Reflex** (Python web framework) that provides seamless real-time communication between frontend and backend. Here's how the components interact:

### 🏗️ Architecture Overview

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Frontend      │    │   Caddy Proxy   │    │   Backend       │
│   (Reflex App)  │◄──►│   (Port 3000)   │◄──►│   (FastAPI)     │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ Reflex State    │    │ Static Files    │    │ IPTV Data       │
│ Management      │    │ & Routing       │    │ Processing      │
│ (Built-in WS)   │    │                 │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

### 🌐 Frontend Components

The frontend is built entirely with **Reflex**, a Python web framework that compiles to React, providing a responsive web interface for browsing and streaming TV channels.

#### **Key Frontend Features:**
- **📱 Responsive Design**: Works on desktop, tablet, and mobile devices
- **🔍 Real-time Search**: Live filtering of channels and events with instant updates
- **📺 Video Player**: Built-in web player with adaptive streaming
- **📊 Real-time Updates**: Live channel status and connection monitoring
- **🎯 Auto-refresh**: Automatic periodic updates with manual refresh controls
- **🔄 State Management**: Reactive state updates without page reloads

#### **Frontend Technologies:**
- **Reflex**: Full-stack Python web framework
- **Python**: All frontend logic written in Python
- **Reactive State**: Built-in state management system
- **Real-time Communication**: Native WebSocket support
- **Adaptive Video Player**: HLS/DASH streaming support
- **Responsive Design**: Mobile-first approach with CSS-in-Python

### 🔧 Backend Components

The backend is built with **FastAPI** and provides REST API endpoints for data processing and content delivery. Real-time communication is handled by Reflex's built-in WebSocket system.

#### **Key Backend Features:**
- **🚀 FastAPI**: High-performance async web framework
- **📊 Data Processing**: Channel parsing and IPTV stream handling
- **🎭 Content Proxying**: Optional video stream proxying with concurrent handling
- **🔒 CORS Handling**: Cross-origin request management
- **⚡ Intelligent Caching**: Stream caching with LRU eviction and TTL
- **🔄 Background Tasks**: Periodic channel updates and monitoring
- **🎯 Concurrent Streaming**: Multithreaded streaming with configurable limits
- **🔗 Stream Isolation**: Each stream gets dedicated HTTP client and TCP connection
- **📈 Real-time Monitoring**: Stream utilization and performance metrics with live session tracking

#### **Backend Technologies:**
- **FastAPI**: Modern Python web framework for API endpoints
- **Reflex Integration**: Seamless integration with Reflex state system
- **Uvicorn**: ASGI server with configurable multiple workers
- **HTTPX**: Modern HTTP client with isolated connections per stream
- **Asyncio**: Advanced concurrency with semaphores and connection management
- **Python 3.13**: Latest Python with enhanced async/await support

### 📡 Communication Protocols

#### **1. HTTP/REST API**
The backend exposes several REST endpoints for standard operations:

```
GET  /                          # Frontend static files (served by Caddy)
GET  /stream/<channel_id>       # Stream video content
GET  /playlist.m3u8            # Download M3U8 playlist
GET  /logo/<logo_id>           # Channel logos and images
GET  /key/<url>/<host>         # Streaming keys
GET  /content/<path>           # Proxied content
GET  /health                   # Health check endpoint
GET  /ping                     # Simple ping endpoint
```

#### **2. Reflex State Management**
Real-time features use Reflex's built-in state management system:

**Python State Updates:**
```python
class State(rx.State):
    channels: List[Channel] = []
    search_query: str = ""
    is_loading: bool = False
    connection_status: str = "connected"
    
    async def load_channels(self):
        """Load channels with real-time updates"""
        self.is_loading = True
        self.channels = backend.get_channels()
        self.is_loading = False
    
    async def search_channels(self, query: str):
        """Real-time search filtering"""
        self.search_query = query
    
    async def refresh_channels(self):
        """Manual refresh with status updates"""
        await self.load_channels()
```

**Reactive Frontend Updates:**
```python
# Real-time filtered results
@rx.var
def filtered_channels(self) -> List[Channel]:
    if not self.search_query:
        return self.channels
    return [ch for ch in self.channels 
            if self.search_query.lower() in ch.name.lower()]

# Automatic UI updates when state changes
rx.foreach(State.filtered_channels, channel_card)
```

### 🔄 Data Flow Examples

#### **1. Channel Browsing Flow**
```
1. User opens homepage
   Reflex Frontend → State.on_load() → Backend
   
2. Backend fetches channel data
   Backend → External IPTV API → Channel List
   
3. State updates automatically
   Backend → State.channels → Reactive UI Update
   
4. Frontend renders channel grid
   Reflex State → rx.foreach() → Automatic UI Rendering
```

#### **2. Real-time Search Flow**
```
1. User types in search box
   Input Field → State.search_channels() → State Update
   
2. State variable updates
   State.search_query → Reactive @rx.var → Filtered Results
   
3. UI updates automatically
   State.filtered_channels → rx.foreach() → Live UI Update
   
4. No page reload needed
   Reflex State Management → Instant Visual Feedback
```

#### **3. Video Streaming Flow**
```
With PROXY_CONTENT=TRUE (Isolated Streaming):
User → Reflex Frontend → Backend → New HTTP Client → New TCP Connection → Upstream → Backend → User
Each stream creates isolated client and connection

With PROXY_CONTENT=FALSE (Direct Streaming):
User → Reflex Frontend → Backend → Stream URL → Frontend → Direct Connection → Upstream
```

### 🛠️ Configuration Impact on Communication

#### **API_URL Configuration**
- **Purpose**: Defines how frontend communicates with backend
- **Local Setup**: `http://192.168.1.100:3000`
- **Production**: `https://yourdomain.com`
- **Impact**: Affects all API calls and WebSocket connections

#### **PROXY_CONTENT Configuration**
- **TRUE**: All video streams proxied through backend with complete isolation
  - ✅ Better CORS handling
  - ✅ Unified authentication
  - ✅ Isolated HTTP clients per stream
  - ✅ Independent TCP connections
  - ✅ Real-time session tracking
  - ⚠️ Higher bandwidth usage
  - ⚠️ More memory per stream
- **FALSE**: Direct streaming from external sources
  - ✅ Lower server load
  - ✅ Reduced memory usage
  - ❌ Potential CORS issues
  - ❌ Limited control over streams
  - ❌ No session isolation benefits

#### **Performance Configuration**
- **WORKERS**: Number of backend worker processes
  - **Purpose**: Process-level concurrency for CPU-bound tasks
  - **Impact**: Affects overall request handling capacity
  - **Recommendation**: 1 worker per CPU core (typically 2-8 workers)
  
- **MAX_CONCURRENT_STREAMS**: Thread-level streaming concurrency per worker
  - **Purpose**: Controls concurrent streaming connections with isolation
  - **Implementation**: Each stream gets dedicated HTTP client and TCP connection
  - **Impact**: Memory usage and streaming capacity (higher per stream due to isolation)
  - **Recommendation**: 5-15 streams per worker (total = WORKERS × MAX_CONCURRENT_STREAMS)

### 🔍 Real-time Features

#### **Live Channel Status**
- **Purpose**: Monitor channel availability and connection status
- **Technology**: Reflex state management with periodic backend updates
- **Frequency**: Every 5 minutes with manual refresh option

#### **Real-time Search**
- **Purpose**: Instant search filtering without page reloads
- **Technology**: Reactive state variables with @rx.var decorators
- **Performance**: Immediate UI updates on keystroke

#### **Auto-refresh System**
- **Purpose**: Keep channel data fresh automatically
- **Technology**: Background asyncio tasks with state updates
- **Control**: User can toggle auto-refresh on/off

#### **Real-time Stream Monitoring**
- **Purpose**: Live tracking of active streaming sessions
- **Technology**: Per-stream session tracking with isolated clients
- **Updates**: Session count updates in real-time (30-second cleanup cycle)
- **Metrics**: Individual stream isolation and connection status

### 🚨 Error Handling

#### **Connection Failures**
```python
# Reflex handles connection issues through state management
async def load_channels(self):
    self.is_loading = True
    self.connection_status = "connecting"
    
    try:
        self.channels = backend.get_channels()
        self.connection_status = "connected"
    except Exception as e:
        self.connection_status = "error"
        # Load from fallback cache
        self.channels = load_fallback_channels()
    finally:
        self.is_loading = False
```

#### **API Timeouts and Stream Isolation**
```python
# Backend implements isolated streaming with timeout handling
@fastapi_app.get("/api/content/{path}")
async def content(path: str):
    # Create isolated client for this specific stream
    isolated_client = create_isolated_stream_client()
    try:
        async with isolated_client.stream("GET", stream_url, timeout=60) as response:
            async for chunk in response.aiter_bytes():
                yield chunk
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Stream timeout")
    finally:
        # Always close isolated client
        await isolated_client.aclose()
```

### 🔒 Security Considerations

#### **CORS Configuration**
- **Frontend Origin**: Automatically configured based on `API_URL`
- **Reflex Security**: Built-in CSRF protection and secure state management
- **API Endpoints**: CORS headers added for browser compatibility

#### **Rate Limiting**
- **Purpose**: Prevent API abuse and ensure fair usage
- **Implementation**: Per-IP rate limiting on API endpoints
- **Limits**: 100 requests per minute per IP

#### **Input Validation**
- **Search Queries**: Sanitized through Reflex state validation
- **Channel IDs**: Validated against known channel list
- **Stream URLs**: Validated before proxying
- **State Security**: Reflex prevents direct state manipulation from client

### 📊 Performance Monitoring

#### **Health Endpoints**
```
GET /health                    # Comprehensive health check with streaming metrics
GET /ping                     # Simple ping endpoint with channel count
```

**Health Response Example:**
```json
{
  "status": "healthy",
  "channels_count": 688,
  "cache_size": 15,
  "active_channels": 3,
  "total_active_streams": 8,
  "max_concurrent_streams": 20,
  "stream_utilization": "40.0%",
  "connection_isolation": "per_stream",
  "connection_reuse": "disabled",
  "multithreading_mode": "full_concurrency",
  "session_tracking": "per_request",
  "uptime": 1752463063.68
}
```

#### **Reflex State Monitoring**
```python
# Built-in performance tracking through state variables
class State(rx.State):
    connection_status: str = "connected"
    last_update: str = ""
    channels_count: int = 0
    is_loading: bool = False
    
    @rx.var
    def status_color(self) -> str:
        """Visual indicator of system health"""
        return "green" if self.connection_status == "connected" else "red"
```

#### **Backend Performance Metrics**
- **Channel Count**: Number of available channels (688+)
- **Concurrent Streaming**: Active streams vs maximum capacity
- **Stream Utilization**: Percentage of streaming capacity in use
- **Cache Performance**: Stream cache hit rates and LRU efficiency
- **Response Times**: Average API response times with X-Process-Time headers
- **Error Rates**: Failed request percentages and timeout handling
- **Background Tasks**: Channel update task status and health
- **Stream Isolation**: Per-stream connection tracking and isolation metrics
- **Real-time Session Tracking**: Live updates of active streaming sessions

### 🔗 Stream Isolation Architecture

freesky implements **complete stream isolation** to ensure maximum performance and reliability for concurrent streaming:

#### **Per-Stream Connection Isolation**
- **Dedicated HTTP Client**: Each streaming session creates its own isolated HTTP client
- **Individual TCP Connections**: No connection sharing or pooling between streams
- **Zero Connection Reuse**: Connections are immediately closed after streaming ends
- **Complete Independence**: Stream failures don't affect other active streams

#### **Isolation Configuration**
```python
# Each stream gets isolated client with these settings:
httpx.AsyncClient(
    http2=False,                     # Simpler connection handling
    max_keepalive_connections=0,     # No connection reuse
    max_connections=1,               # One connection per client
    keepalive_expiry=0.0            # Close immediately after use
)
```

#### **Stream Lifecycle**
```
1. Client Request → New HTTP Client Created
2. HTTP Client → New TCP Connection to Upstream
3. Streaming → Dedicated Connection Active
4. Stream End → Connection Closed & Client Destroyed
```

#### **Benefits of Stream Isolation**
- ✅ **Maximum Reliability**: Individual stream issues don't cascade
- ✅ **True Multithreading**: Complete independence between streams
- ✅ **Resource Isolation**: Memory and connection resources per stream
- ✅ **Clean Cleanup**: No lingering connections or resource leaks
- ✅ **Real-time Tracking**: Accurate session monitoring and metrics

This architecture ensures scalable, real-time performance using Reflex's reactive state management system, eliminating the complexity of manual WebSocket handling while providing instant UI updates.

---

## 🗺️ Site Map

### Pages Overview:

- **🏠 Home**: Browse and search for TV channels.
- **📺 Live Events**: Quickly find channels broadcasting live events and sports.
- **📥 Playlist Download**: Download the `playlist.m3u8` file for integration with media players.

---

## 📸 Screenshots

**Home Page**
<img alt="Home Page" src="https://files.catbox.moe/qlqqs5.png">

**Watch Page**
<img alt="Watch Page" src="https://files.catbox.moe/974r9w.png">

**Live Events**
<img alt="Live Events" src="https://files.catbox.moe/7oawie.png">

---

## 📚 Hosting Options

Check out the [official Reflex hosting documentation](https://reflex.dev/docs/hosting/self-hosting/) for more advanced self-hosting setups!

### 🔒 Security Configuration

For detailed security configuration and best practices, including Content Security Policy (CSP), reverse proxy setup, and network security, please refer to [SECURITY.md](SECURITY.md).

Key security features:
- Content Security Policy (CSP) configuration
- Reverse proxy security with Caddy
- CORS and WebSocket security
- Rate limiting and DDoS protection
- Development vs Production security modes