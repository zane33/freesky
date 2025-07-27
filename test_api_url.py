#!/usr/bin/env python3
"""
Test script to verify API URL configuration.
Run this inside the Docker container to see what API_URL is being used.
"""

import os
import sys

def test_api_url():
    print("=== API URL Configuration Test ===")
    print(f"Environment variables:")
    print(f"  PORT: {os.environ.get('PORT', 'NOT SET')}")
    print(f"  HOST_IP: {os.environ.get('HOST_IP', 'NOT SET')}")
    print(f"  DOCKER_HOST_IP: {os.environ.get('DOCKER_HOST_IP', 'NOT SET')}")
    print(f"  API_URL: {os.environ.get('API_URL', 'NOT SET')}")
    
    print(f"\nDocker detection:")
    print(f"  /.dockerenv exists: {os.path.exists('/.dockerenv')}")
    
    print(f"\nHostname info:")
    import socket
    try:
        hostname = socket.gethostname()
        print(f"  Hostname: {hostname}")
        print(f"  Resolved IP: {socket.gethostbyname(hostname)}")
    except Exception as e:
        print(f"  Error getting hostname: {e}")
    
    print(f"\nTesting rxconfig import:")
    try:
        # Add the current directory to Python path
        sys.path.insert(0, '.')
        from rxconfig import config
        print(f"  API_URL from config: {config.api_url}")
        print(f"  Backend URI: {config.backend_uri}")
    except Exception as e:
        print(f"  Error importing rxconfig: {e}")

if __name__ == "__main__":
    test_api_url() 