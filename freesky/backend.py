import os
import asyncio
import contextlib
import sys
import base64
import glob
import httpx
import logging
import re
import time
from functools import lru_cache
from typing import Optional, Dict, Set
from rxconfig import config
from freesky.free_sky_hybrid import StepDaddyHybrid as StepDaddy
from freesky.free_sky import Channel
from fastapi import Response, status, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
# CORSMiddleware removed - CORS handled by Caddy
from .utils import urlsafe_base64_decode, encrypt, hls_ext, strip_hls_ext
from .vidembed_extractor import extract_hls_from_vidembed
from .multi_service_streamer import multi_streamer
from .stream_monitor import stream_monitor
from . import channel_prefs
from . import users
from . import app_settings
from . import virtual_channels
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
    """Rewrite an M3U8 so segments and keys are fetched through our proxy.

    Note: `config` here is the Reflex config, imported at the top of this
    module. It used to be missing entirely, so every call that reached the
    `config.proxy_content` check below raised NameError — which surfaced as a
    dead channel on the vidembed and fallback paths that use this function.
    """
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
    # Virtual-channel segments are fetched by the player as bare GETs with no
    # cookie, exactly like /api/content, so they need the same token rule.
    "/api/virtual/",
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

    # A virtual channel is produced locally by a browser session rather than
    # fetched from upstream, so none of the resolve/cache/failover machinery
    # below applies. Handled first, and matched on the id prefix so this costs a
    # string compare for every ordinary channel.
    if virtual_channels.is_virtual_id(channel_id):
        return await _virtual_stream(channel_id, request, stream_token)

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

def virtual_channel_objects():
    """The admin's virtual channels as Channel objects.

    Built fresh on every call rather than cached: the store is a small JSON file
    and an admin who just added a channel expects to see it without a restart.
    `stream_type` is "virtual" so the watch page and the M3U8 route can tell
    these apart from a proxied upstream feed without consulting the store.
    """
    try:
        return [
            Channel(
                id=virtual_channels.channel_id(record["name"]),
                name=record["title"],
                tags=record["tags"],
                logo=record["logo"],
                stream_type="virtual",
            )
            for record in virtual_channels.list_channels()
            if record["enabled"]
        ]
    except Exception as e:
        logger.error(f"Error building virtual channels: {e}", exc_info=True)
        return []


def get_channels():
    """Get current channels with fallback handling.

    Virtual channels are appended to whichever list wins below, including the
    fallback one — they are stored locally, so they are exactly the channels that
    should still work when the upstream scrape is down.
    """
    return _upstream_channels() + virtual_channel_objects()


def _upstream_channels():
    """Channels from the scraped upstream source, with fallback handling."""
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
                                  base_url=_public_base(request),
                                  extra=virtual_channel_objects()),
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
                                  base_url=_public_base(request),
                                  extra=virtual_channel_objects()),
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



# --- virtual channels -------------------------------------------------------
# A virtual channel is a web page restreamed as HLS. The heavy lifting (Xvfb,
# Chromium, PulseAudio, ffmpeg) lives in virtual_session; these routes are the
# thin HTTP surface over it.
#
# virtual_session is imported lazily inside each handler rather than at module
# import. It pulls in Playwright, and an install that never uses this feature
# should not pay for that on every backend start.


async def _virtual_stream(channel_id: str, request: Request, stream_token: str):
    """Serve the media playlist for a virtual channel, starting it on demand.

    The first request blocks while the browser loads and the encoder produces
    its first segments — up to ~45s for a slow page. That is deliberate:
    returning a playlist with no segments makes every player conclude the
    channel is dead and stop retrying, which is a much worse failure than a slow
    first load.
    """
    from freesky import virtual_session

    name = virtual_channels.name_from_id(channel_id)
    try:
        session = await virtual_session.manager.acquire(name)
    except virtual_session.VirtualSessionError as exc:
        logger.error(f"Virtual channel {name} failed to start: {exc}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": "virtual_session_failed", "channel_id": channel_id,
                     "message": str(exc)},
        )
    except Exception as exc:
        logger.error(f"Virtual channel {name} crashed on start: {exc}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "virtual_session_error", "channel_id": channel_id,
                     "message": str(exc)},
        )

    try:
        with open(session.playlist_path, "r") as f:
            body = f.read()
    except OSError as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": "virtual_playlist_missing", "message": str(exc)},
        )

    body = virtual_session.rewrite_playlist(
        body, f"/api/virtual/{name}", stream_token or ""
    )
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={
            # A live playlist that a player caches is a stalled player: it holds
            # a window of segments that are deleted seconds later.
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Access-Control-Allow-Origin": "*",
            "X-Stream-Source": "virtual",
        },
    )


@fastapi_app.get("/api/virtual/{name}/{segment}")
async def virtual_segment(name: str, segment: str):
    """Serve one HLS segment from a running session's output directory."""
    from freesky import virtual_session

    # The segment name is joined onto a directory path, so this is the check
    # that stops "../../etc/passwd". An allowlist of the exact shape ffmpeg
    # generates, not a traversal blocklist.
    if not virtual_session.segment_is_safe(segment):
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    # Every segment request is a heartbeat: it is what keeps the session from
    # being reaped while somebody is actually watching.
    session = virtual_session.manager.touch(name)
    if session is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    path = os.path.join(session.out_dir, segment)
    if not os.path.isfile(path):
        # Normal at the trailing edge of the window: the player asked for a
        # segment that has just been rotated out.
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(
        path,
        media_type="video/mp2t",
        headers={
            # Segment filenames are never reused within a session (the counter
            # only increases), so a long cache is safe and saves re-fetches on
            # a seek back within the window.
            "Cache-Control": "public, max-age=30",
            "Access-Control-Allow-Origin": "*",
        },
    )


# Deliberately NOT under /api/virtual/: that prefix is token-protected (segments
# are fetched by cookie-less players) and its {name}/{segment} route would
# shadow any fixed path added beneath it. These are status/control endpoints for
# the settings page, matching how /api/services/status is exposed.


async def start_virtual_sessions():
    """Lifespan task: bring up virtual channels marked autostart.

    Registered in freesky.py. Runs after a short delay so the backend is
    answering health checks before several browsers start competing for CPU —
    otherwise a container with a few autostart channels looks unhealthy for the
    first minute of its life and can be restarted by the orchestrator.
    """
    from freesky import virtual_channels as _vc
    from freesky import virtual_session

    # ALWAYS, before the autostart check returns: clear processes orphaned by a
    # previous backend that died mid-startup. Those keep running after the
    # supervisor restarts the backend, and every failed attempt adds more, until
    # the container has no room left and the feature looks permanently broken.
    # The session manager is empty at this point, so anything matching is
    # necessarily stale.
    try:
        await asyncio.to_thread(virtual_session.reap_orphans)
    except Exception as exc:  # diagnostics must never block startup
        logger.warning(f"Could not reap orphaned virtual-channel processes: {exc}")

    if not any(r.get("autostart") and r.get("enabled") for r in _vc.list_channels()):
        return
    try:
        await asyncio.sleep(5)

        await virtual_session.manager.start_autostart_channels()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Autostart of virtual channels failed: {e}", exc_info=True)


async def close_virtual_sessions():
    """Lifespan task: idle until shutdown, then stop every virtual session.

    Registered in freesky.py. It has nothing to do while the app runs — the work
    is entirely in the cancellation path — because Reflex's lifespan is the only
    hook that actually fires on shutdown here.
    """
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        # Only import if the feature was ever touched; a fresh import here would
        # pull in Playwright during shutdown for nothing.
        module = sys.modules.get("freesky.virtual_session")
        if module is not None:
            logger.info("Shutting down virtual channel sessions")
            await module.manager.stop_all()
        raise


@fastapi_app.websocket("/api/virtual-capture/{name}")
async def virtual_capture_feed(websocket: WebSocket, name: str):
    """The tab-capture extension's recording, straight into ffmpeg's stdin.

    Internal: the browser inside the container connects here over loopback
    (virtual_session.CAPTURE_WS_BASE), never a client through the proxy. It is
    admitted by the session's one-time secret, not by a user token, because the
    feed is not a user -- and a valid user token must NOT be enough to push
    video into someone's channel. Wrong or stale key: closed before accept, so
    a probe learns nothing.
    """
    from freesky import virtual_session

    session = virtual_session.capture_target(name, websocket.query_params.get("key", ""))
    if session is None or not session.accepts_capture(websocket.query_params.get("key", "")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    session.capture_opened()
    logger.info(f"virtual[{name}]: capture feed connected")
    try:
        while True:
            data = await websocket.receive_bytes()
            await session.feed_capture(data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # ffmpeg going away is the usual reason; the janitor sees the stale
        # playlist and rebuilds the session, so this only needs logging.
        logger.warning(f"virtual[{name}]: capture feed ended: {exc}")
    finally:
        session.capture_closed()
        with contextlib.suppress(Exception):
            await websocket.close()


@fastapi_app.get("/api/virtual-sessions/status")
async def virtual_sessions_status():
    """Running sessions plus a preflight check, for the settings page.

    `missing_binaries` is what turns "the stream won't start" into "ffmpeg is
    not installed" without an admin having to read container logs. `host` is
    the CPU picture -- load, the container's quota, and how often the quota
    has been throttling it -- which is what a stuttering stream is nearly
    always explained by.
    """
    from freesky import virtual_session

    return JSONResponse({
        "missing_binaries": virtual_session.preflight(),
        "max_sessions": virtual_session.MAX_SESSIONS,
        "host": virtual_session.host_load(),
        "sessions": virtual_session.manager.statuses(),
    })


@fastapi_app.post("/api/virtual-sessions/{name}/stop")
async def virtual_session_stop(name: str):
    """Force one session down. The next request starts a fresh one."""
    from freesky import virtual_session

    stopped = await virtual_session.manager.stop(name)
    return JSONResponse({"stopped": stopped, "name": name})


# --- virtual channel remote control -----------------------------------------
# Lets an admin drive a running session's browser from Settings: dismiss a
# consent dialog, log into a site, pick a quality, scroll something into place.
#
# These are the most dangerous endpoints in the app — they are remote mouse and
# keyboard on a browser running inside the container — so unlike the read-only
# status route they require an ADMIN token, not merely a valid one. The trusted-
# subnet bypass deliberately does not apply: being on the LAN lets you watch, it
# does not let you drive the server's browser.

# Boundary for the multipart JPEG stream the control panel renders in an <img>.
_MJPEG_BOUNDARY = "freeskyframe"

# Frames per second for the control preview. Deliberately low: this is for
# aiming a mouse, not for watching, and each frame is a full Playwright
# screenshot that competes with the encoder for CPU.
_CONTROL_FPS = float(os.environ.get("VIRTUAL_CONTROL_FPS", "10"))


def _admin_from_request(request: Request) -> Optional[dict]:
    """The admin behind this request, or None.

    Mirrors require_stream_token's "no users yet means unconfigured" rule so a
    fresh install is usable before the admin is bootstrapped, but is otherwise
    strictly role-checked.
    """
    if not users.list_users():
        return {"username": "", "role": "admin"}
    user = users.user_by_token(request.query_params.get("token", ""))
    if user is None or user.get("role") != "admin":
        return None
    return user


async def _control_session(name: str, request: Request):
    """Resolve (session, error_response) for a control request."""
    from freesky import virtual_session

    if _admin_from_request(request) is None:
        return None, Response(status_code=status.HTTP_401_UNAUTHORIZED)
    session = virtual_session.manager.get(name)
    if session is None:
        return None, JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "not_running",
                     "message": f"No running session for '{name}'. Start it first."},
        )
    return session, None


@fastapi_app.post("/api/virtual-control/{name}/start")
async def virtual_control_start(name: str, request: Request):
    """Bring a session up without anyone tuning in.

    An admin needs the browser running before they can set it up — logging into
    a site or dismissing a dialog is exactly the work that has to happen before
    the channel is worth watching.
    """
    from freesky import virtual_session

    if _admin_from_request(request) is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        # The panel needs the browser, not the stream. Waiting for ffmpeg's
        # first segments as well pushed a cold start past Caddy's header
        # timeout, so the panel only ever saw a 502.
        session = await virtual_session.manager.acquire(name, wait_for_stream=False)
    except virtual_session.VirtualSessionError as exc:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            content={"error": "start_failed", "message": str(exc)})
    except Exception as exc:
        # Never let this escape as a bare 500: the control panel shows the
        # message to the admin, and "HTTP 500" tells them nothing about what
        # actually went wrong inside the container.
        logger.error(f"Virtual control start for {name} failed: {exc}", exc_info=True)
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            content={"error": "start_failed",
                                     "message": f"{type(exc).__name__}: {exc}"})
    width, height = await session.page_size()
    return JSONResponse({"started": True, "name": name, "url": session.page_url,
                         "width": width, "height": height})


@fastapi_app.get("/api/virtual-control/{name}/stream.mjpeg")
async def virtual_control_stream(name: str, request: Request):
    """Live view of the session as multipart JPEG.

    Captures the same X display the encoder does, so the panel shows exactly
    what viewers see — browser dialogs included. Only the resolution and frame
    rate are reduced. multipart/x-mixed-replace renders natively in an <img>, so
    the panel needs no player, no WebSocket and no polling loop.
    """
    session, error = await _control_session(name, request)
    if error is not None:
        return error

    def _part(jpeg: bytes) -> bytes:
        return (
            f"--{_MJPEG_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n"
        ).encode() + jpeg + b"\r\n"

    async def frames():
        try:
            async for jpeg in session.screen_frames():
                # The panel being open is itself a sign someone is using this
                # channel, so keep the reaper off it.
                session.last_access = time.monotonic()
                yield _part(jpeg)
        except asyncio.CancelledError:
            # Normal: the admin closed the panel.
            raise

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@fastapi_app.post("/api/virtual-control/{name}/input")
async def virtual_control_input(name: str, request: Request):
    """Apply one mouse/keyboard event to the live page."""
    from freesky import virtual_session

    session, error = await _control_session(name, request)
    if error is not None:
        return error
    try:
        event = await request.json()
    except Exception:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content={"error": "bad_json"})
    if not isinstance(event, dict):
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content={"error": "bad_event"})
    try:
        # X-level input, matching the X-level preview. A page-level (CDP) click
        # cannot reach browser UI such as a save-password bubble, which as far
        # as the page is concerned does not exist.
        await session.dispatch_screen_input(event)
    except virtual_session.VirtualSessionError as exc:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT,
                            content={"error": "input_failed", "message": str(exc)})
    except Exception as exc:
        # A click that lands on a navigating page throws; that is not worth a 500.
        logger.debug(f"Control input for {name} failed: {exc}")
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse({"ok": True})


@fastapi_app.post("/api/virtual-control/{name}/navigate")
async def virtual_control_navigate(name: str, request: Request):
    """Point the live session at another URL, without changing the stored one."""
    from freesky import virtual_session

    session, error = await _control_session(name, request)
    if error is not None:
        return error
    try:
        body = await request.json()
        url = str(body.get("url", ""))
    except Exception:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content={"error": "bad_json"})
    try:
        await session.navigate(url)
    except virtual_session.VirtualSessionError as exc:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content={"error": "navigate_failed", "message": str(exc)})
    return JSONResponse({"ok": True, "url": session.page_url})


@fastapi_app.get("/api/virtual-control/{name}/diagnostics")
async def virtual_control_diagnostics(name: str, request: Request):
    """What the page is rendering, plus what the encoder is receiving.

    The two together are what separate "the browser is not painting" from "the
    encoder cannot keep up" — from outside the container those look identical,
    and each has a completely different fix.
    """
    from freesky import virtual_session

    session, error = await _control_session(name, request)
    if error is not None:
        return error
    try:
        page = await session.diagnostics()
    except Exception as exc:
        page = {"error": f"{type(exc).__name__}: {exc}"}
    return JSONResponse({
        "encoder": session.metrics,
        # The capture feed (tab capture): connected or not, bytes received, and
        # what the recorder inside the browser reports about itself.
        "feed": await session.capture_status(),
        "cpu": session.cpu,
        "host": virtual_session.host_load(),
        "page": page,
        "capture": {"width": session.width, "height": session.height,
                    "framerate": session.record["framerate"],
                    "display": session.display},
        "ffmpeg_log": session._log_tail[-8:],
    })


@fastapi_app.get("/api/virtual-sessions/trace")
async def virtual_sessions_trace(request: Request):
    """The last session-startup breadcrumb trail.

    Session startup spawns an X server, an audio daemon, a browser and an
    encoder, and when one of those takes the whole backend process down there is
    no traceback and no response — just a dropped connection the proxy reports
    as 502. Each stage records itself (fsync'd) before attempting the next, so
    the LAST line here names the stage that killed it.

    Exposed over HTTP because on a container without shell access this is the
    only way to read it.
    """
    from freesky import virtual_session

    if _admin_from_request(request) is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    lines = virtual_session.read_trace()
    return JSONResponse({
        "path": virtual_session.TRACE_PATH,
        "lines": lines,
        # The headline: what was in flight when the trail stopped.
        "last_stage": lines[-1] if lines else None,
        "complete": bool(lines) and (
            "start:complete" in lines[-1] or "start:failed" in lines[-1]),
    })


@fastapi_app.post("/api/virtual-control/{name}/crop")
async def virtual_control_crop(name: str, request: Request):
    """Set (or clear) the streamed region of the screen.

    Separate from the settings form because a crop is something you pick by
    LOOKING at the page: the admin drags a rectangle on the panel's live view,
    which is the same X display the encoder captures, so what they outline is
    exactly what viewers get.

    Changing the crop changes ffmpeg's input geometry, so the session has to be
    rebuilt. That is done here rather than left to the caller, otherwise the
    panel would keep showing the old framing and look broken.
    """
    from freesky import virtual_session

    if _admin_from_request(request) is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    record = virtual_channels.get_channel(name)
    if record is None:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND,
                            content={"error": "not_found",
                                     "message": f"No virtual channel named '{name}'."})
    try:
        body = await request.json()
    except Exception:
        body = {}

    updated = dict(record)
    for key, field in (("x", "crop_x"), ("y", "crop_y"), ("w", "crop_w"), ("h", "crop_h")):
        updated[field] = body.get(key, 0) or 0
    try:
        cleaned = virtual_channels.validate_channel(updated)
    except virtual_channels.VirtualChannelError as exc:
        # A crop the admin dragged slightly off the edge is a normal mistake,
        # not a server fault, so it comes back as a readable 400.
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST,
                            content={"error": "invalid_crop", "message": str(exc)})

    virtual_channels.upsert_channel(cleaned)

    # Rebuild only if the framing actually moved. Re-applying an identical crop
    # should not interrupt a stream anyone is watching.
    restarted = False
    if virtual_channels.needs_restart(record, cleaned):
        with contextlib.suppress(Exception):
            await virtual_session.manager.stop(name)
        restarted = True

    box = virtual_channels.crop_box(cleaned)
    out_w, out_h = virtual_channels.output_size(cleaned)
    return JSONResponse({
        "saved": True,
        "cropped": box is not None,
        "crop": {"x": cleaned["crop_x"], "y": cleaned["crop_y"],
                 "w": cleaned["crop_w"], "h": cleaned["crop_h"]},
        "output": {"width": out_w, "height": out_h},
        "restarted": restarted,
    })


@fastapi_app.get("/api/virtual-control/{name}/panel", response_class=Response)
async def virtual_control_panel(name: str, request: Request):
    """The control panel itself: a self-contained HTML page.

    Served as plain HTML from the backend rather than built as a Reflex
    component on purpose. Faithful remote control needs raw pointer and keyboard
    events with exact coordinates and modifier state, which means addEventListener
    on a real element — fighting Reflex's serialised event model to get that
    would be far more code and much less accurate.
    """
    if _admin_from_request(request) is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    record = virtual_channels.get_channel(name)
    if record is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    width, height = virtual_channels.geometry(record)
    # json.dumps, not an f-string: these values end up inside a <script>, and the
    # token in particular must not be able to break out of its string literal.
    cfg = json.dumps({
        "name": name,
        "title": record["title"],
        "token": request.query_params.get("token", ""),
        "width": width,
        "height": height,
        "url": record["url"],
        # The current crop, so the panel can draw the existing region on load
        # instead of making the admin re-find it. Zeros mean "whole screen".
        "crop": {"x": record["crop_x"], "y": record["crop_y"],
                 "w": record["crop_w"], "h": record["crop_h"]},
    })
    return Response(content=_CONTROL_PANEL_HTML.replace("__CONFIG__", cfg),
                    media_type="text/html")


# Kept at module level rather than in a template file so the backend stays
# deployable as plain Python — there is no template engine or static-HTML
# pipeline in this app, and /srv is the compiled Reflex frontend.
_CONTROL_PANEL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FreeSky - virtual channel control</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #16161a; color: #e6e6e6;
         font: 14px/1.4 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
  header { display: flex; gap: .5rem; align-items: center; padding: .6rem .8rem;
           background: #1f1f24; border-bottom: 1px solid #33333a; flex-wrap: wrap; }
  h1 { font-size: 15px; margin: 0 .5rem 0 0; font-weight: 600; }
  button { background: #2c2c33; color: #e6e6e6; border: 1px solid #44444d;
           border-radius: 6px; padding: .35rem .7rem; cursor: pointer; font-size: 13px; }
  button:hover { background: #383840; }
  button.on { background: #d4342c; border-color: #d4342c; }
  input[type=text] { flex: 1; min-width: 12rem; background: #121215; color: #e6e6e6;
           border: 1px solid #44444d; border-radius: 6px; padding: .35rem .6rem;
           font-family: ui-monospace, monospace; font-size: 12px; }
  #stage { display: flex; justify-content: center; padding: 1rem; }
  /* Wraps the image so the crop overlay can be positioned against exactly the
     rendered picture, not the padded stage around it. */
  #shell { position: relative; display: inline-block; line-height: 0; }
  /* The frame is the coordinate reference for every pointer event, so it must
     never be stretched: any non-uniform scale would misplace clicks. */
  #frame { max-width: 100%; height: auto; background: #000; display: block;
           border: 1px solid #33333a; border-radius: 6px; cursor: crosshair; }
  /* Overlay pieces are pointer-events:none so they never swallow a drag or a
     click meant for the page underneath. */
  #shade { position: absolute; inset: 0; pointer-events: none;
           background: rgba(0,0,0,.55); display: none; }
  #cropbox { position: absolute; pointer-events: none; display: none;
             border: 1px solid #ffd54f; outline: 1px solid rgba(0,0,0,.6);
             box-shadow: 0 0 0 9999px rgba(0,0,0,.55); }
  /* When a crop is saved but the selector is idle, outline it without dimming
     the rest -- the admin still needs to see and drive the whole page. */
  #cropbox.saved { border-color: #7ee787; box-shadow: none; }
  #croplabel { position: absolute; top: -1.35rem; left: 0; white-space: nowrap;
               background: #1f1f24; border: 1px solid #44444d; border-radius: 4px;
               padding: 0 .3rem; font: 11px/1.5 ui-monospace, monospace;
               color: #e6e6e6; line-height: 1.5; }
  body.cropping #frame { cursor: crosshair; }
  #status { padding: 0 .8rem .8rem; color: #9a9aa5; font-size: 12px; }
  .err { color: #ff8a80; }
</style>
</head>
<body>
<header>
  <h1 id="title">virtual channel</h1>
  <button id="toggle">Take control</button>
  <button data-nav="back">&#8592;</button>
  <button data-nav="forward">&#8594;</button>
  <button data-nav="reload">&#8635;</button>
  <input type="text" id="url" spellcheck="false">
  <button id="go">Go</button>
  <button id="cropmode" title="Drag a rectangle on the live view to choose the region that gets streamed">Crop</button>
  <button id="cropapply" hidden>Apply crop</button>
  <button id="cropclear" hidden>Full screen</button>
</header>
<div id="stage"><div id="shell"><img id="frame" alt="live view"><div id="cropbox"><span id="croplabel"></span></div></div></div>
<div id="status">Starting session&hellip;</div>

<script>
const CFG = __CONFIG__;
const qs = "?token=" + encodeURIComponent(CFG.token);
const base = "/api/virtual-control/" + encodeURIComponent(CFG.name);
const frame = document.getElementById("frame");
const statusEl = document.getElementById("status");
const urlEl = document.getElementById("url");
const toggle = document.getElementById("toggle");
let controlling = false;

document.getElementById("title").textContent = CFG.title + "  (" + CFG.width + "x" + CFG.height + ")";
urlEl.value = CFG.url;

function say(msg, isErr) {
  statusEl.textContent = msg;
  statusEl.className = isErr ? "err" : "";
}

async function post(path, body) {
  const res = await fetch(base + path + qs, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
  // Prefer the server's own explanation. "HTTP 500" sent an admin to the
  // container logs for something the response already knew.
  let payload = null;
  try { payload = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) {
    throw new Error((payload && payload.message) || ("HTTP " + res.status));
  }
  return payload || {};
}

// The <img> is laid out responsively, so its rendered size rarely equals the
// page's real viewport. Every pointer event is scaled back into page
// coordinates here — without this, clicks land in the wrong place on any
// display narrower than the capture width.
// The X display's size — the coordinate space clicks are expressed in, and the
// same geometry the encoder captures. Deliberately NOT the frame's
// naturalWidth: the preview is downscaled for bandwidth, so scaling by the
// image size would land every click short.
const PAGE = {w: CFG.width, h: CFG.height};

function toPageCoords(ev) {
  const r = frame.getBoundingClientRect();
  return {
    x: Math.round((ev.clientX - r.left) * (PAGE.w / r.width)),
    y: Math.round((ev.clientY - r.top) * (PAGE.h / r.height)),
  };
}

// A move is only useful if it is the latest one. Sending while a previous
// request is still in flight builds a queue the server works through long after
// the pointer has moved on, which is what makes remote control feel laggy.
// Clicks and keys are never dropped.
let movePending = false;

function send(event) {
  if (!controlling) return;
  const droppable = event.type === "move";
  if (droppable) {
    if (movePending) return;
    movePending = true;
  }
  post("/input", event)
    .catch(e => say("input failed: " + e.message, true))
    .finally(() => { if (droppable) movePending = false; });
}

// --- crop selection -------------------------------------------------------
// The admin drags a rectangle on the live view. That view is a capture of the
// same X display the encoder records, so the rectangle they draw is literally
// the region viewers will get -- no separate preview geometry to get wrong.
// Coordinates go through toPageCoords, the same mapping clicks use, so the
// selection stays correct at any window size.
const cropBoxEl = document.getElementById("cropbox");
const cropLabel = document.getElementById("croplabel");
const cropModeBtn = document.getElementById("cropmode");
const cropApplyBtn = document.getElementById("cropapply");
const cropClearBtn = document.getElementById("cropclear");
let cropping = false;      // selector armed
let dragging = null;       // {x, y} anchor in PAGE coords while dragging
let selection = null;      // {x, y, w, h} in PAGE coords, pending Apply
let savedCrop = (CFG.crop && CFG.crop.w && CFG.crop.h) ? Object.assign({}, CFG.crop) : null;

// PAGE coords -> CSS pixels within the rendered image.
function drawBox(box, isSaved) {
  if (!box) { cropBoxEl.style.display = "none"; return; }
  const r = frame.getBoundingClientRect();
  const sx = r.width / PAGE.w, sy = r.height / PAGE.h;
  cropBoxEl.style.display = "block";
  cropBoxEl.classList.toggle("saved", !!isSaved);
  cropBoxEl.style.left = (box.x * sx) + "px";
  cropBoxEl.style.top = (box.y * sy) + "px";
  cropBoxEl.style.width = (box.w * sx) + "px";
  cropBoxEl.style.height = (box.h * sy) + "px";
  cropLabel.textContent = box.w + "x" + box.h + " @ " + box.x + "," + box.y;
}

function redrawCrop() {
  if (selection) drawBox(selection, false);
  else if (savedCrop) drawBox(savedCrop, true);
  else drawBox(null);
}

// The overlay is positioned in CSS pixels, so it has to be recomputed whenever
// the image is laid out at a different size.
window.addEventListener("resize", redrawCrop);
frame.addEventListener("load", redrawCrop);

function setCropMode(on) {
  cropping = on;
  document.body.classList.toggle("cropping", on);
  cropModeBtn.classList.toggle("on", on);
  cropModeBtn.textContent = on ? "Cancel crop" : "Crop";
  cropApplyBtn.hidden = !on;
  cropClearBtn.hidden = !(on || savedCrop);
  if (!on) { selection = null; dragging = null; }
  if (on && controlling) setControl(false);   // dragging must not click the page
  redrawCrop();
  say(on
    ? "Drag a rectangle on the view to choose what gets streamed, then Apply crop."
    : (savedCrop ? "Streaming a " + savedCrop.w + "x" + savedCrop.h + " region."
                 : "Streaming the whole screen."));
}

cropModeBtn.addEventListener("click", () => setCropMode(!cropping));

frame.addEventListener("pointerdown", ev => {
  if (!cropping) return;
  ev.preventDefault();
  dragging = toPageCoords(ev);
  selection = null;
  frame.setPointerCapture(ev.pointerId);
});

frame.addEventListener("pointermove", ev => {
  if (!cropping || !dragging) return;
  const p = toPageCoords(ev);
  // Normalised so dragging up or left works as naturally as down or right,
  // and clamped to the screen so a drag off the edge cannot save an invalid box.
  const x = Math.max(0, Math.min(dragging.x, p.x));
  const y = Math.max(0, Math.min(dragging.y, p.y));
  const w = Math.min(PAGE.w, Math.max(dragging.x, p.x)) - x;
  const h = Math.min(PAGE.h, Math.max(dragging.y, p.y)) - y;
  selection = {x: x, y: y, w: w - (w % 2), h: h - (h % 2)};
  redrawCrop();
});

frame.addEventListener("pointerup", ev => {
  if (!cropping || !dragging) return;
  dragging = null;
  if (selection && (selection.w < 32 || selection.h < 32)) {
    selection = null;
    say("That region is too small - drag at least 32x32 pixels.", true);
  }
  redrawCrop();
});

cropApplyBtn.addEventListener("click", async () => {
  if (!selection) { say("Drag a rectangle first.", true); return; }
  try {
    const res = await post("/crop", selection);
    savedCrop = res.cropped ? res.crop : null;
    selection = null;
    setCropMode(false);
    say("Now streaming " + res.output.width + "x" + res.output.height
        + (res.restarted ? " - restarting the session to apply it." : "."));
  } catch (e) { say("Could not save the crop: " + e.message, true); }
});

cropClearBtn.addEventListener("click", async () => {
  try {
    const res = await post("/crop", {x: 0, y: 0, w: 0, h: 0});
    savedCrop = null;
    selection = null;
    setCropMode(false);
    say("Streaming the whole " + res.output.width + "x" + res.output.height + " screen"
        + (res.restarted ? " - restarting the session to apply it." : "."));
  } catch (e) { say("Could not clear the crop: " + e.message, true); }
});

// Show an existing crop straight away, and offer "Full screen" without having
// to arm the selector first.
cropClearBtn.hidden = !savedCrop;
redrawCrop();

frame.addEventListener("click", ev => {
  // While the selector is armed the view is a canvas to draw on, not a page to
  // drive, so a click must not also reach the browser underneath.
  if (cropping) { ev.preventDefault(); return; }
  if (!controlling) return;
  ev.preventDefault();
  const p = toPageCoords(ev);
  send({type: "click", x: p.x, y: p.y, button: "left"});
});

frame.addEventListener("contextmenu", ev => {
  if (!controlling) return;
  ev.preventDefault();
  const p = toPageCoords(ev);
  send({type: "click", x: p.x, y: p.y, button: "right"});
});

frame.addEventListener("dblclick", ev => {
  if (!controlling) return;
  const p = toPageCoords(ev);
  send({type: "click", x: p.x, y: p.y, button: "left", clicks: 2});
});

// Pointer moves are throttled hard. Every event is a round-trip that contends
// with the encoder, and hover state does not need 60fps to be useful.
let lastMove = 0;
frame.addEventListener("pointermove", ev => {
  if (!controlling) return;
  const now = performance.now();
  if (now - lastMove < 60) return;
  lastMove = now;
  const p = toPageCoords(ev);
  send({type: "move", x: p.x, y: p.y});
});

frame.addEventListener("wheel", ev => {
  if (!controlling) return;
  ev.preventDefault();
  const p = toPageCoords(ev);
  send({type: "wheel", x: p.x, y: p.y, dx: ev.deltaX, dy: ev.deltaY});
}, {passive: false});

// Keyboard goes to the remote page only while control is on, otherwise the
// admin cannot type in the address bar above.
window.addEventListener("keydown", ev => {
  if (!controlling) return;
  if (document.activeElement === urlEl) return;
  ev.preventDefault();
  if (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    send({type: "text", text: ev.key});
  } else {
    send({type: "key", key: ev.key});
  }
});

// A function rather than inline, so the crop selector can drop control when it
// arms itself: a drag that also clicked through to the page would follow links.
function setControl(on) {
  controlling = on;
  toggle.classList.toggle("on", controlling);
  toggle.textContent = controlling ? "Release control" : "Take control";
  say(controlling
    ? "Control is live - clicks, scrolling and typing go to the remote browser."
    : "Viewing only. This is exactly what viewers see.");
}

toggle.addEventListener("click", () => {
  if (cropping) setCropMode(false);   // the two modes are mutually exclusive
  setControl(!controlling);
});

document.querySelectorAll("[data-nav]").forEach(b => {
  b.addEventListener("click", () => {
    post("/input", {type: b.dataset.nav}).catch(e => say(e.message, true));
  });
});

document.getElementById("go").addEventListener("click", async () => {
  try {
    const r = await post("/navigate", {url: urlEl.value});
    urlEl.value = r.url || urlEl.value;
    say("Navigated. This is temporary - the channel returns to its configured URL on the next session.");
  } catch (e) {
    say("navigate failed: " + e.message, true);
  }
});

// Start the session before wiring the frame up: the browser may not be running
// yet, and pointing <img> at the stream first would just 409 and show nothing.
(async () => {
  try {
    say("Starting session (this can take up to a minute on a slow page)...");
    const r = await post("/start", {});
    if (r.url) urlEl.value = r.url;
    frame.src = base + "/stream.mjpeg" + qs + "&t=" + Date.now();
    say("Viewing only. Press “Take control” to drive the browser.");
  } catch (e) {
    say("Could not start the session: " + e.message, true);
  }
})();

frame.addEventListener("error", () => say("Live view disconnected - reload this page.", true));
</script>
</body>
</html>
"""
