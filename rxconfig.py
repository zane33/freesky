"""
Reflex configuration for freesky.
"""
import os
import reflex as rx

# Get environment variables with defaults
frontend_port = int(os.environ.get("PORT", "3000"))
backend_port = int(os.environ.get("BACKEND_PORT", "8005"))


host_ip = os.environ.get(f"HOST_IP", "0.0.0.0") or "0.0.0.0"  # Handle empty string

# For API_URL, we need to use a publicly accessible hostname/IP, not 0.0.0.0
# If API_URL is explicitly set, use it; otherwise try to construct a sensible default
api_url = os.environ.get("API_URL")
if not api_url:
    # Check if we're running in Docker
    import os
    if os.path.exists("/.dockerenv"):
        # We're in Docker - use the host IP from environment or default to localhost
        docker_host_ip = os.environ.get("DOCKER_HOST_IP", "localhost")
        api_url = f"http://{docker_host_ip}:{frontend_port}"
    else:
        # We're running locally - use localhost
        api_url = f"http://localhost:{frontend_port}"
backend_uri = os.environ.get("BACKEND_URI", f"http://{host_ip}:{backend_port}")  # Backend service
daddylive_uri = os.environ.get("DADDYLIVE_URI", "https://thedaddy.click")
proxy_content = os.environ.get("PROXY_CONTENT", "TRUE").lower() == "true"
socks5 = os.environ.get("SOCKS5", "")

# Create config
config = rx.Config(
    app_name="freesky",
    host=host_ip, #added to fix output elsewhere
    api_url=api_url,  # Frontend interface where clients connect
    backend_port=backend_port,  # Use proper backend port
    env=rx.Env.PROD,  # Use production mode
    show_devtools=False,  # Hide Reflex developer tools
    frontend_packages=[
        "socket.io-client",
        "@emotion/react",
        "@emotion/styled",
        "@mui/material",
        "@mui/icons-material",
    ],
    # Enable WebSocket support
    connect_on_init=True,  # Connect WebSocket on page load
    timeout=120000,  # WebSocket timeout in milliseconds (2 minutes)
    reconnect_delay=3000,  # Delay before attempting reconnection (3 seconds)
    # Allow all origins for WebSocket
    cors_allowed_origins=["*"],
    # Custom configuration
    daddylive_uri=daddylive_uri,
    backend_uri=backend_uri,
    proxy_content=proxy_content,
    socks5=socks5,
    host_ip=host_ip,
    frontend_port=frontend_port,
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
