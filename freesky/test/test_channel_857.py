#!/usr/bin/env python3
"""
Test Channel 857 Specifically - Since User Can Watch It
"""

import asyncio
import aiohttp

async def test_channel_857():
    """Test channel 857 specifically"""
    print("🔍 Testing Channel 857 (Working Channel)...")
    
    base_url = "http://localhost:8005"
    
    async with aiohttp.ClientSession() as session:
        
        # Test 1: Get stream directly from backend
        print("\n1. Testing Backend Stream for Channel 857...")
        try:
            async with session.get(f"{base_url}/api/stream/857.m3u8") as response:
                print(f"Status: {response.status}")
                if response.status == 200:
                    content = await response.text()
                    print(f"Content length: {len(content)} characters")
                    print(f"Content preview:")
                    print("=" * 50)
                    print(content[:1000])  # Show more content
                    print("=" * 50)
                    
                    # Check if it's a vidembed URL
                    if "vidembed.re" in content:
                        print("🔗 Contains vidembed.re URL")
                        vidembed_url = content.split("\n")[-1].strip()
                        print(f"Vidembed URL: {vidembed_url}")
                    else:
                        print("🎬 No vidembed.re URL found - might be direct M3U8")
                        
                    # Check for M3U8 patterns
                    if "#EXTM3U" in content and "#EXTINF" in content:
                        print("✅ Contains M3U8 playlist structure")
                        
                    # Look for direct stream URLs
                    lines = content.split("\n")
                    for i, line in enumerate(lines):
                        if line.startswith("http") and (".m3u8" in line or ".ts" in line):
                            print(f"🎯 Found direct stream URL: {line}")
                            break
                        
                else:
                    print(f"❌ Backend returned status {response.status}")
        except Exception as e:
            print(f"❌ Backend test error: {str(e)}")
        
        # Test 2: Compare with channel 588
        print("\n2. Comparing with Channel 588...")
        try:
            async with session.get(f"{base_url}/api/stream/588.m3u8") as response:
                if response.status == 200:
                    content_588 = await response.text()
                    print(f"Channel 588 length: {len(content_588)} characters")
                    print(f"Channel 857 length: {len(content)} characters")
                    
                    if len(content) > len(content_588):
                        print(f"✅ Channel 857 has more content ({len(content)} vs {len(content_588)})")
                    else:
                        print(f"❓ Both channels have similar content length")
                        
                    # Check if 857 has more M3U8 structure
                    if content.count("#EXT") > content_588.count("#EXT"):
                        print(f"✅ Channel 857 has more M3U8 directives")
                    else:
                        print(f"❓ Similar M3U8 structure")
                        
        except Exception as e:
            print(f"❌ Comparison error: {str(e)}")
        
        # Test 3: Check if 857 vidembed URL is different
        print("\n3. Testing Channel 857 Vidembed URL...")
        try:
            if "vidembed.re" in content:
                vidembed_url = content.split("\n")[-1].strip()
                print(f"Testing vidembed URL: {vidembed_url}")
                
                # Test the vidembed URL
                async with session.get(vidembed_url) as vidembed_response:
                    print(f"Vidembed status: {vidembed_response.status}")
                    if vidembed_response.status == 200:
                        vidembed_content = await vidembed_response.text()
                        print(f"Vidembed content length: {len(vidembed_content)}")
                        
                        # Check if this vidembed returns M3U8
                        if "#EXTM3U" in vidembed_content:
                            print("✅ Vidembed returns M3U8 content!")
                            print("First 200 chars of vidembed content:")
                            print(vidembed_content[:200])
                        else:
                            print("❌ Vidembed doesn't return M3U8")
                    else:
                        print("❌ Vidembed URL not accessible")
        except Exception as e:
            print(f"❌ Vidembed test error: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Channel 857 Test...")
    asyncio.run(test_channel_857())
    print("\n✅ Channel 857 Test completed!") 