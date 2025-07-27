#!/usr/bin/env python3
"""
Test script to verify vidembed URL extraction and M3U8 generation
"""

import asyncio
import aiohttp

async def test_vidembed_extraction():
    """Test that backend extracts M3U8 content from vidembed URLs"""
    print("🔍 Testing vidembed URL extraction and M3U8 generation...")
    
    async with aiohttp.ClientSession() as session:
        # Test the stream endpoint
        async with session.get('http://localhost:8005/api/stream/588.m3u8') as response:
            if response.status == 200:
                content = await response.text()
                print(f"✅ Stream endpoint working!")
                print(f"📊 Response length: {len(content)} characters")
                print(f"📄 Content type: {response.headers.get('content-type', 'unknown')}")
                
                if content.startswith("VIDEMBED_URL:"):
                    print(f"❌ Still returning vidembed URL - extraction failed")
                    vidembed_url = content.replace("VIDEMBED_URL:", "")
                    print(f"🔗 Vidembed URL: {vidembed_url}")
                elif content.startswith("#EXTM3U"):
                    print(f"✅ SUCCESS! Backend extracted M3U8 content from vidembed URL")
                    print(f"🎬 Architecture: New (vidembed.re) → M3U8 extraction")
                    print(f"📄 First 200 chars: {content[:200]}...")
                    
                    # Check if it contains proxied URLs
                    if "/api/content/" in content:
                        print(f"🔗 Contains proxied content URLs")
                    if "/api/key/" in content:
                        print(f"🔑 Contains proxied key URLs")
                else:
                    print(f"❓ Unexpected content type")
                    print(f"📄 First 200 chars: {content[:200]}...")
            else:
                print(f"❌ Stream endpoint failed: {response.status}")
                print(f"📄 Response: {await response.text()}")

if __name__ == "__main__":
    print("🚀 Starting vidembed extraction test...")
    asyncio.run(test_vidembed_extraction())
    print("\n✅ Vidembed extraction test completed!") 