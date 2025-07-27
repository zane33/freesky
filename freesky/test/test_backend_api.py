#!/usr/bin/env python3
"""
Test script to verify backend API is working with hybrid streaming architecture
"""

import asyncio
import aiohttp
import json

async def test_backend_api():
    """Test the backend API endpoints"""
    print("🔍 Testing backend API endpoints...")
    
    # Test health endpoint
    print("\n1. Testing health endpoint...")
    async with aiohttp.ClientSession() as session:
        async with session.get('http://localhost:8005/health') as response:
            if response.status == 200:
                data = await response.json()
                print(f"✅ Health endpoint working: {data.get('status', 'unknown')}")
                print(f"📊 Channels loaded: {data.get('channels_count', 0)}")
            else:
                print(f"❌ Health endpoint failed: {response.status}")
    
    # Test stream endpoint directly on backend
    print("\n2. Testing stream endpoint on backend (port 8005)...")
    async with aiohttp.ClientSession() as session:
        async with session.get('http://localhost:8005/api/stream/588.m3u8') as response:
            if response.status == 200:
                content = await response.text()
                print(f"✅ Stream endpoint working on backend")
                print(f"📊 Response length: {len(content)} characters")
                print(f"📄 Content type: {response.headers.get('content-type', 'unknown')}")
                if content.startswith("VIDEMBED_URL:"):
                    print(f"🎬 Architecture: New (vidembed.re)")
                    vidembed_url = content.replace("VIDEMBED_URL:", "")
                    print(f"🔗 Vidembed URL: {vidembed_url}")
                elif content.startswith("#EXTM3U"):
                    print(f"🎬 Architecture: Legacy (direct M3U8)")
                else:
                    print(f"🎬 Architecture: Unknown")
            else:
                print(f"❌ Stream endpoint failed on backend: {response.status}")
    
    # Test stream endpoint through proxy (port 3000)
    print("\n3. Testing stream endpoint through proxy (port 3000)...")
    async with aiohttp.ClientSession() as session:
        async with session.get('http://localhost:3000/api/stream/588.m3u8') as response:
            if response.status == 200:
                content = await response.text()
                print(f"✅ Stream endpoint working through proxy")
                print(f"📊 Response length: {len(content)} characters")
                print(f"📄 Content type: {response.headers.get('content-type', 'unknown')}")
                if content.startswith("VIDEMBED_URL:"):
                    print(f"🎬 Architecture: New (vidembed.re)")
                    vidembed_url = content.replace("VIDEMBED_URL:", "")
                    print(f"🔗 Vidembed URL: {vidembed_url}")
                elif content.startswith("#EXTM3U"):
                    print(f"🎬 Architecture: Legacy (direct M3U8)")
                else:
                    print(f"🎬 Architecture: Unknown")
            else:
                print(f"❌ Stream endpoint failed through proxy: {response.status}")
                print(f"📄 Response: {await response.text()}")

if __name__ == "__main__":
    print("🚀 Starting backend API tests...")
    asyncio.run(test_backend_api())
    print("\n✅ Backend API tests completed!") 