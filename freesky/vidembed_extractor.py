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
        Extract HLS stream URL from vidembed URL using iframe-based authentication
        
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
            
            # Store captured API requests
            api_requests = []
            hls_requests = []
            
            # Listen for network requests with specific focus on API calls
            async def handle_request(request):
                url = request.url
                
                # Capture vidembed API calls that match the analysis pattern
                if "/api/source/" in url and "type=live" in url:
                    api_requests.append({
                        'url': url,
                        'headers': request.headers,
                        'method': request.method
                    })
                    logger.info(f"Captured vidembed API request: {url}")
                
                # Capture HLS streams
                if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'playlist', 'master', 'stream']):
                    if 'cdnjs.cloudflare.com' not in url and 'googleapis.com' not in url:
                        hls_requests.append(url)
                        logger.debug(f"Captured HLS request: {url}")
            
            # Listen for network responses to capture API responses
            async def handle_response(response):
                url = response.url
                if "/api/source/" in url and "type=live" in url:
                    try:
                        if response.status == 200:
                            # Try to get the response JSON
                            json_data = await response.json()
                            logger.info(f"API Response from {url}: {json_data}")
                            
                            # Look for stream URLs in the response
                            if 'data' in json_data and isinstance(json_data['data'], list):
                                for item in json_data['data']:
                                    if 'file' in item:
                                        hls_requests.append(item['file'])
                                        logger.info(f"Found stream URL in API response: {item['file']}")
                        else:
                            logger.warning(f"API request failed with status {response.status}")
                    except Exception as e:
                        logger.debug(f"Error parsing API response: {str(e)}")
            
            self._page.on("request", handle_request)
            self._page.on("response", handle_response)
            
            # Create an iframe context to properly handle origin-based authentication
            logger.info("Setting up iframe context for authentication...")
            
            # First, navigate to a page that will embed the vidembed iframe
            iframe_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Vidembed Iframe</title>
            </head>
            <body>
                <iframe src="{vidembed_url}" 
                        width="926" height="500" 
                        frameborder="0" 
                        allowfullscreen
                        id="vidembed-frame">
                </iframe>
                <script>
                    // Monitor the iframe for any postMessage communications
                    window.addEventListener('message', function(event) {{
                        console.log('Received message from iframe:', event.data);
                    }});
                </script>
            </body>
            </html>
            """
            
            # Set page content to include the iframe
            await self._page.set_content(iframe_html)
            
            # Wait for iframe to load (reduced time for speed)
            logger.info("Waiting for iframe to load...")
            await asyncio.sleep(3)  # Reduced from 10 to 3 seconds
            
            # Switch to iframe context to interact within the proper origin
            try:
                iframe_element = await self._page.query_selector('#vidembed-frame')
                if iframe_element:
                    iframe_frame = await iframe_element.content_frame()
                    if iframe_frame:
                        logger.info("Successfully accessed iframe context")
                        
                        # Wait for the iframe content to fully load (reduced)
                        await asyncio.sleep(2)  # Reduced from 5 to 2 seconds
                        
                        # Try to interact with elements within the iframe
                        try:
                            # Look for video elements within iframe
                            video_elements = await iframe_frame.query_selector_all("video")
                            if video_elements:
                                logger.info(f"Found {len(video_elements)} video elements in iframe")
                                for i, video in enumerate(video_elements[:2]):
                                    try:
                                        await video.click()
                                        logger.info(f"Clicked video element {i+1} in iframe")
                                        await asyncio.sleep(3)
                                    except:
                                        pass
                            
                            # Look for play buttons within iframe
                            play_buttons = await iframe_frame.query_selector_all("[class*='play'], [id*='play'], button")
                            if play_buttons:
                                logger.info(f"Found {len(play_buttons)} potential play buttons in iframe")
                                for i, button in enumerate(play_buttons[:3]):
                                    try:
                                        await button.click()
                                        logger.info(f"Clicked play button {i+1} in iframe")
                                        await asyncio.sleep(1)  # Reduced interaction delay
                                    except:
                                        pass
                        except Exception as e:
                            logger.warning(f"Error interacting with iframe content: {str(e)}")
                    else:
                        logger.warning("Could not access iframe content frame")
                else:
                    logger.warning("Could not find iframe element")
            except Exception as e:
                logger.warning(f"Error accessing iframe: {str(e)}")
            
            # Wait reduced time for API calls to be made
            await asyncio.sleep(4)  # Reduced from 10 to 4 seconds
            
            logger.info(f"Captured {len(api_requests)} API requests and {len(hls_requests)} HLS requests")
            
            # Test the captured URLs with proper authentication headers
            async with aiohttp.ClientSession() as session:
                # First, test HLS URLs captured from API responses
                for url in hls_requests:
                    try:
                        logger.info(f"Testing HLS URL: {url}")
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Referer": vidembed_url,
                            "Origin": "https://vidembed.re",
                        }
                        
                        async with session.get(url, headers=headers, timeout=8) as response:
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