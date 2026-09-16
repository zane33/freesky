"""Regression check for player-page stream URL extraction.

Upstream switched from atob('<base64>') to a plain `var STREAM_URL = "..."`
assignment (2026-09). The atob-only scanner then found zero candidates, so every
channel failed over through all six players and 504'd. Both forms must work.
"""
import base64


def test_stream_candidates_plain_and_encoded():
    # imported here, not at module scope: the source-inspection tests below must
    # still run where reflex/curl_cffi are not installed.
    from freesky.free_sky_hybrid import StepDaddyHybrid

    plain = 'var STREAM_URL = "https:\\/\\/premium.hls.st\\/playlist\\/premium589.m3u8";'
    encoded = "atob('%s')" % base64.b64encode(b"https://cdn.example/live/1.m3u8").decode()

    got = list(StepDaddyHybrid._stream_candidates(plain + "\n" + encoded))

    assert "https://premium.hls.st/playlist/premium589.m3u8" in got, got
    assert "https://cdn.example/live/1.m3u8" in got, got
    assert list(StepDaddyHybrid._stream_candidates("<p>no stream here</p>")) == []


def test_playlist_cache_shorter_than_live_window():
    """A live media playlist holds ~6 segments x 6s. Caching it longer than that
    hands the player the same segments until its buffer drains and playback dies —
    which is exactly what "plays for 30s then crashes" looked like."""
    import re

    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("backend.py").read_text()
    ttl = int(re.search(r"^cache_ttl = (\d+)", src, re.M).group(1))
    assert ttl < 18, f"playlist cache_ttl={ttl}s starves the player on a ~36s window"


if __name__ == "__main__":
    test_stream_candidates_plain_and_encoded()
    test_playlist_cache_shorter_than_live_window()
    print("ok")


def test_segment_proxy_connection_hygiene():
    """Segments must not share a multiplexed connection.

    With http2=True on streaming_client, a player disconnecting mid-segment left
    the h2 stream dangling and poisoned the pooled connection: 3 aborted segments
    took the next fetch on that host from 0.6s to 30.2s, then to instant 502s.
    Cancelling __aenter__ with asyncio.wait_for half-opened connections the same way.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("backend.py").read_text()
    block = src[src.index("streaming_client = httpx.AsyncClient("):]
    block = block[:block.index(")\n")]
    assert "http2=False" in block, "streaming_client must stay on HTTP/1.1"
    assert "asyncio.wait_for(cm.__aenter__" not in src, "cancelling __aenter__ half-opens connections"


def test_caddy_outlives_backend_stream_deadline():
    """Caddy must not give up on /api/stream before the backend answers.

    The backend's resolve budget was raised without touching the Caddyfile, so
    Caddy cut every request off at 20s against a 22s backend deadline and returned
    a bare 504 with no body — no channel would open at all.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    backend = (root / "freesky" / "backend.py").read_text()
    wait_for = float(re.search(r"timeout=(\d+(?:\.\d+)?)\s*#\s*must exceed the resolver", backend).group(1))

    caddy = (root / "Caddyfile").read_text()
    stream_block = caddy[caddy.index("@api_stream"):]
    stream_block = stream_block[:stream_block.index("@api_logo")]
    caddy_timeout = float(re.search(r"response_header_timeout (\d+)s", stream_block).group(1))

    assert caddy_timeout > wait_for, (
        f"Caddy gives /api/stream {caddy_timeout}s but the backend may take {wait_for}s"
    )
