#!/usr/bin/env python3
"""
Test backend response for channel 588 to understand the React error
"""

import asyncio
import aiohttp

async def test_backend_response():
    """Test what the backend returns for channel 588"""
    print("🔍 Testing backend response for channel 588...")
    
    # Test the stream endpoint
    stream_url = "http://localhost:8005/api/stream/588.m3u8"
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            
            print(f"🔗 Testing: {stream_url}")
            async with session.get(stream_url, headers=headers, timeout=30) as response:
                print(f"📄 Status: {response.status}")
                print(f"📄 Content-Type: {response.headers.get('content-type', 'unknown')}")
                
                content = await response.text()
                print(f"📄 Content length: {len(content)} characters")
                print(f"📄 First 200 chars: {content[:200]}...")
                
                if content.startswith("VIDEMBED_REDIRECT:"):
                    print("✅ Backend is returning VIDEMBED_REDIRECT as expected")
                    vidembed_url = content.replace("VIDEMBED_REDIRECT:", "")
                    print(f"🔗 Vidembed URL: {vidembed_url}")
                elif content.startswith("#EXTM3U"):
                    print("✅ Backend is returning direct M3U8 stream")
                else:
                    print("❌ Backend is returning unexpected content")
                    
    except Exception as e:
        print(f"❌ Error testing backend: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting backend response test...")
    asyncio.run(test_backend_response())
    print("\n✅ Backend response test completed!") 