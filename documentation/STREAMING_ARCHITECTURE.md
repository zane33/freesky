# Streaming Architecture Documentation

## Overview

This application acts as a proxy for a streaming service, reverse-engineering their authentication and routing system to provide access to live TV channels. The core functionality involves acquiring upstream streaming links through a multi-step process that has evolved over time.

## Current Architecture (dlhd.click with vidembed.re)

The application follows a **4-step process** to obtain streaming URLs from the current dlhd.click service:

### Step 1: Initial Stream Request
```python
url = f"{self._base_url}/stream/stream-{channel_id}.php"
# For longer channel IDs:
url = f"{self._base_url}/stream/bet.php?id=bet{channel_id}"
```
- Makes a POST request to the streaming service's stream endpoint
- Uses the channel ID to construct the appropriate URL
- Different URL patterns for different channel ID lengths

### Step 2: Extract vidembed URL
```python
vidembed_pattern = r'https://vidembed\.re/stream/[^"\']+'
vidembed_matches = re.findall(vidembed_pattern, response.text)
vidembed_url = vidembed_matches[0]
```
- Parses the HTML response to find the vidembed.re URL
- The vidembed.re service hosts the actual video player
- Uses a UUID-based URL pattern: `https://vidembed.re/stream/[uuid]#autostart`

### Step 3: Access vidembed Page
```python
vidembed_response = await self._session.get(vidembed_url, headers=self._headers(url))
```
- Makes a GET request to the vidembed.re page
- The vidembed page contains the video player and streaming logic
- This page may contain direct stream URLs or client-side JavaScript

### Step 4: Extract Stream Information
```python
stream_urls = self._extract_stream_urls(vidembed_response.text)
if stream_urls:
    stream_url = stream_urls[0]
    # Fetch and process the stream
else:
    # Return vidembed URL for client-side processing
    return self._create_vidembed_response(vidembed_url)
```
- Looks for direct stream URLs in the vidembed page content
- If found, fetches and processes the stream content
- If not found, returns the vidembed URL for client-side processing

## Legacy Architecture (Deprecated)

**Note**: The following architecture was used with the original streaming service but is no longer applicable to dlhd.click:

### Legacy Step 2: Extract iframe Source
```python
source_url = re.compile("iframe src=\"(.*)\" width").findall(response.text)[0]
source_response = await self._session.post(source_url, headers=self._headers(url))
```
- **DEPRECATED**: This pattern no longer exists in dlhd.click responses
- The original service used iframes with authentication logic

### Legacy Step 3: Extract Authentication Parameters
```python
channel_key = re.compile(r"var\s+channelKey\s*=\s*\"(.*?)\";").findall(source_response.text)[-1]
auth_ts = extract_and_decode_var("__c", source_response.text)
auth_sig = extract_and_decode_var("__e", source_response.text)
auth_path = extract_and_decode_var("__b", source_response.text)
auth_rnd = extract_and_decode_var("__d", source_response.text)
auth_url = extract_and_decode_var("__a", source_response.text)
```
- **DEPRECATED**: Authentication variables are no longer used
- The original service used obfuscated JavaScript variables for authentication

### Legacy Step 4: Authentication Request
```python
auth_request_url = f"{auth_url}{auth_path}?channel_id={channel_key}&ts={auth_ts}&rnd={auth_rnd}&sig={auth_sig}"
auth_response = await self._session.get(auth_request_url, headers=self._headers(source_url))
```
- **DEPRECATED**: Complex authentication flow is no longer required
- The new architecture is simpler and more direct

## Key Differences Between Architectures

| Aspect | Legacy Architecture | Current Architecture |
|--------|-------------------|---------------------|
| **Step 2** | Extract iframe with `iframe src="(.*)" width` | Extract vidembed URL with `https://vidembed.re/stream/[uuid]` |
| **Step 3** | Extract authentication variables (`__a`, `__b`, `__c`, `__d`, `__e`) | Access vidembed.re page directly |
| **Step 4** | Make authentication requests to multiple endpoints | Extract stream URLs or return vidembed URL |
| **Complexity** | High - requires decoding obfuscated variables | Low - direct URL extraction |
| **Authentication** | Server-side with complex token system | Client-side or direct stream access |
| **Reliability** | Fragile - depends on obfuscated JavaScript | More robust - direct URL patterns |

## Stream URL Extraction

The current architecture looks for direct stream URLs using these patterns:

```python
stream_patterns = [
    r'https://[^"\']*\.m3u8[^"\']*',
    r'https://[^"\']*\.mp4[^"\']*',
    r'https://[^"\']*stream[^"\']*',
    r'https://[^"\']*cdn[^"\']*',
]
```

## Stream Content Processing

When direct stream URLs are found, the content is processed:

```python
def _process_stream_content(self, content: str, referer: str) -> str:
    if content.startswith('#EXTM3U'):
        # Process M3U8 playlists
        lines = content.split('\n')
        processed_lines = []
        
        for line in lines:
            if line.startswith('http') and config.proxy_content:
                # Proxy content URLs
                line = f"/api/content/{encrypt(line)}"
            elif line.startswith('#EXT-X-KEY:'):
                # Process encryption keys
                original_url = re.search(r'URI="(.*?)"', line)
                if original_url:
                    line = line.replace(original_url.group(1), 
                        f"/api/key/{encrypt(original_url.group(1))}/{encrypt(urlparse(referer).netloc)}")
            
            processed_lines.append(line)
        
        return '\n'.join(processed_lines)
    else:
        return content
```

## Client-Side Processing

When direct stream URLs are not found, the vidembed URL is returned for client-side processing:

```python
def _create_vidembed_response(self, vidembed_url: str) -> str:
    return f"VIDEMBED_URL:{vidembed_url}"
```

## Error Handling

The system includes comprehensive error handling:
- HTTP status code validation
- Missing vidembed URL detection
- Stream URL extraction failures
- Network timeout handling
- Concurrent request limiting via semaphores

## Performance Considerations

- Uses asyncio for concurrent processing
- Implements semaphores to limit concurrent stream requests
- Caches channel lists to reduce repeated requests
- Uses connection pooling via AsyncSession

## Security Features

- Encrypts sensitive URLs before serving to clients
- Maintains proper referer headers for authentication
- Implements request rate limiting
- Uses secure session management

## Migration Notes

If you're upgrading from the legacy architecture:

1. **Update Step 2**: Change from iframe extraction to vidembed URL extraction
2. **Remove Authentication**: No need for `__a`, `__b`, `__c`, `__d`, `__e` variables
3. **Simplify Flow**: Remove complex authentication requests
4. **Add Client-Side Support**: Handle cases where vidembed URL is returned

This architecture allows the application to provide seamless access to streaming content while adapting to the evolving streaming service infrastructure. 