#!/usr/bin/env python3
"""
Analyze vidembed page content to understand stream loading
"""

import asyncio
import aiohttp
from freesky.free_sky_hybrid import StepDaddyHybrid

async def analyze_vidembed_content():
    """Analyze vidembed page content"""
    print("🔍 Analyzing vidembed page content...")
    
    # Get vidembed URL
    streamer = StepDaddyHybrid()
    await streamer.load_channels()
    
    result = await streamer.stream("588")
    if not result.startswith("VIDEMBED_URL:"):
        print("❌ Not a vidembed URL")
        return
    
    vidembed_url = result.replace("VIDEMBED_URL:", "")
    print(f"🔗 Vidembed URL: {vidembed_url}")
    
    # Load vidembed page
    async with aiohttp.ClientSession() as session:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        
        async with session.get(vidembed_url, headers=headers) as response:
            if response.status != 200:
                print(f"❌ Failed to load vidembed page: {response.status}")
                return
            
            content = await response.text()
            print(f"✅ Vidembed page loaded: {len(content)} characters")
            
            # Save content to file for analysis
            with open("vidembed_page.html", "w", encoding="utf-8") as f:
                f.write(content)
            print("📄 Saved vidembed page to vidembed_page.html")
            
            # Look for key patterns
            print("\n🔍 Analyzing content patterns...")
            
            # Look for script tags
            script_count = content.count("<script")
            print(f"📜 Script tags found: {script_count}")
            
            # Look for iframe tags
            iframe_count = content.count("<iframe")
            print(f"🖼️ Iframe tags found: {iframe_count}")
            
            # Look for video tags
            video_count = content.count("<video")
            print(f"🎥 Video tags found: {video_count}")
            
            # Look for player-related content
            player_indicators = [
                "player", "video", "stream", "hls", "m3u8", "mp4", "flv", "rtmp"
            ]
            
            for indicator in player_indicators:
                count = content.lower().count(indicator)
                if count > 0:
                    print(f"🔍 '{indicator}' found {count} times")
            
            # Look for API endpoints
            api_patterns = [
                "api", "ajax", "fetch", "xhr", "request"
            ]
            
            for pattern in api_patterns:
                count = content.lower().count(pattern)
                if count > 0:
                    print(f"🔗 '{pattern}' found {count} times")
            
            # Extract script content
            print("\n📜 Extracting script content...")
            import re
            script_pattern = r'<script[^>]*>(.*?)</script>'
            scripts = re.findall(script_pattern, content, re.DOTALL)
            
            for i, script in enumerate(scripts[:3]):  # Show first 3 scripts
                print(f"\nScript {i+1} ({len(script)} chars):")
                print(script[:500] + "..." if len(script) > 500 else script)
            
            # Look for network requests in scripts
            print("\n🌐 Looking for network requests...")
            request_patterns = [
                r'fetch\(["\']([^"\']+)["\']',
                r'\.get\(["\']([^"\']+)["\']',
                r'\.post\(["\']([^"\']+)["\']',
                r'url\s*:\s*["\']([^"\']+)["\']',
                r'src\s*:\s*["\']([^"\']+)["\']',
            ]
            
            for pattern in request_patterns:
                matches = re.findall(pattern, content)
                if matches:
                    print(f"🔗 Found {len(matches)} potential requests with pattern: {pattern}")
                    for match in matches[:3]:
                        print(f"  - {match}")

if __name__ == "__main__":
    print("🚀 Starting vidembed content analysis...")
    asyncio.run(analyze_vidembed_content())
    print("\n✅ Vidembed content analysis completed!") 