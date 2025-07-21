#!/usr/bin/env python3
"""
Debug script to check current configuration values
"""
import os
from rxconfig import config

print("=== Environment Variables ===")
print(f"PORT: {os.environ.get('PORT', 'NOT SET')}")
print(f"BACKEND_PORT: {os.environ.get('BACKEND_PORT', 'NOT SET')}")
print(f"API_URL: {os.environ.get('API_URL', 'NOT SET')}")
print(f"HOST_IP: {os.environ.get('HOST_IP', 'NOT SET')}")

print("\n=== Config Values ===")
print(f"config.api_url: {config.api_url}")
print(f"config.backend_port: {config.backend_port}")
print(f"config.host_ip: {config.host_ip}")

print("\n=== Calculated Values ===")
frontend_port = int(os.environ.get("PORT", "3000"))
backend_port = int(os.environ.get("BACKEND_PORT", "8005"))
host_ip = os.environ.get("HOST_IP", "0.0.0.0") or "0.0.0.0"
api_url = os.environ.get("API_URL", f"http://{host_ip}:{frontend_port}")

print(f"Calculated frontend_port: {frontend_port}")
print(f"Calculated backend_port: {backend_port}")
print(f"Calculated host_ip: {host_ip}")
print(f"Calculated api_url: {api_url}") 