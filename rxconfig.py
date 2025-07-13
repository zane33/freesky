"""
Reflex configuration for freesky.
"""
import os
import reflex as rx

# Get environment variables with defaults
frontend_port = int(os.environ.get("PORT", "3000"))
backend_port = int(os.environ.get("BACKEND_PORT", "8005"))

# Dynamic host detection for container deployment
def get_host_ip():
    """Get the host IP dynamically, fallback to 0.0.0.0 for container deployment"""
    host = os.environ.get("HOST_IP")
    if host:
        return host
    # For container deployment, use 0.0.0.0 to bind to all interfaces
    return "0.0.0.0"

host_ip = get_host_ip()
api_url = os.environ.get("API_URL", f"http://{host_ip}:{backend_port}")  # Use backend port for API URLs
backend_uri = os.environ.get("BACKEND_URI", f"http://{host_ip}:{backend_port}")  # Backend service
daddylive_uri = os.environ.get("DADDYLIVE_URI", "https://thedaddy.click")
proxy_content = os.environ.get("PROXY_CONTENT", "TRUE").lower() == "true"
socks5 = os.environ.get("SOCKS5", "")

# Create config
config = rx.Config(
    app_name="freesky",
    api_url=api_url,  # Frontend interface where clients connect
    backend_port=backend_port,  # Use proper backend port
    env=rx.Env.PROD,  # Use production mode
    frontend_packages=[
        "socket.io-client",
        "@emotion/react",
        "@emotion/styled",
        "@mui/material",
        "@mui/icons-material",
    ],
    # Enable WebSocket support
    connect_on_init=True,  # Connect WebSocket on page load
    timeout=30000,  # WebSocket timeout in milliseconds
    # Allow all origins for WebSocket
    cors_allowed_origins=["*"],
    # Custom configuration
    daddylive_uri=daddylive_uri,
    backend_uri=backend_uri,
    proxy_content=proxy_content,
    socks5=socks5,
    # Configure CSP headers with broader permissions for development
    frontend_headers={
        "Content-Security-Policy": (
            "default-src 'self' 'unsafe-inline' 'unsafe-eval'; "  # Allow eval and inline scripts globally
            "script-src 'self' 'unsafe-eval' 'unsafe-inline' blob:; "  # Required for Reflex and video player
            "style-src 'self' 'unsafe-inline'; "  # Required for styled-components
            "img-src 'self' data: https: blob:; "  # Allow images from HTTPS sources and blobs
            "media-src 'self' blob: *; "  # Required for video streaming
            "connect-src 'self' ws: wss: * http: https:; "  # Required for WebSocket and API
            "worker-src 'self' blob:; "  # Required for video processing
            "frame-src 'self' *; "  # Required for video iframes
            "font-src 'self' data: https:; "  # Allow fonts from HTTPS sources
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()"
    }
)
