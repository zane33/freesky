"""
Token validation utilities for DaddyLive streaming URLs
Based on the analysis of the token-based security system described in the documentation
"""

import hashlib
import time
import re
import logging
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

class TokenValidator:
    """Validates and analyzes DaddyLive streaming tokens"""
    
    @staticmethod
    def parse_stream_url(stream_url: str) -> Optional[Dict[str, str]]:
        """
        Parse a stream URL to extract token parameters
        
        Expected format:
        https://{domain}/v3/director/{encoded-path}/master.m3u8?md5={hash}&expires={timestamp}&t={request_time}
        
        Args:
            stream_url: The stream URL to parse
            
        Returns:
            Dictionary with token parameters or None if invalid
        """
        try:
            parsed_url = urlparse(stream_url)
            query_params = parse_qs(parsed_url.query)
            
            # Extract token parameters
            md5_hash = query_params.get('md5', [None])[0]
            expires = query_params.get('expires', [None])[0]
            request_time = query_params.get('t', [None])[0]
            
            if not all([md5_hash, expires, request_time]):
                logger.warning(f"Missing token parameters in URL: {stream_url}")
                return None
            
            return {
                'md5': md5_hash,
                'expires': expires,
                't': request_time,
                'domain': parsed_url.netloc,
                'path': parsed_url.path,
                'base_url': f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            }
        except Exception as e:
            logger.error(f"Error parsing stream URL: {str(e)}")
            return None
    
    @staticmethod
    def validate_token_expiry(expires_timestamp: str) -> Tuple[bool, int]:
        """
        Validate if a token has expired
        
        Args:
            expires_timestamp: Unix timestamp as string
            
        Returns:
            Tuple of (is_valid, seconds_until_expiry)
        """
        try:
            expires_time = int(expires_timestamp)
            current_time = int(time.time())
            seconds_until_expiry = expires_time - current_time
            
            is_valid = seconds_until_expiry > 0
            
            if is_valid:
                logger.debug(f"Token valid for {seconds_until_expiry} seconds")
            else:
                logger.warning(f"Token expired {abs(seconds_until_expiry)} seconds ago")
            
            return is_valid, seconds_until_expiry
        except (ValueError, TypeError) as e:
            logger.error(f"Error validating token expiry: {str(e)}")
            return False, 0
    
    @staticmethod
    def generate_token_hash(url_path: str, expires: str, request_time: str, secret_key: str = None) -> str:
        """
        Generate MD5 hash for token validation (for testing purposes)
        
        Note: The actual secret key used by DaddyLive is unknown,
        this is for understanding the token structure only.
        
        Args:
            url_path: The URL path component
            expires: Expiration timestamp
            request_time: Request timestamp
            secret_key: Secret key for hashing (unknown for DaddyLive)
            
        Returns:
            MD5 hash string
        """
        if secret_key is None:
            # We don't know the actual secret key, so this is just for demonstration
            secret_key = "unknown_secret"
        
        # Construct string to hash (this is speculative)
        hash_string = f"{url_path}{expires}{request_time}{secret_key}"
        
        # Generate MD5 hash
        md5_hash = hashlib.md5(hash_string.encode()).hexdigest()
        
        return md5_hash
    
    @staticmethod
    def analyze_token_security(stream_url: str) -> Dict[str, any]:
        """
        Analyze the security features of a stream token
        
        Args:
            stream_url: The stream URL to analyze
            
        Returns:
            Dictionary with security analysis
        """
        token_data = TokenValidator.parse_stream_url(stream_url)
        
        if not token_data:
            return {"valid": False, "error": "Could not parse token data"}
        
        # Validate expiry
        is_valid, seconds_until_expiry = TokenValidator.validate_token_expiry(token_data['expires'])
        
        # Analyze domain obfuscation
        domain = token_data['domain']
        is_obfuscated = bool(re.match(r'^[a-z0-9]+\.[a-z-]+\.(site|com|net)$', domain))
        
        # Calculate token lifetime
        try:
            expires_time = int(token_data['expires'])
            request_time = int(token_data['t'])
            token_lifetime_hours = (expires_time - request_time) / 3600
        except (ValueError, TypeError):
            token_lifetime_hours = 0
        
        analysis = {
            "valid": is_valid,
            "expires_in_seconds": seconds_until_expiry,
            "token_lifetime_hours": round(token_lifetime_hours, 2),
            "domain_obfuscated": is_obfuscated,
            "domain": domain,
            "md5_hash": token_data['md5'],
            "security_features": {
                "time_limited_access": is_valid,
                "hash_validation": len(token_data['md5']) == 32,  # MD5 is 32 chars
                "domain_obfuscation": is_obfuscated,
                "request_timestamping": token_data['t'].isdigit()
            }
        }
        
        return analysis
    
    @staticmethod
    def extract_tokens_from_m3u8(m3u8_content: str) -> list:
        """
        Extract all tokens from M3U8 playlist content
        
        Args:
            m3u8_content: M3U8 playlist content
            
        Returns:
            List of dictionaries with token information
        """
        tokens = []
        
        # Find URLs with token parameters
        url_pattern = r'https://[^\s]+\?[^\s]*(?:md5|expires|t)=[^\s]*'
        urls = re.findall(url_pattern, m3u8_content)
        
        for url in urls:
            token_data = TokenValidator.parse_stream_url(url)
            if token_data:
                analysis = TokenValidator.analyze_token_security(url)
                tokens.append({
                    "url": url,
                    "token_data": token_data,
                    "analysis": analysis
                })
        
        return tokens
    
    @staticmethod
    def is_token_renewable(stream_url: str, renewal_threshold_hours: float = 2.0) -> bool:
        """
        Check if a token should be renewed based on remaining time
        
        Args:
            stream_url: The stream URL to check
            renewal_threshold_hours: Hours before expiry to trigger renewal
            
        Returns:
            True if token should be renewed
        """
        analysis = TokenValidator.analyze_token_security(stream_url)
        
        if not analysis.get("valid", False):
            return True  # Invalid tokens should be renewed
        
        expires_in_hours = analysis.get("expires_in_seconds", 0) / 3600
        
        return expires_in_hours < renewal_threshold_hours


# Utility function for easy access
def validate_stream_token(stream_url: str) -> Dict[str, any]:
    """
    Convenience function to validate a stream token
    
    Args:
        stream_url: The stream URL to validate
        
    Returns:
        Dictionary with validation results
    """
    return TokenValidator.analyze_token_security(stream_url)


def extract_viable_streams(m3u8_content: str) -> list:
    """
    Extract viable streams from M3U8 content based on token validity
    
    Args:
        m3u8_content: M3U8 playlist content
        
    Returns:
        List of valid stream URLs
    """
    tokens = TokenValidator.extract_tokens_from_m3u8(m3u8_content)
    viable_streams = []
    
    for token_info in tokens:
        if token_info["analysis"].get("valid", False):
            viable_streams.append(token_info["url"])
        else:
            logger.debug(f"Skipping expired/invalid stream: {token_info['url']}")
    
    return viable_streams