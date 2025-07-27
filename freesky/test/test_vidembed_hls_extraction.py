#!/usr/bin/env python3
"""
Advanced vidembed HLS extraction test
"""

import asyncio
import aiohttp
import re
import json
from freesky.free_sky_hybrid import StepDaddyHybrid

async def test_vidembed_hls_extraction():
    """Test different methods to extract HLS streams from vidembed"""
    print("🔍 Advanced vidembed HLS extraction test...")
    
    # Step 1: Get the vidembed URL
    print("\n1. Getting vidembed URL...")
    streamer = StepDaddyHybrid()
    await streamer.load_channels()
    
    result = await streamer.stream("588")
    if not result.startswith("VIDEMBED_URL:"):
        print("❌ Not a vidembed URL")
        return
    
    vidembed_url = result.replace("VIDEMBED_URL:", "")
    print(f"🔗 Vidembed URL: {vidembed_url}")
    
    # Step 2: Extract vidembed page content
    print("\n2. Loading vidembed page...")
    async with aiohttp.ClientSession() as session:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        
        async with session.get(vidembed_url, headers=headers) as response:
            if response.status != 200:
                print(f"❌ Failed to load vidembed page: {response.status}")
                return
            
            content = await response.text()
            print(f"✅ Vidembed page loaded: {len(content)} characters")
            
            # Step 3: Look for different patterns
            print("\n3. Searching for HLS streams...")
            
            # Method 1: Look for direct HLS URLs
            hls_patterns = [
                r'https://[^"\']*\.m3u8[^"\']*',
                r'https://[^"\']*\.m3u8\?[^"\']*',
                r'https://[^"\']*playlist\.m3u8[^"\']*',
                r'https://[^"\']*master\.m3u8[^"\']*',
            ]
            
            found_hls = []
            for pattern in hls_patterns:
                matches = re.findall(pattern, content)
                found_hls.extend(matches)
            
            if found_hls:
                print(f"✅ Found {len(found_hls)} potential HLS URLs:")
                for i, url in enumerate(found_hls[:5]):
                    print(f"  {i+1}. {url}")
            
            # Method 2: Look for JavaScript variables
            print("\n4. Searching for JavaScript variables...")
            js_patterns = [
                r'var\s+streamUrl\s*=\s*["\']([^"\']+)["\']',
                r'var\s+videoUrl\s*=\s*["\']([^"\']+)["\']',
                r'var\s+hlsUrl\s*=\s*["\']([^"\']+)["\']',
                r'streamUrl\s*:\s*["\']([^"\']+)["\']',
                r'videoUrl\s*:\s*["\']([^"\']+)["\']',
                r'hlsUrl\s*:\s*["\']([^"\']+)["\']',
            ]
            
            found_js = []
            for pattern in js_patterns:
                matches = re.findall(pattern, content)
                found_js.extend(matches)
            
            if found_js:
                print(f"✅ Found {len(found_js)} JavaScript variables:")
                for i, url in enumerate(found_js[:5]):
                    print(f"  {i+1}. {url}")
            
            # Method 3: Look for JSON data
            print("\n5. Searching for JSON data...")
            json_patterns = [
                r'\{[^}]*"url"[^}]*\}',
                r'\{[^}]*"stream"[^}]*\}',
                r'\{[^}]*"video"[^}]*\}',
            ]
            
            found_json = []
            for pattern in json_patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    try:
                        data = json.loads(match)
                        if 'url' in data:
                            found_json.append(data['url'])
                        if 'stream' in data:
                            found_json.append(data['stream'])
                        if 'video' in data:
                            found_json.append(data['video'])
                    except:
                        pass
            
            if found_json:
                print(f"✅ Found {len(found_json)} JSON URLs:")
                for i, url in enumerate(found_json[:5]):
                    print(f"  {i+1}. {url}")
            
            # Method 4: Look for iframe sources
            print("\n6. Searching for iframe sources...")
            iframe_pattern = r'<iframe[^>]*src=["\']([^"\']+)["\'][^>]*>'
            iframe_matches = re.findall(iframe_pattern, content)
            
            if iframe_matches:
                print(f"✅ Found {len(iframe_matches)} iframe sources:")
                for i, url in enumerate(iframe_matches[:5]):
                    print(f"  {i+1}. {url}")
            
            # Method 5: Look for video sources
            print("\n7. Searching for video sources...")
            video_pattern = r'<video[^>]*src=["\']([^"\']+)["\'][^>]*>'
            video_matches = re.findall(video_pattern, content)
            
            if video_matches:
                print(f"✅ Found {len(video_matches)} video sources:")
                for i, url in enumerate(video_matches[:5]):
                    print(f"  {i+1}. {url}")
            
            # Step 4: Test found URLs
            all_urls = found_hls + found_js + found_json + iframe_matches + video_matches
            if all_urls:
                print(f"\n8. Testing {len(all_urls)} found URLs...")
                
                for i, url in enumerate(all_urls[:3]):  # Test first 3
                    print(f"\nTesting URL {i+1}: {url}")
                    try:
                        async with session.get(url, timeout=10) as test_response:
                            if test_response.status == 200:
                                test_content = await test_response.text()
                                if test_content.startswith("#EXTM3U"):
                                    print(f"🎬 SUCCESS! Found valid HLS stream: {url}")
                                    print(f"📄 First 200 chars: {test_content[:200]}...")
                                    return url
                                else:
                                    print(f"❌ Not HLS content: {test_content[:100]}...")
                            else:
                                print(f"❌ HTTP {test_response.status}")
                    except Exception as e:
                        print(f"❌ Error: {str(e)}")
            else:
                print("❌ No URLs found in vidembed page")

if __name__ == "__main__":
    print("🚀 Starting advanced vidembed HLS extraction test...")
    asyncio.run(test_vidembed_hls_extraction())
    print("\n✅ Advanced vidembed HLS extraction test completed!") 