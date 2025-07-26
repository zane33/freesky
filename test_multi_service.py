#!/usr/bin/env python3
"""
Test Multi-Service Streamer Integration
"""

import asyncio
import aiohttp
import json

async def test_multi_service():
    """Test the multi-service streamer integration"""
    print("🔍 Testing Multi-Service Streamer Integration...")
    
    base_url = "http://localhost:8005"
    
    async with aiohttp.ClientSession() as session:
        
        # Test 1: Service Status
        print("\n1. Testing Service Status...")
        try:
            async with session.get(f"{base_url}/api/services/status") as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"✅ Service Status: {data}")
                else:
                    print(f"❌ Service Status failed: {response.status}")
        except Exception as e:
            print(f"❌ Service Status error: {str(e)}")
        
        # Test 2: Get All Channels
        print("\n2. Testing Get All Channels...")
        try:
            async with session.get(f"{base_url}/api/channels/all") as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"✅ All Channels: {data['count']} channels found")
                    if data['channels']:
                        print(f"   Sample channel: {data['channels'][0]}")
                else:
                    print(f"❌ Get All Channels failed: {response.status}")
        except Exception as e:
            print(f"❌ Get All Channels error: {str(e)}")
        
        # Test 3: Search Channels
        print("\n3. Testing Search Channels...")
        try:
            async with session.get(f"{base_url}/api/channels/search?query=news") as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"✅ Search Results: {data['count']} channels found for 'news'")
                else:
                    print(f"❌ Search Channels failed: {response.status}")
        except Exception as e:
            print(f"❌ Search Channels error: {str(e)}")
        
        # Test 4: Stream from Multi-Service
        print("\n4. Testing Multi-Service Stream...")
        try:
            async with session.get(f"{base_url}/api/stream/588.m3u8") as response:
                if response.status == 200:
                    content = await response.text()
                    print(f"✅ Multi-Service Stream: {len(content)} characters")
                    print(f"   Content preview: {content[:200]}...")
                else:
                    print(f"❌ Multi-Service Stream failed: {response.status}")
        except Exception as e:
            print(f"❌ Multi-Service Stream error: {str(e)}")
        
        # Test 5: Enable/Disable Services
        print("\n5. Testing Service Management...")
        try:
            # Enable StreamsPro
            async with session.post(f"{base_url}/api/services/StreamsPro/enable") as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"✅ Enable StreamsPro: {data['message']}")
                else:
                    print(f"❌ Enable StreamsPro failed: {response.status}")
            
            # Check status again
            async with session.get(f"{base_url}/api/services/status") as response:
                if response.status == 200:
                    data = await response.json()
                    print(f"✅ Updated Service Status: {data['enabled_count']} services enabled")
                else:
                    print(f"❌ Updated Service Status failed: {response.status}")
                    
        except Exception as e:
            print(f"❌ Service Management error: {str(e)}")

if __name__ == "__main__":
    print("🚀 Starting Multi-Service Streamer Test...")
    asyncio.run(test_multi_service())
    print("\n✅ Multi-Service Streamer Test completed!") 