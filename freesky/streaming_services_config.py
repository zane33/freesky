"""
Streaming Services Configuration
Inspired by Kodi addons from https://github.com/LoopAddon/repository.the-loop
"""

# Configuration for different streaming services
STREAMING_SERVICES = {
    "DLHD": {
        "name": "DaddyLive HD",
        "base_url": "https://thedaddy.click",
        "enabled": True,
        "priority": 1,
        "description": "Your existing DLHD streaming service",
        "features": ["live_tv", "sports", "news"],
        "api_endpoints": {
            "channels": "/24-7-channels.php",
            "stream": "/stream/stream-{channel_id}.php"
        }
    },
    
    "StreamsPro": {
        "name": "StreamsPro Live",
        "base_url": "https://streamspro.live",
        "enabled": False,
        "priority": 2,
        "description": "Live streaming service for sports and entertainment",
        "features": ["live_tv", "sports", "movies"],
        "api_endpoints": {
            "channels": "/api/channels",
            "stream": "/api/stream/{channel_id}"
        },
        "auth_required": False
    },
    
    "TheLoop": {
        "name": "The Loop TV",
        "base_url": "https://theloop.tv",
        "enabled": False,
        "priority": 3,
        "description": "General entertainment and news streaming",
        "features": ["live_tv", "news", "entertainment"],
        "api_endpoints": {
            "channels": "/channels",
            "stream": "/stream/{channel_id}"
        },
        "auth_required": False
    },
    
    "Plexus": {
        "name": "Plexus P2P",
        "base_url": "https://plexus.tv",
        "enabled": False,
        "priority": 4,
        "description": "P2P streaming service",
        "features": ["p2p", "movies", "tv_shows"],
        "api_endpoints": {
            "channels": "/channels",
            "stream": "/stream/{channel_id}"
        },
        "auth_required": False
    },
    
    # Add more services here as needed
    "IPTVProvider1": {
        "name": "IPTV Provider 1",
        "base_url": "https://iptv1.com",
        "enabled": False,
        "priority": 5,
        "description": "Generic IPTV provider",
        "features": ["live_tv", "international"],
        "api_endpoints": {
            "channels": "/api/channels",
            "stream": "/api/stream/{channel_id}"
        },
        "auth_required": True,
        "auth_type": "api_key"
    },
    
    "SportsStream": {
        "name": "Sports Stream",
        "base_url": "https://sportsstream.com",
        "enabled": False,
        "priority": 6,
        "description": "Sports-focused streaming service",
        "features": ["sports", "live_tv"],
        "api_endpoints": {
            "channels": "/api/channels",
            "stream": "/api/stream/{channel_id}"
        },
        "auth_required": False
    }
}

# Service categories for organization
SERVICE_CATEGORIES = {
    "live_tv": ["DLHD", "StreamsPro", "TheLoop", "IPTVProvider1"],
    "sports": ["DLHD", "StreamsPro", "SportsStream"],
    "news": ["DLHD", "TheLoop"],
    "movies": ["StreamsPro", "Plexus"],
    "p2p": ["Plexus"],
    "international": ["IPTVProvider1"]
}

# Default settings
DEFAULT_SETTINGS = {
    "max_concurrent_streams": 10,
    "timeout": 30,
    "retry_attempts": 3,
    "cache_duration": 3600,  # 1 hour
    "enable_fallback": True,
    "log_level": "INFO"
}

# Stream extraction patterns
STREAM_PATTERNS = {
    "m3u8": [
        r'https://[^"\']*\.m3u8[^"\']*',
        r'https://[^"\']*playlist\.m3u8[^"\']*',
        r'https://[^"\']*master\.m3u8[^"\']*',
    ],
    "mp4": [
        r'https://[^"\']*\.mp4[^"\']*',
        r'https://[^"\']*video[^"\']*\.mp4[^"\']*',
    ],
    "stream": [
        r'https://[^"\']*stream[^"\']*',
        r'https://[^"\']*cdn[^"\']*',
        r'https://[^"\']*video[^"\']*',
    ],
    "magnet": [
        r'magnet:\?xt=urn:btih:[^"\']*',
    ]
}

# User agent strings for different services
USER_AGENTS = {
    "default": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "tablet": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
}

def get_enabled_services():
    """Get list of enabled services"""
    return [
        service_id for service_id, config in STREAMING_SERVICES.items()
        if config.get("enabled", False)
    ]

def get_service_config(service_id):
    """Get configuration for a specific service"""
    return STREAMING_SERVICES.get(service_id, {})

def enable_service(service_id):
    """Enable a streaming service"""
    if service_id in STREAMING_SERVICES:
        STREAMING_SERVICES[service_id]["enabled"] = True
        return True
    return False

def disable_service(service_id):
    """Disable a streaming service"""
    if service_id in STREAMING_SERVICES:
        STREAMING_SERVICES[service_id]["enabled"] = False
        return True
    return False

def get_services_by_category(category):
    """Get services that belong to a specific category"""
    return SERVICE_CATEGORIES.get(category, [])

def get_services_by_feature(feature):
    """Get services that support a specific feature"""
    services = []
    for service_id, config in STREAMING_SERVICES.items():
        if feature in config.get("features", []):
            services.append(service_id)
    return services 