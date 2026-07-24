import os
import asyncio
import glob
import httpx
import logging
import re
import time
from functools import lru_cache
from typing import Optional, Dict, Tuple, Set
from freesky.free_sky_hybrid import StepDaddyHybrid as StepDaddy
from freesky.free_sky import Channel
from fastapi import Response, status, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
# CORSMiddleware removed - CORS handled by Caddy
from .utils import urlsafe_base64_decode, encrypt, hls_ext, strip_hls_ext
from .vidembed_extractor import extract_hls_from_vidembed
from .multi_service_streamer import multi_streamer
from .stream_monitor import stream_monitor
from . import channel_prefs
from . import users
from . import app_settings
import json
from urllib.parse import urljoin, urlparse, urlunparse
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
max_concurrent_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "25"))  # Increased from 5 to 25 for better throughput
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

# Create HTTP client with settings optimized for high-performance streaming
# This client is used for non-streaming requests (logos, keys, etc.)
client = httpx.AsyncClient(
    http2=True,
    timeout=httpx.Timeout(30.0, connect=5.0),  # Faster connection timeouts
    limits=httpx.Limits(
        max_keepalive_connections=100,  # Increased for better connection reuse
        max_connections=500,            # Significantly increased for high load
        keepalive_expiry=120.0          # Longer keepalive for better reuse
    ),
    follow_redirects=True
)

# Global persistent streaming client for better connection reuse
streaming_client = httpx.AsyncClient(
    http2=True,   # Enable HTTP/2 for better streaming performance
    timeout=httpx.Timeout(45.0, connect=3.0),  # Aggressive timeouts for fast streams
    limits=httpx.Limits(
        max_keepalive_connections=200,  # Large connection pool for persistent connections
        max_connections=1000,           # Very high limit for concurrent streaming
        keepalive_expiry=180.0          # Long keepalive for persistent streaming
    ),
    follow_redirects=True
)

free_sky = StepDaddy()

# Bootstrap the first admin at import. Reflex owns the ASGI lifespan, so
# @fastapi_app.on_event("startup") never fires here — doing it at import is what
# actually runs in the serving process. No-ops once any user exists.
try:
    _generated_pw = users.ensure_admin()
    if _generated_pw:
        # Printed once, at first boot only. Shown loudly because it is the only
        # time this value is ever available — it is stored hashed.
        logger.warning(
            "=" * 72
            + f"\n  FIRST RUN: created admin user '{os.environ.get('ADMIN_USER', 'admin')}'"
            + f"\n  PASSWORD: {_generated_pw}"
            + "\n  Save it now and change it in Settings. Set ADMIN_PASS to pick your own.\n"
            + "=" * 72
        )
    logger.info(f"{len(users.list_users())} user(s) configured")
except Exception as e:
    logger.error(f"Could not bootstrap admin user: {e}")

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
stream_cache = LRUCache(maxsize=max_concurrent_streams * 15)  # Further increased cache size
cache_ttl = 90  # Increased to 90 seconds for longer-lived cache entries

# Advanced segment prefetching cache
segment_cache = LRUCache(maxsize=500)  # Cache for prefetched segments
segment_cache_ttl = 300  # 5 minutes for segments

# Track active tasks and streaming sessions for cleanup
active_tasks: Dict[str, asyncio.Task] = {}
active_streams: Dict[str, Dict[str, float]] = {}  # Track M3U8 requests per channel with timestamps
active_content_sessions: Dict[str, Dict[str, float]] = {}  # Track actual video streaming sessions
session_to_channel: Dict[str, str] = {}  # Map session IDs to channel IDs

async def _get_stream_parallel(channel_id: str, prefer: str = None):
    """Get stream using parallel approach to multiple services with monitoring.

    `prefer` forces one upstream feed (see PLAYER_PATHS) — used by the watch
    page's manual feed switcher to test a specific source. It overrides any
    admin-pinned source for this request only.
    """
    start_time = time.time()

    try:
        # Check if channel should be skipped due to recent failures
        if stream_monitor.should_skip_channel(channel_id):
            logger.warning(f"Skipping channel {channel_id} due to recent failures")
            stream_monitor.record_stream_attempt(channel_id, False, 0.0)
            return None

        # Create multiple tasks for different streaming approaches
        tasks = []
        # A manual feed pick (watch-page switcher) forces exactly one feed; an
        # admin pin only sets first-try order and still audio-fails-over.
        manual = bool(prefer)
        pinned = prefer or channel_prefs.source_for(channel_id)

        # Task 1: Primary DLHD service. Skipped when the admin pinned a source —
        # this path ignores the preference, so racing it would sometimes hand back
        # a different player than the one that was chosen.
        if not pinned:
            tasks.append(asyncio.create_task(
                multi_streamer.get_stream(channel_id, "DLHD"),
                name="dlhd_primary"
            ))

        # Task 2: Direct channel processing (bypass multi-streamer). Honour any
        # source the admin pinned for this channel — the resolver tries it first
        # and still falls back to the others if it is down. A manual pick resolves
        # ONLY that feed so the viewer hears exactly what it carries.
        tasks.append(asyncio.create_task(
            free_sky.stream(channel_id, prefer=pinned, single_feed=manual),
            name="direct_stream"
        ))

        # Task 3: Try alternative services if enabled — but never for a manual pick,
        # which must resolve only the chosen feed.
        if not manual and len(multi_streamer.enabled_services) > 1:
            for service in multi_streamer.enabled_services[1:2]:  # Try one alternative
                tasks.append(asyncio.create_task(
                    multi_streamer.get_stream(channel_id, service),
                    name=f"alt_{service.lower()}"
                ))
        
        # Wait for the first task that actually SUCCEEDS, not the first that finishes.
        # FIRST_COMPLETED alone cancelled the healthy task whenever a dead service
        # errored out first, so every channel 404'd on the fastest failure.
        pending = set(tasks)
        # 8s cut off channels that resolve correctly but whose first upstream hop
        # slows to ~7s under load, turning working streams into false 504s. 13s
        # stays under the outer 15s wait_for while giving the iframe-chain failover
        # room to try more than one player.
        deadline = time.time() + 13.0

        while pending:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=remaining
            )
            if not done:
                break  # timed out

            for task in done:
                try:
                    result = await task
                except Exception as e:
                    logger.debug(f"Parallel task {task.get_name()} failed: {str(e)}")
                    continue
                if result:
                    for other in pending:
                        other.cancel()
                    response_time = time.time() - start_time
                    stream_monitor.record_stream_attempt(channel_id, True, response_time)
                    logger.info(f"Parallel stream success from {task.get_name()} in {response_time:.2f}s")
                    return result

        # Nothing succeeded within the deadline — drop whatever is still running.
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


        # If all parallel attempts failed, try sequential fallback
        logger.warning("All parallel attempts failed, trying sequential fallback")
        fallback_result = await multi_streamer.get_stream(channel_id)
        
        if fallback_result:
            response_time = time.time() - start_time
            stream_monitor.record_stream_attempt(channel_id, True, response_time)
            return fallback_result
        else:
            stream_monitor.record_stream_attempt(channel_id, False, 0.0)
            return None
        
    except Exception as e:
        logger.error(f"Error in parallel stream fetch: {str(e)}")
        stream_monitor.record_stream_attempt(channel_id, False, 0.0)
        # Final fallback to original method
        try:
            fallback_result = await multi_streamer.get_stream(channel_id)
            if fallback_result:
                response_time = time.time() - start_time
                stream_monitor.record_stream_attempt(channel_id, True, response_time)
            return fallback_result
        except:
            return None

async def prefetch_segments(m3u8_content: str, channel_id: str):
    """Prefetch the first few segments of a stream for faster playback"""
    try:
        lines = m3u8_content.split('\n')
        segment_urls = []
        
        # Extract the first 3 segments for prefetching
        for line in lines:
            if line.startswith('/api/content/') and len(segment_urls) < 3:
                segment_urls.append(line.strip())
        
        if not segment_urls:
            return
        
        logger.info(f"Prefetching {len(segment_urls)} segments for channel {channel_id}")
        
        # Prefetch segments concurrently
        async def prefetch_segment(segment_url: str):
            try:
                segment_key = f"seg_{hash(segment_url)}"
                current_time = time.time()
                
                # Check if already cached
                if segment_key in segment_cache:
                    cached_data, cache_time = segment_cache[segment_key]
                    if current_time - cache_time < segment_cache_ttl:
                        return
                
                # Use the streaming client to prefetch
                full_url = f"{api_url}{segment_url}"
                response = await streaming_client.get(full_url, timeout=5.0)
                
                if response.status_code == 200:
                    segment_cache[segment_key] = (response.content, current_time)
                    logger.debug(f"Prefetched segment {segment_url[:50]}...")
                    
            except Exception as e:
                logger.debug(f"Failed to prefetch segment {segment_url}: {str(e)}")
        
        # Prefetch in parallel but don't wait for completion
        tasks = [asyncio.create_task(prefetch_segment(url)) for url in segment_urls]
        
        # Don't await - let prefetching happen in background
        asyncio.gather(*tasks, return_exceptions=True)
        
    except Exception as e:
        logger.debug(f"Error in segment prefetching: {str(e)}")

async def prefetch_popular_stream(channel_id: str):
    """Prefetch stream to keep cache warm for popular channels"""
    try:
        await asyncio.sleep(45)  # Wait 45s then refresh cache
        cache_key = f"stream_{channel_id}"
        current_time = time.time()
        
        # Check if cache needs refresh
        if cache_key in stream_cache:
            cached_data, cache_time = stream_cache[cache_key]
            if current_time - cache_time < cache_ttl:
                return  # Still fresh
        
        # Prefetch new stream data
        logger.debug(f"Prefetching stream for popular channel {channel_id}")
        stream_data = await asyncio.wait_for(
            multi_streamer.get_stream(channel_id),
            timeout=8.0  # Quick prefetch timeout
        )
        
        if stream_data and stream_data.startswith("VIDEMBED_URL:"):
            vidembed_url = stream_data.replace("VIDEMBED_URL:", "")
            try:
                hls_data = await asyncio.wait_for(
                    extract_hls_from_vidembed(vidembed_url),
                    timeout=6.0  # Quick HLS extraction
                )
                if hls_data:
                    stream_data = _process_stream_content(hls_data, vidembed_url)
            except asyncio.TimeoutError:
                pass  # Use vidembed URL as fallback
        
        if stream_data:
            stream_cache[cache_key] = (stream_data, current_time)
            logger.debug(f"Successfully prefetched stream for channel {channel_id}")
            
    except Exception as e:
        logger.debug(f"Prefetch failed for channel {channel_id}: {str(e)}")
        # Silent failure for prefetch

def _process_stream_content(content: str, referer: str) -> str:
    """Process stream content for proxying"""
    if content.startswith('#EXTM3U'):
        # Process M3U8 playlists
        lines = content.split('\n')
        processed_lines = []
        
        for line in lines:
            if line.startswith('http') and config.proxy_content:
                # Proxy content URLs
                line = f"/api/content/{encrypt(line)}{hls_ext(line)}"
            elif line.startswith('#EXT-X-MEDIA:') and config.proxy_content:
                # Separate audio/subtitle renditions carry their playlist in a
                # URI="..." attr, not on their own line. Without this, ffmpeg
                # (Dispatcharr) can't reach the audio track -> video-only stream.
                m = re.search(r'URI="(https?://.*?)"', line)
                if m:
                    uri = m.group(1)
                    line = line.replace(uri, f"/api/content/{encrypt(uri)}{hls_ext(uri)}")
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

# Concurrency control for streaming with high-performance limits
_stream_semaphore = asyncio.Semaphore(max_concurrent_streams)
_content_semaphore = asyncio.Semaphore(max_concurrent_streams * 10)  # Increased to 10x for much better segment throughput

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
    
    # Close HTTP clients with timeout
    try:
        await asyncio.wait_for(client.aclose(), timeout=10.0)
        logger.info("Main HTTP client closed successfully")
    except asyncio.TimeoutError:
        logger.warning("Main HTTP client close timed out")
    except Exception as e:
        logger.error(f"Error closing main HTTP client: {e}")
    
    try:
        await asyncio.wait_for(streaming_client.aclose(), timeout=10.0)
        logger.info("Streaming HTTP client closed successfully")
    except asyncio.TimeoutError:
        logger.warning("Streaming HTTP client close timed out")
    except Exception as e:
        logger.error(f"Error closing streaming HTTP client: {e}")
    
    logger.info("Shutdown procedure completed")

@fastapi_app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response


# Paths that stream content and must carry a valid per-user token. Everything a
# player fetches after the playlist (segments via /api/content, keys) is covered,
# because Dispatcharr and VLC send no cookie — the token rides in the URL. Auth is
# only enforced when at least one user exists, so a fresh install is usable until
# the admin is bootstrapped.
_TOKEN_PROTECTED = (
    "/playlist.m3u8", "/api/playlist.m3u8",
    "/api/stream/", "/stream/",
    "/api/content/", "/content/",
    "/api/key/", "/key/",
    "/epg.xml", "/api/epg.xml",
)


@fastapi_app.middleware("http")
async def require_stream_token(request: Request, call_next):
    path = request.url.path
    if request.method == "GET" and path.startswith(_TOKEN_PROTECTED):
        # No users yet → app is unconfigured, don't lock the owner out.
        if users.list_users():
            client_ip = app_settings.client_ip_from_headers(
                request.headers, request.client.host if request.client else ""
            )
            # A whitelisted subnet (the LAN) may pull the playlist without a
            # token, so an existing Dispatcharr source keeps working.
            if not app_settings.is_trusted_ip(client_ip):
                token = request.query_params.get("token", "")
                if users.user_by_token(token) is None:
                    # Generic 401, no WWW-Authenticate realm and no hint which
                    # part failed — same response for missing, wrong or revoked.
                    return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    return await call_next(request)

# Add OPTIONS handler for streaming endpoints to handle CORS preflight requests
# OPTIONS handler removed - CORS preflight handled by Caddy

def _public_base(request: Request) -> str:
    """The scheme://host:port the client actually used to reach us.

    Prefers the forwarded headers a reverse proxy sets, then the Host header,
    and only falls back to the configured API_URL. Without this, a client coming
    in through NAT on a different external port (or any reverse proxy) received a
    playlist full of internal LAN URLs it could not resolve.
    """
    try:
        headers = request.headers
        host = headers.get("x-forwarded-host") or headers.get("host")
        if not host:
            return api_url
        proto = headers.get("x-forwarded-proto") or request.url.scheme or "http"
        # X-Forwarded-* may be a comma-separated chain; the left-most is the client's.
        host = host.split(",")[0].strip()
        proto = proto.split(",")[0].strip()
        return f"{proto}://{host}"
    except Exception:
        return api_url


def _authorize_proxied_urls(content: str, token: str) -> str:
    """Carry the caller's token onto every proxied URL inside a playlist.

    Segments and keys are fetched by the player as separate requests with no
    cookie, so without this the middleware would 401 everything after the
    playlist itself. Done here, after generation, so both playlist rewriters are
    covered by one rule.
    """
    if not token:
        return content
    out = []
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("/api/content/", "/api/key/", "/content/", "/key/")) and "token=" not in stripped:
            sep = "&" if "?" in stripped else "?"
            line = f"{stripped}{sep}token={token}"
        elif stripped.startswith("#EXT-X-KEY:") and "/api/key/" in stripped and "token=" not in stripped:
            line = re.sub(r'URI="([^"]+)"',
                          lambda m: f'URI="{m.group(1)}{"&" if "?" in m.group(1) else "?"}token={token}"',
                          line)
        out.append(line)
    return "\n".join(out)


@fastapi_app.get("/stream/{channel_id}.m3u8")
@fastapi_app.get("/api/stream/{channel_id}.m3u8")
async def stream(channel_id: str, request: Request = None):
    stream_token = request.query_params.get("token") if request else None
    # Manual feed override from the watch-page switcher. Bypasses the shared cache
    # so each pick re-resolves that specific upstream feed instead of returning
    # whatever feed happens to be cached for this channel.
    prefer = request.query_params.get("player") if request else None
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

        if not prefer and cache_key in stream_cache:
            cached_data, cached_time = stream_cache[cache_key]
            if current_time - cached_time < cache_ttl:
                logger.info(f"Serving cached stream for channel {channel_id} to client {client_id}")
                return Response(
                    content=_authorize_proxied_urls(cached_data, stream_token),
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
            if not prefer and cache_key in stream_cache:
                cached_data, cached_time = stream_cache[cache_key]
                if current_time - cached_time < cache_ttl:
                    logger.info(f"Serving freshly cached stream for channel {channel_id} to client {client_id}")
                    return Response(
                        content=_authorize_proxied_urls(cached_data, stream_token),
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
                
                # Use parallel multi-service streaming for faster response
                stream_data = await asyncio.wait_for(
                    _get_stream_parallel(channel_id, prefer=prefer),
                    timeout=15.0  # Aggressive timeout for parallel approach
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
                        # Extract HLS stream from vidembed with aggressive timeout
                        hls_data = await asyncio.wait_for(
                            extract_hls_from_vidembed(vidembed_url),
                            timeout=4.0  # Very aggressive timeout for immediate fallback
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
                
                # Cache the processed stream data — but never a manual feed
                # override, so it doesn't become the channel's default for everyone.
                if not prefer:
                    stream_cache[cache_key] = (stream_data, current_time)
                logger.info(f"Successfully generated stream for channel {channel_id}")
                
                # Schedule prefetch for this channel to keep it warm
                asyncio.create_task(prefetch_popular_stream(channel_id))
                
                # Start segment prefetching for faster playback
                if stream_data and stream_data.startswith('#EXTM3U'):
                    asyncio.create_task(prefetch_segments(stream_data, channel_id))
                
                return Response(
                    content=_authorize_proxied_urls(stream_data, stream_token),
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Cache-Control": "max-age=30, public",  # Allow 30s caching for performance
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

def _upstream_headers(ref: str = None) -> dict:
    """Headers for CDN fetches. The CDN 403s any request whose Referer is not the
    embedding player page, so replay the one baked into the URL by the rewriter."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
    }
    if ref:
        try:
            referer = free_sky.content_url(ref)
            headers["Referer"] = referer
            headers["Origin"] = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
        except Exception as ref_error:
            logger.warning(f"Could not decode content referer: {ref_error}")
    return headers


@fastapi_app.get("/api/content/{path}/{ref}")
@fastapi_app.get("/api/content/{path}")
async def content(path: str, request: Request, ref: str = None):
    current_time = time.time()
    session_id = None
    channel_id = None

    # The .ts/.m3u8 suffix exists only to satisfy ffmpeg's URL filters; it sits on
    # whichever component is last, so strip it off both before decrypting.
    if ref is not None:
        ref = strip_hls_ext(ref)
    else:
        path = strip_hls_ext(path)

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

            upstream_url = free_sky.content_url(path)
            upstream_headers = _upstream_headers(ref)

            # A nested playlist has to be rewritten, not streamed through: its segment
            # URLs are on a CDN that 403s any cross-origin browser fetch, so the player
            # can only reach them via this proxy.
            if ".m3u8" in upstream_url.split("?")[0]:
                nested = await streaming_client.get(upstream_url, headers=upstream_headers, timeout=30.0)
                if nested.status_code != 200:
                    raise ValueError(f"Upstream returned HTTP {nested.status_code}")
                # The URL is only a hint: this CDN also serves binary segments from
                # paths containing ".m3u8", and decoding those as text raised
                # UnicodeDecodeError -> 500. Trust the body, not the name.
                if not nested.content.startswith(b"#EXTM3U"):
                    return Response(
                        content=nested.content,
                        media_type=nested.headers.get("content-type", "application/octet-stream"),
                        headers={"Access-Control-Allow-Origin": "*"},
                    )
                referer = free_sky.content_url(ref) if ref else upstream_url
                rewritten = free_sky._process_stream_content(
                    "\n".join(
                        urljoin(upstream_url, line) if line and not line.startswith("#") else line
                        for line in nested.text.split("\n")
                    ),
                    referer,
                )
                # Carry the caller's token onto this playlist's segment URLs too,
                # or the auth middleware 401s every segment the player then asks for.
                rewritten = _authorize_proxied_urls(
                    rewritten, request.query_params.get("token")
                )
                return Response(
                    content=rewritten,
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                    },
                )

            async def proxy_stream():
                last_heartbeat = time.time()
                chunk_count = 0
                
                try:
                    logger.info(f"Using persistent connection pool for stream session {session_id}")
                    
                    # Use persistent streaming client with aggressive timeout
                    async with asyncio.timeout(30.0):  # Reduced timeout for faster failure detection
                        async with streaming_client.stream("GET", upstream_url, headers=upstream_headers, timeout=30.0) as response:
                            logger.info(f"Stream session {session_id} established connection (status: {response.status_code})")
                            if response.status_code != 200:
                                # Surfacing this beats streaming an empty 200 body, which
                                # looked like success and hid the 403 entirely.
                                raise ValueError(f"Upstream returned HTTP {response.status_code}")

                            # Use larger chunk size for better throughput
                            async for chunk in response.aiter_bytes(chunk_size=512 * 1024):  # 512KB chunks for optimal performance
                                chunk_count += 1
                                current_chunk_time = time.time()
                                
                                # Update session timestamp less frequently to reduce overhead
                                if current_chunk_time - last_heartbeat > 5:
                                    if channel_id in active_content_sessions and session_id in active_content_sessions[channel_id]:
                                        active_content_sessions[channel_id][session_id] = current_chunk_time
                                        last_heartbeat = current_chunk_time
                                
                                yield chunk
                                
                    logger.info(f"Stream session {session_id} completed normally after {chunk_count} chunks")
                except asyncio.TimeoutError:
                    logger.warning(f"Stream session {session_id} timed out after 30 seconds")
                    raise
                except Exception as e:
                    logger.error(f"Error in persistent proxy stream for session {session_id}: {str(e)}")
                    raise
                finally:
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
            
            return StreamingResponse(
                proxy_stream(), 
                media_type="application/octet-stream",
                headers={
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Expose-Headers": "*",
                    "Cache-Control": "public, max-age=3600",
                    "Accept-Ranges": "bytes",
                    "Transfer-Encoding": "chunked",
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
                        # Don't block the update loop on it; already-cached logos
                        # make later passes nearly free.
                        asyncio.create_task(warm_logo_cache())
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
                        free_sky.channels = [Channel.from_dict(channel_data) for channel_data in fallback_data]
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
                channels = [Channel.from_dict(channel_data) for channel_data in fallback_data]
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
def playlist(request: Request):
    """Return the playlist as a response.

    The caller's token is echoed into every stream URL so the player, which
    sends no cookie, stays authorised for the rest of the session.
    """
    return Response(
        content=free_sky.playlist(exclude=channel_prefs.disabled_ids(),
                                  token=request.query_params.get("token"),
                                  base_url=_public_base(request)),
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
def api_playlist(request: Request):
    """Return the playlist as a response (API endpoint)"""
    return Response(
        content=free_sky.playlist(exclude=channel_prefs.disabled_ids(),
                                  token=request.query_params.get("token"),
                                  base_url=_public_base(request)),
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
    try:
        return await free_sky.schedule()
    except Exception as e:
        logger.warning(f"Error getting schedule from upstream: {str(e)}")
        # Return fallback empty schedule
        return {}

# Logo URLs that returned nothing, so we stop re-probing them on every page view.
_logo_misses = LRUCache(maxsize=2000)


def _missing_logo():
    """Serve the placeholder image itself, not a JSON 404 that renders as a
    broken image in players and the channel grid."""
    if os.path.exists("./assets/missing.png"):
        return FileResponse("./assets/missing.png", headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=status.HTTP_404_NOT_FOUND)


async def _cache_logo(url: str) -> Optional[str]:
    """Fetch a logo into ./logo-cache and return its path, or None.

    Shared by the HTTP route and the background warmer so both agree on
    extension fallback and on what counts as a miss.
    """
    os.makedirs("./logo-cache", exist_ok=True)

    # Cached under whatever extension actually served, which may differ from the
    # one requested — FileResponse picks the content-type off that extension, so
    # storing an SVG as .png would ship it as image/png and render as nothing.
    file_stem = url.split("/")[-1].rsplit(".", 1)[0]
    cached = glob.glob(f"./logo-cache/{glob.escape(file_stem)}.*")
    if cached:
        return cached[0]
    if url in _logo_misses:
        return None

    # Upstream stores logos as .png, .jpg or .svg with no way to tell which from
    # the channel name, so try the siblings before declaring a miss.
    candidates = [url]
    if url.rsplit(".", 1)[-1].lower() in ("png", "jpg", "jpeg", "svg"):
        url_stem = url.rsplit(".", 1)[0]
        candidates += [f"{url_stem}.{ext}" for ext in ("jpg", "svg", "png") if f"{url_stem}.{ext}" != url]

    errored = False
    for candidate in candidates:
        try:
            response = await client.get(
                candidate,
                headers={"user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"}
            )
        except Exception as e:
            # A timeout is not proof the logo is absent. 900 channels warming a
            # cold cache at once produces plenty of them, and blacklisting on one
            # would lose that logo until the next restart.
            logger.warning(f"Error fetching logo {candidate}: {str(e)}")
            errored = True
            continue
        if response.status_code == 200:
            ext = candidate.rsplit(".", 1)[-1].lower()
            cache_path = f"./logo-cache/{file_stem}.{ext}"
            with open(cache_path, "wb") as f:
                f.write(response.content)
            return cache_path

    # Only remember misses upstream actually answered (a real 404). Without this
    # every page view re-probes 3 dead URLs per logo-less channel; with it applied
    # to timeouts too, a slow moment would blacklist a perfectly good logo.
    if not errored:
        _logo_misses[url] = True
    return None


async def warm_logo_cache():
    """Pull every channel logo into the cache in the background.

    An IPTV client importing playlist.m3u8 asks for all ~900 tvg-logo URLs at
    once. Against a cold cache each is an upstream round-trip, so most time out
    and the client renders no artwork at all. Warming ahead of that turns the
    import into local file reads. Already-cached logos cost nothing, so this is
    cheap to re-run.
    """
    sem = asyncio.Semaphore(8)  # upstream is not worth hammering

    async def one(url: str):
        async with sem:
            try:
                await _cache_logo(url)
            except Exception as e:
                logger.debug(f"Logo warm failed for {url}: {e}")

    urls = []
    for ch in free_sky.channels:
        if ch.logo and ch.logo.startswith("/api/logo/"):
            try:
                urls.append(urlsafe_base64_decode(ch.logo.rsplit("/", 1)[-1]))
            except Exception:
                continue
    if not urls:
        return
    start = time.time()
    await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    logger.info(f"Logo cache warm complete: {len(urls)} logos in {time.time() - start:.1f}s")


@fastapi_app.get("/logo/{logo}")
@fastapi_app.get("/api/logo/{logo}")
async def logo(logo: str):
    try:
        url = urlsafe_base64_decode(logo)
    except Exception as e:
        logger.error(f"Error decoding logo URL: {str(e)}")
        return JSONResponse(content={"error": "Invalid logo URL"}, status_code=status.HTTP_400_BAD_REQUEST)

    path = await _cache_logo(url)
    if path:
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
    return _missing_logo()

@fastapi_app.post("/api/channels/refresh")
@fastapi_app.post("/channels/refresh")
async def refresh_channels_endpoint():
    """Re-scrape the channel list from upstream on demand.

    The list normally refreshes every 5 minutes; this lets the settings page
    force it (e.g. after the upstream site adds channels) without a restart.
    """
    before = len(free_sky.channels)
    await free_sky.load_channels()
    stream_cache.clear()
    asyncio.create_task(warm_logo_cache())
    return {"status": "ok", "before": before, "after": len(free_sky.channels)}


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
    
    # Clean up old stream monitoring metrics
    stream_monitor.cleanup_old_metrics(24)  # Clean up metrics older than 24 hours
    
    # Calculate real streaming statistics from content sessions
    total_active_content_streams = sum(len(sessions) for sessions in active_content_sessions.values())
    total_m3u8_requests = sum(len(clients) for clients in active_streams.values())
    
    # Get stream monitoring metrics
    monitor_metrics = stream_monitor.get_metrics_summary()
    
    return {
        "status": "healthy",
        "channels_count": len(free_sky.channels),
        "cache_size": len(stream_cache),
        "segment_cache_size": len(segment_cache),
        "active_channels": len(active_content_sessions),  # Channels with active video streaming
        "total_active_streams": total_active_content_streams,  # Real video streaming sessions
        "total_m3u8_requests": total_m3u8_requests,  # Playlist requests (for debugging)
        "max_concurrent_streams": max_concurrent_streams,
        "content_semaphore_limit": max_concurrent_streams * 10,  # Content streaming capacity
        "stream_utilization": f"{(total_active_content_streams / max_concurrent_streams) * 100:.1f}%",
        "content_sessions_per_channel": {ch: len(sessions) for ch, sessions in active_content_sessions.items()},
        "multithreading_mode": "full_concurrency",  # Every request = separate thread
        "session_tracking": "per_request",  # No IP-based limitations
        "connection_pooling": "persistent",  # Persistent connection pooling enabled
        "parallel_streaming": "enabled",  # Parallel stream fetching enabled
        "stream_monitoring": monitor_metrics,  # Stream health monitoring
        "uptime": time.time()
    }

@fastapi_app.get("/channels")
@fastapi_app.get("/api/channels")
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

def _filter_schedule_to_enabled(schedule: dict) -> dict:
    """Keep only what the user can actually watch.

    A channel qualifies when it is BOTH in the current channel list AND not
    switched off in settings. Checking only the disabled list wasn't enough: the
    schedule cites ~280 ids that exist nowhere in the channel list (PPV/event
    slots), which accounted for ~200 events advertising channels you can neither
    select nor play. Events left with no qualifying channel are dropped.
    """
    if not isinstance(schedule, dict):
        return {}
    disabled = channel_prefs.disabled_ids()
    known = {c.id for c in (get_channels() or [])}
    # If channels haven't loaded yet, an empty `known` would blank the whole
    # schedule; fall back to the disabled list alone until they arrive.
    enabled = ({c for c in known if c not in disabled} if known else None)

    def keeps(channel_id: str) -> bool:
        cid = str(channel_id)
        return cid not in disabled if enabled is None else cid in enabled

    out = {}
    for day, categories in schedule.items():
        if not isinstance(categories, dict):
            continue
        day_out = {}
        for category, events in categories.items():
            kept = []
            for event in events or []:
                channels = [
                    c for c in (event.get("channels") or [])
                    if keeps(c.get("channel_id", ""))
                ]
                if channels:
                    kept.append({**event, "channels": channels})
            if kept:
                day_out[category] = kept
        if day_out:
            out[day] = day_out
    return out


@fastapi_app.get("/schedule")
@fastapi_app.get("/api/schedule")
async def schedule_endpoint():
    """Get schedule data as JSON, limited to enabled channels.

    ponytail: no fabricated fallback. This used to invent a week of "Sports
    Event 1"/"News Hour 1" entries whenever upstream failed, which looked like a
    working schedule and hid the fact that there is no data. Report empty.
    """
    try:
        schedule_data = await get_schedule()
    except Exception as e:
        logger.error(f"Error in schedule endpoint: {str(e)}")
        return {"schedule": {}, "status": "unavailable", "error": str(e)}
    filtered = _filter_schedule_to_enabled(schedule_data or {})
    return {
        "schedule": filtered,
        "status": "success" if filtered else "unavailable",
    }

@fastapi_app.get("/epg.xml")
@fastapi_app.get("/api/epg.xml")  
async def epg_xml():
    """Return EPG data in XML format for external sources consuming channel data."""
    try:
        schedule_data = await get_schedule()
        
        # Handle empty schedule data gracefully
        if not schedule_data:
            logger.info("No schedule data available, generating minimal EPG")
            schedule_data = {}
        
        # Create XML EPG format
        xml_content = generate_epg_xml(schedule_data)
        
        return Response(
            content=xml_content,
            media_type="application/xml",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache", 
                "Expires": "0",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*"
            }
        )
    except Exception as e:
        logger.error(f"Error generating EPG XML: {str(e)}")
        # Return a basic EPG XML instead of JSON error
        fallback_xml = generate_fallback_epg_xml()
        return Response(
            content=fallback_xml,
            media_type="application/xml",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache", 
                "Expires": "0",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*"
            }
        )

def generate_fallback_epg_xml():
    """Generate a minimal fallback EPG XML when schedule data is unavailable."""
    from xml.sax.saxutils import escape
    
    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml_lines.append('<tv>')
    
    # Get all channels for the channel list
    channels = get_channels()
    
    # Add channel definitions only
    for channel in channels[:50]:  # Limit to first 50 channels for fallback
        xml_lines.append(f'  <channel id="{escape(channel.id)}">')
        xml_lines.append(f'    <display-name>{escape(channel.name)}</display-name>')
        if channel.logo:
            xml_lines.append(f'    <icon src="{escape(channel.logo)}" />')
        xml_lines.append('  </channel>')
    
    xml_lines.append('</tv>')
    return '\n'.join(xml_lines)

def generate_epg_xml(schedule_data):
    """Generate XML EPG format from schedule data."""
    from xml.sax.saxutils import escape
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    
    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml_lines.append('<tv>')
    
    # Get all channels for the channel list
    channels = get_channels()
    channel_dict = {ch.id: ch for ch in channels}
    
    # Add channel definitions
    for channel in channels:
        xml_lines.append(f'  <channel id="{escape(channel.id)}">')
        xml_lines.append(f'    <display-name>{escape(channel.name)}</display-name>')
        if channel.logo:
            xml_lines.append(f'    <icon src="{escape(channel.logo)}" />')
        xml_lines.append('  </channel>')
    
    # Add programme data
    for day_name, categories in schedule_data.items():
        # Parse day from format "DD/MM/YYYY - DayName"
        try:
            date_part = day_name.split(" - ")[0]
            day_date = datetime.strptime(date_part, "%d/%m/%Y").replace(tzinfo=ZoneInfo("UTC"))
        except:
            continue
            
        for category, events in categories.items():
            for event in events:
                try:
                    # Parse time
                    time_str = event.get("time", "00:00")
                    hour, minute = map(int, time_str.split(":"))
                    start_dt = day_date.replace(hour=hour, minute=minute)
                    
                    # Assume 30 minute programs if no end time specified
                    end_dt = start_dt + timedelta(minutes=30)
                    
                    # Get channels for this event
                    event_channels = event.get("channels", [])
                    if event.get("channels2"):
                        event_channels.extend(event.get("channels2", []))
                    
                    # Create programme entry for each channel
                    for channel_info in event_channels:
                        channel_id = channel_info.get("channel_id", "")
                        if channel_id and channel_id in channel_dict:
                            start_time = start_dt.strftime("%Y%m%d%H%M%S %z")
                            end_time = end_dt.strftime("%Y%m%d%H%M%S %z")
                            
                            xml_lines.append(f'  <programme start="{start_time}" stop="{end_time}" channel="{escape(channel_id)}">')
                            xml_lines.append(f'    <title>{escape(event.get("event", ""))}</title>')
                            xml_lines.append(f'    <category>{escape(category)}</category>')
                            xml_lines.append('  </programme>')
                except Exception as e:
                    logger.debug(f"Error processing event {event}: {e}")
                    continue
    
    xml_lines.append('</tv>')
    return '\n'.join(xml_lines)

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

