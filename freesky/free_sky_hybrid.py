#!/usr/bin/env python3
"""
Hybrid streaming architecture that handles both old and new dlhd.click patterns
"""
import json
import os
import re
import reflex as rx
import logging
import asyncio
from urllib.parse import quote, urlparse
from curl_cffi import AsyncSession
from typing import List
from .utils import encrypt, decrypt, urlsafe_base64, extract_and_decode_var
from .token_validator import TokenValidator, extract_viable_streams
from rxconfig import config

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Channel(rx.Base):
    id: str
    name: str
    tags: List[str]
    logo: str


class StepDaddyHybrid:
    def __init__(self):
        socks5 = config.socks5
        max_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "10"))
        
        session_config = {
            "timeout": 45,
            "impersonate": "chrome110",
            "max_redirects": 5,
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
                response = await self._session.get(f"{self._base_url}/24-7-channels.php", headers=self._headers())
                
                logger.debug(f"Got response with status {response.status_code}")
                if response.status_code != 200:
                    logger.error(f"Failed to fetch channels: HTTP {response.status_code}")
                    return

                logger.debug("Looking for channels block in response")
                channels_block = re.compile("<center><h1(.+?)tab-2", re.MULTILINE | re.DOTALL).findall(str(response.text))

                if not channels_block:
                    logger.error("No channels block found in response")
                    logger.debug(f"Response text: {response.text[:500]}...")
                    return

                logger.debug("Found channels block, extracting channel data")
                channels_data = re.compile("href=\"(.*)\" target(.*)<strong>(.*)</strong>").findall(channels_block[0])
                logger.debug(f"Found {len(channels_data)} raw channel entries")

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
        channel_id = channel_data[0].split('-')[1].replace('.php', '')
        channel_name = channel_data[2]
        if channel_id == "666":
            channel_name = "Nick Music"
        if channel_id == "609":
            channel_name = "Yas TV UAE"
        if channel_data[2] == "#0 Spain":
            channel_name = "Movistar Plus+"
        elif channel_data[2] == "#Vamos Spain":
            channel_name = "Vamos Spain"
        clean_channel_name = re.sub(r"\s*\(.*?\)", "", channel_name)
        meta = self._meta.get(clean_channel_name, {})
        logo = meta.get("logo", "/missing.png")
        if logo.startswith("http"):
            logo = f"/api/logo/{urlsafe_base64(logo)}"
        return Channel(id=channel_id, name=channel_name, tags=meta.get("tags", []), logo=logo)

    async def stream(self, channel_id: str):
        """
        Handle channel streaming with proper vidembed.re architecture detection and fallback
        """
        try:
            # First, get the channel page to determine architecture
            channel_page_url = f"{self._base_url}/stream/stream-{channel_id}.php"
            response = await self._session.get(channel_page_url, headers=self._headers())
            
            if response.status_code != 200:
                raise ValueError(f"Failed to access channel page: HTTP {response.status_code}")
            
            # Look for vidembed.re iframe in the channel page
            vidembed_pattern = r'https://vidembed\.re/stream/([a-f0-9-]{36})'
            vidembed_matches = re.findall(vidembed_pattern, response.text)
            
            if vidembed_matches:
                # Found vidembed.re URL - use new architecture
                vidembed_uuid = vidembed_matches[0]
                vidembed_url = f"https://vidembed.re/stream/{vidembed_uuid}"
                logger.info(f"Found vidembed.re URL for channel {channel_id}: {vidembed_url}")
                return await self._handle_new_architecture(vidembed_url, channel_page_url)
            else:
                # No vidembed.re found, fall back to old architecture
                logger.info(f"No vidembed.re URL found for channel {channel_id}, using old architecture")
                iframe_url = f"https://fnjplay.xyz/premiumtv/daddylivehd.php?id={channel_id}"
                return await self._handle_old_architecture(iframe_url, self._base_url)
                
        except Exception as e:
            logger.error(f"Error in stream architecture detection for channel {channel_id}: {str(e)}")
            # Final fallback to old architecture
            try:
                iframe_url = f"https://fnjplay.xyz/premiumtv/daddylivehd.php?id={channel_id}"
                return await self._handle_old_architecture(iframe_url, self._base_url)
            except Exception as fallback_error:
                logger.error(f"Fallback also failed: {str(fallback_error)}")
                raise

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
                elif line.startswith("http") and config.proxy_content:
                    line = f"/api/content/{encrypt(line)}"
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
                        if not token_analysis.get('valid', True):  # Default to True if no tokens
                            logger.debug(f"Skipping expired stream: {line}")
                            continue  # Skip expired streams
                        elif token_analysis.get('expires_in_seconds', float('inf')) < 3600:  # Less than 1 hour
                            logger.warning(f"Stream expires soon: {token_analysis.get('expires_in_seconds', 0)} seconds")
                    except Exception as validation_error:
                        logger.debug(f"Token validation error for {line}: {str(validation_error)}")
                    
                    if config.proxy_content:
                        # Proxy content URLs
                        line = f"/api/content/{encrypt(line)}"
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

    def playlist(self):
        data = "#EXTM3U\n"
        for channel in self.channels:
            entry = f" tvg-logo=\"{channel.logo}\",{channel.name}" if channel.logo else f",{channel.name}"
            data += f"#EXTINF:-1{entry}\n{config.api_url}/api/stream/{channel.id}.m3u8\n"
        return data

    async def schedule(self):
        response = await self._session.get(f"{self._base_url}/schedule/schedule-generated.php", headers=self._headers())
        return response.json() 