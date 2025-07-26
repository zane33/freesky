"""
Vidembed HLS Stream Extractor using Playwright
"""

import asyncio
import aiohttp
import logging
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

class VidembedExtractor:
    """Extract HLS streams from vidembed URLs using Playwright"""
    
    def __init__(self):
        self._browser = None
        self._page = None
        self._playwright = None
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self._setup_browser()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self._cleanup()
    
    async def _setup_browser(self):
        """Setup Playwright browser"""
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-gpu'
                ]
            )
            self._page = await self._browser.new_page()
            
            # Set user agent
            await self._page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })
            
            logger.info("Playwright browser setup complete")
        except Exception as e:
            logger.error(f"Failed to setup Playwright browser: {str(e)}")
            raise
    
    async def _cleanup(self):
        """Cleanup Playwright resources"""
        try:
            if self._page:
                await self._page.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}")
    
    async def extract_hls_stream(self, vidembed_url: str) -> Optional[str]:
        """
        Extract HLS stream URL from vidembed URL
        
        Args:
            vidembed_url: The vidembed URL to extract from
            
        Returns:
            HLS stream URL if found, None otherwise
        """
        if not self._page:
            logger.error("Browser not initialized")
            return None
        
        try:
            logger.info(f"Extracting HLS stream from: {vidembed_url}")
            
            # Store captured requests
            hls_requests = []
            
            # Listen for network requests
            async def handle_request(request):
                url = request.url
                if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'playlist', 'master', 'stream']):
                    if 'cdnjs.cloudflare.com' not in url and 'googleapis.com' not in url:
                        hls_requests.append(url)
                        logger.debug(f"Captured HLS request: {url}")
            
            self._page.on("request", handle_request)
            
            # Navigate to vidembed URL
            logger.info("Loading vidembed page...")
            await self._page.goto(vidembed_url, wait_until="networkidle", timeout=30000)
            
            # Wait for JavaScript execution
            logger.info("Waiting for JavaScript execution...")
            await asyncio.sleep(10)
            
            # Try to interact with the page to trigger more requests
            try:
                # Look for video elements and try to play them
                video_elements = await self._page.query_selector_all("video")
                if video_elements:
                    logger.info(f"Found {len(video_elements)} video elements")
                    for i, video in enumerate(video_elements[:2]):
                        try:
                            await video.click()
                            logger.info(f"Clicked video element {i+1}")
                            await asyncio.sleep(2)
                        except:
                            pass
                
                # Look for play buttons
                play_buttons = await self._page.query_selector_all("[class*='play'], [id*='play'], button")
                if play_buttons:
                    logger.info(f"Found {len(play_buttons)} potential play buttons")
                    for i, button in enumerate(play_buttons[:3]):
                        try:
                            await button.click()
                            logger.info(f"Clicked play button {i+1}")
                            await asyncio.sleep(2)
                        except:
                            pass
            except Exception as e:
                logger.warning(f"Error interacting with page: {str(e)}")
            
            # Wait a bit more after interactions
            await asyncio.sleep(5)
            
            logger.info(f"Captured {len(hls_requests)} potential HLS requests")
            
            # Test the captured URLs
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": vidembed_url,
                }
                
                for url in hls_requests:
                    try:
                        logger.info(f"Testing captured URL: {url}")
                        async with session.get(url, headers=headers, timeout=10) as response:
                            if response.status == 200:
                                content = await response.text()
                                if content.startswith("#EXTM3U"):
                                    logger.info(f"SUCCESS! Found valid HLS stream: {url}")
                                    return url
                                else:
                                    logger.debug(f"Not HLS content: {content[:100]}...")
                            else:
                                logger.debug(f"HTTP {response.status} for {url}")
                    except Exception as e:
                        logger.debug(f"Error testing {url}: {str(e)}")
            
            logger.warning("No valid HLS streams found in captured requests")
            return None
            
        except Exception as e:
            logger.error(f"Error extracting HLS stream: {str(e)}")
            return None

# Global extractor instance
_extractor = None

async def get_extractor() -> VidembedExtractor:
    """Get or create global extractor instance"""
    global _extractor
    if _extractor is None:
        _extractor = VidembedExtractor()
        await _extractor._setup_browser()
    return _extractor

async def extract_hls_from_vidembed(vidembed_url: str) -> Optional[str]:
    """
    Extract HLS stream from vidembed URL
    
    Args:
        vidembed_url: The vidembed URL to extract from
        
    Returns:
        HLS stream URL if found, None otherwise
    """
    try:
        extractor = await get_extractor()
        return await extractor.extract_hls_stream(vidembed_url)
    except Exception as e:
        logger.error(f"Error in extract_hls_from_vidembed: {str(e)}")
        return None

async def cleanup_extractor():
    """Cleanup global extractor instance"""
    global _extractor
    if _extractor:
        await _extractor._cleanup()
        _extractor = None 