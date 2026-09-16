"""Regression check for player-page stream URL extraction.

Upstream switched from atob('<base64>') to a plain `var STREAM_URL = "..."`
assignment (2026-09). The atob-only scanner then found zero candidates, so every
channel failed over through all six players and 504'd. Both forms must work.
"""
import base64

from freesky.free_sky_hybrid import StepDaddyHybrid


def test_stream_candidates_plain_and_encoded():
    plain = 'var STREAM_URL = "https:\\/\\/premium.hls.st\\/playlist\\/premium589.m3u8";'
    encoded = "atob('%s')" % base64.b64encode(b"https://cdn.example/live/1.m3u8").decode()

    got = list(StepDaddyHybrid._stream_candidates(plain + "\n" + encoded))

    assert "https://premium.hls.st/playlist/premium589.m3u8" in got, got
    assert "https://cdn.example/live/1.m3u8" in got, got
    assert list(StepDaddyHybrid._stream_candidates("<p>no stream here</p>")) == []


if __name__ == "__main__":
    test_stream_candidates_plain_and_encoded()
    print("ok")
