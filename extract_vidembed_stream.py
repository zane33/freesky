#!/usr/bin/env python3
"""
Extract actual stream URL from vidembed by analyzing JavaScript and API endpoints
"""

import asyncio
import aiohttp
import re
import json
from freesky.free_sky_hybrid import StepDaddyHybrid

async def extract_vidembed_stream():
    """Extract actual stream URL from vidembed"""
    print("🔍 Extracting vidembed stream URL...")
    
    # Get vidembed URL
    streamer = StepDaddyHybrid()
    await streamer.load_channels()
    
    result = await streamer.stream("588")
    if not result.startswith("VIDEMBED_URL:"):
        print("❌ Not a vidembed URL")
        return None
    
    vidembed_url = result.replace("VIDEMBED_URL:", "")
    print(f"🔗 Vidembed URL: {vidembed_url}")
    
    # Extract UUID from vidembed URL
    uuid_match = re.search(r'/stream/([a-f0-9-]+)', vidembed_url)
    if not uuid_match:
        print("❌ Could not extract UUID from vidembed URL")
        return None
    
    uuid = uuid_match.group(1)
    print(f"📋 UUID: {uuid}")
    
    # Try different approaches to get the actual stream
    async with aiohttp.ClientSession() as session:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": vidembed_url,
        }
        
        # Approach 1: Try vidembed API endpoints
        print("\n🔍 Approach 1: Trying vidembed API endpoints...")
        api_endpoints = [
            f"https://vidembed.re/api/stream/{uuid}",
            f"https://vidembed.re/api/video/{uuid}",
            f"https://vidembed.re/api/playlist/{uuid}",
            f"https://vidembed.re/api/hls/{uuid}",
            f"https://vidembed.re/stream/{uuid}/playlist.m3u8",
            f"https://vidembed.re/stream/{uuid}/master.m3u8",
            f"https://vidembed.re/stream/{uuid}/index.m3u8",
        ]
        
        for endpoint in api_endpoints:
            try:
                print(f"Testing: {endpoint}")
                async with session.get(endpoint, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        content = await response.text()
                        if content.startswith("#EXTM3U"):
                            print(f"🎬 SUCCESS! Found HLS stream: {endpoint}")
                            return endpoint
                        elif "application/json" in response.headers.get("content-type", ""):
                            try:
                                json_data = await response.json()
                                print(f"📄 JSON response: {json_data}")
                                # Look for stream URL in JSON
                                if isinstance(json_data, dict):
                                    for key in ["url", "stream", "video", "hls", "playlist", "src"]:
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
                        print(f"❌ HTTP {response.status}")
            except Exception as e:
                print(f"❌ Error: {str(e)}")
        
        # Approach 2: Load vidembed page and extract JavaScript variables
        print("\n🔍 Approach 2: Loading vidembed page and extracting JavaScript...")
        try:
            async with session.get(vidembed_url, headers=headers, timeout=15) as response:
                if response.status == 200:
                    content = await response.text()
                    print(f"✅ Vidembed page loaded: {len(content)} characters")
                    
                    # Look for JavaScript variables that might contain stream URLs
                    js_patterns = [
                        r'var\s+streamUrl\s*=\s*["\']([^"\']+)["\']',
                        r'var\s+videoUrl\s*=\s*["\']([^"\']+)["\']',
                        r'var\s+hlsUrl\s*=\s*["\']([^"\']+)["\']',
                        r'streamUrl\s*:\s*["\']([^"\']+)["\']',
                        r'videoUrl\s*:\s*["\']([^"\']+)["\']',
                        r'hlsUrl\s*:\s*["\']([^"\']+)["\']',
                        r'src\s*:\s*["\']([^"\']+)["\']',
                        r'url\s*:\s*["\']([^"\']+)["\']',
                    ]
                    
                    for pattern in js_patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            print(f"🔗 Found {len(matches)} potential URLs with pattern: {pattern}")
                            for match in matches:
                                if any(ext in match.lower() for ext in ['.m3u8', '.mp4', 'stream', 'hls']):
                                    print(f"🎬 Testing potential stream URL: {match}")
                                    try:
                                        async with session.get(match, headers=headers, timeout=10) as stream_response:
                                            if stream_response.status == 200:
                                                stream_content = await stream_response.text()
                                                if stream_content.startswith("#EXTM3U"):
                                                    print(f"🎬 SUCCESS! Found HLS stream: {match}")
                                                    return match
                                    except:
                                        pass
                else:
                    print(f"❌ Failed to load vidembed page: {response.status}")
        except Exception as e:
            print(f"❌ Error loading vidembed page: {str(e)}")
        
        # Approach 3: Try to find the actual stream by analyzing the page structure
        print("\n🔍 Approach 3: Analyzing page structure...")
        try:
            async with session.get(vidembed_url, headers=headers, timeout=15) as response:
                if response.status == 200:
                    content = await response.text()
                    
                    # Look for any URLs that might be stream URLs
                    url_pattern = r'https://[^"\']*\.m3u8[^"\']*'
                    matches = re.findall(url_pattern, content)
                    
                    if matches:
                        print(f"🔗 Found {len(matches)} potential M3U8 URLs")
                        for match in matches:
                            if 'cdnjs.cloudflare.com' not in match:
                                print(f"🎬 Testing M3U8 URL: {match}")
                                try:
                                    async with session.get(match, headers=headers, timeout=10) as stream_response:
                                        if stream_response.status == 200:
                                            stream_content = await stream_response.text()
                                            if stream_content.startswith("#EXTM3U"):
                                                print(f"🎬 SUCCESS! Found HLS stream: {match}")
                                                return match
                                except:
                                    pass
        except Exception as e:
            print(f"❌ Error in approach 3: {str(e)}")
    
    print("❌ No stream URL found")
    return None

if __name__ == "__main__":
    print("🚀 Starting vidembed stream extraction...")
    
    result = asyncio.run(extract_vidembed_stream())
    
    if result:
        print(f"\n✅ SUCCESS! Stream URL extracted: {result}")
    else:
        print("\n❌ No stream URL found")
        print("\n💡 The vidembed service appears to be heavily protected against automated extraction.")
        print("   Consider using iframe embedding as a fallback solution.")
    
    print("\n✅ Vidembed stream extraction completed!") 