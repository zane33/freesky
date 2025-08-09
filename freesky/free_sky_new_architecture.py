#!/usr/bin/env python3
"""
Updated streaming architecture for dlhd.click with vidembed.re integration
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


class StepDaddyNew:
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
        
        logger.info(f"StepDaddyNew initialized with max_streams: {max_streams}")

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
        """New streaming method for dlhd.click with vidembed.re integration"""
        try:
            # Step 1: Initial Stream Request
            url = f"{self._base_url}/stream/stream-{channel_id}.php"
            if len(channel_id) > 3:
                url = f"{self._base_url}/stream/bet.php?id=bet{channel_id}"
            
            # Use semaphore to limit concurrent stream requests
            semaphore = self._get_stream_semaphore()
            async with semaphore:
                logger.debug(f"Making initial request to: {url}")
                response = await self._session.post(url, headers=self._headers())
                
                if response.status_code != 200:
                    raise ValueError(f"Failed to get initial response: HTTP {response.status_code}")
                
                # Step 2: Extract vidembed URL (new pattern)
                logger.debug("Looking for vidembed URL...")
                vidembed_pattern = r'https://vidembed\.re/stream/[^"\']+'
                vidembed_matches = re.findall(vidembed_pattern, response.text)
                
                if not vidembed_matches:
                    raise ValueError("No vidembed URL found in response")
                
                vidembed_url = vidembed_matches[0]
                logger.debug(f"Found vidembed URL: {vidembed_url}")
                
                # Step 3: Access vidembed page
                logger.debug("Accessing vidembed page...")
                vidembed_response = await self._session.get(vidembed_url, headers=self._headers(url))
                
                if vidembed_response.status_code != 200:
                    raise ValueError(f"Failed to access vidembed page: HTTP {vidembed_response.status_code}")
                
                # Step 4: Extract stream information from vidembed page
                logger.debug("Extracting stream information from vidembed page...")
                
                # Look for direct stream URLs in the vidembed response
                stream_urls = self._extract_stream_urls(vidembed_response.text)
                
                if stream_urls:
                    # If we found direct stream URLs, return the first one
                    stream_url = stream_urls[0]
                    logger.debug(f"Found direct stream URL: {stream_url}")
                    
                    # Fetch the stream content
                    stream_response = await self._session.get(stream_url, headers=self._headers(vidembed_url))
                    
                    if stream_response.status_code == 200:
                        # Process the stream content (M3U8, etc.)
                        return self._process_stream_content(stream_response.text, vidembed_url)
                    else:
                        raise ValueError(f"Failed to fetch stream: HTTP {stream_response.status_code}")
                else:
                    # If no direct URLs found, the vidembed page might contain the player
                    # We'll need to return the vidembed URL for client-side processing
                    logger.debug("No direct stream URLs found, returning vidembed URL for client processing")
                    return self._create_vidembed_response(vidembed_url)
                
        except Exception as e:
            logger.error(f"Error in stream method for channel {channel_id}: {str(e)}")
            raise

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
        stream_urls = [url for url in unique_urls if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'stream', 'cdn'])]
        
        return stream_urls

    def _process_stream_content(self, content: str, referer: str) -> str:
        """Process stream content (M3U8, etc.) and proxy URLs"""
        if content.startswith('#EXTM3U'):
            # This is an M3U8 playlist, process it
            lines = content.split('\n')
            processed_lines = []
            
            for line in lines:
                if line.startswith('http') and config.proxy_content:
                    # Proxy content URLs
                    line = f"/api/content/{encrypt(line)}"
                elif line.startswith('#EXT-X-KEY:'):
                    # Process encryption keys
                    url_match = re.search(r'URI="(.*?)"', line)
                    if url_match:
                        original_url = url_match.group(1)
                        # Validate the URL before processing
                        if original_url and original_url.startswith(('http://', 'https://')):
                            line = line.replace(original_url, f"/api/key/{encrypt(original_url)}/{encrypt(urlparse(referer).netloc)}")
                        else:
                            logger.warning(f"Skipping invalid key URL: {original_url}")
                    else:
                        logger.warning(f"Could not extract URL from EXT-X-KEY line: {line}")
                
                processed_lines.append(line)
            
            return '\n'.join(processed_lines)
        else:
            # Not an M3U8 playlist, return as is
            return content

    def _create_vidembed_response(self, vidembed_url: str) -> str:
        """Create a response that includes the vidembed URL for client-side processing"""
        # This could be a JSON response or a custom format
        # For now, we'll return a simple text response with the URL
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
        try:
            url = decrypt(url)
            host = decrypt(host)
            logger.debug(f"Decrypted key URL: {url}")
            logger.debug(f"Decrypted host: {host}")
            
            # Validate URL
            if not url or not url.startswith(('http://', 'https://')):
                raise ValueError(f"Invalid key URL after decryption: {url}")
            
            response = await self._session.get(url, headers=self._headers(f"{host}/", host), timeout=60)
            if response.status_code != 200:
                raise Exception(f"Failed to get key: HTTP {response.status_code}")
            return response.content
        except Exception as e:
            logger.error(f"Error in key method - URL: {url if 'url' in locals() else 'not decrypted'}, Host: {host if 'host' in locals() else 'not decrypted'}, Error: {str(e)}")
            raise

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