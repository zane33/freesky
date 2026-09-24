#!/usr/bin/env python3
"""
Hybrid streaming architecture that handles both old and new dlhd.click patterns
"""
import base64
import html
import json
import os
import re
import time
import reflex as rx
import logging
import asyncio
from urllib.parse import quote, urljoin, urlparse
from curl_cffi import AsyncSession
from dataclasses import dataclass
from typing import List
from .free_sky import Channel
from .utils import encrypt, decrypt, urlsafe_base64, extract_and_decode_var, hls_ext
from .token_validator import TokenValidator, extract_viable_streams
from . import channel_prefs
from rxconfig import config

# Set up logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Seconds a single crawl may spend hunting for a working feed. Shared with
# backend.stream_resolve_budget via the same environment variable so the resolver
# and the endpoint that wraps it cannot drift apart; see that constant for why it
# is sized against Dispatcharr's hardcoded 30s client init window.
RESOLVE_BUDGET = float(os.environ.get("STREAM_RESOLVE_BUDGET", "10.0"))


class ChannelOffAirError(ValueError):
    """The upstream CDN says this channel's feed does not exist right now.

    Distinct from "we could not find a stream URL". The signed playlist URL was
    minted successfully by the player chain, but fetching it returned 404 — the
    origin has no such stream, i.e. the channel is not currently broadcasting.
    Verified 2026-09 against channels 588/820/589/260: the site's OWN player
    (driven by a real browser) minted a URL and got the same 404, while live
    channels on the identical code path returned 200.

    Subclasses ValueError so the existing `except Exception` / `except ValueError`
    handlers around playlist fetching keep behaving as they did; callers that want
    to fail fast opt in by catching this type specifically.
    """


# ponytail: Channel used to be redefined here with identical fields. Two classes
# with the same shape are still two types: anything annotated
# `List[free_sky.Channel]` silently rejected the ones built here, which is how the
# settings page came up empty while the backend held 900 channels.
class StepDaddyHybrid:
    def __init__(self):
        socks5 = config.socks5
        max_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "10"))
        
        session_config = {
            "timeout": 15,   # Increased timeout for reliability
            "impersonate": "chrome110",
            "max_redirects": 10,  # Increased to handle more redirects
        }
        
        if socks5:
            session_config["proxy"] = f"socks5://{socks5}"
        
        self._session = AsyncSession(**session_config)
        self._base_url = config.daddylive_uri
        self.channels = []
        self._load_lock = asyncio.Lock()
        with open("freesky/meta.json", "r") as f:
            self._meta = json.load(f)
        
        logger.info(f"StepDaddyHybrid initialized with max_streams: {max_streams}")

    # ponytail: the CDN binds each signed playlist token to the User-Agent that
    # fetched the embed page and minted it. Verified 2026-09 by cross-fetching:
    #
    #   token minted with Chrome 153  -> Chrome 153: 200   Firefox 137: 403
    #   token minted with Firefox 137 -> Firefox 137: 200   Chrome 153: 403
    #
    # So the VALUE is arbitrary — both work — and only CONSISTENCY matters. Vary
    # the UA between minting and fetching and every stream 403s.
    #
    # That makes this constant a hard coupling with backend._upstream_headers,
    # which proxies the playlist and segment fetches: the resolver mints the
    # token, the backend spends it, and if the two disagree nothing plays. Both
    # sites MUST read this one value — do not reintroduce a literal at either.
    # (Measuring this is easy to get wrong: hold one token fixed and vary the UA
    # and the binding looks exactly like an exact-match allowlist.)
    #
    # The segment hop additionally requires a Referer of the player origin, and
    # ignores the UA; the m3u8 hop is the reverse. Both must be satisfied.
    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"
    )
    USER_AGENT = os.environ.get("UPSTREAM_USER_AGENT", "").strip() or DEFAULT_USER_AGENT

    def _headers(self, referer: str = None, origin: str = None):
        if referer is None:
            referer = self._base_url
        headers = {
            "Referer": referer,
            "user-agent": self.USER_AGENT,
        }
        if origin:
            headers["Origin"] = origin
        return headers

    async def load_channels(self):
        # Use lock to prevent concurrent loading
        async with self._load_lock:
            channels = []
            try:
                logger.debug(f"Starting channel load from {self._base_url}/24-7-channels.php")
                response = await self._session.get(
                    f"{self._base_url}/24-7-channels.php",
                    headers=self._headers(),
                    allow_redirects=True,
                    max_redirects=10
                )
                
                logger.debug(f"Got response with status {response.status_code}")
                if response.status_code != 200:
                    logger.error(f"Failed to fetch channels: HTTP {response.status_code}")
                    return

                # ponytail: upstream moved to /watch.php?id=N cards; old
                # "<center><h1 ... tab-2" block + <strong> markup is gone.
                logger.debug("Extracting channel cards from response")
                channels_data = re.findall(
                    r'href="/watch\.php\?id=(\d+)"[^>]*?>\s*<div class="card__title">(.*?)</div>',
                    str(response.text),
                    re.DOTALL,
                )
                logger.debug(f"Found {len(channels_data)} raw channel entries")

                if not channels_data:
                    logger.error("No channel cards found in response")
                    logger.debug(f"Response text: {response.text[:500]}...")
                    return

                # Process channels concurrently for better performance
                tasks = []
                for channel_data in channels_data:
                    tasks.append(self._process_channel_data(channel_data))
                
                # Wait for all channel processing to complete
                logger.debug(f"Processing {len(tasks)} channels concurrently")
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Error processing channel: {str(result)}")
                    elif result is not None:
                        channels.append(result)

                # 24-7-channels.php lists only the always-on channels. Event feeds
                # (DAZN PPV, Event PPV, Backup Stream, ESPN+ ...) exist solely on the
                # homepage schedule, so without this merge they never reach the
                # channel list or the playlist however often you hit refresh.
                channels.extend(await self._load_event_channels({c.id for c in channels}))

                logger.info(f"Successfully processed {len(channels)} channels")
            except Exception as e:
                logger.error(f"Error loading channels: {str(e)}", exc_info=True)
                # Try to load fallback channels if primary source fails
                try:
                    logger.info("Attempting to load fallback channels...")
                    fallback_path = os.path.join(os.path.dirname(__file__), 'fallback_channels.json')
                    if os.path.exists(fallback_path):
                        with open(fallback_path, 'r') as f:
                            fallback_data = json.load(f)
                            for ch in fallback_data:
                                channels.append(Channel(
                                    id=ch.get('id', ''),
                                    name=ch.get('name', 'Unknown'),
                                    tags=ch.get('tags', []),
                                    logo=ch.get('logo', '/missing.png')
                                ))
                        logger.info(f"Loaded {len(channels)} fallback channels")
                except Exception as fb_error:
                    logger.error(f"Failed to load fallback channels: {str(fb_error)}")
            finally:
                if channels:
                    logger.debug(f"Updating channels list with {len(channels)} channels")
                    self.channels = sorted(channels, key=lambda channel: (channel.name.startswith("18"), channel.name))
                    # ids never scraped before start disabled (see channel_prefs)
                    new_ids = channel_prefs.register_channels(c.id for c in self.channels)
                    if new_ids:
                        logger.info(f"{len(new_ids)} new upstream channel(s) added as disabled: {sorted(new_ids)[:20]}")
                else:
                    logger.warning("No channels were loaded, keeping existing channels list")

    # Homepage schedule links carry the feed name in title=, e.g.
    # <a href="/watch.php?id=69" title="DAZN PPV" ...>
    _EVENT_CHAN_RE = re.compile(r'href="/watch\.php\?id=(\d+)"[^>]*?title="([^"]*)"')

    async def _load_event_channels(self, seen: set):
        """Channels that only appear on the homepage schedule (PPV/event feeds).

        Returns Channels for every /watch.php id on the homepage whose id isn't
        already in `seen`. Upstream reuses an id under different titles across
        events (59 is both "PPV Feed" and "DAZN PPV"), so first title wins.
        """
        try:
            response = await self._session.get(self._base_url, headers=self._headers())
            if response.status_code != 200:
                logger.warning(f"Event channel page returned HTTP {response.status_code}")
                return []
            extra = {}
            for channel_id, name in self._EVENT_CHAN_RE.findall(str(response.text)):
                if channel_id not in seen and channel_id not in extra:
                    extra[channel_id] = name
            # Dozens of feeds share a name ("Backup Stream" x130); without the id
            # they're indistinguishable rows in the UI and the playlist.
            used = set()
            channels = []
            for channel_id, name in extra.items():
                channel = self._get_channel((channel_id, name))
                if channel.name in used:
                    channel.name = f"{channel.name} ({channel_id})"
                used.add(channel.name)
                channels.append(channel)
            logger.info(f"Found {len(channels)} event channels not in the 24/7 list")
            return channels
        except Exception as e:
            logger.error(f"Error loading event channels: {str(e)}")
            return []

    async def _process_channel_data(self, channel_data):
        """Process a single channel data asynchronously"""
        try:
            return self._get_channel(channel_data)
        except Exception as e:
            logger.error(f"Error processing channel {channel_data}: {str(e)}")
            return None

    def _get_channel(self, channel_data) -> Channel:
        # channel_data is (id, name) from the /watch.php?id=N card markup
        channel_id = channel_data[0]
        channel_name = html.unescape(channel_data[1]).strip()
        if channel_id == "666":
            channel_name = "Nick Music"
        if channel_id == "609":
            channel_name = "Yas TV UAE"
        if channel_name == "#0 Spain":
            channel_name = "Movistar Plus+"
        elif channel_name == "#Vamos Spain":
            channel_name = "Vamos Spain"
        clean_channel_name = re.sub(r"\s*\(.*?\)", "", channel_name)
        meta = self._meta.get(clean_channel_name, {})
        # Upstream publishes per-channel art at {base}/logos/<slug>.<ext>, so channels
        # added after meta.json was written still get a logo, and it follows
        # DADDYLIVE_URI when the site moves. meta.json only covers ~2/3 of the list;
        # it stays as the fallback because upstream misses ~18%.
        # /api/logo tries the other extensions before giving up.
        slug = re.sub(r"[^a-z0-9]+", "_", clean_channel_name.lower()).strip("_")
        logo = meta.get("logo") or f"{self._base_url}/logos/{slug}.png"
        if logo.startswith("http"):
            logo = f"/api/logo/{urlsafe_base64(logo)}"
        return Channel(id=channel_id, name=channel_name, tags=meta.get("tags", []), logo=logo)

    # ponytail: hosts are discovered from the pages, never hardcoded. Upstream has already
    # moved twice (vidembed.re, fnjplay.xyz -> both dead); following the iframe chain
    # survives the next move without a code change.
    _IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
    _ATOB_RE = re.compile(r"atob\(\s*['\"]([A-Za-z0-9+/=]+)['\"]\s*\)")
    # As of 2026-09 the player page stopped hiding the URL in atob() and just
    # assigns it: var STREAM_URL = "https:\/\/premium.hls.st\/playlist\/premium589.m3u8";
    # Both forms are scanned so either upstream style resolves.
    _PLAIN_M3U8_RE = re.compile(r'["\'](https?:(?:\\?/)+[^"\']+?\.m3u8[^"\']*)["\']')
    # As of 2026-09 the surviving players stopped exposing the URL in any form the
    # two patterns above can see: atob() is now applied to *variables*, and no
    # plaintext URL appears anywhere in the body. The payload moved into a single
    # base64 blob assigned to _econfig, which decodes to the player's whole JSON
    # config. Neither older pattern matches it, which is what took every channel
    # down — the pages were fetched correctly and simply could not be read.
    _ECONFIG_RE = re.compile(r"_econfig\s*=\s*['\"]([A-Za-z0-9+/=]+)['\"]")

    @classmethod
    def _decode_econfig(cls, blob: str):
        """Decode an `_econfig` blob to its stream URL, or None if it isn't one.

        Reversed from the player's obfuscated stream.js. The blob is base64 over a
        4-way split of an inner base64 document: each quarter carries one junk
        character at index 3, and the quarters are emitted in the order [2,0,3,1].
        Undo those and the result is base64 JSON holding the signed playlist URL.

        Pure local computation — no JavaScript is executed and no browser is
        involved, which is the only reason this class of obfuscation is tractable
        at all. Returns None on any malformation so the caller can fall through to
        the other extractors.

        Args:
            blob: the base64 string captured by `_ECONFIG_RE`.

        Returns:
            The stream URL, or None if the blob is absent, malformed, or carries
            no usable URL.
        """
        try:
            s = base64.b64decode(blob).decode("latin1")  # byte-transparent; utf-8 would corrupt
            if len(s) < 8:
                return None
            size = -(-len(s) // 4)  # ceil, without importing math
            parts = [s[i * size:(i + 1) * size] for i in range(4)]
            out = [None] * 4
            for i, part in enumerate(parts):
                if len(part) < 4:
                    return None  # a short quarter means the index-3 strip is meaningless
                part = part[:3] + part[4:]
                out[[2, 0, 3, 1][i]] = base64.b64decode(
                    part + "=" * (-len(part) % 4)
                ).decode("latin1")
            joined = "".join(out)
            cfg = json.loads(base64.b64decode(joined + "=" * (-len(joined) % 4)))
        except Exception as e:
            # Hostile, rotating input: enumerating binascii/JSON/Index errors would
            # be wrong within a month. DEBUG because this runs per page per player
            # and a miss is the normal case on players that use another encoding.
            logger.debug(f"_econfig decode failed ({type(e).__name__})")
            return None
        # nop2p skips the WebRTC swarm the player would otherwise join; irrelevant
        # to a server-side proxy and identical in practice.
        url = cfg.get("stream_url_nop2p") or cfg.get("stream_url")
        if isinstance(url, str) and url.startswith("http") and ".m3u8" in url:
            return url
        return None

    @classmethod
    def _stream_candidates(cls, page: str):
        """Every m3u8 URL a player page offers, base64-obfuscated or plain."""
        for encoded in cls._ATOB_RE.findall(page):
            try:
                url = base64.b64decode(encoded).decode()
            except Exception:
                continue  # not every atob() on the page is the stream URL
            if url.startswith("http") and ".m3u8" in url:
                yield url
        for url in cls._PLAIN_M3U8_RE.findall(page):
            yield url.replace("\\/", "/")
        for blob in cls._ECONFIG_RE.findall(page):
            url = cls._decode_econfig(blob)
            if url:
                yield url

    # Upstream's watch.php offers several "players", each its own path that leads
    # to an independent iframe chain. Trying them in order gives real failover:
    # when one player's CDN feed is offline the next may still be live. Order is
    # best-first from measurement (stream/watch resolve most reliably); the rest
    # are tried before giving up. A channel-specific preference can pin one first.
    # Order is best-first from measurement (2026-09): `stream` is the confirmed
    # working _econfig chain; `plus` and `casting` are live hosts whose payloads
    # use encodings we cannot decode yet; `cast` resolves to the SAME embed as
    # `stream`, so it is a duplicate rather than independent failover and earns no
    # early slot; `watch` (403, plus a DNS-dead second iframe) and `player`
    # (DNS-dead) go last. Nothing is deleted — hosts are discovered, not
    # hardcoded, and upstream has already rotated twice; deleting paths optimises
    # for today's dead hosts and forfeits the next rotation. Once the first player
    # resolves, the tail is never reached, so ordering buys what deletion would
    # without the regression risk.
    PLAYER_PATHS = ["stream", "plus", "casting", "cast", "watch", "player"]

    # channel_id -> (upstream m3u8 url, referer, resolved_at). Crawling the iframe
    # chain costs ~4s, but a live playlist has to be re-fetched every few seconds
    # or the player runs out of segments. Remembering the URL makes the refresh a
    # single ~0.3s GET; a failed refresh drops the entry and re-crawls.
    _resolved: dict = {}
    _RESOLVED_TTL = 600

    # Signed CDN URLs carry their own expiry: ?e=<epoch>, currently ~6h out, and
    # the ?s= signature replays freely until then (verified: a URL minted 10 min
    # earlier still served 200, and again 45s later, while the media sequence
    # advanced). A flat 600s therefore re-crawls ~35 times inside one token's life
    # for no benefit. Deriving the TTL from `e` tracks upstream's own stated
    # expiry and self-corrects if it shortens; the margin means we never hand a
    # player a URL that dies mid-session. Corrupted `s` and expired `e` both
    # return 403 and are indistinguishable without parsing `e` ourselves.
    _M3U8_EXPIRY_MARGIN = int(os.environ.get("M3U8_EXPIRY_MARGIN", "1800"))
    _M3U8_TTL_MIN = int(os.environ.get("M3U8_TTL_MIN", "60"))
    _M3U8_TTL_MAX = int(os.environ.get("M3U8_TTL_MAX", "18000"))

    # How many DIFFERENT players must have their feed 404'd by the CDN before we
    # call a channel off air and stop crawling. One is not enough: players sit on
    # different providers and a page can advertise a stale ad/fallback URL.
    _OFFAIR_PLAYER_QUORUM = 2

    @classmethod
    def _cache_ttl_for(cls, m3u8_url: str) -> int:
        """How long a resolved playlist URL stays usable, from its own `e` param.

        Args:
            m3u8_url: the signed CDN URL just resolved.

        Returns:
            Seconds to cache, clamped to [_M3U8_TTL_MIN, _M3U8_TTL_MAX]. Falls
            back to _RESOLVED_TTL when `e` is absent or unparseable, so an
            upstream that drops the parameter degrades to today's behaviour.
        """
        match = re.search(r"[?&]e=(\d{9,})", m3u8_url or "")
        if not match:
            return cls._RESOLVED_TTL
        remaining = int(match.group(1)) - time.time() - cls._M3U8_EXPIRY_MARGIN
        return int(max(cls._M3U8_TTL_MIN, min(cls._M3U8_TTL_MAX, remaining)))

    async def _resolve_via_iframe_chain(self, channel_id: str, max_hops: int = 4,
                                        max_pages: int = 8, prefer: str = None,
                                        budget: float = None, single_feed: bool = False):
        """
        Follow the live upstream chain to a WORKING HLS playlist, failing over
        across the available players.

        `budget` is the total seconds this crawl may take; None means RESOLVE_BUDGET.
        Raises ChannelOffAirError as soon as _OFFAIR_PLAYER_QUORUM players have had
        their feed 404'd by the CDN, rather than spending the rest of the budget
        re-confirming that a channel which is not broadcasting is still not
        broadcasting.

        As of 2026-07 a player chain is: /<player>/stream-N.php -> <player-host>
        /premiumtv/daddyN.php?id=N, whose Clappr config carries the m3u8 URL inside
        window.atob('<base64>'). Each hop needs the previous page as Referer.

        A candidate m3u8 is only accepted once its body actually starts with
        #EXTM3U — a URL that resolves but returns "Not found" is treated as a dead
        source and we move on, which is what makes failover real rather than
        cosmetic.
        """
        players = list(self.PLAYER_PATHS)
        if prefer and prefer in players:
            players.remove(prefer)
            players.insert(0, prefer)
        # Manual feed pick from the watch-page switcher: resolve ONLY that feed and
        # play exactly what it carries (even if silent), so the viewer can hear which
        # sources actually have audio. Auto (single_feed=False) keeps hunting for one.
        if single_feed and prefer:
            players = [prefer]

        watch_url = f"{self._base_url}/watch.php?id={channel_id}"
        deadline = asyncio.get_event_loop().time() + (RESOLVE_BUDGET if budget is None else budget)
        last_error = None
        # A feed that resolves but declares no audio is kept here and only used if
        # no player with audio turns up — so a silent channel still shows a picture
        # rather than 404ing, but any feed WITH audio always wins.
        video_only_fallback = None
        # Players whose feed the CDN answered 404 for. One 404 is not proof the
        # channel is off air — players sit on different providers, and a page can
        # advertise a stale ad/fallback URL. Two independent providers both saying
        # "no such stream" is proof enough, and stopping there is the difference
        # between ~6s and the full 20s budget for a channel that is not on.
        offair_players = set()

        for player in players:
            if asyncio.get_event_loop().time() > deadline:
                break  # a dead channel shouldn't burn the whole request on every player
            player_saw_offair = False
            # Seed each player from its own entry page but keep watch.php as the
            # referer, mirroring how the site navigates between players.
            start = f"{self._base_url}/{player}/stream-{channel_id}.php"
            queue = [(start, watch_url, 0)]
            seen = set()
            player_dead = False

            while queue and len(seen) < max_pages and not player_dead:
                if asyncio.get_event_loop().time() > deadline:
                    break
                url, referer, depth = queue.pop(0)
                if url in seen or depth > max_hops:
                    continue
                seen.add(url)

                try:
                    # Per-hop cap: several player hosts are dead and hang for the
                    # full session timeout, which alone would blow the whole budget
                    # on one bad iframe. Fail that hop fast and try the next player.
                    response = await asyncio.wait_for(
                        self._session.get(url, headers=self._headers(referer)),
                        timeout=8.0,
                    )
                except Exception as e:
                    logger.debug(f"Hop failed for {url}: {e}")
                    continue
                if response.status_code != 200:
                    continue

                tried_any = False
                for candidate in self._stream_candidates(response.text):
                    tried_any = True
                    try:
                        content, has_audio = await self._fetch_playlist(candidate, url)
                    except ChannelOffAirError as e:
                        # The origin minted this URL and then denied having the
                        # stream. Note it and still try the page's other
                        # candidates, in case this one was an ad or a stale
                        # fallback rather than the feed.
                        player_saw_offair = True
                        last_error = e
                        logger.debug(f"Candidate off air for {channel_id}: {candidate}: {e}")
                        continue
                    except Exception as e:
                        # Not every m3u8 on the page is the feed (ads, fallbacks),
                        # so try the rest before writing this player off.
                        last_error = e
                        logger.debug(f"Candidate dead for {channel_id}: {candidate}: {e}")
                        continue
                    if has_audio or single_feed:
                        logger.info(f"Resolved channel {channel_id} via '{player}' player")
                        self._resolved[channel_id] = (candidate, url, time.time())
                        return content
                    # Resolved but video-only. Remember it, then try the next
                    # player for one that actually carries sound.
                    if video_only_fallback is None:
                        video_only_fallback = (content, candidate, url)
                    logger.info(f"Player '{player}' for {channel_id} is video-only; trying next for audio")
                    player_dead = True
                    break
                else:
                    # Every m3u8 this page offered was dead: the player's feed is
                    # down, so stop crawling its ad iframes and move to the next.
                    if tried_any:
                        player_dead = True
                        logger.debug(f"Player '{player}' feed dead for {channel_id}")

                if not player_dead:
                    for src in self._IFRAME_RE.findall(response.text):
                        # Skip templated srcs like "' + url + '" in inline scripts.
                        if "://" in src or src.startswith("/"):
                            queue.append((urljoin(url, src), url, depth + 1))

            # This player offered a feed and the CDN denied having it. Once two
            # independent players agree, stop crawling: the channel is not being
            # broadcast and the remaining players cost seconds to tell us so again.
            if player_saw_offair:
                offair_players.add(player)
                if len(offair_players) >= self._OFFAIR_PLAYER_QUORUM:
                    raise ChannelOffAirError(
                        f"Channel {channel_id} is off air: players "
                        f"{sorted(offair_players)} all returned HTTP 404 from the CDN"
                    )

        if video_only_fallback is not None:
            logger.warning(f"No feed with audio for channel {channel_id}; using video-only fallback")
            content, candidate, referer = video_only_fallback
            self._resolved[channel_id] = (candidate, referer, time.time())
            return content
        raise ValueError(
            f"No working stream found for channel {channel_id} across "
            f"{len(players)} players" + (f" (last: {last_error})" if last_error else "")
        )

    # Audio codecs a master playlist can name. If CODECS is present on every
    # variant and none of these appear, and there's no separate audio rendition,
    # the feed is genuinely video-only (confirmed against a silent Sky Sports NZ
    # feed whose segment PMT carried H.264 and nothing else).
    _AUDIO_CODECS = ("mp4a", "ac-3", "ec-3", "ac3", "ec3", "opus", "flac", "alac", "dts", "mp3")

    @staticmethod
    def _declares_audio(playlist_text: str) -> bool:
        """Whether an HLS playlist looks like it carries audio.

        ponytail: judged for free from the master playlist's CODECS / EXT-X-MEDIA,
        never a segment download. When CODECS is absent — a media playlist, or an
        upstream that just omits it — we can't tell cheaply, so we assume audio
        rather than pay a segment probe on every resolve. Upgrade path: probe one
        segment's PMT if silent media-playlist feeds ever show up.
        """
        if "TYPE=AUDIO" in playlist_text:  # a separate audio rendition is declared
            return True
        codecs = re.findall(r'CODECS="([^"]*)"', playlist_text)
        if not codecs:
            return True  # nothing declared -> can't judge without a segment; assume ok
        return any(a in group.lower() for group in codecs for a in StepDaddyHybrid._AUDIO_CODECS)

    async def _fetch_playlist(self, m3u8_url: str, referer: str):
        """Fetch an HLS playlist and rewrite it for the proxy.

        Returns (proxied_playlist, has_audio) so the resolver can fail over off a
        video-only feed to one that actually has sound.
        """
        # The CDN answers 503 "Stream starting, please wait a moment..." while it
        # spins the feed up — a live channel, not a dead one. One short retry is
        # the difference between playing and reporting the player dead.
        for attempt in range(3):
            response = await self._session.get(m3u8_url, headers=self._headers(referer))
            if response.status_code == 200 and response.text.startswith("#EXTM3U"):
                break
            # 404 means the origin has no such stream: the channel is off air, not
            # that this candidate was the wrong URL. Retrying or trying the next
            # player cannot conjure a feed that is not being broadcast, and doing
            # so is what made an off-air channel cost the full 20s budget.
            if response.status_code == 404:
                raise ChannelOffAirError(f"Off air (HTTP 404) from {m3u8_url}")
            if response.status_code != 503 or attempt == 2:
                raise ValueError(f"Bad playlist from {m3u8_url}: HTTP {response.status_code}")
            await asyncio.sleep(1.5)

        has_audio = self._declares_audio(response.text)
        # Variant/segment URIs are relative to the playlist, but _process_stream_content
        # only proxies lines starting with "http" — left alone, the player would resolve
        # them against /api/stream/ on our own host and 404.
        absolute = "\n".join(
            urljoin(m3u8_url, line) if line and not line.startswith("#") else line
            for line in response.text.split("\n")
        )
        # Rewrite against the player page, not the CDN URL: the CDN 403s any request
        # whose Referer is not the embedding page, and our proxy has to replay it.
        return self._process_stream_content(absolute, referer), has_audio

    async def stream(self, channel_id: str, prefer: str = None, single_feed: bool = False):
        """
        Resolve a channel to a proxied HLS playlist, failing over across upstream
        players. `prefer` pins one player (see PLAYER_PATHS) to try first;
        `single_feed` (the manual watch-page switcher) resolves ONLY that player.

        ponytail: the old vidembed.re / fnjplay.xyz fallbacks were removed — both
        hosts are dead (DNS no longer resolves), so they only added a multi-second
        stall before the same failure. The iframe chain already tries every live
        player. `_handle_new_architecture`/`_handle_old_architecture` remain for
        the multi_service_streamer callers but are no longer on this path.
        """
        if not prefer:
            hit = self._resolved.get(channel_id)
            if hit and time.time() - hit[2] < self._cache_ttl_for(hit[0]):
                try:
                    content, _ = await self._fetch_playlist(hit[0], hit[1])
                    return content
                except Exception as e:
                    # The feed moved or died; fall through to a full re-crawl.
                    logger.info(f"Cached feed for {channel_id} stopped working ({e}); re-resolving")
                    self._resolved.pop(channel_id, None)
            return await self._resolve_single_flight(channel_id, single_feed=single_feed)
        return await self._resolve_via_iframe_chain(channel_id, prefer=prefer, single_feed=single_feed)

    # channel_id -> in-flight resolution. A crawl costs up to 20s, so without this
    # every viewer arriving during one starts their own: the incident logs show
    # "Active streams: 1/2/3" for a single channel, three independent crawls all
    # failing the same way. Followers wait on the leader instead, and share its
    # failure too, so a dead channel fails fast for everyone after the first.
    _inflight: dict = {}

    async def _resolve_single_flight(self, channel_id: str, single_feed: bool = False):
        """Resolve a channel, collapsing concurrent callers onto one crawl.

        Args:
            channel_id: channel being resolved.
            single_feed: passed through to the resolver.

        Returns:
            The proxied playlist produced by `_resolve_via_iframe_chain`.

        Raises:
            Whatever the resolver raises — propagated to every waiter, so N
            concurrent viewers of a dead channel cost one crawl, not N.
        """
        existing = self._inflight.get(channel_id)
        if existing is not None:
            logger.debug(f"Joining in-flight resolution for channel {channel_id}")
            # shield: a follower timing out or disconnecting must not cancel the
            # leader's crawl out from under the other waiters.
            return await asyncio.shield(existing)

        future = asyncio.get_event_loop().create_future()
        self._inflight[channel_id] = future
        try:
            result = await self._resolve_via_iframe_chain(
                channel_id, single_feed=single_feed
            )
        except BaseException as e:
            if not future.done():
                future.set_exception(e)
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            # Must be finally: a leaked key wedges this channel until restart.
            self._inflight.pop(channel_id, None)
            # Nobody may be awaiting a failed future; retrieve to silence asyncio's
            # "exception was never retrieved" warning.
            if future.done() and future.exception() is not None:
                future.exception()

    async def _handle_new_architecture(self, vidembed_url: str, referer: str):
        """Handle the new vidembed.re architecture with proper iframe-based authentication"""
        logger.debug(f"Processing vidembed URL: {vidembed_url}")
        
        try:
            # Extract UUID from vidembed URL
            uuid_match = re.search(r'/stream/([a-f0-9-]{36})', vidembed_url)
            if not uuid_match:
                raise ValueError("Could not extract UUID from vidembed URL")
            
            uuid = uuid_match.group(1)
            logger.debug(f"Extracted UUID: {uuid}")
            
            # Try to use the iframe-based extractor for proper authentication
            try:
                from .vidembed_extractor import extract_hls_from_vidembed
                logger.info("Attempting iframe-based extraction...")
                hls_url = await extract_hls_from_vidembed(vidembed_url)
                
                if hls_url:
                    logger.info(f"Successfully extracted HLS URL via iframe: {hls_url}")
                    # Test the HLS URL
                    stream_response = await self._session.get(hls_url, headers=self._headers(vidembed_url))
                    if stream_response.status_code == 200 and stream_response.text.startswith('#EXTM3U'):
                        return self._process_stream_content(stream_response.text, vidembed_url)
            except Exception as iframe_error:
                logger.warning(f"Iframe-based extraction failed: {str(iframe_error)}")
            
            # Fallback: Try direct API approach with proper headers
            logger.info("Attempting direct API approach with iframe simulation...")
            api_url = f"https://www.vidembed.re/api/source/{uuid}?type=live"
            
            # Headers that simulate iframe context
            api_headers = self._headers(vidembed_url)
            api_headers.update({
                "Origin": "https://vidembed.re",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            })
            
            api_response = await self._session.get(api_url, headers=api_headers)
            
            if api_response.status_code == 200:
                try:
                    api_data = api_response.json()
                    logger.debug(f"API Response: {api_data}")
                    
                    # Look for stream data in the response
                    if 'data' in api_data and isinstance(api_data['data'], list):
                        for item in api_data['data']:
                            if 'file' in item:
                                stream_url = item['file']
                                logger.info(f"Found stream URL from API: {stream_url}")
                                
                                # Test the stream URL
                                stream_response = await self._session.get(stream_url, headers=self._headers(vidembed_url))
                                if stream_response.status_code == 200 and stream_response.text.startswith('#EXTM3U'):
                                    return self._process_stream_content(stream_response.text, vidembed_url)
                    
                    # If direct API response doesn't have stream URLs, check for encrypted data
                    if 'data' in api_data and isinstance(api_data['data'], str):
                        # This might be encrypted data that needs client-side decryption
                        logger.info("API returned encrypted data, may need client-side processing")
                        return self._create_vidembed_response(vidembed_url)
                        
                except Exception as json_error:
                    logger.warning(f"Error parsing API response JSON: {str(json_error)}")
            else:
                logger.warning(f"API request failed with status {api_response.status_code}")
            
            # Fallback: Access vidembed page directly
            logger.info("Attempting direct vidembed page access...")
            vidembed_response = await self._session.get(vidembed_url, headers=self._headers(referer))
            
            if vidembed_response.status_code != 200:
                raise ValueError(f"Failed to access vidembed page: HTTP {vidembed_response.status_code}")
            
            # Look for direct stream URLs in page content
            stream_urls = self._extract_stream_urls(vidembed_response.text)
            
            if stream_urls:
                # Found direct stream URLs
                stream_url = stream_urls[0]
                logger.debug(f"Found direct stream URL: {stream_url}")
                
                # Fetch the stream content
                stream_response = await self._session.get(stream_url, headers=self._headers(vidembed_url))
                
                if stream_response.status_code == 200:
                    return self._process_stream_content(stream_response.text, vidembed_url)
                else:
                    raise ValueError(f"Failed to fetch stream: HTTP {stream_response.status_code}")
            else:
                # No direct URLs found, try to extract from JavaScript variables
                logger.debug("No direct stream URLs found, trying JavaScript extraction...")
                js_stream_url = self._extract_js_stream_url(vidembed_response.text)
                
                if js_stream_url:
                    logger.debug(f"Found JavaScript stream URL: {js_stream_url}")
                    stream_response = await self._session.get(js_stream_url, headers=self._headers(vidembed_url))
                    
                    if stream_response.status_code == 200:
                        return self._process_stream_content(stream_response.text, vidembed_url)
                    else:
                        logger.warning(f"Failed to fetch JavaScript stream: HTTP {stream_response.status_code}")
                
                # If all else fails, return vidembed URL for client-side processing
                logger.debug("No direct stream URLs found, returning vidembed URL for client-side processing")
                return self._create_vidembed_response(vidembed_url)
                
        except Exception as e:
            logger.error(f"Error in new architecture handling: {str(e)}")
            # Return vidembed URL for client-side processing as last resort
            return self._create_vidembed_response(vidembed_url)

    async def _handle_old_architecture(self, iframe_url: str, referer: str):
        """Handle the old authentication-based architecture"""
        logger.debug(f"Processing iframe URL: {iframe_url}")
        
        # Make request to iframe source
        iframe_response = await self._session.post(iframe_url, headers=self._headers(referer))
        
        if iframe_response.status_code != 200:
            raise ValueError(f"Failed to access iframe: HTTP {iframe_response.status_code}")
        
        # Extract authentication variables
        try:
            channel_key = re.compile(r"var\s+channelKey\s*=\s*\"(.*?)\";").findall(iframe_response.text)[-1]
            auth_ts = extract_and_decode_var("__c", iframe_response.text)
            auth_sig = extract_and_decode_var("__e", iframe_response.text)
            auth_path = extract_and_decode_var("__b", iframe_response.text)
            auth_rnd = extract_and_decode_var("__d", iframe_response.text)
            auth_url = extract_and_decode_var("__a", iframe_response.text)
            
            logger.debug("Successfully extracted authentication variables")
            
            # Make authentication request
            auth_request_url = f"{auth_url}{auth_path}?channel_id={channel_key}&ts={auth_ts}&rnd={auth_rnd}&sig={auth_sig}"
            auth_response = await self._session.get(auth_request_url, headers=self._headers(iframe_url))
            
            if auth_response.status_code != 200:
                raise ValueError("Failed to get auth response")
            
            # Server lookup
            key_url = urlparse(iframe_url)
            key_url = f"{key_url.scheme}://{key_url.netloc}/server_lookup.php?channel_id={channel_key}"
            key_response = await self._session.get(key_url, headers=self._headers(iframe_url))
            server_key = key_response.json().get("server_key")
            
            if not server_key:
                raise ValueError("No server key found in response")
            
            # Construct final stream URL
            if server_key == "top1/cdn":
                server_url = f"https://top1.newkso.ru/top1/cdn/{channel_key}/mono.m3u8"
            else:
                server_url = f"https://{server_key}new.newkso.ru/{server_key}/{channel_key}/mono.m3u8"
            
            # Fetch M3U8 playlist
            m3u8 = await self._session.get(server_url, headers=self._headers(quote(str(iframe_url))))
            
            # Process M3U8 content
            m3u8_data = ""
            for line in m3u8.text.split("\n"):
                if line.startswith("#EXT-X-KEY:"):
                    original_url = re.search(r'URI="(.*?)"', line).group(1)
                    line = line.replace(original_url, f"/api/key/{encrypt(original_url)}/{encrypt(urlparse(iframe_url).netloc)}")
                elif line.startswith("#EXT-X-MEDIA:") and config.proxy_content:
                    # Separate audio/subtitle rendition playlist lives in URI="...";
                    # unproxied, ffmpeg (Dispatcharr) can't fetch audio -> silent stream.
                    m = re.search(r'URI="(https?://.*?)"', line)
                    if m:
                        line = line.replace(m.group(1), f"/api/content/{encrypt(m.group(1))}{hls_ext(m.group(1))}")
                elif line.startswith("http") and config.proxy_content:
                    line = f"/api/content/{encrypt(line)}{hls_ext(line)}"
                m3u8_data += line + "\n"
            
            return m3u8_data
            
        except Exception as e:
            logger.error(f"Error in old architecture: {str(e)}")
            raise ValueError(f"Failed to process old architecture: {str(e)}")

    async def _handle_direct_stream(self, stream_url: str, referer: str):
        """Handle direct stream URLs"""
        logger.debug(f"Processing direct stream URL: {stream_url}")
        
        # Fetch the stream content
        stream_response = await self._session.get(stream_url, headers=self._headers(referer))
        
        if stream_response.status_code == 200:
            return self._process_stream_content(stream_response.text, referer)
        else:
            raise ValueError(f"Failed to fetch direct stream: HTTP {stream_response.status_code}")

    def _extract_stream_urls(self, vidembed_content: str) -> List[str]:
        """Extract direct stream URLs from vidembed content"""
        stream_patterns = [
            r'https://[^"\']*\.m3u8[^"\']*',
            r'https://[^"\']*\.mp4[^"\']*',
            r'https://[^"\']*stream[^"\']*',
            r'https://[^"\']*cdn[^"\']*',
        ]
        
        found_urls = []
        for pattern in stream_patterns:
            matches = re.findall(pattern, vidembed_content)
            found_urls.extend(matches)
        
        # Remove duplicates and filter out non-stream URLs
        unique_urls = list(set(found_urls))
        stream_urls = [url for url in unique_urls if any(ext in url.lower() for ext in ['.m3u8', '.mp4', 'stream', 'cdn']) 
                      and 'cdnjs.cloudflare.com' not in url]  # Exclude CDN libraries
        
        return stream_urls

    def _extract_js_stream_url(self, vidembed_content: str) -> str:
        """Extract stream URL from JavaScript variables"""
        # Look for common JavaScript patterns
        js_patterns = [
            r'var\s+streamUrl\s*=\s*["\']([^"\']+)["\']',
            r'var\s+videoUrl\s*=\s*["\']([^"\']+)["\']',
            r'var\s+src\s*=\s*["\']([^"\']+)["\']',
            r'streamUrl\s*:\s*["\']([^"\']+)["\']',
            r'videoUrl\s*:\s*["\']([^"\']+)["\']',
            r'src\s*:\s*["\']([^"\']+)["\']',
            r'url\s*:\s*["\']([^"\']+)["\']',
        ]
        
        for pattern in js_patterns:
            matches = re.findall(pattern, vidembed_content)
            for match in matches:
                if any(ext in match.lower() for ext in ['.m3u8', '.mp4', 'stream', 'cdn']):
                    return match
        
        return None

    def _process_stream_content(self, content: str, referer: str) -> str:
        """Process stream content (M3U8, etc.) and proxy URLs with token validation"""
        if content.startswith('#EXTM3U'):
            # This is an M3U8 playlist, process it with token validation
            logger.debug("Processing M3U8 playlist with token validation")
            
            # First, extract and validate tokens
            try:
                viable_streams = extract_viable_streams(content)
                if viable_streams:
                    logger.info(f"Found {len(viable_streams)} viable streams with valid tokens")
                else:
                    logger.warning("No viable streams found with valid tokens")
            except Exception as token_error:
                logger.warning(f"Token validation failed: {str(token_error)}")
            
            lines = content.split('\n')
            processed_lines = []
            
            for line in lines:
                if line.startswith('http'):
                    # Validate token if present
                    try:
                        token_analysis = TokenValidator.analyze_token_security(line)
                        # Drop a URL only when the token parsed and is genuinely expired.
                        # "error" means the analyzer could not read a token at all — the
                        # current CDN puts expiry in the path, not query params, so
                        # treating unparseable as invalid emptied every playlist.
                        if not token_analysis.get('valid', True) and 'error' not in token_analysis:
                            logger.debug(f"Skipping expired stream: {line}")
                            continue  # Skip expired streams
                        elif token_analysis.get('expires_in_seconds', float('inf')) < 3600:  # Less than 1 hour
                            logger.warning(f"Stream expires soon: {token_analysis.get('expires_in_seconds', 0)} seconds")
                    except Exception as validation_error:
                        logger.debug(f"Token validation error for {line}: {str(validation_error)}")
                    
                    if config.proxy_content:
                        # Proxy content URLs, carrying the referer the CDN demands —
                        # same two-segment shape /api/key/ already uses. The trailing
                        # extension goes on the LAST component, which is what ffmpeg
                        # inspects.
                        line = f"/api/content/{encrypt(line)}/{encrypt(referer)}{hls_ext(line)}"
                elif line.startswith('#EXT-X-MEDIA:') and config.proxy_content:
                    # A separate audio/subtitle rendition keeps its playlist in a
                    # URI="..." attr, not on its own line, so the http branch never
                    # sees it. Unproxied, ffmpeg (Dispatcharr) can't reach the audio
                    # track and plays video only — the browser fetches it directly
                    # and sounds fine, which is why this only bites external players.
                    m = re.search(r'URI="(https?://.*?)"', line)
                    if m:
                        uri = m.group(1)
                        line = line.replace(uri, f"/api/content/{encrypt(uri)}/{encrypt(referer)}{hls_ext(uri)}")
                elif line.startswith('#EXT-X-KEY:'):
                    # Process encryption keys
                    original_url = re.search(r'URI="(.*?)"', line)
                    if original_url:
                        line = line.replace(original_url.group(1), f"/api/key/{encrypt(original_url.group(1))}/{encrypt(urlparse(referer).netloc)}")
                
                processed_lines.append(line)
            
            processed_content = '\n'.join(processed_lines)
            
            # Log token analysis summary
            try:
                tokens = TokenValidator.extract_tokens_from_m3u8(content)
                if tokens:
                    valid_tokens = sum(1 for t in tokens if t['analysis'].get('valid', False))
                    logger.info(f"Token summary: {valid_tokens}/{len(tokens)} valid tokens")
            except Exception as summary_error:
                logger.debug(f"Error generating token summary: {str(summary_error)}")
            
            return processed_content
        else:
            # Not an M3U8 playlist, return as is
            return content

    def _create_vidembed_response(self, vidembed_url: str) -> str:
        """Create a response that includes the vidembed URL for client-side processing"""
        return f"VIDEMBED_URL:{vidembed_url}"

    # Semaphore for limiting concurrent stream requests
    _stream_semaphore = None
    
    def _get_stream_semaphore(self):
        """Get or create stream semaphore with configurable limit"""
        if self._stream_semaphore is None:
            max_streams = int(os.environ.get("MAX_CONCURRENT_STREAMS", "10"))
            self._stream_semaphore = asyncio.Semaphore(max_streams)
            logger.info(f"Created stream semaphore with limit: {max_streams}")
        return self._stream_semaphore

    async def key(self, url: str, host: str):
        url = decrypt(url)
        host = decrypt(host)
        response = await self._session.get(url, headers=self._headers(f"{host}/", host), timeout=60)
        if response.status_code != 200:
            raise Exception(f"Failed to get key")
        return response.content

    @staticmethod
    def content_url(path: str):
        return decrypt(path)

    def playlist(self, exclude: set = None, token: str = None, base_url: str = None,
                 extra: list = None):
        """Build the M3U handed to external players.

        `extra` appends channels that do not come from the upstream scrape —
        today that is the admin's virtual channels, which are stored locally and
        so are never present in self.channels. They are deliberately a parameter
        rather than something this class fetches, because this class knows how to
        scrape one specific site and nothing else.
        """
        exclude = exclude or set()
        # Point back at whatever host:port the caller actually used. Hardcoding
        # config.api_url handed out LAN addresses to anyone reaching the app
        # through NAT or a reverse proxy on a different port, so every stream and
        # logo URL in the playlist was unreachable for them.
        base = (base_url or config.api_url).rstrip("/")
        # The player fetches each stream URL directly with no cookie, so the
        # caller's token has to be baked into every line for auth to hold.
        suffix = f"?token={token}" if token else ""
        data = "#EXTM3U\n"
        for channel in list(self.channels) + list(extra or []):
            if channel.id in exclude:
                continue
            logo = channel.logo
            # Relative logo paths are useless to an external player like VLC,
            # which has no idea what host the playlist came from.
            if logo and logo.startswith("/"):
                logo = f"{base}{logo}"
            # tvg-id/tvg-name make every row unique for importers like Dispatcharr,
            # which otherwise collapse same-named feeds ("SEE Denmark" x2, event
            # "Backup Stream" ids) into one and the rest go "missing".
            entry = f" tvg-id=\"{channel.id}\" tvg-name=\"{channel.name}\""
            entry += f" tvg-logo=\"{logo}\",{channel.name}" if logo else f",{channel.name}"
            data += f"#EXTINF:-1{entry}\n{base}/api/stream/{channel.id}.m3u8{suffix}\n"
        return data

    # The schedule JSON API is domain-gated (403 "Schedule API Available for
    # allowed Domain only!") and the open .json file is a stale 2025 snapshot, so
    # the live listings are scraped off the homepage where the site renders them.
    _SCHED_DAY_RE = re.compile(r'class="schedule__dayTitle"[^>]*>(.*?)</div>', re.S)
    _SCHED_CAT_RE = re.compile(r'class="card__meta"[^>]*>(.*?)</div>', re.S)
    _SCHED_EVENT_RE = re.compile(r'class="schedule__event"', re.S)
    _SCHED_TIME_RE = re.compile(r'class="schedule__time"[^>]*?data-time="([^"]*)"', re.S)
    _SCHED_TITLE_RE = re.compile(r'class="schedule__eventTitle"[^>]*>(.*?)</span>', re.S)
    _SCHED_CHAN_RE = re.compile(r'href="/watch\.php\?id=(\d+)"[^>]*>(.*?)</a>', re.S)

    @staticmethod
    def _sched_text(fragment: str) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

    def _parse_schedule(self, page: str) -> dict:
        """Turn the homepage's schedule markup into {day: {category: [events]}}.

        Walks day/category/event markers in document order and attaches each event
        to the most recent headings above it — the blocks nest, which regex can't
        match directly, but their order in the document is unambiguous.
        """
        marks = []
        for m in self._SCHED_DAY_RE.finditer(page):
            marks.append((m.start(), "day", self._sched_text(m.group(1))))
        for m in self._SCHED_CAT_RE.finditer(page):
            marks.append((m.start(), "cat", self._sched_text(m.group(1))))
        for m in self._SCHED_EVENT_RE.finditer(page):
            marks.append((m.start(), "event", None))
        marks.sort(key=lambda x: x[0])

        out, day, cat = {}, None, None
        for i, (pos, kind, value) in enumerate(marks):
            if kind == "day":
                day, cat = value, None
                out.setdefault(day, {})
            elif kind == "cat":
                cat = value
            elif kind == "event" and day and cat:
                end = marks[i + 1][0] if i + 1 < len(marks) else len(page)
                block = page[pos:end]
                when = self._SCHED_TIME_RE.search(block)
                title = self._SCHED_TITLE_RE.search(block)
                if not (when and title):
                    continue
                channels = [
                    {"channel_name": self._sched_text(name), "channel_id": cid}
                    for cid, name in self._SCHED_CHAN_RE.findall(block)
                ]
                out[day].setdefault(cat, []).append({
                    "time": when.group(1).strip(),
                    "event": self._sched_text(title.group(1)),
                    "channels": channels,
                })
        out = {d: c for d, c in out.items() if c}
        for cats in out.values():
            for events in cats.values():
                self._mark_day_offsets(events)
        return out

    @staticmethod
    def _mark_day_offsets(events: list) -> None:
        """Set event["day_offset"] (0/1) for a category's chronological list.

        Upstream files US Saturday-night games under "Saturday" with times like
        00:00-03:00 - that is Sunday in the UK. Within a category the list is
        chronological, so a small-hours time that follows an afternoon/evening
        one is the next day. ponytail: heuristic (<06:00 after >=12:00); a stray
        out-of-order row is the only thing it gets wrong.
        """
        seen_pm = False
        for e in events:
            try:
                hour = int(e["time"].split(":")[0])
            except (ValueError, KeyError, IndexError):
                e["day_offset"] = 0
                continue
            if hour >= 12:
                seen_pm = True
            e["day_offset"] = 1 if hour < 6 and seen_pm else 0

    async def schedule(self):
        try:
            response = await self._session.get(self._base_url, headers=self._headers())
            if response.status_code != 200:
                logger.warning(f"Schedule page returned status {response.status_code}")
                return {}
            parsed = self._parse_schedule(response.text)
            if not parsed:
                logger.warning("No schedule entries found in upstream page")
            else:
                total = sum(len(e) for c in parsed.values() for e in c.values())
                logger.info(f"Parsed schedule: {len(parsed)} day(s), {total} events")
            return parsed
        except Exception as e:
            logger.error(f"Error fetching schedule: {str(e)}")
            return {}
