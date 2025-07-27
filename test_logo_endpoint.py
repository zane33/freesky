#!/usr/bin/env python3
"""
Test script to debug logo endpoint issues.
"""

import base64
import sys
import os

# Add the current directory to Python path
sys.path.insert(0, '.')

def test_logo_decoding():
    # The failing URL: aHR0cHM6Ly9maWxlcy5jYXRib3gubW9lLzh6aHdwbC5wbmc=
    encoded_logo = "aHR0cHM6Ly9maWxlcy5jYXRib3gubW9lLzh6aHdwbC5wbmc="
    
    print("=== Logo Endpoint Debug ===")
    print(f"Encoded logo: {encoded_logo}")
    
    try:
        # Test the urlsafe_base64_decode function
        from freesky.utils import urlsafe_base64_decode
        decoded_url = urlsafe_base64_decode(encoded_logo)
        print(f"Decoded URL: {decoded_url}")
        
        # Extract filename
        file = decoded_url.split("/")[-1]
        print(f"Filename: {file}")
        
        # Test if the URL is valid
        import urllib.parse
        parsed = urllib.parse.urlparse(decoded_url)
        print(f"Parsed URL: {parsed}")
        print(f"Scheme: {parsed.scheme}")
        print(f"Netloc: {parsed.netloc}")
        print(f"Path: {parsed.path}")
        
    except Exception as e:
        print(f"Error decoding: {e}")
        import traceback
        traceback.print_exc()

def test_base64_encoding():
    """Test if the encoding matches what we expect"""
    test_url = "https://files.catbox.moe/8zhwpl.png"
    print(f"\n=== Base64 Encoding Test ===")
    print(f"Original URL: {test_url}")
    
    # Encode using standard base64
    import base64
    encoded = base64.urlsafe_b64encode(test_url.encode()).decode()
    print(f"Standard base64 encoded: {encoded}")
    
    # Encode using our function
    from freesky.utils import urlsafe_base64
    our_encoded = urlsafe_base64(test_url)
    print(f"Our function encoded: {our_encoded}")
    
    # Test decoding
    from freesky.utils import urlsafe_base64_decode
    decoded = urlsafe_base64_decode(our_encoded)
    print(f"Decoded back: {decoded}")
    print(f"Match: {decoded == test_url}")

if __name__ == "__main__":
    test_logo_decoding()
    test_base64_encoding() 