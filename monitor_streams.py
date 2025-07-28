#!/usr/bin/env python3
"""
Streaming Performance Monitor
Monitors the freesky backend for streaming performance issues.
"""

import asyncio
import aiohttp
import time
import json
import sys
from datetime import datetime

class StreamMonitor:
    def __init__(self, base_url="http://localhost:3000"):
        self.base_url = base_url
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def check_health(self):
        """Check backend health status"""
        try:
            start_time = time.time()
            async with self.session.get(f"{self.base_url}/health") as response:
                duration = time.time() - start_time
                if response.status == 200:
                    data = await response.json()
                    print(f"✅ Health check: {duration:.3f}s")
                    print(f"   Active streams: {data.get('active_streams', 0)}")
                    print(f"   Active content sessions: {data.get('active_content_sessions', 0)}")
                    return True
                else:
                    print(f"❌ Health check failed: {response.status}")
                    return False
        except Exception as e:
            print(f"❌ Health check error: {e}")
            return False
    
    async def test_stream_generation(self, channel_id="588"):
        """Test stream generation performance"""
        try:
            start_time = time.time()
            async with self.session.get(f"{self.base_url}/api/stream/{channel_id}.m3u8") as response:
                duration = time.time() - start_time
                if response.status == 200:
                    content = await response.text()
                    print(f"✅ Stream generation: {duration:.3f}s")
                    print(f"   Content length: {len(content)} bytes")
                    print(f"   Cache source: {response.headers.get('X-Stream-Source', 'unknown')}")
                    return duration
                else:
                    print(f"❌ Stream generation failed: {response.status}")
                    return None
        except Exception as e:
            print(f"❌ Stream generation error: {e}")
            return None
    
    async def test_content_proxy(self, test_url="test"):
        """Test content proxy performance"""
        try:
            start_time = time.time()
            async with self.session.get(f"{self.base_url}/api/content/{test_url}", timeout=5) as response:
                duration = time.time() - start_time
                if response.status == 200:
                    print(f"✅ Content proxy: {duration:.3f}s")
                    return duration
                else:
                    print(f"❌ Content proxy failed: {response.status}")
                    return None
        except Exception as e:
            print(f"❌ Content proxy error: {e}")
            return None
    
    async def monitor_continuously(self, interval=30):
        """Monitor continuously with specified interval"""
        print(f"🔍 Starting continuous monitoring (interval: {interval}s)")
        print("=" * 50)
        
        while True:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n📊 {timestamp}")
            print("-" * 30)
            
            # Health check
            health_ok = await self.check_health()
            
            if health_ok:
                # Test stream generation
                stream_time = await self.test_stream_generation()
                
                # Test content proxy (will likely fail with test URL, but that's expected)
                content_time = await self.test_content_proxy()
                
                # Performance analysis
                if stream_time:
                    if stream_time < 2.0:
                        print("🎯 Stream generation: EXCELLENT")
                    elif stream_time < 5.0:
                        print("✅ Stream generation: GOOD")
                    elif stream_time < 10.0:
                        print("⚠️  Stream generation: SLOW")
                    else:
                        print("❌ Stream generation: VERY SLOW")
            
            print(f"⏰ Next check in {interval} seconds...")
            await asyncio.sleep(interval)

async def main():
    if len(sys.argv) > 1:
        base_url = sys.argv[1]
    else:
        base_url = "http://localhost:3000"
    
    async with StreamMonitor(base_url) as monitor:
        if len(sys.argv) > 2 and sys.argv[2] == "continuous":
            await monitor.monitor_continuously()
        else:
            # Single check
            print("🔍 Single performance check")
            print("=" * 30)
            await monitor.check_health()
            await monitor.test_stream_generation()

if __name__ == "__main__":
    asyncio.run(main()) 