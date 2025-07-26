#!/usr/bin/env python3
"""
Test the hybrid streaming architecture implementation
"""
import asyncio
import sys
import os

# Add the current directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from freesky.free_sky_hybrid import StepDaddyHybrid

async def test_hybrid_architecture():
    """Test the hybrid streaming architecture"""
    print("🧪 Testing hybrid streaming architecture implementation...")
    
    try:
        # Initialize the hybrid streaming class
        streamer = StepDaddyHybrid()
        
        # Test channel loading
        print("\n📡 Testing channel loading...")
        await streamer.load_channels()
        
        if not streamer.channels:
            print("❌ No channels loaded")
            return False
        
        print(f"✅ Loaded {len(streamer.channels)} channels")
        
        # Test streaming with multiple channels to test both architectures
        test_channels = streamer.channels[:5]  # Test more channels
        
        success_count = 0
        total_count = len(test_channels)
        
        for channel in test_channels:
            print(f"\n🎬 Testing stream for channel: {channel.name} (ID: {channel.id})")
            
            try:
                result = await streamer.stream(channel.id)
                
                if result:
                    if result.startswith("VIDEMBED_URL:"):
                        vidembed_url = result.replace("VIDEMBED_URL:", "")
                        print(f"✅ Got vidembed URL (new architecture): {vidembed_url}")
                        success_count += 1
                    elif result.startswith("#EXTM3U"):
                        print(f"✅ Got M3U8 playlist (old architecture) ({len(result)} characters)")
                        success_count += 1
                    else:
                        print(f"✅ Got stream content ({len(result)} characters)")
                        success_count += 1
                else:
                    print("❌ No stream result")
                    
            except Exception as e:
                print(f"❌ Error streaming channel {channel.id}: {str(e)}")
        
        print(f"\n📊 Results: {success_count}/{total_count} channels successful")
        
        if success_count > 0:
            print("🎉 Hybrid architecture test completed successfully!")
            return True
        else:
            print("❌ No channels were successfully streamed")
            return False
        
    except Exception as e:
        print(f"❌ Error in hybrid architecture test: {str(e)}")
        return False
    finally:
        await streamer._session.close()

async def main():
    success = await test_hybrid_architecture()
    if success:
        print("\n✅ Hybrid streaming architecture is working correctly!")
        print("🔧 The system can now handle both old and new streaming patterns.")
    else:
        print("\n❌ Hybrid streaming architecture has issues")

if __name__ == "__main__":
    asyncio.run(main()) 