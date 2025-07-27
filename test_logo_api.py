#!/usr/bin/env python3
"""
Test script to verify the logo API endpoint.
"""

import requests
import base64

def test_logo_endpoint():
    # Test the specific failing URL
    encoded_logo = "aHR0cHM6Ly9maWxlcy5jYXRib3gubW9lLzh6aHdwbC5wbmc="
    
    # Test both localhost and external IP
    test_urls = [
        "http://localhost:3000",
        "http://192.168.4.5:3000"
    ]
    
    for base_url in test_urls:
        print(f"\n=== Testing {base_url} ===")
        
        # Test the API endpoint
        api_url = f"{base_url}/api/logo/{encoded_logo}"
        print(f"Testing: {api_url}")
        
        try:
            response = requests.get(api_url, timeout=10)
            print(f"Status: {response.status_code}")
            print(f"Headers: {dict(response.headers)}")
            
            if response.status_code == 200:
                print("✅ Success!")
                print(f"Content-Type: {response.headers.get('content-type', 'unknown')}")
                print(f"Content-Length: {len(response.content)} bytes")
            else:
                print(f"❌ Failed: {response.text}")
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Request failed: {e}")
        
        # Also test the non-API endpoint
        logo_url = f"{base_url}/logo/{encoded_logo}"
        print(f"Testing: {logo_url}")
        
        try:
            response = requests.get(logo_url, timeout=10)
            print(f"Status: {response.status_code}")
            
            if response.status_code == 200:
                print("✅ Success!")
            else:
                print(f"❌ Failed: {response.text}")
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Request failed: {e}")

if __name__ == "__main__":
    test_logo_endpoint() 