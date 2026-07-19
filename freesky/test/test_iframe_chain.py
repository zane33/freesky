"""Checks for the iframe-chain stream resolver (freesky.free_sky_hybrid)."""
import base64
import re
from urllib.parse import urljoin

# Mirrors of the patterns in StepDaddyHybrid — kept here so the check runs without
# importing reflex/rxconfig, which need a full app environment.
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
ATOB_RE = re.compile(r"atob\(\s*['\"]([A-Za-z0-9+/=]+)['\"]\s*\)")

WATCH_PAGE = """<html><body>
<iframe src="https://dlhd.st/stream/stream-857.php" width="100%"></iframe>
<script>document.write('<iframe src="' + url + '"></iframe>');</script>
</body></html>"""

PLAYER_PAGE = """<html><body><script>
var junk = atob('bm90LWEtdXJs');
var player = new Clappr.Player({source:window.atob('%s'), mute:false});
</script></body></html>""" % base64.b64encode(
    b"https://xameleon.example.top/two/secure/abc/1784441456/premium857/index.m3u8"
).decode()

MASTER_PLAYLIST = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=8810000,RESOLUTION=1920x1080
tracks-v1a1/mono.m3u8
"""


def _next_iframe(html):
    return next((s for s in IFRAME_RE.findall(html) if "://" in s or s.startswith("/")), None)


def _decoded_stream_url(html):
    for encoded in ATOB_RE.findall(html):
        try:
            candidate = base64.b64decode(encoded).decode()
        except Exception:
            continue
        if candidate.startswith("http") and ".m3u8" in candidate:
            return candidate
    return None


def test_iframe_hop_skips_templated_src():
    assert _next_iframe(WATCH_PAGE) == "https://dlhd.st/stream/stream-857.php"


def test_atob_extraction_ignores_non_stream_payloads():
    url = _decoded_stream_url(PLAYER_PAGE)
    assert url is not None and url.endswith("/premium857/index.m3u8")


def test_watch_page_alone_yields_no_stream_url():
    assert _decoded_stream_url(WATCH_PAGE) is None


def test_relative_variant_is_absolutized():
    m3u8_url = "https://xameleon.example.top/two/secure/abc/1784441456/premium857/index.m3u8"
    absolute = "\n".join(
        urljoin(m3u8_url, line) if line and not line.startswith("#") else line
        for line in MASTER_PLAYLIST.split("\n")
    )
    # Without this the player resolves the variant against our own /api/stream/ path.
    assert "https://xameleon.example.top/two/secure/abc/1784441456/premium857/tracks-v1a1/mono.m3u8" in absolute
    assert absolute.startswith("#EXTM3U")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
