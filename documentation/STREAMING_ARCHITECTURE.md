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
---

## Upstream chain as of 2026-09 (verified)

The `auth.php` / `server_lookup.php` / `channelKey` / `authTs`-`authRnd`-`authSig`
dance described earlier in this document **no longer exists upstream**. The signed
playlist URL now arrives pre-minted inside the player page. The live chain is three
requests plus one local decode:

| # | Request | Referer sent | Extract |
|---|---------|--------------|---------|
| 1 | `{DADDYLIVE_URI}/{player}/stream-{id}.php` | `{base}/watch.php?id={id}` | `<iframe … id="thatframe">` → player host + slug |
| 2 | `https://{player_host}/e/{slug}` | same | `_econfig='<base64>'` |
| 3 | *local decode, no HTTP* | — | `stream_url_nop2p` |
| 4 | the signed `.m3u8`, then its segments | player page URL | playlist / MPEG-TS |

`_econfig` decodes as: base64 → split into 4 quarters → drop the character at
index 3 of each → reorder `[2,0,3,1]` → base64 → JSON. Pure local computation;
**no JavaScript is executed and no browser is required.** Implemented as
`StepDaddyHybrid._decode_econfig`.

### Header requirements differ per hop — and the token is UA-bound

Two separate mechanisms, and both must be satisfied:

| Hop | Requires | Ignores |
|-----|----------|---------|
| `.m3u8` | the **same User-Agent that minted the token** | Referer (absent or bogus both pass) |
| segment | **Referer** of the player origin | User-Agent |

The CDN binds each signed token to the User-Agent that fetched the embed page.
Verified by cross-fetching:

```
token minted with Chrome 153  ->  Chrome 153: 200   Firefox 137: 403
token minted with Firefox 137 ->  Firefox 137: 200   Chrome 153: 403
```

**The UA value is arbitrary — both work. Only consistency matters.** There is no
allowlist and no "current Chrome" requirement, so there is nothing here that rots
on Google's release cadence.

**This creates a hard coupling.** `StepDaddyHybrid.USER_AGENT` mints the token and
`backend._upstream_headers` spends it when proxying playlists and segments. If the
two ever disagree, every stream 403s while every page still loads normally. Both
now read the single constant; do not reintroduce a literal at either site.
`UPSTREAM_USER_AGENT` overrides both together.

> **Measurement caution.** This gate is easy to misdiagnose. Holding one token
> fixed and varying the UA makes the binding look exactly like an exact-string
> allowlist — every other UA 403s, including adjacent Chrome versions and other
> platforms. Distinguishing the two requires minting a *fresh* token per UA and
> cross-fetching. The same trap applies to the channel page's Referer check,
> which is satisfied by *any* non-empty Referer (15/15 with, 0/15 without) but
> looks origin-specific if only present-vs-absent is tested.

### Playlist URL lifetime

Signed URLs carry `?s=<signature>&e=<epoch>`. The signature **replays freely**
until `e` (verified: a URL minted 10 minutes earlier still returned 200, and again
45s later, while `#EXT-X-MEDIA-SEQUENCE` advanced). `e` currently sits ~6h out.
A corrupted `s` and an expired `e` both return **403** and are indistinguishable
without parsing `e` locally, which `_cache_ttl_for` does:

```
ttl = clamp(e - now - M3U8_EXPIRY_MARGIN, M3U8_TTL_MIN, M3U8_TTL_MAX)
```

Tunable via `M3U8_EXPIRY_MARGIN` (1800), `M3U8_TTL_MIN` (60), `M3U8_TTL_MAX`
(18000). Falls back to the flat `_RESOLVED_TTL` when `e` is absent.

### Player path status

`PLAYER_PATHS` is ordered best-first from measurement, and nothing is deleted —
hosts are discovered rather than hardcoded, and upstream has already rotated twice.

| Path | State (2026-09) |
|------|-----------------|
| `stream` | **working** — `_econfig` chain |
| `plus` | live host, payload in `/setup.js` — no extractor yet |
| `casting` | live host, payload split across 9 base64 fragments — no extractor yet; endpoint also returns `503 provider-cap` |
| `cast` | resolves to the *same* embed as `stream` — duplicate, not independent failover |
| `watch` | 403, plus a second iframe whose host no longer resolves |
| `player` | host no longer resolves |

### How this failure presented

Worth recording, because it was invisible to every conventional check: upstream
returned **HTTP 200 at every hop**, the correct document each time, with the real
player iframe present. The only defect was that no extractor matched a page that
visibly contained a stream — `_ATOB_RE` requires `atob('<literal>')` and upstream
had moved to applying `atob()` to *variables*, while `_PLAIN_M3U8_RE` had no
plaintext URL to find. The observable symptom was `No working stream found for
channel N across 6 players`, a 20s crawl, and a Caddy 504. Two of the six players
did have genuinely dead hosts (DNS `NODATA` and `SERVFAIL`), which made the logs
look like an upstream outage rather than a parsing failure.

The useful signal for this class of fault is **not** "did the request succeed" but
"did a page that passed its sentinel yield zero candidates" — the right document,
unreadable.
