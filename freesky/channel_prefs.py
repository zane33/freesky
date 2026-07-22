"""Which channels are enabled, persisted server-side.

The playlist is served to external players that carry no session, so the
preference has to live on the server, not in a Reflex session. This is a
single-user self-hosted app, so one shared file is the whole model.

ponytail: stores only the DISABLED ids. Channels that appear upstream later
default to enabled, which is what you want from a list that grows on its own.
"""
import json
import os
import threading
from typing import Iterable, Set

PREFS_FILE = os.environ.get(
    "CHANNEL_PREFS_FILE",
    os.path.join(os.path.dirname(__file__), "channel_prefs.json"),
)

_write_lock = threading.Lock()


def disabled_ids() -> Set[str]:
    """Ids the user has switched off. Missing/corrupt file means nothing is off."""
    try:
        with open(PREFS_FILE, "r") as f:
            return {str(i) for i in json.load(f)}
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def set_disabled(ids: Iterable[str]) -> Set[str]:
    """Replace the disabled set. Written atomically so a crash mid-write can't
    truncate the file into an empty list and silently re-enable everything."""
    disabled = {str(i) for i in ids}
    with _write_lock:
        os.makedirs(os.path.dirname(PREFS_FILE) or ".", exist_ok=True)
        tmp = f"{PREFS_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(disabled), f)
        os.replace(tmp, PREFS_FILE)
    return disabled


def is_enabled(channel_id: str) -> bool:
    return str(channel_id) not in disabled_ids()


# --- per-channel source (upstream "player") preference -----------------------
# Stored beside the disabled list. Empty/missing means "auto": try every player
# in the default order and take the first that yields a working playlist.

SOURCES_FILE = os.environ.get(
    "CHANNEL_SOURCES_FILE",
    os.path.join(os.path.dirname(PREFS_FILE) or ".", "channel_sources.json"),
)


def sources() -> dict:
    """{channel_id: player} for channels pinned to a specific upstream source."""
    try:
        with open(SOURCES_FILE, "r") as f:
            data = json.load(f)
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, TypeError, AttributeError):
        return {}


def source_for(channel_id: str) -> str:
    """The pinned player for this channel, or "" for automatic failover."""
    return sources().get(str(channel_id), "")


def set_source(channel_id: str, player: str) -> None:
    """Pin a channel to one upstream player. Empty string restores automatic."""
    data = sources()
    if player:
        data[str(channel_id)] = player
    else:
        data.pop(str(channel_id), None)
    with _write_lock:
        os.makedirs(os.path.dirname(SOURCES_FILE) or ".", exist_ok=True)
        tmp = f"{SOURCES_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SOURCES_FILE)


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        PREFS_FILE = os.path.join(d, "prefs.json")
        assert disabled_ids() == set(), "missing file must mean nothing disabled"
        assert set_disabled(["1", 2]) == {"1", "2"}, "ints must coerce to str"
        assert disabled_ids() == {"1", "2"}
        assert not is_enabled(1) and is_enabled("3")
        assert set_disabled([]) == set() and disabled_ids() == set()
        with open(PREFS_FILE, "w") as f:
            f.write("{ not json")
        assert disabled_ids() == set(), "corrupt file must fail open, not crash"

        SOURCES_FILE = os.path.join(d, "sources.json")
        assert sources() == {} and source_for("1") == ""
        set_source("1", "cast")
        assert source_for("1") == "cast" and source_for("2") == ""
        set_source("1", "")  # back to automatic
        assert source_for("1") == "" and sources() == {}
        print("channel_prefs ok")
