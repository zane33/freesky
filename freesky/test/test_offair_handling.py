"""Off-air channels must fail fast, be remembered, and be visible to the monitor.

Regression cover for the 2026-09 incident: 18 of 33 channels were off air
upstream, and every request for one cost 22s and returned 504 while
/health reported 18/18 channels healthy.
"""
import time

from freesky.stream_monitor import StreamMonitor


def test_failure_is_recorded():
    """A failed attempt must move the metrics — it previously moved nothing."""
    m = StreamMonitor()
    m.record_stream_attempt("588", False, 22.0)

    assert m.metrics["588"].success_rate == 0.0
    assert m.metrics["588"].consecutive_failures == 1
    assert m.is_stream_healthy("588") is False


def test_consecutive_failures_count_up_and_reset_on_success():
    m = StreamMonitor()
    for _ in range(3):
        m.record_stream_attempt("588", False, 0.0)
    assert m.metrics["588"].consecutive_failures == 3

    m.record_stream_attempt("588", True, 0.4)
    assert m.metrics["588"].consecutive_failures == 0


def test_breaker_fires_for_a_channel_that_never_succeeded():
    """The core bug: last_success defaults to 0, so the backoff compared against
    the epoch and should_skip_channel never returned True for exactly the
    channels it exists to protect."""
    m = StreamMonitor()
    for _ in range(m.max_consecutive_failures):
        m.record_stream_attempt("588", False, 0.0)

    assert m.should_skip_channel("588") is True


def test_breaker_releases_once_the_backoff_window_passes():
    m = StreamMonitor()
    for _ in range(m.max_consecutive_failures):
        m.record_stream_attempt("588", False, 0.0)
    assert m.should_skip_channel("588") is True

    # First window is 60s; pretend the last attempt was longer ago than that.
    m.last_failures["588"] = time.time() - 61
    m._update_metrics("588")
    assert m.should_skip_channel("588") is False


def test_backoff_grows_with_repeated_failures():
    m = StreamMonitor()
    for _ in range(m.max_consecutive_failures + 1):
        m.record_stream_attempt("588", False, 0.0)

    # One failure past the threshold doubles the window to 120s, so a channel
    # last tried 61s ago is still inside it.
    m.last_failures["588"] = time.time() - 61
    m._update_metrics("588")
    assert m.should_skip_channel("588") is True


def test_healthy_channel_is_never_skipped():
    m = StreamMonitor()
    for _ in range(5):
        m.record_stream_attempt("44", True, 0.4)

    assert m.should_skip_channel("44") is False
    assert m.is_stream_healthy("44") is True


def test_unknown_channel_is_not_skipped():
    """No data is not evidence of failure — a new channel must get its chance."""
    assert StreamMonitor().should_skip_channel("999") is False


def test_summary_reports_the_outage():
    """/health claimed 18/18 healthy through an 18-channel outage."""
    m = StreamMonitor()
    for _ in range(3):
        m.record_stream_attempt("588", False, 0.0)
    m.record_stream_attempt("44", True, 0.4)

    summary = m.get_metrics_summary()
    assert summary["total_channels"] == 2
    assert summary["healthy_channels"] == 1
    assert summary["health_rate"] == 0.5


def test_offair_error_is_a_valueerror():
    """Existing `except ValueError` handlers around playlist fetching must keep
    catching it, so introducing the type cannot change old behaviour."""
    try:
        from freesky.free_sky_hybrid import ChannelOffAirError
    except ImportError:  # reflex not installed in this environment
        print("  (skipped: reflex unavailable)")
        return

    assert issubclass(ChannelOffAirError, ValueError)


def test_each_failure_kind_keeps_its_own_response():
    """A cached failure must replay what the live attempt returned. Caching only
    the reason string made a timeout report itself as "not currently
    broadcasting", which read as a total outage during the 2026-09 incident."""
    try:
        from freesky import backend
    except Exception:  # reflex not installed in this environment
        print("  (skipped: reflex unavailable)")
        return

    offair = backend._failure_response("588", backend._FAILURE_OFFAIR, "HTTP 404 from CDN")
    timeout = backend._failure_response("588", backend._FAILURE_TIMEOUT)
    missing = backend._failure_response("588", backend._FAILURE_NOT_FOUND)

    assert offair.status_code == 404
    assert timeout.status_code == 504   # honest: we timed out, we did not learn it is off air
    assert missing.status_code == 404
    # The three must not be mistakable for one another.
    bodies = {r.body for r in (offair, timeout, missing)}
    assert len(bodies) == 3
    assert b"not currently broadcasting" in offair.body
    assert b"not currently broadcasting" not in timeout.body


def test_timeout_is_forgotten_sooner_than_offair():
    """A timeout is a weak verdict — a cold browser or a slow hop — so it must be
    re-checked far sooner than a CDN 404. Caching a single cold-start overrun for a
    full minute made a working channel look permanently dead after a restart."""
    try:
        from freesky import backend
    except Exception:
        print("  (skipped: reflex unavailable)")
        return
    assert backend._failure_ttl(backend._FAILURE_TIMEOUT) < backend._failure_ttl(backend._FAILURE_OFFAIR)


def test_warm_is_a_noop_when_disabled():
    import asyncio, os
    os.environ["BROWSER_RESOLVE"] = "0"
    try:
        br = _load_browser_resolver()
        assert asyncio.run(br.browser_resolver.warm()) is False
    finally:
        os.environ.pop("BROWSER_RESOLVE", None)


def _load_browser_resolver():
    """Import browser_resolver without pulling in reflex via the package."""
    import importlib.util, pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "browser_resolver.py"
    spec = importlib.util.spec_from_file_location("_br", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_browser_resolver_is_a_noop_when_disabled():
    """BROWSER_RESOLVE=0 must not start a browser or raise."""
    import asyncio, os
    os.environ["BROWSER_RESOLVE"] = "0"
    try:
        br = _load_browser_resolver()
        assert br.ENABLED is False
        out = asyncio.run(br.browser_resolver.resolve(
            "https://example.invalid/e/x", referer="https://example.invalid/p",
            user_agent="UA"))
        assert out is None
    finally:
        os.environ.pop("BROWSER_RESOLVE", None)


def test_browser_resolver_degrades_when_browser_unavailable():
    """A missing Chromium must return None, not explode: the caller then falls
    back to static decoding rather than failing the whole resolve."""
    import asyncio, os
    os.environ["BROWSER_EXECUTABLE_PATH"] = "/nonexistent/chrome-binary"
    try:
        br = _load_browser_resolver()
        out = asyncio.run(br.browser_resolver.resolve(
            "https://example.invalid/e/x", referer="https://example.invalid/p",
            user_agent="UA", timeout=2.0))
        assert out is None
    finally:
        os.environ.pop("BROWSER_EXECUTABLE_PATH", None)


def test_browser_escalation_is_capped():
    """The per-resolve escalation cap must be a small positive number — each
    attempt costs the full timeout when a provider has no feed."""
    try:
        from freesky.free_sky_hybrid import StepDaddyHybrid
    except Exception:
        print("  (skipped: reflex unavailable)")
        return
    assert 1 <= StepDaddyHybrid._MAX_BROWSER_ATTEMPTS <= 6


# The exact 42-byte header observed on the `plus` provider's segments, 2026-09-24.
_REAL_WEBP_HEADER = bytes.fromhex(
    "52494646d6666a00574542505650384c0d0000002f00000010071011118888fe070045584946b4666a00"
)


def _unwrapper():
    """Load _media_payload_offset without importing the whole backend (reflex)."""
    import pathlib, re
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("backend.py").read_text()
    block = re.search(r"_TS_SYNC = 0x47.*?\n_CONTENT_M3U8_RE", src, re.S).group(0)
    ns = {}
    exec(block.replace("_CONTENT_M3U8_RE", "pass  #"), ns)
    return ns["_media_payload_offset"]


def test_image_wrapped_segment_is_unwrapped():
    """Segments arrive disguised as WebP images; ffmpeg rejects them as "Invalid
    data found when processing input" while hls.js in a browser plays them, so the
    watch page worked and Dispatcharr did not."""
    off = _unwrapper()
    payload = bytes([0x47]) + bytes(187) + bytes([0x47]) + bytes(187)
    assert off(_REAL_WEBP_HEADER + payload) == 42


def test_plain_transport_stream_is_untouched():
    """An ordinary provider must pass through byte-for-byte."""
    off = _unwrapper()
    assert off(bytes([0x47]) + bytes(187) + bytes([0x47]) + bytes(187)) == 0


def test_non_wrapped_image_is_not_misread_as_a_stream():
    """A real image (no TS inside) must not be truncated into garbage."""
    off = _unwrapper()
    assert off(b"\x89PNG\r\n\x1a\n" + bytes(1000)) == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("off-air handling ok")
