#!/usr/bin/env python3
"""
Test Architecture Distribution Across Channels
"""

import asyncio
import aiohttp
import re

async def test_architecture_distribution():
    """Test architecture distribution across multiple channels"""
    print("🔍 Testing Architecture Distribution...")
    
    base_url = "https://thedaddy.click"
    test_channels = ["1", "100", "200", "300", "400", "500", "588", "857", "900", "1000"]
    
    old_architecture = []
    new_architecture = []
    
    async with aiohttp.ClientSession() as session:
        
        for channel_id in test_channels:
            print(f"\n📺 Testing Channel {channel_id}...")
            
            try:
                url = f"{base_url}/stream/stream-{channel_id}.php"
                if len(channel_id) > 3:
                    url = f"{base_url}/stream/bet.php?id=bet{channel_id}"
                
                headers = {
                    "Referer": base_url,
                    "user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
                }
                
                async with session.post(url, headers=headers) as response:
                    if response.status == 200:
                        content = await response.text()
                        
                        # Check for vidembed patterns
                        vidembed_pattern = r'https://vidembed\.re/stream/[^"\']+'
                        vidembed_matches = re.findall(vidembed_pattern, content)
                        
                        # Check for old architecture iframe
                        iframe_pattern = r'iframe src="([^"]+)" width'
                        iframe_matches = re.findall(iframe_pattern, content)
                        
                        if vidembed_matches:
                            print(f"  🔗 New Architecture (vidembed.re)")
                            new_architecture.append(channel_id)
                        elif iframe_matches and 'fnjplay.xyz' in iframe_matches[0]:
                            print(f"  🎬 Old Architecture (fnjplay.xyz)")
                            old_architecture.append(channel_id)
                        else:
                            print(f"  ❓ Unknown Architecture")
                    else:
                        print(f"  ❌ Status {response.status}")
                        
            except Exception as e:
                print(f"  ❌ Error: {str(e)}")
    
    print(f"\n{'='*60}")
    print(f"📊 ARCHITECTURE DISTRIBUTION SUMMARY")
    print(f"{'='*60}")
    print(f"Old Architecture (fnjplay.xyz): {len(old_architecture)} channels")
    print(f"  Channels: {', '.join(old_architecture)}")
    print(f"New Architecture (vidembed.re): {len(new_architecture)} channels")
    print(f"  Channels: {', '.join(new_architecture)}")
    print(f"Total tested: {len(test_channels)} channels")
    
    if old_architecture:
        print(f"\n✅ Channels that should work: {', '.join(old_architecture)}")
    if new_architecture:
        print(f"❌ Channels that need vidembed handling: {', '.join(new_architecture)}")

if __name__ == "__main__":
    print("🚀 Starting Architecture Distribution Test...")
    asyncio.run(test_architecture_distribution())
    print("\n✅ Architecture Distribution Test completed!") 