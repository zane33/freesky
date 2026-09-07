"""Tests for virtual channels — a web page restreamed as live HLS.

Covers the parts that can be exercised without a display server: the record
store, the ffmpeg command that is built from a record, the HTTP surface, and the
authorisation rules on the remote-control endpoints.

The capture pipeline itself (Xvfb + Chromium + PulseAudio + ffmpeg) needs a real
container and is not simulated here — stubbing it would only test the stub. What
IS tested is everything that decides whether that pipeline is invoked correctly
and whether its output is served safely.
"""
import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from freesky import virtual_channels, virtual_session


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch):
    """Point the store at a temp file so tests never touch the real data volume."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setattr(virtual_channels, "CHANNELS_FILE", os.path.join(d, "vc.json"))
        yield d


@pytest.fixture
def client(monkeypatch):
    """A TestClient with authentication disabled.

    backend.require_stream_token and the control endpoints both treat "no users
    configured" as an unconfigured install and let requests through, so an empty
    user list is the cleanest way to isolate routing from auth. The auth rules
    themselves are tested separately, with users present.
    """
    from freesky import backend, users

    monkeypatch.setattr(users, "list_users", lambda: [])
    return TestClient(backend.fastapi_app)


# --- store ------------------------------------------------------------------


def test_record_roundtrip_and_defaults():
    saved = virtual_channels.upsert_channel({"name": "News", "url": "https://e.com/live"})
    assert saved["name"] == "news", "the name is the id, so it is normalised"
    assert saved["resolution"] == virtual_channels.DEFAULT_RESOLUTION
    assert virtual_channels.geometry(saved) == (1280, 720)
    assert virtual_channels.get_channel("virt-news") == saved, "lookup by channel id works"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "chrome://gpu", "data:text/html,x", ""])
def test_only_http_urls_accepted(url):
    """The scheme check is what stops a virtual channel becoming a file viewer."""
    with pytest.raises(virtual_channels.VirtualChannelError):
        virtual_channels.upsert_channel({"name": "x", "url": url})


def test_selectors_reject_quotes():
    """Selectors are interpolated into injected CSS/JS, so quotes must not pass."""
    with pytest.raises(virtual_channels.VirtualChannelError):
        virtual_channels.upsert_channel(
            {"name": "x", "url": "https://e.com", "hide_selectors": ['a"]); alert(1)//']}
        )


def test_corrupt_store_fails_open(isolated_store):
    virtual_channels.upsert_channel({"name": "a", "url": "https://e.com"})
    with open(virtual_channels.CHANNELS_FILE, "w") as f:
        f.write("{ not json")
    assert virtual_channels.list_channels() == [], "a bad file must not take the page down"


def test_rename_does_not_leave_a_duplicate():
    virtual_channels.upsert_channel({"name": "old", "url": "https://e.com"})
    virtual_channels.rename_channel("old", "new")
    names = [c["name"] for c in virtual_channels.list_channels()]
    assert names == ["new"], "a rename must not leave the old id in the playlist"


# --- ffmpeg command ---------------------------------------------------------


def test_gop_matches_segment_length():
    """If the GOP and the segment length disagree, segments stop starting on a
    keyframe and EXT-X-INDEPENDENT-SEGMENTS becomes a lie that stalls players."""
    record = virtual_channels.validate_channel(
        {"name": "d", "url": "https://e.com", "framerate": 25}
    )
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert argv[argv.index("-g") + 1] == str(25 * virtual_session.SEGMENT_SECONDS)
    assert argv[argv.index("-keyint_min") + 1] == argv[argv.index("-g") + 1]
    assert argv[argv.index("-sc_threshold") + 1] == "0"


def test_no_fabricated_llhls_options():
    """ffmpeg's hls muxer has no LL-HLS support; -lhls and -hls_part_size are
    dash-muxer/invented options that several guides wrongly prescribe. Passing
    them makes ffmpeg exit with 'Option not found' and the channel never starts."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    joined = " ".join(virtual_session.VirtualSession(record, 99)._ffmpeg_argv())
    assert "-hls_part_size" not in joined
    assert "-lhls" not in joined


def test_no_playlist_type_so_segments_are_deleted():
    """hls_playlist_type=event forbids removing segments, which silently defeats
    delete_segments and grows the output directory without bound."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    joined = " ".join(virtual_session.VirtualSession(record, 99)._ffmpeg_argv())
    assert "-hls_playlist_type" not in joined
    assert "delete_segments" in joined


def test_silent_channel_has_no_audio_input():
    record = virtual_channels.validate_channel(
        {"name": "d", "url": "https://e.com", "audio": False}
    )
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert "-an" in argv
    assert "pulse" not in argv and "aresample" not in " ".join(argv)


def test_both_inputs_get_a_deep_thread_queue():
    """With the tiny default queue, a momentary x11grab stall drops audio packets
    and the stream desyncs permanently."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert argv.count("-thread_queue_size") == 2


def test_exactly_one_cfr_flag():
    """-fps_mode replaced -vsync in ffmpeg 5.1; sending both, or the wrong one for
    the installed version, is an immediate 'Unrecognized option' exit."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert ("-fps_mode" in argv) != ("-vsync" in argv)


# --- playlist rewriting -----------------------------------------------------


REAL_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:6\n"
    "#EXT-X-TARGETDURATION:2\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXT-X-INDEPENDENT-SEGMENTS\n"
    "#EXTINF:2.000000,\n"
    "#EXT-X-PROGRAM-DATE-TIME:2026-09-07T18:39:11.084+1200\n"
    "seg_000000.ts\n"
    "#EXTINF:2.000000,\n"
    "seg_000001.ts\n"
)


def test_rewrite_playlist_targets_the_proxy_and_carries_the_token():
    """Players fetch the playlist from /api/stream/, so a bare `seg_000000.ts`
    would resolve to /api/stream/seg_000000.ts. They also send no cookie, so the
    token has to be in the URL."""
    out = virtual_session.rewrite_playlist(REAL_PLAYLIST, "/api/virtual/demo", "tok")
    assert "/api/virtual/demo/seg_000000.ts?token=tok" in out
    assert "/api/virtual/demo/seg_000001.ts?token=tok" in out
    assert "#EXT-X-TARGETDURATION:2" in out, "tags pass through untouched"
    assert "/api/virtual/demo/#" not in out, "comment lines must not be rewritten"


def test_rewrite_playlist_without_token():
    out = virtual_session.rewrite_playlist(REAL_PLAYLIST, "/api/virtual/demo", "")
    assert "/api/virtual/demo/seg_000000.ts\n" in out
    assert "token=" not in out


@pytest.mark.parametrize(
    "name", ["../../etc/passwd", "index.m3u8", "seg_1.ts", "", "seg_000001.ts/../x", "a.ts"]
)
def test_segment_names_are_allowlisted(name):
    """The segment name is joined onto a directory, so this is the traversal guard."""
    assert not virtual_session.segment_is_safe(name)


def test_real_segment_name_is_accepted():
    assert virtual_session.segment_is_safe("seg_000123.ts")


# --- channel surface --------------------------------------------------------


def test_virtual_channels_appear_in_the_playlist(client):
    from freesky import backend

    virtual_channels.upsert_channel(
        {"name": "lobby", "title": "Lobby Cam", "url": "https://e.com/cam"}
    )
    body = client.get("/playlist.m3u8").text
    assert "Lobby Cam" in body
    assert "/api/stream/virt-lobby.m3u8" in body

    ids = [c.id for c in backend.get_channels()]
    assert "virt-lobby" in ids, "and in the UI channel list"


def test_disabled_virtual_channel_is_hidden(client):
    virtual_channels.upsert_channel(
        {"name": "off", "url": "https://e.com", "enabled": False}
    )
    assert "virt-off" not in client.get("/playlist.m3u8").text


def test_unknown_virtual_channel_is_not_a_500(client):
    """A stale playlist entry must give a clean error, not a stack trace."""
    res = client.get("/api/stream/virt-nope.m3u8")
    assert res.status_code == 503
    assert res.json()["error"] == "virtual_session_failed"


def test_segment_request_for_unknown_session_404s(client):
    assert client.get("/api/virtual/nope/seg_000001.ts").status_code == 404


def test_segment_traversal_is_rejected_before_lookup(client):
    """Checked before the session lookup, so it holds even for a live session."""
    res = client.get("/api/virtual/any/..%2F..%2Fetc%2Fpasswd")
    assert res.status_code == 404


def test_sessions_status_reports_preflight(client):
    body = client.get("/api/virtual-sessions/status").json()
    assert "missing_binaries" in body and isinstance(body["sessions"], list)


# --- remote control authorisation ------------------------------------------


@pytest.fixture
def admin_and_standard(monkeypatch):
    """A populated user table, so the "unconfigured install" bypass is off."""
    from freesky import users

    table = {
        "root": {"role": "admin", "token": "admin-token"},
        "viewer": {"role": "standard", "token": "viewer-token"},
    }
    monkeypatch.setattr(users, "list_users", lambda: [{"username": k} for k in table])
    monkeypatch.setattr(
        users, "user_by_token",
        lambda tok: next(
            ({"username": k, **v} for k, v in table.items() if v["token"] == tok), None
        ),
    )
    from freesky import backend

    return TestClient(backend.fastapi_app)


def test_control_requires_a_token(admin_and_standard):
    """Remote mouse and keyboard on the server's browser is admin-only."""
    assert admin_and_standard.post("/api/virtual-control/x/start").status_code == 401


def test_control_rejects_a_standard_user(admin_and_standard):
    """A valid stream token is NOT enough — it authorises watching, not driving."""
    res = admin_and_standard.post("/api/virtual-control/x/start?token=viewer-token")
    assert res.status_code == 401


def test_control_panel_needs_an_existing_channel(admin_and_standard):
    res = admin_and_standard.get("/api/virtual-control/nope/panel?token=admin-token")
    assert res.status_code == 404


def test_control_panel_renders_for_an_admin(admin_and_standard):
    virtual_channels.upsert_channel({"name": "cam", "url": "https://e.com/cam"})
    res = admin_and_standard.get("/api/virtual-control/cam/panel?token=admin-token")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    # The config is injected as JSON, not string-interpolated, so a token
    # containing a quote cannot break out of the script literal.
    assert '"name": "cam"' in res.text or '"name":"cam"' in res.text
    assert "__CONFIG__" not in res.text, "the placeholder must be substituted"


def test_control_on_a_channel_that_is_not_running(admin_and_standard):
    """Input to a stopped session is a 409 with an explanation, not a crash."""
    virtual_channels.upsert_channel({"name": "cam", "url": "https://e.com/cam"})
    res = admin_and_standard.post(
        "/api/virtual-control/cam/input?token=admin-token", json={"type": "click"}
    )
    assert res.status_code == 409
    assert res.json()["error"] == "not_running"


def test_panel_config_is_json_encoded(admin_and_standard):
    """A token is attacker-influenced only by the admin, but the panel embeds it
    in a <script>; json.dumps is what keeps that from being an injection."""
    virtual_channels.upsert_channel({"name": "cam", "url": "https://e.com/cam"})
    res = admin_and_standard.get(
        '/api/virtual-control/cam/panel?token=admin-token'
    )
    # Whatever the config contains, it must parse as JSON on its own line.
    line = next(ln for ln in res.text.splitlines() if ln.startswith("const CFG = "))
    payload = line[len("const CFG = "):].rstrip(";")
    assert json.loads(payload)["name"] == "cam"
