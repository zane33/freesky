import os
import asyncio
import httpx
import logging
import time
from functools import lru_cache
from typing import Optional, Dict, Tuple, Set
from freesky.free_sky import StepDaddy, Channel
from fastapi import Response, status, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
# CORSMiddleware removed - CORS handled by Caddy
from .utils import urlsafe_base64_decode
import json
from urllib.parse import urlparse, urlunparse
from collections import OrderedDict

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
# Reduce httpcore debug noise
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Get environment variables
frontend_port = int(os.environ.get("PORT", "3000"))
backend_port = int(os.environ.get("BACKEND_PORT", "8005"))
max_concurrent_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "10"))
api_url = os.environ.get("API_URL", f"http://0.0.0.0:{backend_port}")  # Use backend port and bind to all interfaces

# Parse API_URL to create WebSocket URL with backend port
parsed_url = urlparse(api_url)
ws_url = urlunparse(parsed_url._replace(netloc=f"{parsed_url.hostname}:{backend_port}"))

# Create FastAPI app with better configuration
fastapi_app = FastAPI(
    title="freesky API",
    description="IPTV proxy API",
    version="1.0.0"
)

# CORS is handled by Caddy reverse proxy to prevent duplicate headers

# Create HTTP client with optimized settings for high-concurrency streaming
client = httpx.AsyncClient(
    http2=True,
    timeout=httpx.Timeout(30.0, connect=10.0),  # Longer timeouts for streaming
    limits=httpx.Limits(
        max_keepalive_connections=max_concurrent_streams * 5,  # Dynamic based on concurrent streams
        max_connections=max_concurrent_streams * 10,           # 10x the concurrent stream limit
        keepalive_expiry=120.0                                 # Longer keepalive for streaming
    ),
    follow_redirects=True
)

free_sky = StepDaddy()

# Use OrderedDict for LRU cache behavior
class LRUCache(OrderedDict):
    def __init__(self, maxsize=0, *args, **kwargs):
        self.maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if self.maxsize > 0 and len(self) > self.maxsize:
            oldest = next(iter(self))
            del self[oldest]

# Cache with size limit and TTL optimized for streaming
stream_cache = LRUCache(maxsize=max_concurrent_streams * 10)  # Dynamic cache size
cache_ttl = 30  # 30 seconds for live streaming freshness

# Track active tasks and streaming sessions for cleanup
active_tasks: Dict[str, asyncio.Task] = {}
active_streams: Dict[str, Dict[str, float]] = {}  # Track clients per channel with timestamps

# Concurrency control for streaming with configurable limits
_stream_semaphore = asyncio.Semaphore(max_concurrent_streams)
_content_semaphore = asyncio.Semaphore(max_concurrent_streams * 2)  # More content streams allowed

logger.info("Backend initialized with connection pooling")

# Start channel update task
channel_update_task = None

@fastapi_app.on_event("startup")
async def startup_event():
    # Background task now managed by Reflex lifespan
    logger.info("FastAPI startup complete - channel loading managed by Reflex")

@fastapi_app.on_event("shutdown")
async def shutdown_event():
    logger.info("Starting shutdown procedure...")
    
    # Cancel all active tasks with timeout
    for task_name, task in active_tasks.items():
        if not task.done():
            logger.info(f"Cancelling task: {task_name}")
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.info(f"Task {task_name} cancelled/timed out")
            except Exception as e:
                logger.error(f"Error cancelling task {task_name}: {e}")
    
    # Clear task registry
    active_tasks.clear()
    
    # Close HTTP client with timeout
    try:
        await asyncio.wait_for(client.aclose(), timeout=10.0)
        logger.info("HTTP client closed successfully")
    except asyncio.TimeoutError:
        logger.warning("HTTP client close timed out")
    except Exception as e:
        logger.error(f"Error closing HTTP client: {e}")
    
    logger.info("Shutdown procedure completed")

@fastapi_app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response

# Add OPTIONS handler for streaming endpoints to handle CORS preflight requests
# OPTIONS handler removed - CORS preflight handled by Caddy

@fastapi_app.get("/stream/{channel_id}.m3u8")
@fastapi_app.get("/api/stream/{channel_id}.m3u8") 
async def stream(channel_id: str):
    try:
        # Get current time for tracking and caching
        current_time = time.time()
        
        # Generate unique client ID for tracking (simplified without request object)
        client_id = f"stream_{current_time}_{id(asyncio.current_task())}"
        
        # Track active streams per channel with timestamp
        if channel_id not in active_streams:
            active_streams[channel_id] = {}
        active_streams[channel_id][client_id] = current_time
        
        logger.info(f"Client {client_id} requesting stream for channel {channel_id}. Active streams: {len(active_streams[channel_id])}")
        
        # Check cache first
        cache_key = f"stream_{channel_id}"
        
        if cache_key in stream_cache:
            cached_data, cached_time = stream_cache[cache_key]
            if current_time - cached_time < cache_ttl:
                logger.info(f"Serving cached stream for channel {channel_id} to client {client_id}")
                return Response(
                    content=cached_data,
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                        "Accept-Ranges": "bytes",
                        "X-Stream-Source": "cache"
                    }
                )
        
        # Use semaphore to control concurrent stream generation
        async with _stream_semaphore:
            # Double-check cache after acquiring semaphore (another request might have populated it)
            if cache_key in stream_cache:
                cached_data, cached_time = stream_cache[cache_key]
                if current_time - cached_time < cache_ttl:
                    logger.info(f"Serving freshly cached stream for channel {channel_id} to client {client_id}")
                    return Response(
                        content=cached_data,
                        media_type="application/vnd.apple.mpegurl",
                        headers={
                            "Cache-Control": "no-cache, no-store, must-revalidate",
                            "Pragma": "no-cache",
                            "Expires": "0",
                            "Accept-Ranges": "bytes",
                            "X-Stream-Source": "cache-after-semaphore"
                        }
                    )
            
            # Generate new stream with timeout
            try:
                logger.info(f"Generating new stream for channel {channel_id} for client {client_id}")
                stream_data = await asyncio.wait_for(
                    free_sky.stream(channel_id),
                    timeout=15.0  # Increased timeout for stream generation
                )
            except asyncio.TimeoutError:
                logger.error(f"Timeout generating stream for channel {channel_id} for client {client_id}")
                # Clean up client tracking
                if channel_id in active_streams and client_id in active_streams[channel_id]:
                    del active_streams[channel_id][client_id]
                return JSONResponse(
                    content={"error": "Stream generation timeout"},
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT
                )
            
            # Cache the result (LRU cache handles cleanup)
            stream_cache[cache_key] = (stream_data, current_time)
            logger.info(f"Successfully generated and cached stream for channel {channel_id}")
        
        return Response(
            content=stream_data,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Expose-Headers": "*",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Accept-Ranges": "bytes",
                "X-Stream-Source": "generated"
            }
        )
    except IndexError:
        # Clean up client tracking
        if channel_id in active_streams and client_id in active_streams[channel_id]:
            del active_streams[channel_id][client_id]
        return JSONResponse(content={"error": "Stream not found"}, status_code=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"Error streaming channel {channel_id} for client {client_id}: {str(e)}")
        # Clean up client tracking
        if channel_id in active_streams and client_id in active_streams[channel_id]:
            del active_streams[channel_id][client_id]
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    finally:
        # Clean up completed stream requests
        if channel_id in active_streams and client_id in active_streams[channel_id]:
            del active_streams[channel_id][client_id]
            logger.debug(f"Cleaned up client {client_id} from channel {channel_id}")
            
            # Clean up empty channel entries
            if not active_streams[channel_id]:
                del active_streams[channel_id]
                logger.debug(f"Removed empty channel {channel_id} from active streams")

@fastapi_app.get("/api/key/{url}/{host}")
async def key(url: str, host: str):
    try:
        # Add timeout to key retrieval
        key_data = await asyncio.wait_for(
            free_sky.key(url, host),
            timeout=5.0  # 5 second timeout for key retrieval
        )
        return Response(
            content=key_data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=key"}
        )
    except asyncio.TimeoutError:
        logger.error(f"Timeout getting key for {url}")
        return JSONResponse(
            content={"error": "Key retrieval timeout"},
            status_code=status.HTTP_504_GATEWAY_TIMEOUT
        )
    except Exception as e:
        logger.error(f"Error getting key: {str(e)}")
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@fastapi_app.options("/api/content/{path}")
async def content_options(path: str):
    return Response(
        content="",
        headers={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS", 
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*",
            "Access-Control-Max-Age": "86400"
        }
    )

@fastapi_app.get("/api/content/{path}")
async def content(path: str):
    try:
        # Use dedicated content semaphore for higher throughput
        async with _content_semaphore:
            logger.debug(f"Proxying content: {path}")
            
            async def proxy_stream():
                try:
                    async with client.stream("GET", free_sky.content_url(path), timeout=60) as response:
                        async for chunk in response.aiter_bytes(chunk_size=8 * 1024):  # Larger chunks for better throughput
                            yield chunk
                except Exception as e:
                    logger.error(f"Error in proxy stream: {str(e)}")
                    raise
            
            return StreamingResponse(
                proxy_stream(), 
                media_type="application/octet-stream",
                headers={
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Expose-Headers": "*",
                    "Cache-Control": "public, max-age=3600",
                    "Accept-Ranges": "bytes",
                    "Transfer-Encoding": "chunked"
                }
            )
    except Exception as e:
        logger.error(f"Error proxying content: {str(e)}")
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

async def update_channels():
    update_interval = 300  # 5 minutes
    retry_interval = 60   # 1 minute on failure
    max_retries = 3      # Maximum number of retries
    
    while True:
        try:
            logger.info("Loading channels...")
            retries = 0
            success = False
            
            while not success and retries < max_retries:
                try:
                    await free_sky.load_channels()
                    if free_sky.channels:
                        success = True
                        logger.info(f"Successfully loaded {len(free_sky.channels)} channels")
                        # Clear stream cache when channels are updated
                        stream_cache.clear()
                        await asyncio.sleep(update_interval)
                    else:
                        raise Exception("No channels loaded from primary source")
                except Exception as e:
                    retries += 1
                    logger.error(f"Error loading channels (attempt {retries}/{max_retries}): {str(e)}")
                    if retries < max_retries:
                        await asyncio.sleep(retry_interval)
            
            if not success:
                # All retries failed, try fallback
                if os.path.exists("freesky/fallback_channels.json"):
                    logger.info("Loading channels from fallback file...")
                    with open("freesky/fallback_channels.json", "r") as f:
                        fallback_data = json.load(f)
                        free_sky.channels = [Channel(**channel_data) for channel_data in fallback_data]
                    if free_sky.channels:
                        logger.info(f"Loaded {len(free_sky.channels)} channels from fallback")
                    else:
                        logger.error("No channels in fallback file")
                else:
                    logger.error("No fallback file available")
                
                # Wait before next attempt even if using fallback
                await asyncio.sleep(update_interval)
                
        except asyncio.CancelledError:
            logger.info("Channel update task cancelled")
            break
        except Exception as e:
            logger.error(f"Unexpected error in channel update loop: {str(e)}")
            await asyncio.sleep(retry_interval)

def get_channels():
    """Get current channels with fallback handling."""
    try:
        logger.debug("Attempting to get channels from free_sky instance")
        channels = free_sky.channels
        if channels:
            logger.info(f"Successfully retrieved {len(channels)} channels")
            return channels
        
        logger.warning("No channels available from primary source, trying fallback")
        # Try loading from fallback synchronously if no channels available
        if os.path.exists("freesky/fallback_channels.json"):
            with open("freesky/fallback_channels.json", "r") as f:
                fallback_data = json.load(f)
                channels = [Channel(**channel_data) for channel_data in fallback_data]
            logger.info(f"Loaded {len(channels)} channels from fallback in get_channels()")
            return channels
        else:
            logger.error("No fallback channels file found")
            return []
    except Exception as e:
        logger.error(f"Error in get_channels(): {str(e)}", exc_info=True)
        return []

def get_channel(channel_id) -> Optional[Channel]:
    if not channel_id or channel_id == "":
        return None
    channels = get_channels()  # Use get_channels() to ensure fallback handling
    return next((channel for channel in channels if channel.id == channel_id), None)

@fastapi_app.options("/playlist.m3u8")
def playlist_options():
    return Response(
        content="",
        headers={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*",
            "Access-Control-Max-Age": "86400"
        }
    )

@fastapi_app.get("/playlist.m3u8")
def playlist():
    """Return the playlist as a response"""
    return Response(
        content=free_sky.playlist(),
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Accept-Ranges": "bytes"
        }
    )

@fastapi_app.options("/api/playlist.m3u8")
def api_playlist_options():
    return Response(
        content="",
        headers={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*",
            "Access-Control-Max-Age": "86400"
        }
    )

@fastapi_app.get("/api/playlist.m3u8")
def api_playlist():
    """Return the playlist as a response (API endpoint)"""
    return Response(
        content=free_sky.playlist(),
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Accept-Ranges": "bytes"
        }
    )

async def get_schedule():
    return await free_sky.schedule()

@fastapi_app.get("/logo/{logo}")
async def logo(logo: str):
    url = urlsafe_base64_decode(logo)
    file = url.split("/")[-1]
    if not os.path.exists("./logo-cache"):
        os.makedirs("./logo-cache")
    if os.path.exists(f"./logo-cache/{file}"):
        return FileResponse(
            f"./logo-cache/{file}",
            headers={"Cache-Control": "public, max-age=86400"}  # Cache for 24 hours
        )
    try:
        response = await client.get(
            url, 
            headers={"user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"}
        )
        if response.status_code == 200:
            with open(f"./logo-cache/{file}", "wb") as f:
                f.write(response.content)
            return FileResponse(
                f"./logo-cache/{file}",
                headers={"Cache-Control": "public, max-age=86400"}
            )
        else:
            return JSONResponse(content={"error": "Logo not found"}, status_code=status.HTTP_404_NOT_FOUND)
    except httpx.ConnectTimeout:
        return JSONResponse(content={"error": "Request timed out"}, status_code=status.HTTP_504_GATEWAY_TIMEOUT)
    except Exception as e:
        logger.error(f"Error fetching logo: {str(e)}")
        return JSONResponse(content={"error": str(e)}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@fastapi_app.get("/ping")
async def ping():
    return {"status": "ok", "channels_count": len(free_sky.channels)}

@fastapi_app.get("/health")
async def health():
    # Calculate total active streams and clean up stale entries
    current_time = time.time()
    stale_timeout = 300  # 5 minutes timeout for stale entries
    
    for channel_id in list(active_streams.keys()):
        # Remove stale entries (older than 5 minutes)
        stale_clients = [
            client_id for client_id, timestamp in active_streams[channel_id].items()
            if current_time - timestamp > stale_timeout
        ]
        for client_id in stale_clients:
            del active_streams[channel_id][client_id]
            logger.debug(f"Removed stale client {client_id} from channel {channel_id}")
        
        # Remove empty channels
        if not active_streams[channel_id]:
            del active_streams[channel_id]
    
    total_active_streams = sum(len(clients) for clients in active_streams.values())
    
    return {
        "status": "healthy",
        "channels_count": len(free_sky.channels),
        "cache_size": len(stream_cache),
        "active_channels": len(active_streams),
        "total_active_streams": total_active_streams,
        "max_concurrent_streams": max_concurrent_streams,
        "stream_utilization": f"{(total_active_streams / max_concurrent_streams) * 100:.1f}%",
        "uptime": time.time()
    }

@fastapi_app.get("/channels")
async def channels_endpoint():
    """Get all channels as JSON."""
    try:
        channels = get_channels()
        return {
            "channels": [
                {
                    "id": ch.id,
                    "name": ch.name,
                    "logo": ch.logo,
                    "tags": ch.tags
                }
                for ch in channels
            ],
            "count": len(channels)
        }
    except Exception as e:
        logger.error(f"Error in channels endpoint: {str(e)}")
        return {"error": str(e), "channels": [], "count": 0}

@fastapi_app.get("/schedule")
async def schedule_endpoint():
    """Get schedule data as JSON."""
    try:
        schedule_data = await get_schedule()
        return {"schedule": schedule_data, "status": "success"}
    except Exception as e:
        logger.error(f"Error in schedule endpoint: {str(e)}")
        # Return fallback schedule data
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        
        now = datetime.now(ZoneInfo("UTC"))
        fallback_schedule = {}
        
        for day_offset in range(7):  # 7 days
            day = now + timedelta(days=day_offset)
            day_key = day.strftime("%d/%m/%Y - %A")
            fallback_schedule[day_key] = {
                "Sports": [
                    {
                        "event": f"Sports Event {i+1}",
                        "time": f"{9+i*3}:00",
                        "channels": [{"channel_name": "ESPN", "channel_id": "1"}]
                    }
                    for i in range(3)
                ],
                "News": [
                    {
                        "event": f"News Hour {i+1}",
                        "time": f"{10+i*4}:00",
                        "channels": [{"channel_name": "CNN", "channel_id": "2"}]
                    }
                    for i in range(3)
                ],
                "Entertainment": [
                    {
                        "event": f"Entertainment Show {i+1}",
                        "time": f"{20+i}:00",
                        "channels": [{"channel_name": "HBO", "channel_id": "3"}]
                    }
                    for i in range(3)
                ]
            }
        
        return {"schedule": fallback_schedule, "status": "fallback"}

