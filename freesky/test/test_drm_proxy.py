"""Tests for the DRM license proxy and the DRM stream-type guard.

Covers the three wrap/unwrap modes end to end, token authentication, the
challenge size cap, and the 409 returned when an M3U8 is requested for a DRM
channel. The provider registry and the upstream license server are both mocked —
nothing here talks to a real license endpoint, and no key material is involved.

Run with:  pytest freesky/test/test_drm_proxy.py
"""
import base64
import json
from types import SimpleNamespace

import pytest

# The backend pulls in the full app stack (reflex, curl_cffi, httpx). Skip with a
# clear message rather than erroring when the tests are run outside the app
# environment / container.
backend = pytest.importorskip(
    "freesky.backend",
    reason="freesky.backend needs the application dependencies installed",
)
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402  (after the skip guard)

from freesky.free_sky import Channel  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

RAW_LICENSE = b"\x00\x01\xff\xfe binary license payload \x80\x7f"
CHALLENGE = b"\x08\x04\x12\x10opaque-cdm-blob\xff"


def _provider(**overrides) -> dict:
    """A provider record matching the drm_providers interface."""
    record = {
        "name": "acme",
        "key_system": "com.widevine.alpha",
        "license_url": "https://license.example/acme",
        "license_headers": {"X-Auth-Token": "s3cret"},
        "request_wrap": "raw",
        "response_unwrap": "raw",
        "manifest_url": "https://cdn.example/acme/manifest.mpd",
        "manifest_type": "dash",
        "enabled": True,
    }
    record.update(overrides)
    return record


@pytest.fixture
def registry(monkeypatch):
    """Install a fake drm_providers registry the routes will resolve against."""
    store = {}

    module = SimpleNamespace(
        list_providers=lambda: list(store.values()),
        get_provider=lambda name: store.get(name),
    )
    monkeypatch.setattr(backend, "_drm_module", lambda: module)
    return store


@pytest.fixture
def no_users(monkeypatch):
    """Unconfigured install: no users exist, so auth is not enforced."""
    monkeypatch.setattr(backend.users, "list_users", lambda: [])


@pytest.fixture
def with_users(monkeypatch):
    """One admin and one standard user, addressed by their stream tokens."""
    accounts = {
        "admin-token": {"username": "root", "role": "admin"},
        "user-token": {"username": "bob", "role": "standard"},
    }
    monkeypatch.setattr(backend.users, "list_users",
                        lambda: [{"username": u["username"]} for u in accounts.values()])
    monkeypatch.setattr(backend.users, "user_by_token", lambda t: accounts.get(t))
    return accounts


@pytest.fixture
def client():
    return TestClient(backend.fastapi_app)


def _mock_upstream(monkeypatch, status_code=200, content=b"", capture=None, raises=None):
    """Replace the shared httpx client's post with a stub."""
    async def _post(url, content=None, headers=None, timeout=None, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture["body"] = content
            capture["headers"] = headers
            capture["timeout"] = timeout
        if raises is not None:
            raise raises
        return httpx.Response(
            status_code,
            content=_mock_upstream.body,
            request=httpx.Request("POST", url),
        )

    _mock_upstream.body = content
    monkeypatch.setattr(backend.client, "post", _post)


# --------------------------------------------------------------------------
# Wrap / unwrap round-trips
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["raw", "base64", "json:license"])
def test_wrap_unwrap_round_trip(mode):
    """Every supported mode must survive wrap -> unwrap byte-exact."""
    wrapped, content_type = backend._wrap_challenge(RAW_LICENSE, mode)
    assert backend._unwrap_license(wrapped, mode) == RAW_LICENSE
    assert content_type == ("application/json" if mode.startswith("json:")
                            else "application/octet-stream")


def test_wrap_raw_is_untouched():
    assert backend._wrap_challenge(CHALLENGE, "raw") == (CHALLENGE, "application/octet-stream")


def test_wrap_base64_encodes():
    body, _ = backend._wrap_challenge(CHALLENGE, "base64")
    assert body == base64.b64encode(CHALLENGE)


def test_wrap_json_puts_base64_in_named_field():
    body, content_type = backend._wrap_challenge(CHALLENGE, "json:payload")
    assert content_type == "application/json"
    assert json.loads(body) == {"payload": base64.b64encode(CHALLENGE).decode()}


def test_unwrap_rejects_bad_base64():
    with pytest.raises(backend.DrmWrapError):
        backend._unwrap_license(b"not base64 !!!", "base64")


def test_unwrap_rejects_missing_json_field():
    body = json.dumps({"other": "AAAA"}).encode()
    with pytest.raises(backend.DrmWrapError):
        backend._unwrap_license(body, "json:license")


def test_unwrap_rejects_unknown_mode():
    with pytest.raises(backend.DrmWrapError):
        backend._unwrap_license(b"x", "protobuf")


# --------------------------------------------------------------------------
# License route: end-to-end through each mode
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,encode", [
    ("raw", lambda b: b),
    ("base64", base64.b64encode),
    ("json:license", lambda b: json.dumps({"license": base64.b64encode(b).decode()}).encode()),
])
def test_license_relay_returns_bytes_exact(monkeypatch, registry, no_users, client, mode, encode):
    """The CDM must receive the license byte-exact, whatever the wire format."""
    registry["acme"] = _provider(request_wrap=mode, response_unwrap=mode)
    capture = {}
    _mock_upstream(monkeypatch, 200, encode(RAW_LICENSE), capture=capture)

    resp = client.post("/api/drm/license/acme", content=CHALLENGE)

    assert resp.status_code == 200
    assert resp.content == RAW_LICENSE
    assert resp.headers["content-type"] == "application/octet-stream"
    # Byte-transparency: nothing may re-encode this body.
    assert resp.headers["content-encoding"] == "identity"
    # The challenge went upstream wrapped as configured, with provider creds.
    assert capture["body"] == encode(CHALLENGE)
    assert capture["headers"]["X-Auth-Token"] == "s3cret"
    assert capture["timeout"] == backend._DRM_UPSTREAM_TIMEOUT


def test_license_requires_token(registry, with_users, client):
    registry["acme"] = _provider()
    resp = client.post("/api/drm/license/acme", content=CHALLENGE)
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthenticated"


def test_license_accepts_valid_token(monkeypatch, registry, with_users, client):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, 200, RAW_LICENSE)
    resp = client.post("/api/drm/license/acme", content=CHALLENGE,
                       headers={"Authorization": "Bearer user-token"})
    assert resp.status_code == 200
    assert resp.content == RAW_LICENSE


def test_license_rejects_empty_challenge(registry, no_users, client):
    registry["acme"] = _provider()
    resp = client.post("/api/drm/license/acme", content=b"")
    assert resp.status_code == 400
    assert resp.json()["error"] == "empty_challenge"


def test_license_enforces_size_cap(registry, no_users, client):
    registry["acme"] = _provider()
    oversized = b"A" * (backend._DRM_MAX_CHALLENGE_BYTES + 1)
    resp = client.post("/api/drm/license/acme", content=oversized)
    assert resp.status_code == 400
    assert resp.json()["error"] == "challenge_too_large"


def test_license_allows_challenge_at_cap(monkeypatch, registry, no_users, client):
    """Exactly at the limit is still valid — the cap is inclusive."""
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, 200, RAW_LICENSE)
    resp = client.post("/api/drm/license/acme",
                       content=b"A" * backend._DRM_MAX_CHALLENGE_BYTES)
    assert resp.status_code == 200


def test_license_unknown_provider(registry, no_users, client):
    resp = client.post("/api/drm/license/nope", content=CHALLENGE)
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_provider"


def test_license_disabled_provider(registry, no_users, client):
    registry["acme"] = _provider(enabled=False)
    resp = client.post("/api/drm/license/acme", content=CHALLENGE)
    assert resp.status_code == 404
    assert resp.json()["error"] == "provider_disabled"


@pytest.mark.parametrize("upstream_status", [401, 403])
def test_license_upstream_rejection_is_403(monkeypatch, registry, no_users, client, upstream_status):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, upstream_status, b"denied")
    resp = client.post("/api/drm/license/acme", content=CHALLENGE)
    assert resp.status_code == 403
    assert resp.json()["error"] == "upstream_rejected"


def test_license_timeout_is_504(monkeypatch, registry, no_users, client):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, raises=httpx.ReadTimeout("slow"))
    resp = client.post("/api/drm/license/acme", content=CHALLENGE)
    assert resp.status_code == 504
    assert resp.json()["error"] == "upstream_timeout"


def test_license_transport_failure_is_502(monkeypatch, registry, no_users, client):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, raises=httpx.ConnectError("refused"))
    resp = client.post("/api/drm/license/acme", content=CHALLENGE)
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_unreachable"


def test_license_upstream_5xx_is_502(monkeypatch, registry, no_users, client):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, 500, b"boom")
    resp = client.post("/api/drm/license/acme", content=CHALLENGE)
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_error"


def test_license_bad_unwrap_is_502(monkeypatch, registry, no_users, client):
    registry["acme"] = _provider(response_unwrap="json:license")
    _mock_upstream(monkeypatch, 200, b"<html>not json</html>")
    resp = client.post("/api/drm/license/acme", content=CHALLENGE)
    assert resp.status_code == 502
    assert resp.json()["error"] == "bad_response_unwrap"


def test_license_never_logs_secrets(monkeypatch, registry, no_users, client, caplog):
    """Header values and bodies are credentials/payload — names and sizes only."""
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, 200, RAW_LICENSE)
    with caplog.at_level("DEBUG"):
        client.post("/api/drm/license/acme", content=CHALLENGE)
    logged = caplog.text
    assert "s3cret" not in logged
    assert "X-Auth-Token" in logged  # the key name is fine to log


# --------------------------------------------------------------------------
# Test route
# --------------------------------------------------------------------------

def test_drm_test_requires_admin(registry, with_users, client):
    registry["acme"] = _provider()
    resp = client.post("/api/drm/test/acme", headers={"Authorization": "Bearer user-token"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"


def test_drm_test_admin_ok(monkeypatch, registry, with_users, client):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, 200, RAW_LICENSE)
    resp = client.post("/api/drm/test/acme", headers={"Authorization": "Bearer admin-token"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True and body["code"] == "ok"


def test_drm_test_reports_unauthorized(monkeypatch, registry, no_users, client):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, 401, b"")
    body = client.post("/api/drm/test/acme").json()
    assert body["ok"] is False and body["code"] == "unauthorized"
    assert "license_headers" in body["message"]


def test_drm_test_reports_bad_unwrap(monkeypatch, registry, no_users, client):
    registry["acme"] = _provider(response_unwrap="base64")
    _mock_upstream(monkeypatch, 200, b"\x00\x01not-base64!!")
    body = client.post("/api/drm/test/acme").json()
    assert body["ok"] is False and body["code"] == "bad_response_unwrap"


@pytest.mark.parametrize("exc,code", [
    (httpx.ConnectError("[Errno -2] Name or service not known"), "dns_error"),
    (httpx.ConnectError("certificate verify failed"), "tls_error"),
    (httpx.ConnectError("Connection refused"), "connect_error"),
    (httpx.ConnectTimeout("timed out"), "timeout"),
])
def test_drm_test_classifies_transport_failures(monkeypatch, registry, no_users, client, exc, code):
    registry["acme"] = _provider()
    _mock_upstream(monkeypatch, raises=exc)
    body = client.post("/api/drm/test/acme").json()
    assert body["ok"] is False
    assert body["code"] == code
    assert body["message"]  # actionable text for the settings UI


def test_drm_test_unknown_provider(registry, no_users, client):
    body = client.post("/api/drm/test/ghost").json()
    assert body["ok"] is False and body["code"] == "unknown_provider"


def test_drm_test_disabled_provider(registry, no_users, client):
    registry["acme"] = _provider(enabled=False)
    body = client.post("/api/drm/test/acme").json()
    assert body["ok"] is False and body["code"] == "provider_disabled"


# --------------------------------------------------------------------------
# Channel dataclass + stream-type guard
# --------------------------------------------------------------------------

def test_channel_defaults_are_backwards_compatible():
    """Existing positional construction must keep working."""
    ch = Channel("1", "BBC One", ["UK"], "/logo.png")
    assert ch.provider == "" and ch.stream_type == "hls"


def test_channel_from_dict_reads_drm_fields():
    ch = Channel.from_dict({"id": 7, "name": "Sky", "provider": "acme", "stream_type": "drm"})
    assert ch.id == "7" and ch.provider == "acme" and ch.stream_type == "drm"


def test_channel_from_dict_tolerates_unknown_keys_and_bad_type():
    ch = Channel.from_dict({"id": "9", "is_live": True, "stream_type": "nonsense"})
    assert ch.stream_type == "hls"


def test_stream_route_409s_for_drm_channel(monkeypatch, no_users, client):
    monkeypatch.setattr(
        backend, "get_channel",
        lambda cid: Channel(cid, "Sky Sports", ["Sports"], "/l.png", provider="acme", stream_type="drm"),
    )
    resp = client.get("/api/stream/123.m3u8")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "drm_channel"
    assert body["provider"] == "acme"
    assert "in-browser" in body["message"]


def test_stream_route_allows_hls_channel(monkeypatch, no_users, client):
    """The guard must not intercept an ordinary channel."""
    monkeypatch.setattr(
        backend, "get_channel",
        lambda cid: Channel(cid, "BBC One", ["UK"], "/l.png"),
    )

    async def _fake_stream(channel_id, prefer=None):
        return "#EXTM3U\n#EXT-X-ENDLIST\n"

    monkeypatch.setattr(backend, "_get_stream_parallel", _fake_stream)
    backend.stream_cache.clear()
    resp = client.get("/api/stream/456.m3u8")
    assert resp.status_code == 200
    assert resp.text.startswith("#EXTM3U")
