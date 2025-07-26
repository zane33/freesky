#!/usr/bin/env python3
"""
Test Improved Hybrid Streaming
"""

import asyncio
import aiohttp

async def test_improved_hybrid():
    """Test improved hybrid streaming"""
    print("🔍 Testing Improved Hybrid Streaming...")
    
    base_url = "http://localhost:8005"
    test_channels = ["588", "857"]
    
    async with aiohttp.ClientSession() as session:
        
        for channel_id in test_channels:
            print(f"\n📺 Testing Channel {channel_id}...")
            try:
                async with session.get(f"{base_url}/api/stream/{channel_id}.m3u8") as response:
                    print(f"Status: {response.status}")
                    if response.status == 200:
                        content = await response.text()
                        print(f"Content length: {len(content)} characters")
                        
                        if "vidembed.re" in content:
                            print(f"Type: 🔗 Vidembed URL (New Architecture)")
                            vidembed_url = content.split("\n")[-1].strip()
                            print(f"URL: {vidembed_url[:60]}...")
                        elif "#EXTM3U" in content and "#EXT-X-MEDIA-SEQUENCE" in content:
                            print(f"Type: 🎬 Direct M3U8 Stream (Old Architecture)")
                            print(f"✅ This should work with your video player!")
                            # Count M3U8 directives
                            ext_count = content.count("#EXT")
                            print(f"M3U8 directives: {ext_count}")
                        elif "#EXTM3U" in content:
                            print(f"Type: 🎬 Basic M3U8 Stream")
                            print(f"✅ This should work with your video player!")
                        else:
                            print(f"Type: ❓ Unknown format")
                            print(f"Preview: {content[:200]}...")
                    else:
                        print(f"❌ Backend returned status {response.status}")
                        
            except Exception as e:
                print(f"❌ Test error: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Improved Hybrid Test...")
    asyncio.run(test_improved_hybrid())
    print("\n✅ Improved Hybrid Test completed!") 