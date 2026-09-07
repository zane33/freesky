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
import re
import tempfile
import time

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


# --- x11grab encoder command -----------------------------------------------
# These pin the libx264 command for the x11grab path. Tab capture (the default)
# does not encode video in ffmpeg at all; its command is tested further down.


def _x11(**fields):
    fields.setdefault("name", "d")
    fields.setdefault("url", "https://e.com")
    fields["capture"] = "x11grab"
    return virtual_channels.validate_channel(fields)


def test_gop_matches_segment_length():
    """If the GOP and the segment length disagree, segments stop starting on a
    keyframe and EXT-X-INDEPENDENT-SEGMENTS becomes a lie that stalls players."""
    record = _x11(framerate=20)
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert argv[argv.index("-g") + 1] == str(20 * virtual_session.SEGMENT_SECONDS)
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
    record = _x11()
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert argv.count("-thread_queue_size") == 2


def test_exactly_one_cfr_flag():
    """-fps_mode replaced -vsync in ffmpeg 5.1; sending both, or the wrong one for
    the installed version, is an immediate 'Unrecognized option' exit."""
    record = _x11()
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


# --- Channel model after the DRM removal ------------------------------------


def test_legacy_drm_record_degrades_to_hls():
    """Saved channel data from before DRM was removed must not crash the app.

    fallback_channels.json and any cached record may still carry `provider` and
    stream_type "drm". from_dict tolerates unknown keys, and an unrecognised
    stream_type falls back to plain HLS rather than propagating a type nothing
    handles any more.
    """
    from freesky.free_sky import Channel

    channel = Channel.from_dict(
        {"id": "1", "name": "X", "tags": [], "logo": "/l.png",
         "provider": "acme", "stream_type": "drm"}
    )
    assert channel.stream_type == "hls"
    assert not hasattr(channel, "provider")


def test_virtual_stream_type_survives_from_dict():
    from freesky.free_sky import Channel

    channel = Channel.from_dict(
        {"id": "virt-a", "name": "V", "tags": [], "logo": "", "stream_type": "virtual"}
    )
    assert channel.stream_type == "virtual"


# --- Chromium dialog suppression --------------------------------------------
# Browser UI is drawn into the browser's X window, so an unsuppressed dialog is
# captured and broadcast to viewers. These assertions encode findings verified
# against the shipped Chrome for Testing binary, because the widely-copied
# advice for this is largely dead flags.


@pytest.fixture
def seeded_profile(tmp_path):
    record = virtual_channels.validate_channel({"name": "s", "url": "https://e.com"})
    session = virtual_session.VirtualSession(record, 99)
    session.profile_dir = str(tmp_path / "profile")
    session._seed_profile()
    return session


def test_save_password_bubble_is_disabled_by_pref(seeded_profile):
    """`credentials_enable_service` is the surviving control for the bubble.

    There is no command-line switch for it any more, so if this pref is not
    written the bubble appears on the live stream.
    """
    prefs = json.load(open(os.path.join(seeded_profile.profile_dir, "Default", "Preferences")))
    assert prefs["credentials_enable_service"] is False


def test_first_run_sentinel_exists(seeded_profile):
    """Without it Chromium treats the profile as new and overwrites the
    Preferences file we just wrote, silently undoing every suppression."""
    assert os.path.isfile(os.path.join(seeded_profile.profile_dir, "First Run"))


def test_exit_type_is_normal(seeded_profile):
    """Sessions are stopped abruptly. Chromium writes "Crashed" at startup and
    only clears it on a clean shutdown, so without resetting this each launch
    the next session shows a "Restore pages?" bubble — on the stream."""
    prefs = json.load(open(os.path.join(seeded_profile.profile_dir, "Default", "Preferences")))
    assert prefs["profile"]["exit_type"] == "Normal"


def test_no_dead_password_manager_pref(seeded_profile):
    """`profile.password_manager_enabled` no longer exists in Chromium. Writing
    it would look like the bubble was handled while doing nothing."""
    prefs = json.load(open(os.path.join(seeded_profile.profile_dir, "Default", "Preferences")))
    assert "password_manager_enabled" not in prefs["profile"]


def test_no_dead_chromium_switches():
    """These were all removed from Chromium; shipping them is cargo cult."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    args = virtual_session.VirtualSession(record, 99)._browser_args()
    for dead in ("--disable-save-password-bubble", "--disable-session-crashed-bubble",
                 "--disable-translate", "--enable-zero-copy",
                 "--enable-gpu-rasterization", "--ignore-gpu-blocklist"):
        assert dead not in args, f"{dead} is a no-op or GPU-only in modern Chromium"


def test_does_not_pass_its_own_disable_features():
    """Playwright already sends one comma-joined --disable-features list. A
    second occurrence of the switch is a merge hazard, so we add none."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    args = virtual_session.VirtualSession(record, 99)._browser_args()
    assert not any(a.startswith("--disable-features=") for a in args)


def test_xdotool_key_translation():
    """Browser key names are not X keysyms; Enter/Backspace/arrows must map."""
    assert virtual_session._xdotool_key("Enter") == "Return"
    assert virtual_session._xdotool_key("Backspace") == "BackSpace"
    assert virtual_session._xdotool_key("ArrowLeft") == "Left"
    assert virtual_session._xdotool_key("PageDown") == "Next"
    assert virtual_session._xdotool_key("a") == "a", "plain keys pass through"


# --- persistent profiles ----------------------------------------------------
# The profile is what carries cookies and stored logins between sessions and
# across container restarts. Wiping it, or overwriting its Preferences, silently
# turns "resumes signed in" back into "lands on a login page".


def test_profile_lives_on_the_data_volume():
    """Not /tmp: a profile in /tmp does not survive a container restart."""
    record = virtual_channels.validate_channel({"name": "p", "url": "https://e.com"})
    session = virtual_session.VirtualSession(record, 99)
    assert session.profile_dir.startswith(virtual_session.PROFILE_ROOT)
    assert not session.profile_dir.startswith("/tmp/")


def test_seeding_preserves_existing_profile_data(tmp_path):
    """Seeding must MERGE. Chromium stores session state in this same file, so
    replacing it wholesale on every launch would discard what we are trying to
    keep."""
    record = virtual_channels.validate_channel({"name": "p", "url": "https://e.com"})
    session = virtual_session.VirtualSession(record, 99)
    session.profile_dir = str(tmp_path / "profile")

    default = os.path.join(session.profile_dir, "Default")
    os.makedirs(default)
    with open(os.path.join(default, "Preferences"), "w") as f:
        json.dump(
            {"profile": {"content_settings": {"keep": 1}, "exit_type": "Crashed"},
             "some_saved_state": {"token": "keepme"}},
            f,
        )

    session._seed_profile()

    prefs = json.load(open(os.path.join(default, "Preferences")))
    assert prefs["some_saved_state"] == {"token": "keepme"}, "unrelated keys survive"
    assert prefs["profile"]["content_settings"] == {"keep": 1}, "nested keys survive"
    # ...while our suppressions are still applied on top.
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["exit_type"] == "Normal", "reset every launch"


def test_seeding_clears_stale_singleton_locks(tmp_path):
    """A killed session leaves these behind and Chromium then refuses to start.
    With a persistent profile that failure would be permanent, not self-healing.
    """
    record = virtual_channels.validate_channel({"name": "p", "url": "https://e.com"})
    session = virtual_session.VirtualSession(record, 99)
    session.profile_dir = str(tmp_path / "profile")
    os.makedirs(session.profile_dir)
    for stale in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        open(os.path.join(session.profile_dir, stale), "w").close()

    session._seed_profile()

    for stale in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        assert not os.path.exists(os.path.join(session.profile_dir, stale))


def test_stop_does_not_delete_the_profile():
    """Teardown clears HLS output only. Deleting the profile here is what would
    make every restart require signing in again."""
    import inspect

    source = inspect.getsource(virtual_session.VirtualSession.stop)
    assert "self.out_dir" in source
    assert "profile_dir" not in source, "the profile must survive a stop()"


# --- saving must not needlessly restart a session ---------------------------


def test_cosmetic_edit_does_not_need_a_restart():
    base = virtual_channels.validate_channel({"name": "a", "url": "https://e.com"})
    assert not virtual_channels.needs_restart(base, {**base, "title": "Renamed"})
    assert not virtual_channels.needs_restart(base, {**base, "idle_timeout": 600})
    assert not virtual_channels.needs_restart(base, {**base, "autostart": True})


@pytest.mark.parametrize(
    "field,value",
    [("url", "https://other.com"), ("resolution", "1080p"), ("framerate", 15),
     ("audio", False), ("video_bitrate", 900), ("preset", "ultrafast")],
)
def test_capture_edit_needs_a_restart(field, value):
    base = virtual_channels.validate_channel({"name": "a", "url": "https://e.com"})
    assert virtual_channels.needs_restart(base, {**base, field: value})


# --- editing a virtual channel ----------------------------------------------
# save_vc is driven directly through its underlying function with a stand-in
# `self`. The handler only touches its own vc_* attributes and module-level
# functions, so this exercises the real save logic without a Reflex runtime —
# and these are all bugs that reached a running server.


class _FakeSettingsState:
    """Minimal stand-in for SettingsState, carrying just the form fields."""

    def __init__(self, **overrides):
        self.vc_editing = ""
        self.vc_name = ""
        self.vc_title = ""
        self.vc_url = "https://example.com/live"
        self.vc_resolution = virtual_channels.DEFAULT_RESOLUTION
        self.vc_framerate = str(virtual_channels.DEFAULT_FRAMERATE)
        self.vc_preset = virtual_channels.DEFAULT_PRESET
        self.vc_capture = virtual_channels.DEFAULT_CAPTURE
        self.vc_video_bitrate = "2500"
        self.vc_audio = True
        self.vc_audio_bitrate = "128"
        self.vc_warmup = "6"
        self.vc_idle_timeout = "120"
        self.vc_click_selectors = ""
        self.vc_hide_selectors = ""
        self.vc_logo = ""
        self.vc_tags = "Virtual"
        self.vc_enabled = True
        self.vc_autostart = False
        self.vc_error = ""
        self.vc_token = ""
        self.vc_list = []
        self.reset_called = False
        self.__dict__.update(overrides)

    def _load_virtual(self, token=""):
        self.vc_list = virtual_channels.list_channels()

    def reset_vc_form(self):
        self.reset_called = True
        self.vc_editing = ""


def _run_save(state):
    """Drive save_vc and return the events it yielded."""
    from freesky.pages.settings import SettingsState

    result = SettingsState.save_vc.fn(state)
    return list(result) if result is not None else []


def test_save_rejects_rename_onto_an_existing_channel():
    """This silently destroyed the target channel: it disappeared from the
    playlist while the UI reported a successful save."""
    virtual_channels.upsert_channel({"name": "bbc", "url": "https://a.com/1"})
    virtual_channels.upsert_channel({"name": "cnn", "url": "https://b.com/2"})

    state = _FakeSettingsState(vc_editing="bbc", vc_name="cnn", vc_url="https://a.com/1")
    _run_save(state)

    assert "already exists" in state.vc_error
    names = sorted(c["name"] for c in virtual_channels.list_channels())
    assert names == ["bbc", "cnn"], "neither channel may be lost"
    assert virtual_channels.get_channel("cnn")["url"] == "https://b.com/2", "target untouched"


def test_save_rejects_adding_a_duplicate_name():
    virtual_channels.upsert_channel({"name": "bbc", "url": "https://a.com/1"})

    state = _FakeSettingsState(vc_name="bbc", vc_url="https://evil.com/x")
    _run_save(state)

    assert "already exists" in state.vc_error
    assert virtual_channels.get_channel("bbc")["url"] == "https://a.com/1"


def test_failed_rename_does_not_half_commit_other_edits():
    """The old code wrote the edited fields under the old name and only then
    renamed, so an invalid name left the other edits committed to disk behind an
    error message that implied nothing had happened."""
    virtual_channels.upsert_channel(
        {"name": "bbc", "title": "BBC", "url": "https://a.com/1"}
    )

    state = _FakeSettingsState(
        vc_editing="bbc", vc_name="BBC One!", vc_title="Changed",
        vc_url="https://evil.com/x",
    )
    _run_save(state)

    assert state.vc_error, "an invalid name must be reported"
    stored = virtual_channels.get_channel("bbc")
    assert stored["url"] == "https://a.com/1", "URL must not have been written"
    assert stored["title"] == "BBC", "title must not have been written"


def test_successful_rename_moves_the_record():
    virtual_channels.upsert_channel({"name": "bbc", "title": "BBC", "url": "https://a.com/1"})

    state = _FakeSettingsState(
        vc_editing="bbc", vc_name="bbc-one", vc_title="BBC One", vc_url="https://a.com/1"
    )
    _run_save(state)

    assert not state.vc_error
    assert virtual_channels.get_channel("bbc") is None, "old id must not linger"
    assert virtual_channels.get_channel("bbc-one")["title"] == "BBC One"


def test_rename_restarts_the_session_under_its_OLD_name():
    """The session is keyed by the old name. reset_vc_form() clears vc_editing,
    so reading it afterwards restarted a session that never existed and leaked
    the real one — a browser and an encoder left running forever."""
    virtual_channels.upsert_channel({"name": "bbc", "url": "https://a.com/1"})

    state = _FakeSettingsState(vc_editing="bbc", vc_name="bbc-one", vc_url="https://a.com/1")
    events = _run_save(state)

    payloads = [str(getattr(e, "args", e)) for e in events]
    assert any("bbc" in p and "bbc-one" not in p for p in payloads), (
        f"expected a restart keyed on the old name, got {payloads}"
    )


def test_cosmetic_edit_does_not_restart_the_session():
    virtual_channels.upsert_channel({"name": "bbc", "title": "BBC", "url": "https://a.com/1"})

    state = _FakeSettingsState(
        vc_editing="bbc", vc_name="bbc", vc_title="BBC News", vc_url="https://a.com/1"
    )
    events = _run_save(state)

    assert not state.vc_error
    assert virtual_channels.get_channel("bbc")["title"] == "BBC News"
    rendered = " ".join(str(getattr(e, "name", e)) for e in events)
    assert "restart_vc_session" not in rendered, "a title change must not kill the browser"


# --- capture tuning ---------------------------------------------------------


def test_every_framerate_gives_a_whole_gop():
    """A GOP that is not exactly one segment long makes ffmpeg cut at the next
    keyframe instead, producing erratic segment durations — far worse for
    players than a slightly higher latency."""
    for fps in virtual_channels.FRAMERATES:
        assert (fps * virtual_session.SEGMENT_SECONDS) % 1 == 0
        record = _x11(framerate=fps)
        argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
        assert argv[argv.index("-g") + 1] == str(fps * virtual_session.SEGMENT_SECONDS)


def test_audio_resampler_uses_a_stretching_async_value():
    """async=1 only fills and trims — it inserts silence or hard-cuts samples,
    audible as clicks over a long session. A larger value stretches instead."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    joined = " ".join(virtual_session.VirtualSession(record, 99)._ffmpeg_argv())
    assert "aresample=async=1000" in joined
    assert "aresample=async=1:" not in joined


def test_encoder_threads_are_pinned():
    """Unpinned, x264 spawns ~1.5x ncpu threads and starves the very browser it
    is capturing — which shows up as duplicated frames, not as a slow encode."""
    record = _x11()
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert argv[argv.index("-threads") + 1] == str(virtual_session.ENCODER_THREADS)


def test_browser_does_not_use_swiftshader():
    """SwiftShader is a WebGL emulator and the expensive software path; a
    <video> page wants Skia CPU raster instead."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    args = virtual_session.VirtualSession(record, 99)._browser_args()
    assert not any("swiftshader" in a.lower() for a in args)
    assert "--disable-gpu" in args and "--disable-software-rasterizer" in args
    # Reported to starve video decode on a CPU-bound host.
    assert "--disable-frame-rate-limit" not in args
    # Playwright already passes these; a second copy is noise at best.
    for duplicated in ("--disable-renderer-backgrounding",
                       "--disable-background-timer-throttling",
                       "--no-first-run"):
        assert duplicated not in args


# --- encoder telemetry ------------------------------------------------------
# Without these numbers, "the stream is laggy" cannot be diagnosed from outside
# the container: a browser that is not painting and an encoder that cannot keep
# up look identical, and they need opposite fixes.


import shutil as _shutil
import subprocess as _subprocess


def test_progress_metrics_projection():
    block = {
        "frame": "750", "fps": "24.90", "bitrate": "2500.1kbits/s",
        "dup_frames": "37", "drop_frames": "0", "speed": "0.98x",
        "out_time": "00:00:30.00", "progress": "continue",
    }
    m = virtual_session.progress_metrics(block)
    assert m["fps"] == "24.90" and m["speed"] == "0.98x"
    assert m["dup"] == "37" and m["drop"] == "0"


def test_progress_metrics_defaults_are_safe():
    """An early block arrives before every counter exists; the UI must still
    render rather than KeyError."""
    m = virtual_session.progress_metrics({})
    assert m["dup"] == "0" and m["drop"] == "0" and m["fps"] == ""


@pytest.mark.skipif(not _shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_progress_stream_parses_real_ffmpeg_output():
    """Parse the actual -progress stream, so a change in ffmpeg's key names is
    caught here rather than by an empty telemetry column in Settings."""
    proc = _subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-nostats",
         "-progress", "pipe:1", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
         "-t", "1", "-c:v", "libx264", "-preset", "ultrafast", "-f", "null", "-"],
        capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode == 0, proc.stderr[:300]

    block, seen = {}, []
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        block[key.strip()] = value.strip()
        if key.strip() == "progress":
            seen.append(virtual_session.progress_metrics(block))
            block = {}

    assert seen, "ffmpeg produced no progress blocks"
    last = seen[-1]
    assert last["frames"] and int(last["frames"]) > 0
    assert last["dup"].isdigit() and last["drop"].isdigit()
    assert last["speed"], "speed is how we tell encoder overload from a stalled browser"


def test_status_exposes_flat_metrics():
    """Flat keys, not a nested dict: Reflex cannot index a nested dict inside an
    rx.foreach, and the sessions table would fail to render."""
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    status = virtual_session.VirtualSession(record, 99).status()
    for key in ("fps", "speed", "dup", "drop"):
        assert key in status and not isinstance(status[key], dict)


def test_ffmpeg_emits_machine_readable_progress():
    record = virtual_channels.validate_channel({"name": "d", "url": "https://e.com"})
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    assert "-progress" in argv and argv[argv.index("-progress") + 1] == "pipe:1"
    # -nostats too, or the human progress line (carriage-return delimited)
    # would never terminate a readline().
    assert "-nostats" in argv


# --- cold start ------------------------------------------------------------
# Building a session means an X display, a browser and an encoder, and for the
# playlist route also the first HLS segments. Doing all of that inside one HTTP
# request exceeded the reverse proxy's header timeout, so the control panel only
# ever saw a 502 while the session was in fact coming up correctly.


def test_start_can_skip_waiting_for_segments():
    """The control panel needs the browser, not the stream."""
    import inspect

    sig = inspect.signature(virtual_session.VirtualSession.start)
    assert sig.parameters["wait_for_stream"].default is True
    sig = inspect.signature(virtual_session.SessionManager.acquire)
    assert sig.parameters["wait_for_stream"].default is True


def test_control_start_does_not_wait_for_the_stream():
    import inspect

    from freesky import backend

    source = inspect.getsource(backend.virtual_control_start)
    assert "wait_for_stream=False" in source


def test_warming_session_is_not_reaped_as_stalled(tmp_path):
    """A session with no playlist yet is warming up, not dead. Judging it by
    playlist freshness alone made it get torn down and rebuilt in a loop."""
    record = virtual_channels.validate_channel({"name": "w", "url": "https://e.com"})
    session = virtual_session.VirtualSession(record, 99)
    session.out_dir = str(tmp_path)
    session.playlist_path = os.path.join(str(tmp_path), "index.m3u8")

    class _LiveProc:
        returncode = None

    session._ffmpeg = _LiveProc()

    # Just started, no playlist on disk yet.
    assert session.is_alive(), "a freshly started session must survive its warm-up"

    # Past the grace period with still no playlist, it is genuinely dead.
    session.started_at -= virtual_session._STARTUP_GRACE + 1
    assert not session.is_alive()


def test_playlist_freshness_still_governs_a_warm_session(tmp_path):
    record = virtual_channels.validate_channel({"name": "w", "url": "https://e.com"})
    session = virtual_session.VirtualSession(record, 99)
    session.playlist_path = os.path.join(str(tmp_path), "index.m3u8")

    class _LiveProc:
        returncode = None

    session._ffmpeg = _LiveProc()
    open(session.playlist_path, "w").close()
    # Well past the grace period, but the playlist is fresh -> alive.
    session.started_at -= virtual_session._STARTUP_GRACE + 100
    assert session.is_alive()

    # Stale playlist -> stalled, regardless of grace.
    stale = time.time() - virtual_session.SEGMENT_SECONDS * 10
    os.utime(session.playlist_path, (stale, stale))
    assert not session.is_alive()



# --- single-worker enforcement ---------------------------------------------
#
# These cover the bug that made the control panel return 502 and the backend
# look like it would not start: reflex ran granian with `(cpu_count * 2) + 1`
# worker processes, because start.sh brings up Redis and
# reflex.utils.processes.get_num_workers() scales with cpu_count as soon as it
# can ping it. WORKERS was never read by anything. Every worker then ran the
# autostart lifespan task against the same display, profile and playlist.


def _start_sh() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(here, "start.sh")) as f:
        return f.read()


def test_start_sh_exports_granian_workers():
    """GRANIAN_WORKERS is the only worker knob reflex actually reads."""
    body = _start_sh()
    assert 'export GRANIAN_WORKERS="$WORKERS"' in body, (
        "start.sh must export GRANIAN_WORKERS; setting WORKERS alone is ignored "
        "by reflex and it falls back to (cpu_count * 2) + 1 workers."
    )


def test_start_sh_defaults_to_one_worker():
    """A default above 1 reintroduces the multi-process race."""
    body = _start_sh()
    assert "WORKERS=${WORKERS:-1}" in body, (
        "start.sh must default to a single worker: virtual channels keep "
        "per-process state (manager, display counter, autostart task)."
    )


def test_channel_lock_is_exclusive_across_processes(tmp_path, monkeypatch):
    """A second holder of the same channel is refused, not silently allowed."""
    monkeypatch.setattr(virtual_session, "LOCK_ROOT", str(tmp_path / "locks"))
    record = virtual_channels.validate_channel({"name": "dup", "url": "https://e.com"})

    first = virtual_session.VirtualSession(record, 99)
    first._acquire_channel_lock()
    try:
        second = virtual_session.VirtualSession(record, 100)
        with pytest.raises(virtual_session.VirtualSessionError) as exc:
            second._acquire_channel_lock()
        assert "already running" in str(exc.value)
        # The loser must not hold an fd it will later unlock out from under the
        # winner -- that would make the guard worse than none at all.
        assert second._lock_fd is None
    finally:
        first._release_channel_lock()

    # Once released, the channel can be claimed again.
    third = virtual_session.VirtualSession(record, 101)
    third._acquire_channel_lock()
    third._release_channel_lock()


def test_channel_lock_is_per_channel(tmp_path, monkeypatch):
    """Different channels must not block each other."""
    monkeypatch.setattr(virtual_session, "LOCK_ROOT", str(tmp_path / "locks"))
    a = virtual_session.VirtualSession(
        virtual_channels.validate_channel({"name": "a", "url": "https://e.com"}), 99)
    b = virtual_session.VirtualSession(
        virtual_channels.validate_channel({"name": "b", "url": "https://e.com"}), 100)
    a._acquire_channel_lock()
    b._acquire_channel_lock()
    a._release_channel_lock()
    b._release_channel_lock()


def test_release_channel_lock_is_safe_without_one(tmp_path, monkeypatch):
    """stop() runs on half-built sessions, including ones that never locked."""
    monkeypatch.setattr(virtual_session, "LOCK_ROOT", str(tmp_path / "locks"))
    session = virtual_session.VirtualSession(
        virtual_channels.validate_channel({"name": "n", "url": "https://e.com"}), 99)
    session._release_channel_lock()
    session._release_channel_lock()


def test_lock_root_is_not_inside_hls_root():
    """stop() rmtree's the HLS dir; a lock file under it would vanish."""
    assert not virtual_session.LOCK_ROOT.startswith(
        virtual_session.HLS_ROOT.rstrip("/") + "/")


def _repo_file(name: str) -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(here, name)) as f:
        return f.read()


def test_env_file_does_not_request_multiple_workers():
    """.env feeds compose's ${WORKERS:-1}, so a value here overrides the default.

    This is not hypothetical: .env carried WORKERS=4, which silently beat the
    single-worker default in docker-compose.yml and start.sh.
    """
    body = _repo_file(".env")
    values = re.findall(r"^WORKERS=(\S+)", body, re.MULTILINE)
    assert values == ["1"], f"expected .env to pin WORKERS=1, found {values}"


def test_compose_defaults_to_one_worker():
    body = _repo_file("docker-compose.yml")
    assert "WORKERS=${WORKERS:-1}" in body


def test_start_sh_clamps_multiple_workers():
    """A stray WORKERS value must not silently restore the broken config.

    Defaulting was not enough: docker-compose.yml and start.sh both already
    defaulted to 1, and `WORKERS=4` in .env still won, because compose's
    ${WORKERS:-1} only applies when the variable is unset.
    """
    body = _start_sh()
    assert 'if [ "$WORKERS" != "1" ]; then' in body
    assert "ALLOW_MULTIPLE_WORKERS" in body, (
        "an explicit override must exist so the clamp is documented, not magic"
    )
    # The clamp has to assign, not merely warn.
    # Bounded by the export, not by "fi" -- "fi" also occurs inside the word
    # "configuration" in the warning text.
    clamp = body.split('if [ "$WORKERS" != "1" ]; then', 1)[1].split(
        'export GRANIAN_WORKERS', 1)[0]
    assert "WORKERS=1" in clamp


# --- cropping ---------------------------------------------------------------


def _rec(**kw):
    base = {"name": "c", "url": "https://e.com", "resolution": "720p"}
    base.update(kw)
    return virtual_channels.validate_channel(base)


def test_no_crop_by_default_streams_the_whole_screen():
    record = _rec()
    assert virtual_channels.crop_box(record) is None
    assert virtual_channels.output_size(record) == virtual_channels.geometry(record)
    assert (record["crop_x"], record["crop_y"], record["crop_w"], record["crop_h"]) == (0, 0, 0, 0)


def test_crop_dimensions_are_forced_even():
    """H.264 with yuv420p rejects odd dimensions outright."""
    record = _rec(crop_x=100, crop_y=50, crop_w=641, crop_h=361)
    assert (record["crop_w"], record["crop_h"]) == (640, 360)
    assert virtual_channels.output_size(record) == (640, 360)


def test_crop_outside_the_screen_is_rejected():
    with pytest.raises(virtual_channels.VirtualChannelError) as exc:
        _rec(crop_x=1000, crop_y=0, crop_w=400, crop_h=400)
    assert "outside" in str(exc.value)


def test_crop_needs_both_width_and_height():
    with pytest.raises(virtual_channels.VirtualChannelError):
        _rec(crop_w=400)
    with pytest.raises(virtual_channels.VirtualChannelError):
        _rec(crop_h=400)


def test_crop_below_minimum_is_rejected():
    with pytest.raises(virtual_channels.VirtualChannelError) as exc:
        _rec(crop_x=0, crop_y=0, crop_w=10, crop_h=10)
    assert str(virtual_channels.MIN_CROP) in str(exc.value)


def test_crop_exactly_filling_the_screen_is_allowed():
    record = _rec(crop_x=0, crop_y=0, crop_w=1280, crop_h=720)
    assert virtual_channels.output_size(record) == (1280, 720)


def test_crop_is_applied_to_the_x11grab_input_not_a_filter():
    """Grabbing the region directly is cheaper than grabbing all and cropping."""
    record = _rec(crop_x=100, crop_y=50, crop_w=640, crop_h=360, capture="x11grab")
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    i = argv.index("-video_size")
    assert argv[i + 1] == "640x360"
    assert argv[i + 2] == "-i"
    assert argv[i + 3] == ":99.0+100,50"
    # A -vf crop would mean the whole screen was read and thrown away.
    assert "crop=" not in " ".join(argv)


def test_uncropped_session_grabs_the_whole_display():
    record = _rec(capture="x11grab")
    argv = virtual_session.VirtualSession(record, 99)._ffmpeg_argv()
    i = argv.index("-video_size")
    assert argv[i + 1] == "1280x720"
    assert argv[i + 3] == ":99.0"


def test_changing_the_crop_requires_a_restart():
    """The crop is baked into ffmpeg's input geometry."""
    old = _rec()
    new = _rec(crop_x=0, crop_y=0, crop_w=640, crop_h=360)
    assert virtual_channels.needs_restart(old, new)
    assert not virtual_channels.needs_restart(new, dict(new))


def test_crop_survives_a_round_trip_through_validate():
    """Records are re-validated on every read from disk."""
    once = _rec(crop_x=12, crop_y=34, crop_w=200, crop_h=100)
    twice = virtual_channels.validate_channel(once)
    assert virtual_channels.crop_box(twice) == (12, 34, 200, 100)


def test_crop_is_validated_against_the_channels_own_resolution():
    """A crop that fits 1080p must not be accepted on a 720p channel."""
    big = virtual_channels.validate_channel(
        {"name": "c", "url": "https://e.com", "resolution": "1080p",
         "crop_x": 0, "crop_y": 0, "crop_w": 1600, "crop_h": 900})
    assert virtual_channels.output_size(big) == (1600, 900)
    with pytest.raises(virtual_channels.VirtualChannelError):
        virtual_channels.validate_channel(
            {"name": "c", "url": "https://e.com", "resolution": "720p",
             "crop_x": 0, "crop_y": 0, "crop_w": 1600, "crop_h": 900})


def test_crop_endpoint_saves_and_reports_output_size(client):
    virtual_channels.upsert_channel({"name": "cam", "url": "https://e.com"})
    res = client.post("/api/virtual-control/cam/crop",
                      json={"x": 40, "y": 20, "w": 800, "h": 450})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["cropped"] is True
    assert body["crop"] == {"x": 40, "y": 20, "w": 800, "h": 450}
    assert body["output"] == {"width": 800, "height": 450}
    assert virtual_channels.crop_box(virtual_channels.get_channel("cam")) == (40, 20, 800, 450)


def test_crop_endpoint_clears_with_zeros(client):
    virtual_channels.upsert_channel(
        {"name": "cam", "url": "https://e.com", "crop_x": 0, "crop_y": 0,
         "crop_w": 800, "crop_h": 450})
    res = client.post("/api/virtual-control/cam/crop", json={"x": 0, "y": 0, "w": 0, "h": 0})
    assert res.status_code == 200, res.text
    assert res.json()["cropped"] is False
    assert res.json()["output"] == {"width": 1280, "height": 720}
    assert virtual_channels.crop_box(virtual_channels.get_channel("cam")) is None


def test_crop_endpoint_rejects_an_out_of_bounds_region(client):
    """A bad drag is the admin's mistake, so it must read as 400, not 500."""
    virtual_channels.upsert_channel({"name": "cam", "url": "https://e.com"})
    res = client.post("/api/virtual-control/cam/crop",
                      json={"x": 1200, "y": 0, "w": 400, "h": 400})
    assert res.status_code == 400
    assert "outside" in res.json()["message"]
    # Nothing may be committed when validation fails.
    assert virtual_channels.crop_box(virtual_channels.get_channel("cam")) is None


def test_crop_endpoint_404s_for_an_unknown_channel(client):
    res = client.post("/api/virtual-control/nope/crop", json={"x": 0, "y": 0, "w": 0, "h": 0})
    assert res.status_code == 404


def test_settings_save_preserves_a_crop_set_from_the_panel():
    """The settings form has no crop fields, so it must carry them across.

    Without this, saving any unrelated field in Settings reset the streamed
    region to the whole screen.
    """
    source = _repo_file(os.path.join("freesky", "pages", "settings.py"))
    body = source.split("def save_vc", 1)[1].split("\n    def ", 1)[0]
    assert 'for field in ("crop_x", "crop_y", "crop_w", "crop_h")' in body, (
        "save_vc must copy the crop from the previous record"
    )
    assert "previous" in body


def test_control_panel_exposes_crop_controls(client):
    """The panel is the only place a crop can be drawn, so it must ship the UI."""
    virtual_channels.upsert_channel({"name": "cam", "url": "https://e.com"})
    res = client.get("/api/virtual-control/cam/panel")
    assert res.status_code == 200
    html = res.text
    for marker in ('id="cropmode"', 'id="cropapply"', 'id="cropclear"',
                   'id="cropbox"', '"/crop"'):
        assert marker in html, f"panel is missing {marker}"
    # The current crop must reach the page so it can be drawn on load.
    assert '"crop"' in html


def test_control_panel_javascript_parses():
    """The panel is one big inline script; a syntax error blanks the whole page.

    Skipped when node is unavailable rather than silently passing.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    node = _shutil.which("node")
    if node is None:
        pytest.skip("node not available to parse the panel script")

    from freesky import backend

    html = backend._CONTROL_PANEL_HTML.replace("__CONFIG__", json.dumps({
        "name": "c", "title": "t", "token": "", "width": 1280, "height": 720,
        "url": "https://e.com", "crop": {"x": 0, "y": 0, "w": 0, "h": 0},
    }))
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "panel has no inline script"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write("\n".join(scripts))
        path = f.name
    try:
        proc = _subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    finally:
        os.unlink(path)


# --- crash-proof startup trace and orphan reaping ---------------------------


def test_trace_survives_and_reports_the_last_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(virtual_session, "TRACE_PATH", str(tmp_path / "t.log"))
    virtual_session.reset_trace("cam")
    virtual_session.trace("xvfb:begin")
    virtual_session.trace("xvfb:ok", "pid=123")
    lines = virtual_session.read_trace()
    assert "attempt" in lines[0] and "channel=cam" in lines[0]
    assert lines[-1].endswith("pid=123")
    # A fresh attempt must not leave the previous trail behind, or the last
    # line would name a stage from an older run.
    virtual_session.reset_trace("cam")
    assert len(virtual_session.read_trace()) == 1


def test_trace_never_raises_on_a_bad_path(monkeypatch):
    """Diagnostics must not break the thing they diagnose."""
    monkeypatch.setattr(virtual_session, "TRACE_PATH", "/proc/cannot/write/here.log")
    virtual_session.trace("x")          # must not raise
    assert virtual_session.read_trace() == []


def test_trace_endpoint_reports_the_last_stage(client, tmp_path, monkeypatch):
    monkeypatch.setattr(virtual_session, "TRACE_PATH", str(tmp_path / "t.log"))
    virtual_session.reset_trace("cam")
    virtual_session.trace("browser:begin")
    res = client.get("/api/virtual-sessions/trace")
    assert res.status_code == 200
    body = res.json()
    assert "browser:begin" in body["last_stage"]
    assert body["complete"] is False


@pytest.mark.parametrize("cmdline,expected", [
    ("Xvfb :99 -screen 0 1280x720x24", True),
    ("Xvfb :0 -screen 0 1280x720x24", False),          # a real desktop, not ours
    ("ffmpeg -f x11grab -i :99.0 out.ts", True),
    ("ffmpeg -i movie.mp4 out.ts", False),             # unrelated ffmpeg
    ("/usr/bin/python3 manage.py", False),
    ("", False),
])
def test_orphan_matcher_is_narrow(cmdline, expected):
    """It sends SIGKILL, so it must only ever match what this feature creates."""
    assert virtual_session._own_orphan(cmdline) is expected


def test_orphan_matcher_matches_our_browser_profiles(monkeypatch):
    monkeypatch.setattr(virtual_session, "PROFILE_ROOT", "/app/data/virtual-profiles")
    assert virtual_session._own_orphan(
        "chrome --user-data-dir=/app/data/virtual-profiles/cam --no-sandbox")
    assert not virtual_session._own_orphan(
        "chrome --user-data-dir=/home/someone/.config/google-chrome")


def test_reap_orphans_does_not_kill_this_process():
    """A matcher bug here would take the backend down on every boot."""
    killed = virtual_session.reap_orphans()
    assert isinstance(killed, list)
    # Still running, which is the assertion that matters.
    assert os.getpid() > 0


def test_backend_runs_in_production_mode():
    """`reflex run` defaults to DEV, and dev mode enables a file watcher.

    Dev granian runs with reload=True, reload_tick=100 and
    reload_paths=[Path.cwd()] -- and start.sh cds to /app, so the whole app tree
    is watched, including the ./data volume. Chromium writing its profile into
    /app/data/virtual-profiles then restarted the worker mid-request, and
    workers_kill_timeout=2 dropped the connection ~2.2s in. That was the "HTTP
    502 after 2.2s" on every control-panel start, and the "it reloads every time
    settings change" symptom, since users.json and friends live there too.

    Reproduced and fixed empirically: one .ldb write took the dev backend down
    for 2.2s; under --env prod the identical write changed nothing.
    """
    body = _start_sh()
    assert "reflex run --env prod" in body, (
        "start.sh must pass --env prod; REFLEX_ENV and rxconfig's env=PROD do "
        "NOT override the CLI default of DEV, and dev mode watches /app"
    )


def test_hot_reload_override_is_pinned_away_from_the_data_volume():
    """Belt and braces if --env prod is ever dropped from the run line."""
    body = _start_sh()
    assert "REFLEX_HOT_RELOAD_OVERRIDE_PATHS" in body
    assert "/app/freesky" in body, "the watcher must point at source, not /app"


# --- tab capture (the default path) -----------------------------------------
# Frames come from Chromium's compositor via freesky/virtual_capture_ext and
# arrive already H.264-encoded on a loopback WebSocket; ffmpeg only remuxes.
# The failure that motivated it: x11grab on a busy host delivered ~2 distinct
# pictures a second (5000 duplicated + 2000 dropped frames in three minutes)
# while the page itself was presenting 44fps. Wall-clock sampling cannot be
# made smooth under load; compositor timestamps can.


def _tab(**fields):
    fields.setdefault("name", "d")
    fields.setdefault("url", "https://e.com")
    return virtual_channels.validate_channel(fields)


def test_tab_capture_is_the_default():
    assert virtual_channels.DEFAULT_CAPTURE == "tab"
    assert _tab()["capture"] == "tab"
    assert _tab(capture="x11grab")["capture"] == "x11grab"
    with pytest.raises(virtual_channels.VirtualChannelError):
        _tab(capture="screencast")


def test_switching_capture_path_restarts_the_session():
    """The extension flags and the encoder command are baked in at launch."""
    assert virtual_channels.needs_restart(_tab(), _tab(capture="x11grab"))


def test_tab_ffmpeg_remuxes_and_does_not_encode_video():
    """The whole point: Chromium already encoded it. A second libx264 pass would
    cost the CPU that was starving the browser in the first place."""
    argv = virtual_session.VirtualSession(_tab(), 99)._ffmpeg_argv()
    joined = " ".join(argv)
    assert "pipe:0" in argv, "the feed arrives on stdin"
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "libx264" not in argv and "x11grab" not in joined and "pulse" not in joined
    assert "-nostdin" not in argv, "stdin IS the input here"
    assert "-c:a aac" in joined, "MPEG-TS players do not take Opus"
    assert "delete_segments" in joined and "independent_segments" in joined
    assert argv[-1].endswith("index.m3u8")


def test_tab_crop_reencodes_with_a_filter():
    """A crop is the one thing that forces decode + libx264 on this path."""
    argv = virtual_session.VirtualSession(
        _tab(crop_x=100, crop_y=50, crop_w=640, crop_h=360), 99
    )._ffmpeg_argv()
    joined = " ".join(argv)
    assert "crop=640:360:100:50" in joined
    assert "libx264" in argv and "copy" not in argv
    assert argv[argv.index("-g") + 1] == str(_tab()["framerate"] * virtual_session.SEGMENT_SECONDS)


def test_tab_silent_channel_drops_audio():
    argv = virtual_session.VirtualSession(_tab(audio=False), 99)._ffmpeg_argv()
    assert "-an" in argv and "aac" not in argv


def test_tab_sessions_load_and_trust_the_extension():
    """Without --allowlisted-extension-id, getMediaStreamId fails with
    "Extension has not been invoked for the current page"."""
    args = virtual_session.VirtualSession(_tab(), 99)._browser_args()
    ext_id = virtual_session.extension_id()
    assert f"--load-extension={virtual_session.EXT_DIR}" in args
    assert f"--disable-extensions-except={virtual_session.EXT_DIR}" in args
    assert f"--allowlisted-extension-id={ext_id}" in args
    # x11grab sessions do not carry an extension they never drive.
    x11 = virtual_session.VirtualSession(_x11(), 99)._browser_args()
    assert not any(a.startswith("--load-extension") for a in x11)


def test_extension_id_is_derived_from_the_manifest_key():
    """Chromium ids an extension by the SHA-256 of its public key, a-p encoded.
    Hard-coding the id next to the manifest is how the two drift apart."""
    import base64
    import hashlib

    manifest = json.load(open(os.path.join(virtual_session.EXT_DIR, "manifest.json")))
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    expected = "".join(chr(ord("a") + int(c, 16)) for c in digest)
    assert virtual_session.extension_id() == expected
    assert re.fullmatch(r"[a-p]{32}", expected)
    assert "tabCapture" in manifest["permissions"] and "offscreen" in manifest["permissions"]
    assert manifest["manifest_version"] == 3, "MV2 extensions no longer load in current Chromium"
    for name in ("sw.js", "offscreen.html", "offscreen.js"):
        assert os.path.exists(os.path.join(virtual_session.EXT_DIR, name)), name


def test_recorder_asks_for_one_keyframe_per_segment():
    """With -c:v copy the keyframe cadence is Chromium's, so the extension has to
    request it; otherwise segments cut at whatever interval OpenH264 picks."""
    src = open(os.path.join(virtual_session.EXT_DIR, "offscreen.js")).read()
    assert "videoKeyFrameIntervalDuration" in src
    session = virtual_session.VirtualSession(_tab(), 99)
    assert session.record["framerate"] * virtual_session.SEGMENT_SECONDS > 0


def test_capture_feed_is_admitted_only_by_the_session_secret(client, monkeypatch):
    """A valid *user* token must not be enough to push video into a channel."""
    session = virtual_session.VirtualSession(_tab(name="feed"), 99)
    # Not registered: nothing to feed.
    assert virtual_session.capture_target("feed", session.capture_secret) is None
    virtual_session._capture_targets[session.capture_secret] = session
    try:
        assert virtual_session.capture_target("feed", session.capture_secret) is session
        assert virtual_session.capture_target("other", session.capture_secret) is None
        assert virtual_session.capture_target("feed", "wrong") is None
        # Right key, but no encoder to feed yet.
        assert not session.accepts_capture(session.capture_secret)
        with pytest.raises(Exception):
            with client.websocket_connect("/api/virtual-capture/feed?key=wrong"):
                pass
    finally:
        virtual_session._capture_targets.pop(session.capture_secret, None)


def test_status_reports_host_cpu_picture(client):
    """Stutter is nearly always CPU; the page should show the number that
    explains it (quota throttling) without a shell into the container."""
    body = client.get("/api/virtual-sessions/status").json()
    assert "host" in body
    for key in ("cpus", "quota", "load", "throttled_pct"):
        assert key in body["host"]


def test_session_status_carries_capture_mode_and_cpu():
    status = virtual_session.VirtualSession(_tab(), 99).status()
    assert status["capture"] == "tab"
    assert status["capture_connected"] is False
    assert status["cpu_browser"] == "-" and status["cpu_encoder"] == "-"


def test_framerates_include_the_60hz_grid_rates():
    """Tab capture takes every Nth compositor frame off a 60Hz timer, so the
    rates that divide 60 give even intervals. 30 is the default for that
    reason; 25 is kept for x11grab, which samples on its own clock."""
    assert virtual_channels.DEFAULT_FRAMERATE == 30
    for fps in (20, 30, 60):
        assert fps in virtual_channels.FRAMERATES
    assert 50 in virtual_channels.FRAMERATES, "50fps sources exist"


def test_diagnostics_endpoint_works_for_a_registered_session(client):
    """Regression: the handler referenced virtual_session without importing it
    once the host CPU picture was added, and every diagnostics call was a 500."""
    session = virtual_session.VirtualSession(_tab(name="diag"), 99)
    virtual_session.manager._sessions["diag"] = session
    try:
        body = client.get("/api/virtual-control/diag/diagnostics").json()
    finally:
        virtual_session.manager._sessions.pop("diag", None)
    assert body["feed"]["mode"] == "tab"
    assert body["capture"]["framerate"] == virtual_channels.DEFAULT_FRAMERATE
    assert "host" in body and "cpu" in body
    assert "error" in body["page"], "no live page, reported rather than raised"


def test_tab_copy_path_snaps_timestamps_to_the_frame_grid():
    """Compositor timestamps sit on a 60Hz grid; snapping to 1/fps is what turns
    a 17/33/50ms pattern into an even 33ms without drifting over hours."""
    argv = virtual_session.VirtualSession(_tab(framerate=30), 99)._ffmpeg_argv()
    bsf = argv[argv.index("-bsf:v") + 1]
    assert bsf.startswith("setts=ts=")
    assert "round(TS*TB*30)/(TB*30)" in bsf
    assert "PREV_OUTPTS+1/(TB*30)" in bsf, "never earlier than one slot past the last"
    # The re-encode path is CFR already and must not carry the bsf.
    cropped = virtual_session.VirtualSession(
        _tab(crop_w=640, crop_h=360), 99)._ffmpeg_argv()
    assert "-bsf:v" not in cropped
