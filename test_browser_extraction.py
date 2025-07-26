#!/usr/bin/env python3
"""
Test browser-based extraction of HLS streams from vidembed
"""

import asyncio
import aiohttp
from freesky.free_sky_hybrid import StepDaddyHybrid

async def test_browser_extraction():
    """Test browser-based extraction"""
    print("🔍 Testing browser-based HLS extraction...")
    
    # Get vidembed URL
    streamer = StepDaddyHybrid()
    await streamer.load_channels()
    
    result = await streamer.stream("588")
    if not result.startswith("VIDEMBED_URL:"):
        print("❌ Not a vidembed URL")
        return
    
    vidembed_url = result.replace("VIDEMBED_URL:", "")
    print(f"🔗 Vidembed URL: {vidembed_url}")
    
    # Try to find the actual stream URL by analyzing the vidembed URL structure
    print("\n🔍 Analyzing vidembed URL structure...")
    
    # Extract the UUID from the vidembed URL
    import re
    uuid_match = re.search(r'/stream/([a-f0-9-]+)', vidembed_url)
    if uuid_match:
        uuid = uuid_match.group(1)
        print(f"📋 UUID: {uuid}")
        
        # Try different API endpoints that vidembed might use
        possible_endpoints = [
            f"https://vidembed.re/api/stream/{uuid}",
            f"https://vidembed.re/api/video/{uuid}",
            f"https://vidembed.re/stream/{uuid}/playlist.m3u8",
            f"https://vidembed.re/stream/{uuid}/master.m3u8",
            f"https://vidembed.re/api/playlist/{uuid}",
            f"https://vidembed.re/api/hls/{uuid}",
        ]
        
        print(f"\n🔗 Testing {len(possible_endpoints)} possible endpoints...")
        
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": vidembed_url,
            }
            
            for endpoint in possible_endpoints:
                try:
                    print(f"\nTesting: {endpoint}")
                    async with session.get(endpoint, headers=headers, timeout=10) as response:
                        if response.status == 200:
                            content = await response.text()
                            if content.startswith("#EXTM3U"):
                                print(f"🎬 SUCCESS! Found HLS stream: {endpoint}")
                                print(f"📄 First 200 chars: {content[:200]}...")
                                return endpoint
                            elif "application/json" in response.headers.get("content-type", ""):
                                try:
                                    json_data = await response.json()
                                    print(f"📄 JSON response: {json_data}")
                                    # Look for stream URL in JSON
                                    if isinstance(json_data, dict):
                                        for key in ["url", "stream", "video", "hls", "playlist"]:
                                            if key in json_data:
                                                stream_url = json_data[key]
                                                print(f"🔗 Found stream URL in JSON: {stream_url}")
                                                # Test the stream URL
                                                async with session.get(stream_url, headers=headers, timeout=10) as stream_response:
                                                    if stream_response.status == 200:
                                                        stream_content = await stream_response.text()
                                                        if stream_content.startswith("#EXTM3U"):
                                                            print(f"🎬 SUCCESS! Found HLS stream via JSON: {stream_url}")
                                                            return stream_url
                                except:
                                    pass
                            else:
                                print(f"📄 Response: {content[:100]}...")
                        else:
                            print(f"❌ HTTP {response.status}")
                except Exception as e:
                    print(f"❌ Error: {str(e)}")
    
    print("\n❌ No HLS stream found via direct API endpoints")
    print("\n💡 Alternative approach: Use browser automation to execute JavaScript")
    print("   This would require tools like Playwright or Selenium to:")
    print("   1. Load the vidembed page")
    print("   2. Execute the JavaScript")
    print("   3. Capture network requests")
    print("   4. Extract the HLS stream URL")

if __name__ == "__main__":
    print("🚀 Starting browser-based extraction test...")
    asyncio.run(test_browser_extraction())
    print("\n✅ Browser-based extraction test completed!") 