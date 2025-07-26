#!/usr/bin/env python3
"""
Manual test of vidembed extraction process
"""

import asyncio
import aiohttp
import re
from freesky.free_sky_hybrid import StepDaddyHybrid

async def test_manual_vidembed_extraction():
    """Manually test the vidembed extraction process"""
    print("🔍 Manual vidembed extraction test...")
    
    # Step 1: Get the vidembed URL from the hybrid class
    print("\n1. Getting vidembed URL from hybrid class...")
    streamer = StepDaddyHybrid()
    await streamer.load_channels()
    
    result = await streamer.stream("588")
    print(f"✅ Hybrid class result: {result[:100]}...")
    
    if result.startswith("VIDEMBED_URL:"):
        vidembed_url = result.replace("VIDEMBED_URL:", "")
        print(f"🔗 Vidembed URL: {vidembed_url}")
        
        # Step 2: Manually extract stream from vidembed
        print("\n2. Manually extracting stream from vidembed...")
        async with aiohttp.ClientSession() as session:
            async with session.get(vidembed_url) as response:
                if response.status == 200:
                    vidembed_content = await response.text()
                    print(f"✅ Vidembed page loaded: {len(vidembed_content)} characters")
                    
                    # Step 3: Look for stream URLs
                    print("\n3. Looking for stream URLs...")
                    stream_patterns = [
                        r'https://[^"\']*\.m3u8[^"\']*',
                        r'https://[^"\']*\.mp4[^"\']*',
                        r'https://[^"\']*stream[^"\']*',
                        r'https://[^"\']*cdn[^"\']*',
                    ]
                    
                    found_urls = []
                    for pattern in stream_patterns:
                        matches = re.findall(pattern, vidembed_content)
                        # Filter out CDN libraries
                        stream_urls = [url for url in matches if 'cdnjs.cloudflare.com' not in url]
                        found_urls.extend(stream_urls)
                    
                    if found_urls:
                        print(f"✅ Found {len(found_urls)} potential stream URLs:")
                        for i, url in enumerate(found_urls[:5]):  # Show first 5
                            print(f"  {i+1}. {url}")
                        
                        # Step 4: Test the first stream URL
                        direct_stream_url = found_urls[0]
                        print(f"\n4. Testing direct stream URL: {direct_stream_url}")
                        
                        async with session.get(direct_stream_url) as stream_response:
                            if stream_response.status == 200:
                                stream_content = await stream_response.text()
                                print(f"✅ Direct stream loaded: {len(stream_content)} characters")
                                print(f"📄 First 200 chars: {stream_content[:200]}...")
                                
                                if stream_content.startswith("#EXTM3U"):
                                    print(f"🎬 SUCCESS! Found valid M3U8 content")
                                else:
                                    print(f"❌ Not M3U8 content")
                            else:
                                print(f"❌ Direct stream failed: {stream_response.status}")
                    else:
                        print(f"❌ No stream URLs found in vidembed content")
                else:
                    print(f"❌ Vidembed page failed: {response.status}")

if __name__ == "__main__":
    print("🚀 Starting manual vidembed extraction test...")
    asyncio.run(test_manual_vidembed_extraction())
    print("\n✅ Manual vidembed extraction test completed!") 