"""
Multi-Service Streamer for Multiple Upstream Feeds
Inspired by Kodi addons from https://github.com/LoopAddon/repository.the-loop
"""

import asyncio
import aiohttp
import re
import logging
from typing import Optional, Dict, List, Any
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlparse
from curl_cffi import AsyncSession

logger = logging.getLogger(__name__)

class BaseStreamer(ABC):
    """Base class for all streaming services"""
    
    def __init__(self, name: str):
        self.name = name
        self.session = AsyncSession(
            timeout=30,
            impersonate="chrome110",
            max_redirects=5
        )
    
    @abstractmethod
    async def get_stream_url(self, channel_id: str) -> Optional[str]:
        """Get stream URL for a channel"""
        pass
    
    @abstractmethod
    async def get_channels(self) -> List[Dict[str, Any]]:
        """Get list of available channels"""
        pass
    
    def _headers(self, referer: str = None) -> Dict[str, str]:
        """Get headers for requests"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        return headers

class DLHDStreamer(BaseStreamer):
    """DaddyLive HD Streamer (your existing service)"""
    
    def __init__(self):
        super().__init__("DLHD")
        self.base_url = "https://thedaddy.click"
    
    async def get_stream_url(self, channel_id: str) -> Optional[str]:
        """Get stream URL using your existing hybrid approach"""
        try:
            from .free_sky_hybrid import StepDaddyHybrid
            streamer = StepDaddyHybrid()
            result = await streamer.stream(channel_id)
            
            if result.startswith("VIDEMBED_URL:"):
                vidembed_url = result.replace("VIDEMBED_URL:", "")
                # Return M3U8 format for vidembed
                return f"#EXTM3U\n#EXTINF:-1,DLHD Stream\n{vidembed_url}"
            else:
                return result
        except Exception as e:
            logger.error(f"DLHD stream error: {str(e)}")
            return None
    
    async def get_channels(self) -> List[Dict[str, Any]]:
        """Get channels from DLHD"""
        try:
            from .free_sky_hybrid import StepDaddyHybrid
            streamer = StepDaddyHybrid()
            await streamer.load_channels()
            return [
                {
                    "id": channel.id,
                    "name": channel.name,
                    "logo": channel.logo,
                    "tags": channel.tags,
                    "service": "DLHD"
                }
                for channel in streamer.channels
            ]
        except Exception as e:
            logger.error(f"DLHD channels error: {str(e)}")
            return []

class StreamsProStreamer(BaseStreamer):
    """StreamsPro Live Streaming Service"""
    
    def __init__(self):
        super().__init__("StreamsPro")
        self.base_url = "https://streamspro.live"  # Example URL
        self.api_url = f"{self.base_url}/api"
    
    async def get_stream_url(self, channel_id: str) -> Optional[str]:
        """Get stream URL from StreamsPro"""
        try:
            # Example implementation - adjust based on actual StreamsPro API
            url = f"{self.api_url}/stream/{channel_id}"
            async with self.session.get(url, headers=self._headers()) as response:
                if response.status_code == 200:
                    data = response.json()
                    return data.get("stream_url")
            return None
        except Exception as e:
            logger.error(f"StreamsPro stream error: {str(e)}")
            return None
    
    async def get_channels(self) -> List[Dict[str, Any]]:
        """Get channels from StreamsPro"""
        try:
            url = f"{self.api_url}/channels"
            async with self.session.get(url, headers=self._headers()) as response:
                if response.status_code == 200:
                    data = response.json()
                    return [
                        {
                            "id": channel["id"],
                            "name": channel["name"],
                            "logo": channel.get("logo", ""),
                            "tags": channel.get("tags", []),
                            "service": "StreamsPro"
                        }
                        for channel in data.get("channels", [])
                    ]
            return []
        except Exception as e:
            logger.error(f"StreamsPro channels error: {str(e)}")
            return []

class TheLoopStreamer(BaseStreamer):
    """The Loop Video Streaming Service"""
    
    def __init__(self):
        super().__init__("TheLoop")
        self.base_url = "https://theloop.tv"  # Example URL
    
    async def get_stream_url(self, channel_id: str) -> Optional[str]:
        """Get stream URL from The Loop"""
        try:
            # Example implementation - adjust based on actual The Loop API
            url = f"{self.base_url}/stream/{channel_id}"
            async with self.session.get(url, headers=self._headers()) as response:
                if response.status_code == 200:
                    content = response.text
                    # Look for stream URLs in response
                    stream_patterns = [
                        r'https://[^"\']*\.m3u8[^"\']*',
                        r'https://[^"\']*\.mp4[^"\']*',
                        r'https://[^"\']*stream[^"\']*',
                    ]
                    for pattern in stream_patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            return matches[0]
            return None
        except Exception as e:
            logger.error(f"TheLoop stream error: {str(e)}")
            return None
    
    async def get_channels(self) -> List[Dict[str, Any]]:
        """Get channels from The Loop"""
        try:
            url = f"{self.base_url}/channels"
            async with self.session.get(url, headers=self._headers()) as response:
                if response.status_code == 200:
                    content = response.text
                    # Parse channels from HTML/JSON response
                    # This is a simplified example
                    return [
                        {
                            "id": f"loop_{i}",
                            "name": f"The Loop Channel {i}",
                            "logo": "",
                            "tags": ["TheLoop"],
                            "service": "TheLoop"
                        }
                        for i in range(1, 11)  # Example: 10 channels
                    ]
            return []
        except Exception as e:
            logger.error(f"TheLoop channels error: {str(e)}")
            return []

class PlexusStreamer(BaseStreamer):
    """Plexus P2P Streaming Service"""
    
    def __init__(self):
        super().__init__("Plexus")
        self.base_url = "https://plexus.tv"  # Example URL
    
    async def get_stream_url(self, channel_id: str) -> Optional[str]:
        """Get stream URL from Plexus"""
        try:
            # Plexus typically uses P2P streaming
            # This would need to integrate with Plexus protocol
            url = f"{self.base_url}/stream/{channel_id}"
            async with self.session.get(url, headers=self._headers()) as response:
                if response.status_code == 200:
                    data = response.json()
                    return data.get("magnet_url") or data.get("stream_url")
            return None
        except Exception as e:
            logger.error(f"Plexus stream error: {str(e)}")
            return None
    
    async def get_channels(self) -> List[Dict[str, Any]]:
        """Get channels from Plexus"""
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
                            "service": "Plexus"
                        }
                        for channel in data.get("channels", [])
                    ]
            return []
        except Exception as e:
            logger.error(f"Plexus channels error: {str(e)}")
            return []

class MultiServiceStreamer:
    """Main multi-service streamer that coordinates all services"""
    
    def __init__(self):
        self.services = {
            "DLHD": DLHDStreamer(),
            "StreamsPro": StreamsProStreamer(),
            "TheLoop": TheLoopStreamer(),
            "Plexus": PlexusStreamer(),
        }
        self.enabled_services = ["DLHD"]  # Start with DLHD enabled
    
    def enable_service(self, service_name: str):
        """Enable a streaming service"""
        if service_name in self.services:
            if service_name not in self.enabled_services:
                self.enabled_services.append(service_name)
                logger.info(f"Enabled service: {service_name}")
    
    def disable_service(self, service_name: str):
        """Disable a streaming service"""
        if service_name in self.enabled_services:
            self.enabled_services.remove(service_name)
            logger.info(f"Disabled service: {service_name}")
    
    async def get_stream(self, channel_id: str, service_name: str = None) -> Optional[str]:
        """
        Get stream URL from specified service or try all enabled services
        
        Args:
            channel_id: Channel ID to get stream for
            service_name: Specific service to use (optional)
        
        Returns:
            Stream URL or None if not found
        """
        if service_name:
            # Use specific service
            if service_name in self.services and service_name in self.enabled_services:
                return await self.services[service_name].get_stream_url(channel_id)
            else:
                logger.warning(f"Service {service_name} not available or disabled")
                return None
        
        # Try all enabled services in order
        for service_name in self.enabled_services:
            try:
                logger.info(f"Trying service {service_name} for channel {channel_id}")
                stream_url = await self.services[service_name].get_stream_url(channel_id)
                if stream_url:
                    logger.info(f"Found stream on {service_name}: {stream_url[:100]}...")
                    return stream_url
            except Exception as e:
                logger.error(f"Error with service {service_name}: {str(e)}")
                continue
        
        logger.warning(f"No stream found for channel {channel_id} on any service")
        return None
    
    async def get_all_channels(self) -> List[Dict[str, Any]]:
        """Get all channels from all enabled services"""
        all_channels = []
        
        for service_name in self.enabled_services:
            try:
                logger.info(f"Getting channels from {service_name}")
                channels = await self.services[service_name].get_channels()
                all_channels.extend(channels)
            except Exception as e:
                logger.error(f"Error getting channels from {service_name}: {str(e)}")
                continue
        
        return all_channels
    
    async def search_channels(self, query: str) -> List[Dict[str, Any]]:
        """Search for channels across all services"""
        all_channels = await self.get_all_channels()
        
        query_lower = query.lower()
        results = []
        
        for channel in all_channels:
            if (query_lower in channel["name"].lower() or 
                any(query_lower in tag.lower() for tag in channel.get("tags", []))):
                results.append(channel)
        
        return results
    
    def get_service_status(self) -> Dict[str, bool]:
        """Get status of all services"""
        return {
            service_name: service_name in self.enabled_services
            for service_name in self.services.keys()
        }

# Global instance
multi_streamer = MultiServiceStreamer() 