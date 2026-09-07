"""Virtual channel definitions — web pages restreamed as live HLS.

A "virtual channel" is a URL plus capture settings. At play time FreeSky opens
that URL in a headless Chromium on a private X display, records the display and
the browser's audio sink with ffmpeg, and writes a rolling HLS playlist. To a
player (VLC, Dispatcharr, Jellyfin, the built-in web player) the result is
indistinguishable from any other channel: it appears in /playlist.m3u8 and is
fetched from /api/stream/{id}.m3u8.

Same file-backed pattern as channel_prefs.py / app_settings.py: one JSON file
under the ./data volume, a threading.Lock around writes, and os.replace so a
crash mid-write cannot truncate the file into a half-record. Path comes from
VIRTUAL_CHANNELS_FILE.

The `name` is a slug and doubles as the channel id suffix, because the id is
embedded in a URL path, in an M3U tvg-id, and in a filesystem directory name for
the HLS segments. Validation is therefore stricter than a display name needs:
see _NAME_RE.

SECURITY — these records drive a real browser
---------------------------------------------
`url` is fetched by a browser running inside the container, on the container's
network. An admin who can add a virtual channel can therefore make the server
issue requests to hosts the client cannot reach directly — a classic SSRF shape.
Two things bound it:

  * only http/https URLs are accepted, so file://, chrome://, data: and friends
    cannot be used to read the container's filesystem or driver internals;
  * the settings page that edits these records is admin-gated (require_admin).

Blocking RFC1918 destinations was considered and deliberately NOT done: this is
a self-hosted LAN app whose whole point may be restreaming something on the same
network. The trust boundary is "admin", not "URL".
"""
import json
import logging
import os
import re
import threading
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CHANNELS_FILE = os.environ.get(
    "VIRTUAL_CHANNELS_FILE",
    os.path.join(os.path.dirname(__file__), "virtual_channels.json"),
)

_write_lock = threading.Lock()

# Every virtual channel id starts with this, so a virtual channel can never
# collide with an upstream DLHD numeric id and backend.py can route on the
# prefix alone without loading the store.
ID_PREFIX = "virt-"

# Slug: lowercase, used verbatim as the id suffix, so it has to survive being a
# URL path segment, an M3U tvg-id, and a directory name for HLS segments.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# Capture presets. Height drives the encoder ladder; the width is derived 16:9
# so an admin cannot pick a geometry Chromium and x264 disagree about (x264
# requires even dimensions, and odd widths produced a silent encoder abort).
RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}
DEFAULT_RESOLUTION = "720p"

FRAMERATES = (15, 24, 25, 30)
DEFAULT_FRAMERATE = 30

# x264 presets worth offering. Anything slower than "veryfast" cannot keep up
# with 1080p30 realtime capture on the CPUs this app typically runs on, so the
# list stops there rather than letting an admin pick a preset that guarantees a
# dropped-frame stream.
PRESETS = ("ultrafast", "superfast", "veryfast")
DEFAULT_PRESET = "veryfast"


class VirtualChannelError(ValueError):
    """A virtual channel record was rejected.

    Carries a message written for the person editing the form, not for a log
    line — the settings UI renders `str(exc)` directly.
    """


def channel_id(name: str) -> str:
    """The Channel.id a virtual channel appears under."""
    return f"{ID_PREFIX}{name}"


def name_from_id(cid: str) -> str:
    """Inverse of channel_id(). Empty string when `cid` is not a virtual id."""
    cid = str(cid or "")
    return cid[len(ID_PREFIX):] if cid.startswith(ID_PREFIX) else ""


def is_virtual_id(cid: str) -> bool:
    return str(cid or "").startswith(ID_PREFIX)


def _validate_url(value: str) -> str:
    """Accept only an absolute http(s) URL.

    A browser will happily open file:///etc/passwd or chrome://gpu and we would
    then restream it. Restricting the scheme here is the single check that keeps
    a virtual channel from becoming a filesystem viewer.
    """
    value = str(value or "").strip()
    if not value:
        raise VirtualChannelError("Page URL is required.")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise VirtualChannelError("Page URL must start with http:// or https://.")
    if not parsed.netloc:
        raise VirtualChannelError("Page URL is missing a host.")
    return value


def _validate_selectors(value, label: str) -> List[str]:
    """CSS selectors, one per entry. Only sanity-checked, not parsed.

    A malformed selector is a no-op in the page (querySelectorAll throws and we
    catch it there), so rejecting on syntax would be stricter than the runtime
    actually needs. What we do reject is a selector containing a quote or
    backslash, because these are interpolated into an injected script and that
    is where an escaping bug would turn into arbitrary JS.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [v for v in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise VirtualChannelError(f"{label} must be a list of CSS selectors.")

    cleaned = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        if len(item) > 200:
            raise VirtualChannelError(f"{label}: selector is too long (max 200 characters).")
        if any(c in item for c in ('"', "'", "\\", "\n")):
            raise VirtualChannelError(
                f"{label}: selectors may not contain quotes or backslashes."
            )
        cleaned.append(item)
    return cleaned


def _validate_int(value, label: str, low: int, high: int, default: int) -> int:
    """Bounded integer with a default for blank input."""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise VirtualChannelError(f"{label} must be a whole number.")
    if not low <= number <= high:
        raise VirtualChannelError(f"{label} must be between {low} and {high}.")
    return number


def validate_channel(record: dict) -> dict:
    """Normalise and validate one record, or raise VirtualChannelError.

    Returns a new dict with every field present and typed, so callers never have
    to defend against a partially-populated record read back off disk.
    """
    if not isinstance(record, dict):
        raise VirtualChannelError("Virtual channel must be an object.")

    name = str(record.get("name", "")).strip().lower()
    if not _NAME_RE.match(name):
        raise VirtualChannelError(
            "Name must be 1-48 characters, lowercase letters, digits, '-' or '_', "
            "and start with a letter or digit."
        )

    title = str(record.get("title", "")).strip() or name
    if len(title) > 120:
        raise VirtualChannelError("Display name is too long (max 120 characters).")

    resolution = str(record.get("resolution", DEFAULT_RESOLUTION)).strip() or DEFAULT_RESOLUTION
    if resolution not in RESOLUTIONS:
        raise VirtualChannelError(
            f"Resolution must be one of: {', '.join(RESOLUTIONS)}."
        )

    framerate = _validate_int(
        record.get("framerate"), "Frame rate", min(FRAMERATES), max(FRAMERATES), DEFAULT_FRAMERATE
    )
    if framerate not in FRAMERATES:
        raise VirtualChannelError(
            f"Frame rate must be one of: {', '.join(str(f) for f in FRAMERATES)}."
        )

    preset = str(record.get("preset", DEFAULT_PRESET)).strip() or DEFAULT_PRESET
    if preset not in PRESETS:
        raise VirtualChannelError(f"Encoder preset must be one of: {', '.join(PRESETS)}.")

    tags = record.get("tags") or ["Virtual"]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [str(t).strip() for t in tags if str(t).strip()]
    if not tags:
        tags = ["Virtual"]

    logo = str(record.get("logo", "")).strip()
    if logo and not (logo.startswith("http://") or logo.startswith("https://") or logo.startswith("/")):
        raise VirtualChannelError("Logo must be an http(s) URL or a path starting with '/'.")

    return {
        "name": name,
        "title": title,
        "url": _validate_url(record.get("url")),
        "resolution": resolution,
        "framerate": framerate,
        "preset": preset,
        # 0 disables the audio input entirely. Some pages have no sound and
        # capturing a silent PulseAudio monitor still costs an encoder and can
        # desync a long-running stream, so it is worth being able to turn off.
        "video_bitrate": _validate_int(
            record.get("video_bitrate"), "Video bitrate (kbps)", 200, 20000, 2500
        ),
        "audio": bool(record.get("audio", True)),
        "audio_bitrate": _validate_int(
            record.get("audio_bitrate"), "Audio bitrate (kbps)", 32, 320, 128
        ),
        # Seconds to let the page settle (fonts, players, consent dialogs) before
        # the encoder starts, so viewers don't join on a half-painted page.
        "warmup": _validate_int(record.get("warmup"), "Warm-up seconds", 0, 120, 6),
        # Seconds with no playlist request before the session is torn down. A
        # browser and an encoder per channel is expensive; nothing should stay up
        # because someone opened a tab yesterday.
        "idle_timeout": _validate_int(
            record.get("idle_timeout"), "Idle timeout (seconds)", 30, 3600, 120
        ),
        "click_selectors": _validate_selectors(
            record.get("click_selectors"), "Click selectors"
        ),
        "hide_selectors": _validate_selectors(
            record.get("hide_selectors"), "Hide selectors"
        ),
        "tags": tags,
        "logo": logo,
        "enabled": bool(record.get("enabled", True)),
    }


def _load() -> Dict[str, dict]:
    """All records keyed by name. A corrupt file reads as empty rather than
    raising, so one bad edit cannot take the whole channel list down."""
    try:
        with open(CHANNELS_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (ValueError, TypeError) as exc:
        logger.error("virtual_channels: %s is not valid JSON (%s); treating as empty", CHANNELS_FILE, exc)
        return {}
    if not isinstance(data, dict):
        logger.error("virtual_channels: %s is not an object; treating as empty", CHANNELS_FILE)
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save(data: Dict[str, dict]) -> None:
    with _write_lock:
        os.makedirs(os.path.dirname(CHANNELS_FILE) or ".", exist_ok=True)
        tmp = f"{CHANNELS_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, CHANNELS_FILE)


def list_channels() -> List[dict]:
    """Every stored record, name-sorted. Invalid rows are skipped, not raised on."""
    out = []
    for name, record in sorted(_load().items()):
        record = dict(record)
        record.setdefault("name", name)
        try:
            out.append(validate_channel(record))
        except VirtualChannelError as exc:
            logger.warning("virtual_channels: skipping invalid record %r (%s)", name, exc)
    return out


def get_channel(name: str) -> Optional[dict]:
    """One record by name, or None. Also accepts a full `virt-` channel id."""
    name = name_from_id(name) or str(name or "").strip().lower()
    record = _load().get(name)
    if record is None:
        return None
    record = dict(record)
    record.setdefault("name", name)
    try:
        return validate_channel(record)
    except VirtualChannelError as exc:
        logger.warning("virtual_channels: stored record %r is invalid (%s)", name, exc)
        return None


def upsert_channel(record: dict) -> dict:
    """Validate and store. Returns the normalised record."""
    cleaned = validate_channel(record)
    data = _load()
    data[cleaned["name"]] = cleaned
    _save(data)
    return cleaned


def delete_channel(name: str) -> bool:
    """Remove by name. True when something was actually removed."""
    name = name_from_id(name) or str(name or "").strip().lower()
    data = _load()
    if name not in data:
        return False
    del data[name]
    _save(data)
    return True


def rename_channel(old: str, new: str) -> dict:
    """Rename in place.

    A separate operation because the name is the id: upsert() with a new name
    would leave the old record behind as a duplicate channel.
    """
    old = str(old or "").strip().lower()
    record = get_channel(old)
    if record is None:
        raise VirtualChannelError(f"No virtual channel named {old!r}.")
    record["name"] = str(new or "").strip().lower()
    cleaned = validate_channel(record)
    if cleaned["name"] == old:
        return cleaned
    data = _load()
    data.pop(old, None)
    data[cleaned["name"]] = cleaned
    _save(data)
    return cleaned


def geometry(record: dict) -> tuple:
    """(width, height) for a record's resolution preset."""
    return RESOLUTIONS.get(record.get("resolution", DEFAULT_RESOLUTION), RESOLUTIONS[DEFAULT_RESOLUTION])


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        CHANNELS_FILE = os.path.join(d, "vc.json")

        assert list_channels() == [], "missing file must read as empty"
        assert get_channel("nope") is None

        rec = upsert_channel({"name": "News", "url": "https://example.com/live"})
        assert rec["name"] == "news", "name must be lowercased"
        assert rec["title"] == "news", "title defaults to the name"
        assert rec["resolution"] == DEFAULT_RESOLUTION and rec["framerate"] == DEFAULT_FRAMERATE
        assert rec["tags"] == ["Virtual"] and rec["enabled"] is True
        assert geometry(rec) == (1280, 720)

        assert channel_id("news") == "virt-news"
        assert name_from_id("virt-news") == "news"
        assert name_from_id("588") == "", "upstream ids are not virtual"
        assert is_virtual_id("virt-news") and not is_virtual_id("588")
        assert get_channel("virt-news")["url"] == "https://example.com/live", "id lookup works"

        # Scheme restriction is the SSRF/file-read guard; keep it tested.
        for bad in ("file:///etc/passwd", "chrome://gpu", "data:text/html,x", "javascript:1", ""):
            try:
                upsert_channel({"name": "x", "url": bad})
                raise AssertionError(f"should have rejected {bad!r}")
            except VirtualChannelError:
                pass

        for bad_name in ("", "-leading", "UPPER!", "a" * 49, "has space"):
            try:
                upsert_channel({"name": bad_name, "url": "https://e.com"})
                raise AssertionError(f"should have rejected name {bad_name!r}")
            except VirtualChannelError:
                pass

        # Selector injection guard: these are interpolated into injected JS.
        try:
            injected = 'a"]); alert(1);//'
            upsert_channel({"name": "y", "url": "https://e.com", "hide_selectors": [injected]})
            raise AssertionError("should have rejected a quoted selector")
        except VirtualChannelError:
            pass
        assert upsert_channel(
            {"name": "y", "url": "https://e.com", "hide_selectors": ".cookie-banner\n #ad "}
        )["hide_selectors"] == [".cookie-banner", "#ad"], "newline-separated selectors"

        for field, bad in (("framerate", 7), ("resolution", "4k"), ("preset", "slow"),
                           ("video_bitrate", 1), ("idle_timeout", 5), ("audio_bitrate", 9000)):
            try:
                upsert_channel({"name": "z", "url": "https://e.com", field: bad})
                raise AssertionError(f"should have rejected {field}={bad!r}")
            except VirtualChannelError:
                pass

        assert len(list_channels()) == 2, "news + y"
        renamed = rename_channel("news", "world-news")
        assert renamed["name"] == "world-news" and get_channel("news") is None
        assert delete_channel("virt-world-news") is True, "delete accepts an id"
        assert delete_channel("world-news") is False, "second delete is a no-op"

        # A corrupt file must fail open, matching channel_prefs.py.
        with open(CHANNELS_FILE, "w") as f:
            f.write("{ not json")
        assert list_channels() == [] and get_channel("y") is None

        print("virtual_channels ok")
