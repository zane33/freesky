"""Unit tests for freesky.drm_providers.

Covers record validation (the part a user can get wrong from the settings form)
and round-trip persistence including the 0600 file mode that keeps license
credentials off other accounts on the host.

Runs under pytest. It also runs standalone (`python freesky/test/test_drm_providers.py`)
via the shims at the bottom, because pytest is not in requirements.txt.
"""
import json
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from freesky import drm_providers
from freesky.drm_providers import DRMProviderError


def _valid(**overrides) -> dict:
    """A minimal provider record, with any field overridden for the test."""
    record = {
        "name": "skyshowtime",
        "key_system": "com.widevine.alpha",
        "license_url": "https://license.example.com/wv",
        "license_headers": {"Authorization": "Bearer secret-token"},
        "request_wrap": "raw",
        "response_unwrap": "json:license",
        "manifest_url": "https://cdn.example.com/stream.mpd",
        "manifest_type": "dash",
        "enabled": True,
    }
    record.update(overrides)
    return record


def _isolate(monkeypatch, tmp_path) -> str:
    """Point the module at a throwaway store and return its path."""
    path = os.path.join(str(tmp_path), "drm", "providers.json")
    monkeypatch.setattr(drm_providers, "PROVIDERS_FILE", path)
    return path


# --- validation --------------------------------------------------------------

def test_valid_record_normalises():
    """A good record comes back with exactly the known keys."""
    out = drm_providers.validate_provider(_valid())
    assert set(out) == {
        "name", "key_system", "license_url", "license_headers", "request_wrap",
        "response_unwrap", "manifest_url", "manifest_type", "enabled",
    }
    assert out["name"] == "skyshowtime"
    assert out["enabled"] is True


def test_name_is_lowercased_and_trimmed():
    assert drm_providers.validate_provider(_valid(name="  SkyShowTime "))["name"] == "skyshowtime"


def test_bad_names_rejected():
    for bad in ("", "-leading", "has space", "has.dot", "a" * 65, "über"):
        try:
            drm_providers.validate_provider(_valid(name=bad))
        except DRMProviderError:
            continue
        raise AssertionError(f"name {bad!r} should have been rejected")


def test_good_names_accepted():
    for good in ("sky", "sky-uk", "sky_uk_2", "0abc"):
        assert drm_providers.validate_provider(_valid(name=good))["name"] == good


def test_key_system_enum():
    assert drm_providers.validate_provider(
        _valid(key_system="com.microsoft.playready")
    )["key_system"] == "com.microsoft.playready"
    for bad in ("", "com.apple.fps", "widevine"):
        try:
            drm_providers.validate_provider(_valid(key_system=bad))
        except DRMProviderError:
            continue
        raise AssertionError(f"key_system {bad!r} should have been rejected")


def test_manifest_type_enum():
    assert drm_providers.validate_provider(_valid(manifest_type="HLS"))["manifest_type"] == "hls"
    try:
        drm_providers.validate_provider(_valid(manifest_type="smooth"))
        raise AssertionError("smooth should have been rejected")
    except DRMProviderError:
        pass


def test_url_scheme_restricted():
    """file:// and javascript: must never make it into a stored record."""
    for field in ("license_url", "manifest_url"):
        for bad in ("", "file:///etc/passwd", "javascript:alert(1)", "https://", "example.com/x"):
            try:
                drm_providers.validate_provider(_valid(**{field: bad}))
            except DRMProviderError:
                continue
            raise AssertionError(f"{field}={bad!r} should have been rejected")


def test_wrap_grammar():
    for good in ("raw", "base64", "json:license", "json:payload.data"):
        out = drm_providers.validate_provider(
            _valid(request_wrap=good, response_unwrap=good)
        )
        assert out["request_wrap"] == good and out["response_unwrap"] == good
    for bad in ("", "json", "json:", "json:   ", "hex", "base32"):
        try:
            drm_providers.validate_provider(_valid(request_wrap=bad))
        except DRMProviderError:
            continue
        raise AssertionError(f"request_wrap {bad!r} should have been rejected")
        # response_unwrap uses the same checker, exercised below
    for bad in ("json:", "hex"):
        try:
            drm_providers.validate_provider(_valid(response_unwrap=bad))
        except DRMProviderError:
            continue
        raise AssertionError(f"response_unwrap {bad!r} should have been rejected")


def test_headers_validated_without_leaking_values():
    """A rejected header must not put its value in the error message."""
    try:
        drm_providers.validate_provider(
            _valid(license_headers={"Bad Name": "super-secret-token"})
        )
        raise AssertionError("invalid header name should have been rejected")
    except DRMProviderError as e:
        assert "super-secret-token" not in str(e)

    try:
        drm_providers.validate_provider(
            _valid(license_headers={"X-Auth": "tok\r\nX-Evil: 1"})
        )
        raise AssertionError("header value with CRLF should have been rejected")
    except DRMProviderError as e:
        assert "X-Evil" not in str(e)

    assert drm_providers.validate_provider(_valid(license_headers=None))["license_headers"] == {}
    try:
        drm_providers.validate_provider(_valid(license_headers="Authorization: x"))
        raise AssertionError("non-dict headers should have been rejected")
    except DRMProviderError:
        pass


# --- persistence -------------------------------------------------------------

def test_empty_store_reads_empty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert drm_providers.list_providers() == []
    assert drm_providers.get_provider("nope") is None
    assert drm_providers.delete_provider("nope") is False


def test_round_trip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    saved = drm_providers.upsert_provider(_valid())
    loaded = drm_providers.get_provider("skyshowtime")
    assert loaded == saved
    # Headers survive the round trip — the relay needs them verbatim.
    assert loaded["license_headers"] == {"Authorization": "Bearer secret-token"}
    assert [p["name"] for p in drm_providers.list_providers()] == ["skyshowtime"]


def test_upsert_replaces_by_name(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    drm_providers.upsert_provider(_valid())
    drm_providers.upsert_provider(_valid(manifest_type="hls", enabled=False))
    providers = drm_providers.list_providers()
    assert len(providers) == 1, "name is the key; a second save must not duplicate"
    assert providers[0]["manifest_type"] == "hls"
    assert providers[0]["enabled"] is False


def test_list_is_sorted(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    for name in ("zed", "alpha", "mid"):
        drm_providers.upsert_provider(_valid(name=name))
    assert [p["name"] for p in drm_providers.list_providers()] == ["alpha", "mid", "zed"]


def test_lookup_and_delete_are_case_insensitive(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    drm_providers.upsert_provider(_valid())
    assert drm_providers.get_provider(" SkyShowTime ") is not None
    assert drm_providers.delete_provider("SKYSHOWTIME") is True
    assert drm_providers.get_provider("skyshowtime") is None


def test_invalid_record_is_not_written(monkeypatch, tmp_path):
    path = _isolate(monkeypatch, tmp_path)
    try:
        drm_providers.upsert_provider(_valid(license_url="ftp://x/y"))
        raise AssertionError("should have raised")
    except DRMProviderError:
        pass
    assert not os.path.exists(path), "a rejected record must not create the store"


def test_file_mode_is_0600(monkeypatch, tmp_path):
    """License headers are plaintext at rest; the mode is the only thing keeping
    them off other accounts on the host."""
    path = _isolate(monkeypatch, tmp_path)
    drm_providers.upsert_provider(_valid())
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # And re-asserted if something loosened it.
    os.chmod(path, 0o644)
    drm_providers.upsert_provider(_valid(name="other"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_no_temp_file_left_behind(monkeypatch, tmp_path):
    path = _isolate(monkeypatch, tmp_path)
    drm_providers.upsert_provider(_valid())
    assert not os.path.exists(f"{path}.tmp")


def test_corrupt_file_reads_empty(monkeypatch, tmp_path):
    path = _isolate(monkeypatch, tmp_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{ not json")
    assert drm_providers.list_providers() == [], "corrupt file must fail soft"


def test_invalid_stored_record_is_skipped(monkeypatch, tmp_path):
    """A hand-edited file with one bad entry still yields the good ones."""
    path = _isolate(monkeypatch, tmp_path)
    drm_providers.upsert_provider(_valid(name="good"))
    data = json.load(open(path))
    data["bad"] = {"name": "bad", "key_system": "nope"}
    with open(path, "w") as f:
        json.dump(data, f)
    assert [p["name"] for p in drm_providers.list_providers()] == ["good"]
    assert drm_providers.get_provider("bad") is None


# --- browser-facing redaction ------------------------------------------------

def test_public_provider_drops_headers(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    drm_providers.upsert_provider(_valid())
    public = drm_providers.public_providers()
    assert len(public) == 1
    assert "license_headers" not in public[0]
    assert "license_url" not in public[0], "the license URL is relayed, not dialled by the browser"
    assert public[0]["has_license_headers"] is True
    assert "secret-token" not in json.dumps(public)


def test_public_provider_flags_absent_headers():
    public = drm_providers.public_provider(
        drm_providers.validate_provider(_valid(license_headers={}))
    )
    assert public["has_license_headers"] is False


if __name__ == "__main__":
    # Minimal pytest stand-ins so this file runs without pytest installed.
    import tempfile
    import traceback

    class _Monkeypatch:
        """Just the setattr/undo slice of pytest's monkeypatch fixture."""

        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value):
            self._undo.append((target, name, getattr(target, name)))
            setattr(target, name, value)

        def undo(self):
            for target, name, old in reversed(self._undo):
                setattr(target, name, old)
            self._undo = []

    failures = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        _mp = _Monkeypatch()
        with tempfile.TemporaryDirectory() as _d:
            _kwargs = {}
            _params = _fn.__code__.co_varnames[:_fn.__code__.co_argcount]
            if "monkeypatch" in _params:
                _kwargs["monkeypatch"] = _mp
            if "tmp_path" in _params:
                _kwargs["tmp_path"] = _d
            try:
                _fn(**_kwargs)
                print(f"  ok   {_name}")
            except Exception:
                failures += 1
                print(f"  FAIL {_name}")
                traceback.print_exc()
            finally:
                _mp.undo()
    print("drm_providers tests failed" if failures else "drm_providers tests ok")
    sys.exit(1 if failures else 0)
