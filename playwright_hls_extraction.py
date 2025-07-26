#!/usr/bin/env python3
"""
Extract HLS streams from vidembed using Playwright
"""

import asyncio
import aiohttp
from freesky.free_sky_hybrid import StepDaddyHybrid

async def extract_hls_with_playwright():
    """Extract HLS stream using Playwright"""
    print("🔍 Extracting HLS stream with Playwright...")
    
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ Playwright not installed. Install with: pip install playwright")
        print("   Then run: playwright install")
        return None
    
    # Get vidembed URL
    streamer = StepDaddyHybrid()
    await streamer.load_channels()
    
    result = await streamer.stream("588")
    if not result.startswith("VIDEMBED_URL:"):
        print("❌ Not a vidembed URL")
        return None
    
    vidembed_url = result.replace("VIDEMBED_URL:", "")
    print(f"🔗 Vidembed URL: {vidembed_url}")
    
    # Use Playwright to capture network requests
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Store captured requests and responses
        hls_requests = []
        hls_responses = []
        
        # Listen for network requests
        async def handle_request(request):
            url = request.url
            # More specific HLS patterns
            if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'playlist', 'master', 'stream']):
                if 'cdnjs.cloudflare.com' not in url and 'googleapis.com' not in url:
                    hls_requests.append(url)
                    print(f"🔗 Captured HLS request: {url}")
        
        # Listen for network responses
        async def handle_response(response):
            url = response.url
            content_type = response.headers.get("content-type", "")
            
            # Check for HLS content types
            if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'playlist', 'master', 'stream']):
                if 'cdnjs.cloudflare.com' not in url and 'googleapis.com' not in url:
                    hls_responses.append({
                        'url': url,
                        'status': response.status,
                        'content_type': content_type
                    })
                    print(f"📄 Captured HLS response: {url} (Status: {response.status}, Type: {content_type})")
        
        page.on("request", handle_request)
        page.on("response", handle_response)
        
        try:
            print("🌐 Loading vidembed page with Playwright...")
            await page.goto(vidembed_url, wait_until="networkidle", timeout=30000)
            
            # Wait longer for JavaScript to execute and load streams
            print("⏳ Waiting for JavaScript execution...")
            await asyncio.sleep(10)
            
            # Try to interact with the page to trigger more requests
            try:
                # Look for video elements and try to play them
                video_elements = await page.query_selector_all("video")
                if video_elements:
                    print(f"🎥 Found {len(video_elements)} video elements")
                    for i, video in enumerate(video_elements[:2]):  # Try first 2
                        try:
                            await video.click()
                            print(f"🎬 Clicked video element {i+1}")
                            await asyncio.sleep(2)
                        except:
                            pass
                
                # Look for play buttons
                play_buttons = await page.query_selector_all("[class*='play'], [id*='play'], button")
                if play_buttons:
                    print(f"▶️ Found {len(play_buttons)} potential play buttons")
                    for i, button in enumerate(play_buttons[:3]):  # Try first 3
                        try:
                            await button.click()
                            print(f"🎬 Clicked play button {i+1}")
                            await asyncio.sleep(2)
                        except:
                            pass
            except Exception as e:
                print(f"⚠️ Error interacting with page: {str(e)}")
            
            # Wait a bit more after interactions
            await asyncio.sleep(5)
            
            print(f"✅ Captured {len(hls_requests)} potential HLS requests")
            print(f"✅ Captured {len(hls_responses)} potential HLS responses")
            
            # Test the captured URLs
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": vidembed_url,
                }
                
                # Test requests
                for url in hls_requests:
                    try:
                        print(f"\nTesting captured URL: {url}")
                        async with session.get(url, headers=headers, timeout=10) as response:
                            if response.status == 200:
                                content = await response.text()
                                if content.startswith("#EXTM3U"):
                                    print(f"🎬 SUCCESS! Found valid HLS stream: {url}")
                                    print(f"📄 First 200 chars: {content[:200]}...")
                                    await browser.close()
                                    return url
                                else:
                                    print(f"❌ Not HLS content: {content[:100]}...")
                            else:
                                print(f"❌ HTTP {response.status}")
                    except Exception as e:
                        print(f"❌ Error testing {url}: {str(e)}")
                
                # Test responses that might contain HLS
                for response_info in hls_responses:
                    if response_info['status'] == 200:
                        url = response_info['url']
                        try:
                            print(f"\nTesting response URL: {url}")
                            async with session.get(url, headers=headers, timeout=10) as response:
                                if response.status == 200:
                                    content = await response.text()
                                    if content.startswith("#EXTM3U"):
                                        print(f"🎬 SUCCESS! Found valid HLS stream: {url}")
                                        print(f"📄 First 200 chars: {content[:200]}...")
                                        await browser.close()
                                        return url
                                    else:
                                        print(f"❌ Not HLS content: {content[:100]}...")
                                else:
                                    print(f"❌ HTTP {response.status}")
                        except Exception as e:
                            print(f"❌ Error testing {url}: {str(e)}")
            
            await browser.close()
            print("❌ No valid HLS streams found in captured requests")
            return None
            
        except Exception as e:
            print(f"❌ Error with Playwright: {str(e)}")
            await browser.close()
            return None

if __name__ == "__main__":
    print("🚀 Starting Playwright HLS extraction...")
    
    # Try Playwright extraction
    result = asyncio.run(extract_hls_with_playwright())
    
    if result:
        print(f"\n✅ SUCCESS! HLS stream extracted: {result}")
    else:
        print("\n❌ No HLS stream found")
    
    print("\n✅ Playwright HLS extraction completed!") 