import os
import asyncio
import httpx
import logging
import time
import re
from functools import lru_cache
from typing import Optional, Dict, Tuple, Set
from freesky.free_sky_hybrid import StepDaddyHybrid as StepDaddy
from freesky.free_sky import Channel
from fastapi import Response, status, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
# CORSMiddleware removed - CORS handled by Caddy
from .utils import urlsafe_base64_decode, encrypt
from .vidembed_extractor import extract_hls_from_vidembed
from .multi_service_streamer import multi_streamer
import json
from urllib.parse import urlparse, urlunparse
from rxconfig import config
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
max_concurrent_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "5"))  # Reduced from 10 to 5
api_url = os.environ.get("API_URL", f"http://0.0.0.0:{frontend_port}")  # Use frontend port for client-facing URLs

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

# Create HTTP client with settings optimized for isolated connections per stream
# This client is used for non-streaming requests (logos, keys, etc.)
client = httpx.AsyncClient(
    http2=True,
    timeout=httpx.Timeout(30.0, connect=10.0),
    limits=httpx.Limits(
        max_keepalive_connections=5,  # Reduced from 10 to 5
        max_connections=25,            # Reduced from 50 to 25
        keepalive_expiry=30.0         # Shorter keepalive
    ),
    follow_redirects=True
)

# Function to create isolated HTTP clients for each streaming session
def create_isolated_stream_client():
    """Create a new HTTP client for each streaming session to ensure complete isolation"""
    return httpx.AsyncClient(
        http2=False,  # Disable HTTP/2 for simpler connection handling
        timeout=httpx.Timeout(30.0, connect=10.0),  # Reduced timeout from 60 to 30 seconds
        limits=httpx.Limits(
            max_keepalive_connections=0,  # No connection reuse - every request gets new connection
            max_connections=1,            # Only one connection per client
            keepalive_expiry=0.0          # No keepalive - close immediately after use
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
stream_cache = LRUCache(maxsize=max_concurrent_streams * 5)  # Reduced from 10 to 5
cache_ttl = 5  # Keep playlist extremely fresh for live streaming

# Track active tasks and streaming sessions for cleanup
active_tasks: Dict[str, asyncio.Task] = {}
active_streams: Dict[str, Dict[str, float]] = {}  # Track M3U8 requests per channel with timestamps
active_content_sessions: Dict[str, Dict[str, float]] = {}  # Track actual video streaming sessions
session_to_channel: Dict[str, str] = {}  # Map session IDs to channel IDs

def _process_stream_content(content: str, referer: str) -> str:
    """Process stream content for proxying"""
    if content.startswith('#EXTM3U'):
        # Process M3U8 playlists
        lines = content.split('\n')
        processed_lines = []
        
        for line in lines:
            if line.startswith('http') and config.proxy_content:
                # Proxy content URLs
                line = f"/api/content/{encrypt(line)}"
            elif line.startswith('#EXT-X-KEY:'):
                # Process encryption keys
                original_url = re.search(r'URI="(.*?)"', line)
                if original_url:
                    line = line.replace(original_url.group(1), 
                        f"/api/key/{encrypt(original_url.group(1))}/{encrypt(urlparse(referer).netloc)}")
            
            processed_lines.append(line)
        
        return '\n'.join(processed_lines)
    else:
        return content

# Concurrency control for streaming with configurable limits
_stream_semaphore = asyncio.Semaphore(max_concurrent_streams)
_content_semaphore = asyncio.Semaphore(max_concurrent_streams)  # Reduced from 2x to 1x

logger.info("Backend initialized with connection pooling")

def extract_channel_from_content_path(content_path: str) -> str:
    """Extract channel identifier from content path for session tracking"""
    try:
        # Decrypt the content URL to analyze it
        decrypted_url = free_sky.content_url(content_path)
        
        # Look for channel identifiers in the URL
        # Common patterns: /channel_id/, /stream-123/, etc.
        import re
        
        # Try to find channel ID patterns in the URL
        patterns = [
            r'/([0-9]+)/',  # /123/
            r'stream-([0-9]+)',  # stream-123
            r'channel_([0-9]+)',  # channel_123
            r'/([0-9]+)\.',  # /123.ts
        ]
        
        for pattern in patterns:
            match = re.search(pattern, decrypted_url)
            if match:
                return match.group(1)
        
        # Fallback: use a hash of the base URL for grouping
        from urllib.parse import urlparse
        parsed = urlparse(decrypted_url)
        base_path = '/'.join(parsed.path.split('/')[:3])  # First 3 path segments
        return str(abs(hash(base_path)) % 10000)  # Convert to a 4-digit identifier
        
    except Exception as e:
        logger.debug(f"Could not extract channel from content path: {e}")
        return "unknown"

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
        
        # Generate unique client ID for tracking - each request is a separate session
        task_id = id(asyncio.current_task())
        client_id = f"stream_{current_time}_{task_id}_{hash(str(current_time) + str(task_id)) % 10000}"
        
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
                
                # Use multi-service streamer to try multiple upstream feeds with shorter timeout
                stream_data = await asyncio.wait_for(
                    multi_streamer.get_stream(channel_id),
                    timeout=10.0  # Reduced from 15.0 to 10.0 seconds
                )
                
                if not stream_data:
                    logger.error(f"No stream found for channel {channel_id} on any service")
                    return JSONResponse(
                        content={"error": "Stream not found on any service"},
                        status_code=status.HTTP_404_NOT_FOUND
                    )
                
                # Handle vidembed URLs - try to extract HLS stream
                if stream_data.startswith("VIDEMBED_URL:"):
                    vidembed_url = stream_data.replace("VIDEMBED_URL:", "")
                    logger.info(f"Extracting HLS from vidembed URL for channel {channel_id}")
                    
                    try:
                        # Extract HLS stream from vidembed with timeout
                        hls_data = await asyncio.wait_for(
                            extract_hls_from_vidembed(vidembed_url),
                            timeout=8.0  # 8 second timeout for HLS extraction
                        )
                        
                        if hls_data:
                            stream_data = _process_stream_content(hls_data, vidembed_url)
                            logger.info(f"Successfully extracted HLS stream for channel {channel_id}")
                        else:
                            # Fallback to vidembed URL for client-side processing
                            stream_data = f"VIDEMBED_URL:{vidembed_url}"
                            logger.info(f"Using vidembed fallback for channel {channel_id}")
                    except asyncio.TimeoutError:
                        logger.warning(f"HLS extraction timed out for channel {channel_id}, using vidembed fallback")
                        stream_data = f"VIDEMBED_URL:{vidembed_url}"
                    except Exception as e:
                        logger.error(f"Error extracting HLS from vidembed for channel {channel_id}: {str(e)}")
                        stream_data = f"VIDEMBED_URL:{vidembed_url}"
                else:
                    # Process regular stream data
                    stream_data = _process_stream_content(stream_data, api_url)
                
                # Cache the processed stream data
                stream_cache[cache_key] = (stream_data, current_time)
                logger.info(f"Successfully generated and cached stream for channel {channel_id}")
                
                return Response(
                    content=stream_data,
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                        "Accept-Ranges": "bytes",
                        "X-Stream-Source": "generated"
                    }
                )
                
            except asyncio.TimeoutError:
                logger.error(f"Timeout generating stream for channel {channel_id}")
                return JSONResponse(
                    content={"error": "Stream generation timeout"},
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT
                )
            except Exception as e:
                logger.error(f"Error generating stream for channel {channel_id}: {str(e)}")
                return JSONResponse(
                    content={"error": str(e)},
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
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
async def content(path: str, request: Request):
    current_time = time.time()
    session_id = None
    channel_id = None
    
    try:
        # Extract channel ID from content path for session tracking
        channel_id = extract_channel_from_content_path(path)
        
        # Generate unique session ID for this streaming session 
        # Each request gets its own session regardless of IP - true multithreading
        task_id = id(asyncio.current_task())
        session_id = f"content_{current_time}_{task_id}_{hash(str(current_time) + str(task_id)) % 10000}"
        
        # Track this streaming session
        if channel_id not in active_content_sessions:
            active_content_sessions[channel_id] = {}
        active_content_sessions[channel_id][session_id] = current_time
        session_to_channel[session_id] = channel_id
        
        logger.info(f"Starting content stream session {session_id} for channel {channel_id}. Total active content sessions for this channel: {len(active_content_sessions[channel_id])}")
        
        # Use dedicated content semaphore for higher throughput
        async with _content_semaphore:
            logger.debug(f"Proxying content: {path[:100]}...")  # Truncate for cleaner logs
            
            async def proxy_stream():
                last_heartbeat = time.time()
                chunk_count = 0
                # Create isolated client for this specific streaming session
                isolated_client = create_isolated_stream_client()
                
                try:
                    logger.info(f"Creating isolated connection for stream session {session_id}")
                    
                    # Build upstream request with proper headers to avoid CDN throttling
                    upstream_url = free_sky.content_url(path)
                    parsed = urlparse(upstream_url)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    request_headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept-Encoding": "identity",
                        "Referer": origin + "/",
                        "Origin": origin,
                    }
                    # Forward Range header when present (important for some players/CDNs)
                    incoming_range = request.headers.get("Range")
                    if incoming_range:
                        request_headers["Range"] = incoming_range
                    else:
                        # Only hint range for progressive MP4; avoid for HLS segments
                        if parsed.path.lower().endswith(".mp4"):
                            request_headers["Range"] = "bytes=0-"

                    # Disable read timeout while keeping connect timeout to avoid mid-segment stalls
                    http_timeout = httpx.Timeout(connect=11.0, read=None, write=10.0, pool=10.0)

                    async with isolated_client.stream("GET", upstream_url, headers=request_headers, timeout=http_timeout) as response:
                        logger.info(f"Stream session {session_id} established new isolated connection (status: {response.status_code})")

                        async for chunk in response.aiter_bytes(chunk_size=256 * 1024):  # Larger chunks for better throughput
                            chunk_count += 1
                            current_chunk_time = time.time()

                            # Update session timestamp every 3 seconds for real-time tracking
                            if current_chunk_time - last_heartbeat > 3:
                                if channel_id in active_content_sessions and session_id in active_content_sessions[channel_id]:
                                    active_content_sessions[channel_id][session_id] = current_chunk_time
                                    last_heartbeat = current_chunk_time

                            yield chunk
                                
                    logger.info(f"Stream session {session_id} completed normally after {chunk_count} chunks")
                except asyncio.TimeoutError:
                    logger.warning(f"Stream session {session_id} timed out after 25 seconds")
                    raise
                except Exception as e:
                    logger.error(f"Error in isolated proxy stream for session {session_id}: {str(e)}")
                    raise
                finally:
                    # Always close the isolated client to ensure no connection reuse
                    try:
                        await isolated_client.aclose()
                        logger.debug(f"Closed isolated client for session {session_id}")
                    except Exception as e:
                        logger.error(f"Error closing isolated client for session {session_id}: {str(e)}")
                    
                    # Clean up session tracking when streaming actually ends
                    if session_id and channel_id:
                        if channel_id in active_content_sessions and session_id in active_content_sessions[channel_id]:
                            del active_content_sessions[channel_id][session_id]
                            if session_id in session_to_channel:
                                del session_to_channel[session_id]
                            logger.info(f"Cleaned up content stream session {session_id} for channel {channel_id}. Remaining sessions for this channel: {len(active_content_sessions.get(channel_id, {}))}")
                            
                            # Clean up empty channel entries
                            if channel_id in active_content_sessions and not active_content_sessions[channel_id]:
                                del active_content_sessions[channel_id]
            
            # Heuristic media type based on URL
            media_type = "application/octet-stream"
            lower_url = free_sky.content_url(path).lower()
            if any(ext in lower_url for ext in [".ts", ".m2ts", ".m2t"]):
                media_type = "video/mp2t"
            elif any(ext in lower_url for ext in [".mp4", ".m4s", ".cmfv", ".cmfa"]):
                media_type = "video/mp4"

            return StreamingResponse(
                proxy_stream(), 
                media_type=media_type,
                headers={
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Expose-Headers": "*",
                    "Cache-Control": "no-cache, no-store, must-revalidate, private, no-transform",
                    "Pragma": "no-cache",
                    "Expires": "0",
                    "Accept-Ranges": "bytes",
                    "X-Accel-Buffering": "no",
                    "X-Session-ID": session_id
                }
            )
    except Exception as e:
        logger.error(f"Error proxying content for session {session_id}: {str(e)}")
        # Clean up session on error
        if session_id and channel_id:
            if channel_id in active_content_sessions and session_id in active_content_sessions[channel_id]:
                del active_content_sessions[channel_id][session_id]
                if session_id in session_to_channel:
                    del session_to_channel[session_id]
                logger.info(f"Cleaned up failed content stream session {session_id} for channel {channel_id}")
                
                # Clean up empty channel entries
                if channel_id in active_content_sessions and not active_content_sessions[channel_id]:
                    del active_content_sessions[channel_id]
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
@fastapi_app.get("/api/logo/{logo}")
async def logo(logo: str):
    logger.debug(f"Logo request received: {logo}")
    try:
        url = urlsafe_base64_decode(logo)
        logger.debug(f"Decoded URL: {url}")
        file = url.split("/")[-1]
        logger.debug(f"Filename: {file}")
    except Exception as e:
        logger.error(f"Error decoding logo URL: {str(e)}")
        return JSONResponse(content={"error": "Invalid logo URL"}, status_code=status.HTTP_400_BAD_REQUEST)
    
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
@fastapi_app.get("/api/health")
async def health():
    # Calculate streaming statistics and clean up stale entries
    current_time = time.time()
    stale_timeout = 300  # 5 minutes timeout for stale entries
    
    # Clean up stale M3U8 requests
    for channel_id in list(active_streams.keys()):
        # Remove stale entries (older than 5 minutes)
        stale_clients = [
            client_id for client_id, timestamp in active_streams[channel_id].items()
            if current_time - timestamp > stale_timeout
        ]
        for client_id in stale_clients:
            del active_streams[channel_id][client_id]
            logger.debug(f"Removed stale M3U8 client {client_id} from channel {channel_id}")
        
        # Remove empty channels
        if not active_streams[channel_id]:
            del active_streams[channel_id]
    
    # Clean up stale content streaming sessions
    for channel_id in list(active_content_sessions.keys()):
        # Remove stale sessions (older than 30 seconds for more real-time tracking)
        content_stale_timeout = 30  # 30 seconds for content sessions (shorter for more real-time tracking)
        stale_sessions = [
            session_id for session_id, timestamp in active_content_sessions[channel_id].items()
            if current_time - timestamp > content_stale_timeout
        ]
        for session_id in stale_sessions:
            del active_content_sessions[channel_id][session_id]
            if session_id in session_to_channel:
                del session_to_channel[session_id]
            logger.debug(f"Removed stale content session {session_id} from channel {channel_id}")
        
        # Remove empty channels
        if not active_content_sessions[channel_id]:
            del active_content_sessions[channel_id]
    
    # Calculate real streaming statistics from content sessions
    total_active_content_streams = sum(len(sessions) for sessions in active_content_sessions.values())
    total_m3u8_requests = sum(len(clients) for clients in active_streams.values())
    
    return {
        "status": "healthy",
        "channels_count": len(free_sky.channels),
        "cache_size": len(stream_cache),
        "active_channels": len(active_content_sessions),  # Channels with active video streaming
        "total_active_streams": total_active_content_streams,  # Real video streaming sessions
        "total_m3u8_requests": total_m3u8_requests,  # Playlist requests (for debugging)
        "max_concurrent_streams": max_concurrent_streams,
        "content_semaphore_limit": max_concurrent_streams * 2,  # Content streaming capacity
        "stream_utilization": f"{(total_active_content_streams / max_concurrent_streams) * 100:.1f}%",
        "content_sessions_per_channel": {ch: len(sessions) for ch, sessions in active_content_sessions.items()},
        "multithreading_mode": "full_concurrency",  # Every request = separate thread
        "session_tracking": "per_request",  # No IP-based limitations
        "connection_isolation": "per_stream",  # Each stream gets isolated HTTP client
        "connection_reuse": "disabled",  # No connection pooling for streams
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

@fastapi_app.get("/api/vidembed/{channel_id}")
async def vidembed_redirect(channel_id: str):
    """Redirect to vidembed URL for channels that use vidembed.re"""
    try:
        # Get the vidembed URL from the hybrid streaming class
        stream_data = await free_sky.stream(channel_id)
        
        if stream_data.startswith("VIDEMBED_URL:"):
            vidembed_url = stream_data.replace("VIDEMBED_URL:", "")
            logger.info(f"Redirecting channel {channel_id} to vidembed: {vidembed_url}")
            
            # Return a JSON response with the vidembed URL
            return JSONResponse(content={
                "type": "vidembed",
                "url": vidembed_url,
                "channel_id": channel_id
            })
        else:
            # This channel doesn't use vidembed, return error
            return JSONResponse(
                content={"error": "Channel does not use vidembed architecture"},
                status_code=status.HTTP_400_BAD_REQUEST
            )
            
    except Exception as e:
        logger.error(f"Error getting vidembed URL for channel {channel_id}: {str(e)}")
        return JSONResponse(
            content={"error": "Failed to get vidembed URL"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@fastapi_app.get("/api/services/status")
async def get_service_status():
    """Get status of all streaming services"""
    try:
        status = multi_streamer.get_service_status()
        return JSONResponse(content={
            "services": status,
            "enabled_count": len(multi_streamer.enabled_services),
            "total_count": len(multi_streamer.services)
        })
    except Exception as e:
        logger.error(f"Error getting service status: {str(e)}")
        return JSONResponse(
            content={"error": "Failed to get service status"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@fastapi_app.post("/api/services/{service_name}/enable")
async def enable_service(service_name: str):
    """Enable a streaming service"""
    try:
        multi_streamer.enable_service(service_name)
        return JSONResponse(content={
            "message": f"Service {service_name} enabled",
            "enabled_services": multi_streamer.enabled_services
        })
    except Exception as e:
        logger.error(f"Error enabling service {service_name}: {str(e)}")
        return JSONResponse(
            content={"error": f"Failed to enable service {service_name}"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@fastapi_app.post("/api/services/{service_name}/disable")
async def disable_service(service_name: str):
    """Disable a streaming service"""
    try:
        multi_streamer.disable_service(service_name)
        return JSONResponse(content={
            "message": f"Service {service_name} disabled",
            "enabled_services": multi_streamer.enabled_services
        })
    except Exception as e:
        logger.error(f"Error disabling service {service_name}: {str(e)}")
        return JSONResponse(
            content={"error": f"Failed to disable service {service_name}"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@fastapi_app.get("/api/channels/search")
async def search_channels(query: str = ""):
    """Search for channels across all services"""
    try:
        if not query:
            # Return all channels if no query provided
            channels = await multi_streamer.get_all_channels()
        else:
            channels = await multi_streamer.search_channels(query)
        
        return JSONResponse(content={
            "channels": channels,
            "count": len(channels),
            "query": query
        })
    except Exception as e:
        logger.error(f"Error searching channels: {str(e)}")
        return JSONResponse(
            content={"error": "Failed to search channels"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@fastapi_app.get("/api/channels/all")
async def get_all_channels():
    """Get all channels from all enabled services"""
    try:
        channels = await multi_streamer.get_all_channels()
        return JSONResponse(content={
            "channels": channels,
            "count": len(channels),
            "enabled_services": multi_streamer.enabled_services
        })
    except Exception as e:
        logger.error(f"Error getting all channels: {str(e)}")
        return JSONResponse(
            content={"error": "Failed to get channels"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

