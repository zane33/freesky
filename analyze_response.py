#!/usr/bin/env python3
"""
Analyze the actual response from dlhd.click to understand the new structure
"""
import asyncio
import re
from curl_cffi import AsyncSession

class ResponseAnalyzer:
    def __init__(self):
        self._base_url = "https://dlhd.click"
        self._session = AsyncSession(
            timeout=45,
            impersonate="chrome110",
            max_redirects=5
        )
        
    def _headers(self, referer: str = None):
        if referer is None:
            referer = self._base_url
        headers = {
            "Referer": referer,
            "user-agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
        }
        return headers

    async def analyze_response(self, channel_id: str = "1"):
        """Analyze the response to understand the structure"""
        print(f"🔍 Analyzing response for channel {channel_id}")
        
        try:
            # Make the initial request
            url = f"{self._base_url}/stream/stream-{channel_id}.php"
            if len(channel_id) > 3:
                url = f"{self._base_url}/stream/bet.php?id=bet{channel_id}"
            
            print(f"Requesting: {url}")
            response = await self._session.post(url, headers=self._headers())
            
            if response.status_code != 200:
                print(f"Failed to get response: {response.status_code}")
                return
            
            print(f"✅ Got response: {len(response.text)} characters")
            
            # Save response to file for analysis
            with open(f"response_channel_{channel_id}.html", "w", encoding="utf-8") as f:
                f.write(response.text)
            print(f"Saved response to response_channel_{channel_id}.html")
            
            # Analyze iframe patterns
            print("\n🔍 Analyzing iframe patterns...")
            
            # Look for all iframes
            iframe_patterns = [
                r'<iframe[^>]*src=["\']([^"\']+)["\'][^>]*>',
                r'iframe\s+src=["\']([^"\']+)["\']',
                r'src=["\']([^"\']*iframe[^"\']*)["\']',
            ]
            
            for i, pattern in enumerate(iframe_patterns):
                matches = re.findall(pattern, response.text, re.IGNORECASE)
                print(f"\nPattern {i+1}: {pattern}")
                print(f"Found {len(matches)} matches:")
                for j, match in enumerate(matches[:5]):  # Show first 5
                    print(f"  {j+1}. {match}")
                if len(matches) > 5:
                    print(f"  ... and {len(matches) - 5} more")
            
            # Look for video-related patterns
            print("\n🔍 Looking for video-related patterns...")
            video_patterns = [
                r'vidembed\.re/stream/[^"\']+',
                r'player\.php[^"\']*',
                r'embed\.php[^"\']*',
                r'stream\.php[^"\']*',
                r'watch\.php[^"\']*',
            ]
            
            for pattern in video_patterns:
                matches = re.findall(pattern, response.text, re.IGNORECASE)
                if matches:
                    print(f"\nVideo pattern: {pattern}")
                    for match in matches[:3]:
                        print(f"  - {match}")
            
            # Look for JavaScript variables
            print("\n🔍 Looking for JavaScript variables...")
            js_patterns = [
                r'var\s+(\w+)\s*=\s*["\']([^"\']+)["\'];',
                r'let\s+(\w+)\s*=\s*["\']([^"\']+)["\'];',
                r'const\s+(\w+)\s*=\s*["\']([^"\']+)["\'];',
                r'(\w+)\s*:\s*["\']([^"\']+)["\']',  # Object properties
            ]
            
            for pattern in js_patterns:
                matches = re.findall(pattern, response.text)
                if matches:
                    print(f"\nJS pattern: {pattern}")
                    for var_name, value in matches[:10]:  # Show first 10
                        if any(keyword in var_name.lower() for keyword in ['key', 'id', 'url', 'src', 'stream', 'channel']):
                            print(f"  {var_name}: {value}")
            
            # Look for the specific vidembed.re URL
            print("\n🔍 Analyzing vidembed.re URL...")
            vidembed_matches = re.findall(r'https://vidembed\.re/stream/[^"\']+', response.text)
            if vidembed_matches:
                vidembed_url = vidembed_matches[0]
                print(f"Found vidembed URL: {vidembed_url}")
                
                # Try to access the vidembed URL
                print(f"\n🔍 Testing vidembed URL: {vidembed_url}")
                try:
                    vidembed_response = await self._session.get(vidembed_url, headers=self._headers(url))
                    print(f"Vidembed response status: {vidembed_response.status_code}")
                    
                    if vidembed_response.status_code == 200:
                        print(f"✅ Vidembed response: {len(vidembed_response.text)} characters")
                        
                        # Save vidembed response
                        with open(f"vidembed_response_{channel_id}.html", "w", encoding="utf-8") as f:
                            f.write(vidembed_response.text)
                        print(f"Saved vidembed response to vidembed_response_{channel_id}.html")
                        
                        # Look for authentication variables in vidembed response
                        print("\n🔍 Looking for auth variables in vidembed response...")
                        auth_patterns = [
                            r'var\s+__(\w+)\s*=\s*atob\("([^"]+)"\);',
                            r'var\s+(\w+)\s*=\s*["\']([^"\']+)["\'];',
                        ]
                        
                        for pattern in auth_patterns:
                            matches = re.findall(pattern, vidembed_response.text)
                            if matches:
                                print(f"Auth pattern: {pattern}")
                                for match in matches[:5]:
                                    print(f"  {match}")
                    else:
                        print(f"❌ Vidembed request failed: {vidembed_response.status_code}")
                        
                except Exception as e:
                    print(f"❌ Error accessing vidembed URL: {str(e)}")
            
        except Exception as e:
            print(f"❌ Error analyzing response: {str(e)}")
        finally:
            await self._session.close()

async def main():
    analyzer = ResponseAnalyzer()
    await analyzer.analyze_response("1")

if __name__ == "__main__":
    asyncio.run(main()) 