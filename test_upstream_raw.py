#!/usr/bin/env python3
"""
Test Upstream Raw Responses
"""

import asyncio
import aiohttp
import re

async def test_upstream_raw():
    """Test what the upstream service returns for both channels"""
    print("🔍 Testing Upstream Raw Responses...")
    
    base_url = "https://thedaddy.click"
    test_channels = ["588", "857"]
    
    async with aiohttp.ClientSession() as session:
        
        for channel_id in test_channels:
            print(f"\n{'='*60}")
            print(f"📺 UPSTREAM RAW RESPONSE FOR CHANNEL {channel_id}")
            print(f"{'='*60}")
            
            try:
                # Make the same request that StepDaddyHybrid makes
                url = f"{base_url}/stream/stream-{channel_id}.php"
                if len(channel_id) > 3:
                    url = f"{base_url}/stream/bet.php?id=bet{channel_id}"
                
                print(f"Requesting: {url}")
                
                headers = {
                    "Referer": base_url,
                    "user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
                }
                
                async with session.post(url, headers=headers) as response:
                    print(f"Status: {response.status}")
                    if response.status == 200:
                        content = await response.text()
                        print(f"Content length: {len(content)} characters")
                        
                        # Check for vidembed patterns
                        vidembed_pattern = r'https://vidembed\.re/stream/[^"\']+'
                        vidembed_matches = re.findall(vidembed_pattern, content)
                        
                        if vidembed_matches:
                            print(f"🔍 DETECTED: New Architecture (vidembed.re)")
                            print(f"   Vidembed URL: {vidembed_matches[0]}")
                        else:
                            print(f"🔍 DETECTED: No vidembed URLs found")
                        
                        # Check for iframe patterns
                        iframe_pattern = r'iframe src="([^"]+)" width'
                        iframe_matches = re.findall(iframe_pattern, content)
                        
                        if iframe_matches:
                            print(f"🔍 DETECTED: Old Architecture (iframe)")
                            print(f"   Iframe URL: {iframe_matches[0]}")
                        else:
                            print(f"🔍 DETECTED: No iframe patterns found")
                        
                        # Check for any iframe
                        alt_iframe_pattern = r'<iframe[^>]*src=["\']([^"\']+)["\'][^>]*>'
                        alt_iframe_matches = re.findall(alt_iframe_pattern, content)
                        
                        if alt_iframe_matches:
                            print(f"🔍 DETECTED: Alternative iframe patterns")
                            for i, match in enumerate(alt_iframe_matches[:3]):  # Show first 3
                                print(f"   Iframe {i+1}: {match}")
                        
                        # Show a preview of the content
                        print(f"\nContent preview (first 500 chars):")
                        print("-" * 40)
                        print(content[:500])
                        print("-" * 40)
                        
                    else:
                        print(f"❌ Upstream returned status {response.status}")
                        
            except Exception as e:
                print(f"❌ Test error: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Upstream Raw Test...")
    asyncio.run(test_upstream_raw())
    print("\n✅ Upstream Raw Test completed!") 