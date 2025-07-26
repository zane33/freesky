#!/usr/bin/env python3
"""
Test Multiple Channels to Compare Stream Types
"""

import asyncio
import aiohttp

async def test_multiple_channels():
    """Test multiple channels to see stream types"""
    print("🔍 Testing Multiple Channels...")
    
    base_url = "http://localhost:8005"
    test_channels = ["588", "857", "1", "100", "200", "300"]
    
    async with aiohttp.ClientSession() as session:
        
        for channel_id in test_channels:
            print(f"\n📺 Testing Channel {channel_id}...")
            try:
                async with session.get(f"{base_url}/api/stream/{channel_id}.m3u8") as response:
                    if response.status == 200:
                        content = await response.text()
                        print(f"  Status: ✅ 200 OK")
                        print(f"  Length: {len(content)} characters")
                        
                        if "vidembed.re" in content:
                            print(f"  Type: 🔗 Vidembed URL")
                            vidembed_url = content.split("\n")[-1].strip()
                            print(f"  URL: {vidembed_url[:60]}...")
                        elif "#EXTM3U" in content and "http" in content:
                            print(f"  Type: 🎬 Direct M3U8 Stream")
                            # Find the stream URL
                            lines = content.split("\n")
                            for line in lines:
                                if line.startswith("http"):
                                    print(f"  Stream: {line[:60]}...")
                                    break
                        else:
                            print(f"  Type: ❓ Unknown format")
                            print(f"  Preview: {content[:100]}...")
                    else:
                        print(f"  Status: ❌ {response.status}")
                        
            except Exception as e:
                print(f"  Error: ❌ {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Multiple Channel Test...")
    asyncio.run(test_multiple_channels())
    print("\n✅ Multiple Channel Test completed!") 