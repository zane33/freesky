#!/usr/bin/env python3
"""
Hybrid streaming architecture that handles both old and new dlhd.click patterns
"""
import base64
import html
import json
import os
import re
import reflex as rx
import logging
import asyncio
from urllib.parse import quote, urljoin, urlparse
from curl_cffi import AsyncSession
from dataclasses import dataclass
from typing import List
from .free_sky import Channel
from .utils import encrypt, decrypt, urlsafe_base64, extract_and_decode_var, hls_ext
from .token_validator import TokenValidator, extract_viable_streams
from rxconfig import config

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ponytail: Channel used to be redefined here with identical fields. Two classes
# with the same shape are still two types: anything annotated
# `List[free_sky.Channel]` silently rejected the ones built here, which is how the
# settings page came up empty while the backend held 900 channels.
class StepDaddyHybrid:
    def __init__(self):
        socks5 = config.socks5
        max_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "10"))
        
        session_config = {
            "timeout": 15,   # Increased timeout for reliability
            "impersonate": "chrome110",
            "max_redirects": 10,  # Increased to handle more redirects
        }
        
        if socks5:
            session_config["proxy"] = f"socks5://{socks5}"
        
        self._session = AsyncSession(**session_config)
        self._base_url = config.daddylive_uri
        self.channels = []
        self._load_lock = asyncio.Lock()
        with open("freesky/meta.json", "r") as f:
            self._meta = json.load(f)
        
        logger.info(f"StepDaddyHybrid initialized with max_streams: {max_streams}")

    def _headers(self, referer: str = None, origin: str = None):
        if referer is None:
            referer = self._base_url
        headers = {
            "Referer": referer,
            "user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
        }
        if origin:
            headers["Origin"] = origin
        return headers

    async def load_channels(self):
        # Use lock to prevent concurrent loading
        async with self._load_lock:
            channels = []
            try:
                logger.debug(f"Starting channel load from {self._base_url}/24-7-channels.php")
                response = await self._session.get(
                    f"{self._base_url}/24-7-channels.php",
                    headers=self._headers(),
                    allow_redirects=True,
                    max_redirects=10
                )
                
                logger.debug(f"Got response with status {response.status_code}")
                if response.status_code != 200:
                    logger.error(f"Failed to fetch channels: HTTP {response.status_code}")
                    return

                # ponytail: upstream moved to /watch.php?id=N cards; old
                # "<center><h1 ... tab-2" block + <strong> markup is gone.
                logger.debug("Extracting channel cards from response")
                channels_data = re.findall(
                    r'href="/watch\.php\?id=(\d+)"[^>]*?>\s*<div class="card__title">(.*?)</div>',
                    str(response.text),
                    re.DOTALL,
                )
                logger.debug(f"Found {len(channels_data)} raw channel entries")

                if not channels_data:
                    logger.error("No channel cards found in response")
                    logger.debug(f"Response text: {response.text[:500]}...")
                    return

                # Process channels concurrently for better performance
                tasks = []
                for channel_data in channels_data:
                    tasks.append(self._process_channel_data(channel_data))
                
                # Wait for all channel processing to complete
                logger.debug(f"Processing {len(tasks)} channels concurrently")
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Error processing channel: {str(result)}")
                    elif result is not None:
                        channels.append(result)

                logger.info(f"Successfully processed {len(channels)} channels")
            except Exception as e:
                logger.error(f"Error loading channels: {str(e)}", exc_info=True)
                # Try to load fallback channels if primary source fails
                try:
                    logger.info("Attempting to load fallback channels...")
                    fallback_path = os.path.join(os.path.dirname(__file__), 'fallback_channels.json')
                    if os.path.exists(fallback_path):
                        with open(fallback_path, 'r') as f:
                            fallback_data = json.load(f)
                            for ch in fallback_data:
                                channels.append(Channel(
                                    id=ch.get('id', ''),
                                    name=ch.get('name', 'Unknown'),
                                    tags=ch.get('tags', []),
                                    logo=ch.get('logo', '/missing.png')
                                ))
                        logger.info(f"Loaded {len(channels)} fallback channels")
                except Exception as fb_error:
                    logger.error(f"Failed to load fallback channels: {str(fb_error)}")
            finally:
                if channels:
                    logger.debug(f"Updating channels list with {len(channels)} channels")
                    self.channels = sorted(channels, key=lambda channel: (channel.name.startswith("18"), channel.name))
                else:
                    logger.warning("No channels were loaded, keeping existing channels list")

    async def _process_channel_data(self, channel_data):
        """Process a single channel data asynchronously"""
        try:
            return self._get_channel(channel_data)
        except Exception as e:
            logger.error(f"Error processing channel {channel_data}: {str(e)}")
            return None

    def _get_channel(self, channel_data) -> Channel:
        # channel_data is (id, name) from the /watch.php?id=N card markup
        channel_id = channel_data[0]
        channel_name = html.unescape(channel_data[1]).strip()
        if channel_id == "666":
            channel_name = "Nick Music"
        if channel_id == "609":
            channel_name = "Yas TV UAE"
        if channel_name == "#0 Spain":
            channel_name = "Movistar Plus+"
        elif channel_name == "#Vamos Spain":
            channel_name = "Vamos Spain"
        clean_channel_name = re.sub(r"\s*\(.*?\)", "", channel_name)
        meta = self._meta.get(clean_channel_name, {})
        # Upstream publishes per-channel art at {base}/logos/<slug>.<ext>, so channels
        # added after meta.json was written still get a logo, and it follows
        # DADDYLIVE_URI when the site moves. meta.json only covers ~2/3 of the list;
        # it stays as the fallback because upstream misses ~18%.
        # /api/logo tries the other extensions before giving up.
        slug = re.sub(r"[^a-z0-9]+", "_", clean_channel_name.lower()).strip("_")
        logo = meta.get("logo") or f"{self._base_url}/logos/{slug}.png"
        if logo.startswith("http"):
            logo = f"/api/logo/{urlsafe_base64(logo)}"
        return Channel(id=channel_id, name=channel_name, tags=meta.get("tags", []), logo=logo)

    # ponytail: hosts are discovered from the pages, never hardcoded. Upstream has already
    # moved twice (vidembed.re, fnjplay.xyz -> both dead); following the iframe chain
    # survives the next move without a code change.
    _IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
    _ATOB_RE = re.compile(r"atob\(\s*['\"]([A-Za-z0-9+/=]+)['\"]\s*\)")

    # Upstream's watch.php offers several "players", each its own path that leads
    # to an independent iframe chain. Trying them in order gives real failover:
    # when one player's CDN feed is offline the next may still be live. Order is
    # best-first from measurement (stream/watch resolve most reliably); the rest
    # are tried before giving up. A channel-specific preference can pin one first.
    PLAYER_PATHS = ["stream", "watch", "cast", "plus", "casting", "player"]

    async def _resolve_via_iframe_chain(self, channel_id: str, max_hops: int = 4,
                                        max_pages: int = 8, prefer: str = None,
                                        budget: float = 9.5):
        """
        Follow the live upstream chain to a WORKING HLS playlist, failing over
        across the available players.

        As of 2026-07 a player chain is: /<player>/stream-N.php -> <player-host>
        /premiumtv/daddyN.php?id=N, whose Clappr config carries the m3u8 URL inside
        window.atob('<base64>'). Each hop needs the previous page as Referer.

        A candidate m3u8 is only accepted once its body actually starts with
        #EXTM3U — a URL that resolves but returns "Not found" is treated as a dead
        source and we move on, which is what makes failover real rather than
        cosmetic.
        """
        players = list(self.PLAYER_PATHS)
        if prefer and prefer in players:
            players.remove(prefer)
            players.insert(0, prefer)

        watch_url = f"{self._base_url}/watch.php?id={channel_id}"
        deadline = asyncio.get_event_loop().time() + budget
        last_error = None
        # A feed that resolves but declares no audio is kept here and only used if
        # no player with audio turns up — so a silent channel still shows a picture
        # rather than 404ing, but any feed WITH audio always wins.
        video_only_fallback = None

        for player in players:
            if asyncio.get_event_loop().time() > deadline:
                break  # a dead channel shouldn't burn the whole request on every player
            # Seed each player from its own entry page but keep watch.php as the
            # referer, mirroring how the site navigates between players.
            start = f"{self._base_url}/{player}/stream-{channel_id}.php"
            queue = [(start, watch_url, 0)]
            seen = set()
            player_dead = False

            while queue and len(seen) < max_pages and not player_dead:
                if asyncio.get_event_loop().time() > deadline:
                    break
                url, referer, depth = queue.pop(0)
                if url in seen or depth > max_hops:
                    continue
                seen.add(url)

                try:
                    # Per-hop cap: several player hosts are dead and hang for the
                    # full session timeout, which alone would blow the whole budget
                    # on one bad iframe. Fail that hop fast and try the next player.
                    response = await asyncio.wait_for(
                        self._session.get(url, headers=self._headers(referer)),
                        timeout=4.0,
                    )
                except Exception as e:
                    logger.debug(f"Hop failed for {url}: {e}")
                    continue
                if response.status_code != 200:
                    continue

                for encoded in self._ATOB_RE.findall(response.text):
                    try:
                        candidate = base64.b64decode(encoded).decode()
                    except Exception:
                        continue  # not every atob() on the page is the stream URL
                    if candidate.startswith("http") and ".m3u8" in candidate:
                        try:
                            content, has_audio = await self._fetch_playlist(candidate, url)
                        except Exception as e:
                            # We reached this player's CDN and it returned no real
                            # playlist: the feed is down. Stop crawling this player's
                            # ad iframes and move straight to the next player.
                            last_error = e
                            player_dead = True
                            logger.debug(f"Player '{player}' feed dead for {channel_id}: {e}")
                            break
                        if has_audio:
                            logger.info(f"Resolved channel {channel_id} via '{player}' player")
                            return content
                        # Resolved but video-only. Remember it, then try the next
                        # player for one that actually carries sound.
                        if video_only_fallback is None:
                            video_only_fallback = content
                        logger.info(f"Player '{player}' for {channel_id} is video-only; trying next for audio")
                        player_dead = True
                        break

                if not player_dead:
                    for src in self._IFRAME_RE.findall(response.text):
                        # Skip templated srcs like "' + url + '" in inline scripts.
                        if "://" in src or src.startswith("/"):
                            queue.append((urljoin(url, src), url, depth + 1))

        if video_only_fallback is not None:
            logger.warning(f"No feed with audio for channel {channel_id}; using video-only fallback")
            return video_only_fallback
        raise ValueError(
            f"No working stream found for channel {channel_id} across "
            f"{len(players)} players" + (f" (last: {last_error})" if last_error else "")
        )

    # Audio codecs a master playlist can name. If CODECS is present on every
    # variant and none of these appear, and there's no separate audio rendition,
    # the feed is genuinely video-only (confirmed against a silent Sky Sports NZ
    # feed whose segment PMT carried H.264 and nothing else).
    _AUDIO_CODECS = ("mp4a", "ac-3", "ec-3", "ac3", "ec3", "opus", "flac", "alac", "dts", "mp3")

    @staticmethod
    def _declares_audio(playlist_text: str) -> bool:
        """Whether an HLS playlist looks like it carries audio.

        ponytail: judged for free from the master playlist's CODECS / EXT-X-MEDIA,
        never a segment download. When CODECS is absent — a media playlist, or an
        upstream that just omits it — we can't tell cheaply, so we assume audio
        rather than pay a segment probe on every resolve. Upgrade path: probe one
        segment's PMT if silent media-playlist feeds ever show up.
        """
        if "TYPE=AUDIO" in playlist_text:  # a separate audio rendition is declared
            return True
        codecs = re.findall(r'CODECS="([^"]*)"', playlist_text)
        if not codecs:
            return True  # nothing declared -> can't judge without a segment; assume ok
        return any(a in group.lower() for group in codecs for a in StepDaddyHybrid._AUDIO_CODECS)

    async def _fetch_playlist(self, m3u8_url: str, referer: str):
        """Fetch an HLS playlist and rewrite it for the proxy.

        Returns (proxied_playlist, has_audio) so the resolver can fail over off a
        video-only feed to one that actually has sound.
        """
        response = await self._session.get(m3u8_url, headers=self._headers(referer))
        if response.status_code != 200 or not response.text.startswith("#EXTM3U"):
            raise ValueError(f"Bad playlist from {m3u8_url}: HTTP {response.status_code}")

        has_audio = self._declares_audio(response.text)
        # Variant/segment URIs are relative to the playlist, but _process_stream_content
        # only proxies lines starting with "http" — left alone, the player would resolve
        # them against /api/stream/ on our own host and 404.
        absolute = "\n".join(
            urljoin(m3u8_url, line) if line and not line.startswith("#") else line
            for line in response.text.split("\n")
        )
        # Rewrite against the player page, not the CDN URL: the CDN 403s any request
        # whose Referer is not the embedding page, and our proxy has to replay it.
        return self._process_stream_content(absolute, referer), has_audio

    async def stream(self, channel_id: str, prefer: str = None):
        """
        Resolve a channel to a proxied HLS playlist, failing over across upstream
        players. `prefer` pins one player (see PLAYER_PATHS) to try first.

        ponytail: the old vidembed.re / fnjplay.xyz fallbacks were removed — both
        hosts are dead (DNS no longer resolves), so they only added a multi-second
        stall before the same failure. The iframe chain already tries every live
        player. `_handle_new_architecture`/`_handle_old_architecture` remain for
        the multi_service_streamer callers but are no longer on this path.
        """
        return await self._resolve_via_iframe_chain(channel_id, prefer=prefer)

    async def _handle_new_architecture(self, vidembed_url: str, referer: str):
        """Handle the new vidembed.re architecture with proper iframe-based authentication"""
        logger.debug(f"Processing vidembed URL: {vidembed_url}")
        
        try:
            # Extract UUID from vidembed URL
            uuid_match = re.search(r'/stream/([a-f0-9-]{36})', vidembed_url)
            if not uuid_match:
                raise ValueError("Could not extract UUID from vidembed URL")
            
            uuid = uuid_match.group(1)
            logger.debug(f"Extracted UUID: {uuid}")
            
            # Try to use the iframe-based extractor for proper authentication
            try:
                from .vidembed_extractor import extract_hls_from_vidembed
                logger.info("Attempting iframe-based extraction...")
                hls_url = await extract_hls_from_vidembed(vidembed_url)
                
                if hls_url:
                    logger.info(f"Successfully extracted HLS URL via iframe: {hls_url}")
                    # Test the HLS URL
                    stream_response = await self._session.get(hls_url, headers=self._headers(vidembed_url))
                    if stream_response.status_code == 200 and stream_response.text.startswith('#EXTM3U'):
                        return self._process_stream_content(stream_response.text, vidembed_url)
            except Exception as iframe_error:
                logger.warning(f"Iframe-based extraction failed: {str(iframe_error)}")
            
            # Fallback: Try direct API approach with proper headers
            logger.info("Attempting direct API approach with iframe simulation...")
            api_url = f"https://www.vidembed.re/api/source/{uuid}?type=live"
            
            # Headers that simulate iframe context
            api_headers = self._headers(vidembed_url)
            api_headers.update({
                "Origin": "https://vidembed.re",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            })
            
            api_response = await self._session.get(api_url, headers=api_headers)
            
            if api_response.status_code == 200:
                try:
                    api_data = api_response.json()
                    logger.debug(f"API Response: {api_data}")
                    
                    # Look for stream data in the response
                    if 'data' in api_data and isinstance(api_data['data'], list):
                        for item in api_data['data']:
                            if 'file' in item:
                                stream_url = item['file']
                                logger.info(f"Found stream URL from API: {stream_url}")
                                
                                # Test the stream URL
                                stream_response = await self._session.get(stream_url, headers=self._headers(vidembed_url))
                                if stream_response.status_code == 200 and stream_response.text.startswith('#EXTM3U'):
                                    return self._process_stream_content(stream_response.text, vidembed_url)
                    
                    # If direct API response doesn't have stream URLs, check for encrypted data
                    if 'data' in api_data and isinstance(api_data['data'], str):
                        # This might be encrypted data that needs client-side decryption
                        logger.info("API returned encrypted data, may need client-side processing")
                        return self._create_vidembed_response(vidembed_url)
                        
                except Exception as json_error:
                    logger.warning(f"Error parsing API response JSON: {str(json_error)}")
            else:
                logger.warning(f"API request failed with status {api_response.status_code}")
            
            # Fallback: Access vidembed page directly
            logger.info("Attempting direct vidembed page access...")
            vidembed_response = await self._session.get(vidembed_url, headers=self._headers(referer))
            
            if vidembed_response.status_code != 200:
                raise ValueError(f"Failed to access vidembed page: HTTP {vidembed_response.status_code}")
            
            # Look for direct stream URLs in page content
            stream_urls = self._extract_stream_urls(vidembed_response.text)
            
            if stream_urls:
                # Found direct stream URLs
                stream_url = stream_urls[0]
                logger.debug(f"Found direct stream URL: {stream_url}")
                
                # Fetch the stream content
                stream_response = await self._session.get(stream_url, headers=self._headers(vidembed_url))
                
                if stream_response.status_code == 200:
                    return self._process_stream_content(stream_response.text, vidembed_url)
                else:
                    raise ValueError(f"Failed to fetch stream: HTTP {stream_response.status_code}")
            else:
                # No direct URLs found, try to extract from JavaScript variables
                logger.debug("No direct stream URLs found, trying JavaScript extraction...")
                js_stream_url = self._extract_js_stream_url(vidembed_response.text)
                
                if js_stream_url:
                    logger.debug(f"Found JavaScript stream URL: {js_stream_url}")
                    stream_response = await self._session.get(js_stream_url, headers=self._headers(vidembed_url))
                    
                    if stream_response.status_code == 200:
                        return self._process_stream_content(stream_response.text, vidembed_url)
                    else:
                        logger.warning(f"Failed to fetch JavaScript stream: HTTP {stream_response.status_code}")
                
                # If all else fails, return vidembed URL for client-side processing
                logger.debug("No direct stream URLs found, returning vidembed URL for client-side processing")
                return self._create_vidembed_response(vidembed_url)
                
        except Exception as e:
            logger.error(f"Error in new architecture handling: {str(e)}")
            # Return vidembed URL for client-side processing as last resort
            return self._create_vidembed_response(vidembed_url)

    async def _handle_old_architecture(self, iframe_url: str, referer: str):
        """Handle the old authentication-based architecture"""
        logger.debug(f"Processing iframe URL: {iframe_url}")
        
        # Make request to iframe source
        iframe_response = await self._session.post(iframe_url, headers=self._headers(referer))
        
        if iframe_response.status_code != 200:
            raise ValueError(f"Failed to access iframe: HTTP {iframe_response.status_code}")
        
        # Extract authentication variables
        try:
            channel_key = re.compile(r"var\s+channelKey\s*=\s*\"(.*?)\";").findall(iframe_response.text)[-1]
            auth_ts = extract_and_decode_var("__c", iframe_response.text)
            auth_sig = extract_and_decode_var("__e", iframe_response.text)
            auth_path = extract_and_decode_var("__b", iframe_response.text)
            auth_rnd = extract_and_decode_var("__d", iframe_response.text)
            auth_url = extract_and_decode_var("__a", iframe_response.text)
            
            logger.debug("Successfully extracted authentication variables")
            
            # Make authentication request
            auth_request_url = f"{auth_url}{auth_path}?channel_id={channel_key}&ts={auth_ts}&rnd={auth_rnd}&sig={auth_sig}"
            auth_response = await self._session.get(auth_request_url, headers=self._headers(iframe_url))
            
            if auth_response.status_code != 200:
                raise ValueError("Failed to get auth response")
            
            # Server lookup
            key_url = urlparse(iframe_url)
            key_url = f"{key_url.scheme}://{key_url.netloc}/server_lookup.php?channel_id={channel_key}"
            key_response = await self._session.get(key_url, headers=self._headers(iframe_url))
            server_key = key_response.json().get("server_key")
            
            if not server_key:
                raise ValueError("No server key found in response")
            
            # Construct final stream URL
            if server_key == "top1/cdn":
                server_url = f"https://top1.newkso.ru/top1/cdn/{channel_key}/mono.m3u8"
            else:
                server_url = f"https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8"
            
            # Fetch M3U8 playlist
            m3u8 = await self._session.get(server_url, headers=self._headers(quote(str(iframe_url))))
            
            # Process M3U8 content
            m3u8_data = ""
            for line in m3u8.text.split("\n"):
                if line.startswith("#EXT-X-KEY:"):
                    original_url = re.search(r'URI="(.*?)"', line).group(1)
                    line = line.replace(original_url, f"/api/key/{encrypt(original_url)}/{encrypt(urlparse(iframe_url).netloc)}")
                elif line.startswith("#EXT-X-MEDIA:") and config.proxy_content:
                    # Separate audio/subtitle rendition playlist lives in URI="...";
                    # unproxied, ffmpeg (Dispatcharr) can't fetch audio -> silent stream.
                    m = re.search(r'URI="(https?://.*?)"', line)
                    if m:
                        line = line.replace(m.group(1), f"/api/content/{encrypt(m.group(1))}{hls_ext(m.group(1))}")
                elif line.startswith("http") and config.proxy_content:
                    line = f"/api/content/{encrypt(line)}{hls_ext(line)}"
                m3u8_data += line + "\n"
            
            return m3u8_data
            
        except Exception as e:
            logger.error(f"Error in old architecture: {str(e)}")
            raise ValueError(f"Failed to process old architecture: {str(e)}")

    async def _handle_direct_stream(self, stream_url: str, referer: str):
        """Handle direct stream URLs"""
        logger.debug(f"Processing direct stream URL: {stream_url}")
        
        # Fetch the stream content
        stream_response = await self._session.get(stream_url, headers=self._headers(referer))
        
        if stream_response.status_code == 200:
            return self._process_stream_content(stream_response.text, referer)
        else:
            raise ValueError(f"Failed to fetch direct stream: HTTP {stream_response.status_code}")

    def _extract_stream_urls(self, vidembed_content: str) -> List[str]:
        """Extract direct stream URLs from vidembed content"""
        stream_patterns = [
            r'https://[^"\']*\.m3u8[^"\']*',
            r'https://[^"\']*\.mp4[^"\']*',
            r'https://[^"\']*stream[^"\']*',
            r'https://[^"\']*cdn[^"\']*',
        ]
        
        found_urls = []
        for pattern in stream_patterns:
            matches = re.findall(pattern, vidembed_content)
            found_urls.extend(matches)
        
        # Remove duplicates and filter out non-stream URLs
        unique_urls = list(set(found_urls))
        stream_urls = [url for url in unique_urls if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'stream', 'cdn']) 
                      and 'cdnjs.cloudflare.com' not in url]  # Exclude CDN libraries
        
        return stream_urls

    def _extract_js_stream_url(self, vidembed_content: str) -> str:
        """Extract stream URL from JavaScript variables"""
        # Look for common JavaScript patterns
        js_patterns = [
            r'var\s+streamUrl\s*=\s*["\']([^"\']+)["\']',
            r'var\s+videoUrl\s*=\s*["\']([^"\']+)["\']',
            r'var\s+src\s*=\s*["\']([^"\']+)["\']',
            r'streamUrl\s*:\s*["\']([^"\']+)["\']',
            r'videoUrl\s*:\s*["\']([^"\']+)["\']',
            r'src\s*:\s*["\']([^"\']+)["\']',
            r'url\s*:\s*["\']([^"\']+)["\']',
        ]
        
        for pattern in js_patterns:
            matches = re.findall(pattern, vidembed_content)
            for match in matches:
                if any(ext in match.lower() for ext in ['.m3u8', '.mp4', 'stream', 'cdn']):
                    return match
        
        return None

    def _process_stream_content(self, content: str, referer: str) -> str:
        """Process stream content (M3U8, etc.) and proxy URLs with token validation"""
        if content.startswith('#EXTM3U'):
            # This is an M3U8 playlist, process it with token validation
            logger.debug("Processing M3U8 playlist with token validation")
            
            # First, extract and validate tokens
            try:
                viable_streams = extract_viable_streams(content)
                if viable_streams:
                    logger.info(f"Found {len(viable_streams)} viable streams with valid tokens")
                else:
                    logger.warning("No viable streams found with valid tokens")
            except Exception as token_error:
                logger.warning(f"Token validation failed: {str(token_error)}")
            
            lines = content.split('\n')
            processed_lines = []
            
            for line in lines:
                if line.startswith('http'):
                    # Validate token if present
                    try:
                        token_analysis = TokenValidator.analyze_token_security(line)
                        # Drop a URL only when the token parsed and is genuinely expired.
                        # "error" means the analyzer could not read a token at all — the
                        # current CDN puts expiry in the path, not query params, so
                        # treating unparseable as invalid emptied every playlist.
                        if not token_analysis.get('valid', True) and 'error' not in token_analysis:
                            logger.debug(f"Skipping expired stream: {line}")
                            continue  # Skip expired streams
                        elif token_analysis.get('expires_in_seconds', float('inf')) < 3600:  # Less than 1 hour
                            logger.warning(f"Stream expires soon: {token_analysis.get('expires_in_seconds', 0)} seconds")
                    except Exception as validation_error:
                        logger.debug(f"Token validation error for {line}: {str(validation_error)}")
                    
                    if config.proxy_content:
                        # Proxy content URLs, carrying the referer the CDN demands —
                        # same two-segment shape /api/key/ already uses. The trailing
                        # extension goes on the LAST component, which is what ffmpeg
                        # inspects.
                        line = f"/api/content/{encrypt(line)}/{encrypt(referer)}{hls_ext(line)}"
                elif line.startswith('#EXT-X-MEDIA:') and config.proxy_content:
                    # A separate audio/subtitle rendition keeps its playlist in a
                    # URI="..." attr, not on its own line, so the http branch never
                    # sees it. Unproxied, ffmpeg (Dispatcharr) can't reach the audio
                    # track and plays video only — the browser fetches it directly
                    # and sounds fine, which is why this only bites external players.
                    m = re.search(r'URI="(https?://.*?)"', line)
                    if m:
                        uri = m.group(1)
                        line = line.replace(uri, f"/api/content/{encrypt(uri)}/{encrypt(referer)}{hls_ext(uri)}")
                elif line.startswith('#EXT-X-KEY:'):
                    # Process encryption keys
                    original_url = re.search(r'URI="(.*?)"', line)
                    if original_url:
                        line = line.replace(original_url.group(1), f"/api/key/{encrypt(original_url.group(1))}/{encrypt(urlparse(referer).netloc)}")
                
                processed_lines.append(line)
            
            processed_content = '\n'.join(processed_lines)
            
            # Log token analysis summary
            try:
                tokens = TokenValidator.extract_tokens_from_m3u8(content)
                if tokens:
                    valid_tokens = sum(1 for t in tokens if t['analysis'].get('valid', False))
                    logger.info(f"Token summary: {valid_tokens}/{len(tokens)} valid tokens")
            except Exception as summary_error:
                logger.debug(f"Error generating token summary: {str(summary_error)}")
            
            return processed_content
        else:
            # Not an M3U8 playlist, return as is
            return content

    def _create_vidembed_response(self, vidembed_url: str) -> str:
        """Create a response that includes the vidembed URL for client-side processing"""
        return f"VIDEMBED_URL:{vidembed_url}"

    # Semaphore for limiting concurrent stream requests
    _stream_semaphore = None
    
    def _get_stream_semaphore(self):
        """Get or create stream semaphore with configurable limit"""
        if self._stream_semaphore is None:
            max_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "10"))
            self._stream_semaphore = asyncio.Semaphore(max_streams)
            logger.info(f"Created stream semaphore with limit: {max_streams}")
        return self._stream_semaphore

    async def key(self, url: str, host: str):
        url = decrypt(url)
        host = decrypt(host)
        response = await self._session.get(url, headers=self._headers(f"{host}/", host), timeout=60)
        if response.status_code != 200:
            raise Exception(f"Failed to get key")
        return response.content

    @staticmethod
    def content_url(path: str):
        return decrypt(path)

    def playlist(self, exclude: set = None, token: str = None, base_url: str = None):
        exclude = exclude or set()
        # Point back at whatever host:port the caller actually used. Hardcoding
        # config.api_url handed out LAN addresses to anyone reaching the app
        # through NAT or a reverse proxy on a different port, so every stream and
        # logo URL in the playlist was unreachable for them.
        base = (base_url or config.api_url).rstrip("/")
        # The player fetches each stream URL directly with no cookie, so the
        # caller's token has to be baked into every line for auth to hold.
        suffix = f"?token={token}" if token else ""
        data = "#EXTM3U\n"
        for channel in self.channels:
            if channel.id in exclude:
                continue
            logo = channel.logo
            # Relative logo paths are useless to an external player like VLC,
            # which has no idea what host the playlist came from.
            if logo and logo.startswith("/"):
                logo = f"{base}{logo}"
            entry = f" tvg-logo=\"{logo}\",{channel.name}" if logo else f",{channel.name}"
            data += f"#EXTINF:-1{entry}\n{base}/api/stream/{channel.id}.m3u8{suffix}\n"
        return data

    # The schedule JSON API is domain-gated (403 "Schedule API Available for
    # allowed Domain only!") and the open .json file is a stale 2025 snapshot, so
    # the live listings are scraped off the homepage where the site renders them.
    _SCHED_DAY_RE = re.compile(r'class="schedule__dayTitle"[^>]*>(.*?)</div>', re.S)
    _SCHED_CAT_RE = re.compile(r'class="card__meta"[^>]*>(.*?)</div>', re.S)
    _SCHED_EVENT_RE = re.compile(r'class="schedule__event"', re.S)
    _SCHED_TIME_RE = re.compile(r'class="schedule__time"[^>]*?data-time="([^"]*)"', re.S)
    _SCHED_TITLE_RE = re.compile(r'class="schedule__eventTitle"[^>]*>(.*?)</span>', re.S)
    _SCHED_CHAN_RE = re.compile(r'href="/watch\.php\?id=(\d+)"[^>]*>(.*?)</a>', re.S)

    @staticmethod
    def _sched_text(fragment: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

    def _parse_schedule(self, page: str) -> dict:
        """Turn the homepage's schedule markup into {day: {category: [events]}}.

        Walks day/category/event markers in document order and attaches each event
        to the most recent headings above it — the blocks nest, which regex can't
        match directly, but their order in the document is unambiguous.
        """
        marks = []
        for m in self._SCHED_DAY_RE.finditer(page):
            marks.append((m.start(), "day", self._sched_text(m.group(1))))
        for m in self._SCHED_CAT_RE.finditer(page):
            marks.append((m.start(), "cat", self._sched_text(m.group(1))))
        for m in self._SCHED_EVENT_RE.finditer(page):
            marks.append((m.start(), "event", None))
        marks.sort(key=lambda x: x[0])

        out, day, cat = {}, None, None
        for i, (pos, kind, value) in enumerate(marks):
            if kind == "day":
                day, cat = value, None
                out.setdefault(day, {})
            elif kind == "cat":
                cat = value
            elif kind == "event" and day and cat:
                end = marks[i + 1][0] if i + 1 < len(marks) else len(page)
                block = page[pos:end]
                when = self._SCHED_TIME_RE.search(block)
                title = self._SCHED_TITLE_RE.search(block)
                if not (when and title):
                    continue
                channels = [
                    {"channel_name": self._sched_text(name), "channel_id": cid}
                    for cid, name in self._SCHED_CHAN_RE.findall(block)
                ]
                out[day].setdefault(cat, []).append({
                    "time": when.group(1).strip(),
                    "event": self._sched_text(title.group(1)),
                    "channels": channels,
                })
        return {d: c for d, c in out.items() if c}

    async def schedule(self):
        try:
            response = await self._session.get(self._base_url, headers=self._headers())
            if response.status_code != 200:
                logger.warning(f"Schedule page returned status {response.status_code}")
                return {}
            parsed = self._parse_schedule(response.text)
            if not parsed:
                logger.warning("No schedule entries found in upstream page")
            else:
                total = sum(len(e) for c in parsed.values() for e in c.values())
                logger.info(f"Parsed schedule: {len(parsed)} day(s), {total} events")
            return parsed
        except Exception as e:
            logger.error(f"Error fetching schedule: {str(e)}")
            return {}
