#!/usr/bin/env python3
"""
Test backend HLS extraction from vidembed
"""

import asyncio
import aiohttp
from freesky.vidembed_extractor import extract_hls_from_vidembed

async def test_backend_hls_extraction():
    """Test the backend HLS extraction functionality"""
    print("🔍 Testing backend HLS extraction...")
    
    # Test vidembed URL (channel 588)
    vidembed_url = "https://vidembed.re/stream/dbd34bec-ad3f-463b-ba3b-dc7ef0a5a20e#autostart"
    
    print(f"🔗 Testing vidembed URL: {vidembed_url}")
    
    try:
        # Test the HLS extraction
        hls_url = await extract_hls_from_vidembed(vidembed_url)
        
        if hls_url:
            print(f"🎬 SUCCESS! HLS stream extracted: {hls_url}")
            
            # Test that the HLS URL actually works
            async with aiohttp.ClientSession() as session:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": vidembed_url,
                }
                
                async with session.get(hls_url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        content = await response.text()
                        if content.startswith("#EXTM3U"):
                            print(f"✅ HLS stream is valid and accessible")
                            print(f"📄 First 200 chars: {content[:200]}...")
                            return True
                        else:
                            print(f"❌ HLS URL returned non-HLS content: {content[:100]}...")
                            return False
                    else:
                        print(f"❌ HLS URL returned HTTP {response.status}")
                        return False
        else:
            print("❌ No HLS stream extracted")
            return False
            
    except Exception as e:
        print(f"❌ Error during HLS extraction: {str(e)}")
        return False

if __name__ == "__main__":
    print("🚀 Starting backend HLS extraction test...")
    
    success = asyncio.run(test_backend_hls_extraction())
    
    if success:
        print("\n✅ Backend HLS extraction test PASSED!")
        print("   The backend can now extract HLS streams from vidembed URLs.")
    else:
        print("\n❌ Backend HLS extraction test FAILED!")
        print("   The backend will fall back to vidembed redirect for these channels.")
    
    print("\n✅ Backend HLS extraction test completed!") 