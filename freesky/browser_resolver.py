"""Resolve a player embed to its HLS playlist by running it in a real browser.

Why this exists
---------------
Upstream serves each channel through several independent "players", each on its
own provider. Only one of them (`stream`, via `_econfig`) hands up a payload our
static decoders can read. The rest compute the playlist URL in obfuscated
JavaScript at runtime — `plus`, for instance, is a JW Player embed whose
`slamdunk.js` is a 640KB blob resolving `fileURL` from `cdnDomain` at run time.

Measured 2026-09-24 on channel 588: the `stream` provider returned 404 while
`plus` served a perfectly playable feed. Statically we saw only the 404 and
reported the channel dead. Everything — the watch page, Dispatcharr, curl —
failed for that one reason. Executing the page is the only general way to read
providers like these, and it keeps working when upstream rotates its obfuscation,
which it does every few months.

Cost
----
Measured ~2s per resolve (page load plus the player's own startup), against the
10s resolve budget, so this runs inline rather than in the background. A resolved
URL is then cached by the caller for hours, so the real cost is roughly one
browser context per channel per few hours, not per request.

The User-Agent is load-bearing
------------------------------
The CDN binds each signed playlist token to the User-Agent that minted it (see
StepDaddyHybrid.USER_AGENT). The browser mints the token here and the backend
spends it later, so this module MUST drive the browser with that same UA — a
default Chromium UA produces a token that 403s on our own proxy hop, which looks
exactly like a dead feed. Verified: minting with the production Firefox UA and
replaying with it returns 200, with or without a Referer.
"""

import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

# Off switch: set BROWSER_RESOLVE=0 to fall back to static decoding only, e.g. if
# Chromium is unavailable in a slim image.
ENABLED = os.environ.get("BROWSER_RESOLVE", "1").strip().lower() not in ("0", "false", "no")
# Wall-clock cap for one resolve. A successful capture measured ~2s; 6s leaves room
# for a slow provider while leaving the 10s budget enough for a second player,
# because a provider with no feed costs the FULL timeout before it gives up.
TIMEOUT = float(os.environ.get("BROWSER_RESOLVE_TIMEOUT", "6.0"))
# Concurrent browser contexts. Each costs real memory, and resolves are short and
# cached for hours afterwards, so a small number is enough to absorb a burst.
MAX_CONCURRENT = int(os.environ.get("MAX_BROWSER_RESOLVES", "2"))
# Explicit Chromium path, for images where Playwright's own download is absent and
# a system browser is used instead. Empty means "let Playwright find it".
EXECUTABLE_PATH = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--mute-audio",
    # The player only starts fetching once it believes it may play.
    "--autoplay-policy=no-user-gesture-required",
]


class BrowserResolver:
    """A lazily started, shared browser used to read players we cannot decode.

    One browser process is reused across resolves; each resolve gets its own
    context so cookies and handlers cannot leak between channels. This is
    deliberately NOT the module-global single-page design of
    `vidembed_extractor.py`, where concurrent callers shared one page and
    overwrote each other's network handlers and content.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        # Guards startup only. Without it two concurrent first calls each launch a
        # browser and all but the last leak.
        self._start_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def _ensure_browser(self):
        """Start the shared browser if it is not already running.

        Returns:
            The running browser, or None if Playwright/Chromium is unavailable —
            in which case the caller silently falls back to static decoding.
        """
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._start_lock:
            # Re-check: another caller may have started it while we waited.
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True, args=_LAUNCH_ARGS,
                    **({"executable_path": EXECUTABLE_PATH} if EXECUTABLE_PATH else {}),
                )
                logger.info("Browser resolver started")
            except Exception as e:
                logger.warning(f"Browser resolver unavailable ({type(e).__name__}: {e})")
                self._browser = None
        return self._browser

    async def warm(self) -> bool:
        """Start the browser ahead of first use.

        Called at application startup. Chromium takes seconds to launch, and if
        that happens inside a request it comes out of that channel's resolve
        budget — which made the first request for a browser-resolved channel time
        out after every restart, and then sit in the negative cache for a minute.

        Returns:
            True if the browser is running, False if it is unavailable (in which
            case resolves silently fall back to static decoding).
        """
        if not ENABLED:
            return False
        return await self._ensure_browser() is not None

    async def resolve(self, embed_url: str, referer: str, user_agent: str,
                      timeout: float = None) -> str:
        """Run `embed_url` in a browser and return the playlist URL it fetches.

        Args:
            embed_url: the provider's embed page, as found in the player's iframe.
            referer: the player page that framed it. The embed is loaded inside an
                iframe on a stub page served from this origin, because these embeds
                redirect away when they detect they are the top frame
                (`if (window == window.top) document.location = "/"`).
            user_agent: the UA to mint with — pass StepDaddyHybrid.USER_AGENT, see
                the module docstring.
            timeout: seconds to wait, default TIMEOUT.

        Returns:
            The first playlist URL the page fetched successfully, or None. Only a
            URL the provider itself got a 2xx for is returned: the page often
            probes several, and a 404 here is exactly the dead feed we are
            trying to route around.
        """
        if not ENABLED:
            return None
        browser = await self._ensure_browser()
        if browser is None:
            return None

        budget = TIMEOUT if timeout is None else timeout
        wrapper = referer.rsplit("/", 1)[0] + "/__resolve__"
        found = []
        started = time.time()

        async with self._semaphore:
            context = None
            try:
                context = await browser.new_context(user_agent=user_agent)
                page = await context.new_page()

                def on_response(response):
                    if ".m3u8" in response.url and 200 <= response.status < 300:
                        found.append(response.url)

                page.on("response", on_response)
                # Serve the stub from the player's own origin so the embed sees the
                # referer and ancestor it expects.
                await page.route("**/__resolve__", lambda route: route.fulfill(
                    content_type="text/html",
                    body=('<html><body style="margin:0">'
                          f'<iframe src="{embed_url}" width="960" height="540" '
                          'allow="autoplay; encrypted-media" allowfullscreen></iframe>'
                          '</body></html>'),
                ))
                await page.goto(wrapper, wait_until="domcontentloaded",
                                timeout=budget * 1000)
                # Poll rather than wait_for_event: we want the FIRST successful
                # playlist and to stop the moment we have it.
                while not found and time.time() - started < budget:
                    await page.wait_for_timeout(250)
            except Exception as e:
                logger.debug(f"Browser resolve failed for {embed_url}: "
                             f"{type(e).__name__}: {e}")
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass

        if found:
            logger.info(f"Browser resolved {embed_url} in {time.time() - started:.1f}s")
            return found[0]
        logger.debug(f"Browser found no playlist for {embed_url} "
                     f"in {time.time() - started:.1f}s")
        return None

    async def close(self):
        """Shut the browser down. Safe to call when it never started."""
        for obj, name in ((self._browser, "browser"), (self._playwright, "playwright")):
            if obj is None:
                continue
            try:
                await (obj.close() if name == "browser" else obj.stop())
            except Exception as e:
                logger.debug(f"Error closing {name}: {type(e).__name__}: {e}")
        self._browser = None
        self._playwright = None


# One shared instance; the browser inside it is started on first use.
browser_resolver = BrowserResolver()
