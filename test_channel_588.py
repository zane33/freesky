#!/usr/bin/env python3
"""
Test Channel 588 Stream
"""

import asyncio
import aiohttp

async def test_channel_588():
    """Test channel 588 stream"""
    print("🔍 Testing Channel 588 Stream...")
    
    base_url = "http://localhost:8005"
    
    async with aiohttp.ClientSession() as session:
        
        # Test 1: Get stream directly from backend
        print("\n1. Testing Backend Stream...")
        try:
            async with session.get(f"{base_url}/api/stream/588.m3u8") as response:
                print(f"Status: {response.status}")
                if response.status == 200:
                    content = await response.text()
                    print(f"Content length: {len(content)} characters")
                    print(f"Content preview:")
                    print("=" * 50)
                    print(content[:500])
                    print("=" * 50)
                    
                    # Check if it's a vidembed URL
                    if "vidembed.re" in content:
                        print("✅ Contains vidembed.re URL")
                        vidembed_url = content.split("\n")[-1].strip()
                        print(f"Vidembed URL: {vidembed_url}")
                    else:
                        print("❌ No vidembed.re URL found")
                        
                else:
                    print(f"❌ Backend returned status {response.status}")
        except Exception as e:
            print(f"❌ Backend test error: {str(e)}")
        
        # Test 2: Test through proxy (frontend port)
        print("\n2. Testing Frontend Proxy...")
        try:
            async with session.get("http://localhost:3000/api/stream/588.m3u8") as response:
                print(f"Status: {response.status}")
                if response.status == 200:
                    content = await response.text()
                    print(f"Content length: {len(content)} characters")
                    print("✅ Frontend proxy working")
                else:
                    print(f"❌ Frontend proxy returned status {response.status}")
        except Exception as e:
            print(f"❌ Frontend proxy error: {str(e)}")
        
        # Test 3: Check if vidembed URL is accessible
        print("\n3. Testing Vidembed URL...")
        try:
            # Extract vidembed URL from previous test
            async with session.get(f"{base_url}/api/stream/588.m3u8") as response:
                if response.status == 200:
                    content = await response.text()
                    if "vidembed.re" in content:
                        vidembed_url = content.split("\n")[-1].strip()
                        print(f"Testing vidembed URL: {vidembed_url}")
                        
                        # Test the vidembed URL
                        async with session.get(vidembed_url) as vidembed_response:
                            print(f"Vidembed status: {vidembed_response.status}")
                            if vidembed_response.status == 200:
                                vidembed_content = await vidembed_response.text()
                                print(f"Vidembed content length: {len(vidembed_content)}")
                                print("✅ Vidembed URL accessible")
                            else:
                                print("❌ Vidembed URL not accessible")
        except Exception as e:
            print(f"❌ Vidembed test error: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Channel 588 Test...")
    asyncio.run(test_channel_588())
    print("\n✅ Channel 588 Test completed!") 