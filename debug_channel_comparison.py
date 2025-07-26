#!/usr/bin/env python3
"""
Debug Channel Comparison - Raw Response Analysis
"""

import asyncio
import aiohttp

async def debug_channel_comparison():
    """Debug comparison of channels 588 and 857"""
    print("🔍 Debug Channel Comparison...")
    
    base_url = "http://localhost:8005"
    test_channels = ["588", "857"]
    
    async with aiohttp.ClientSession() as session:
        
        for channel_id in test_channels:
            print(f"\n{'='*60}")
            print(f"📺 CHANNEL {channel_id} RAW RESPONSE")
            print(f"{'='*60}")
            
            try:
                async with session.get(f"{base_url}/api/stream/{channel_id}.m3u8") as response:
                    print(f"Status: {response.status}")
                    if response.status == 200:
                        content = await response.text()
                        print(f"Content length: {len(content)} characters")
                        print(f"Raw content:")
                        print("-" * 40)
                        print(content)
                        print("-" * 40)
                        
                        # Analyze the content
                        if "vidembed.re" in content:
                            print(f"🔍 ANALYSIS: Contains vidembed.re URL")
                            lines = content.split("\n")
                            for i, line in enumerate(lines):
                                if "vidembed.re" in line:
                                    print(f"   Line {i+1}: {line}")
                        elif "#EXTM3U" in content:
                            print(f"🔍 ANALYSIS: Contains M3U8 structure")
                            ext_count = content.count("#EXT")
                            print(f"   M3U8 directives: {ext_count}")
                            if "#EXT-X-MEDIA-SEQUENCE" in content:
                                print(f"   ✅ Full M3U8 playlist with media sequence")
                            else:
                                print(f"   ⚠️ Basic M3U8 structure")
                        else:
                            print(f"🔍 ANALYSIS: Unknown format")
                    else:
                        print(f"❌ Backend returned status {response.status}")
                        
            except Exception as e:
                print(f"❌ Test error: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Debug Channel Comparison...")
    asyncio.run(debug_channel_comparison())
    print("\n✅ Debug Channel Comparison completed!") 