"""Tests for the Kodi-style license key importer.

Covers the shapes a `license_key` string takes in the wild, and asserts that a
credential value never reaches an error message.
"""

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    """Point the provider store at a temp file so imports never touch real data.

    Patches the module attribute rather than reloading the module: a reload
    rebinds DRMProviderError to a new class object, so `pytest.raises` in these
    tests would no longer match the class `drm_import` bound at import time.
    """
    from freesky import drm_providers
    monkeypatch.setattr(drm_providers, "PROVIDERS_FILE", str(tmp_path / "drm.json"))
    yield


def _import(key, **kw):
    from freesky.drm_import import parse_license_key
    kw.setdefault("name", "svc")
    kw.setdefault("manifest_url", "https://example.test/manifest.mpd")
    return parse_license_key(key, **kw)


# --- field splitting -------------------------------------------------------

def test_url_only_defaults_to_raw_both_ways():
    rec = _import("https://lic.example.test/widevine")
    assert rec["license_url"] == "https://lic.example.test/widevine"
    assert rec["license_headers"] == {}
    assert rec["request_wrap"] == "raw"
    assert rec["response_unwrap"] == "raw"


def test_empty_string_rejected():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError):
        _import("   ")


def test_too_many_fields_rejected_with_encoding_hint():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError) as e:
        _import("https://a.test|b|c|d|e")
    assert "%7C" in str(e.value)


def test_empty_url_rejected():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError):
        _import("|Auth=x|R{SSM}|R")


# --- headers ---------------------------------------------------------------

def test_headers_parsed_and_url_decoded():
    rec = _import(
        "https://lic.example.test/w|"
        "Authorization=Bearer%20abc&User-Agent=Test%2FClient+1.0|R{SSM}|R"
    )
    assert rec["license_headers"] == {
        "Authorization": "Bearer abc",
        "User-Agent": "Test/Client 1.0",
    }


def test_header_without_equals_rejected():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError) as e:
        _import("https://a.test|BrokenHeader|R{SSM}|R")
    assert "Name=value" in str(e.value)


def test_header_value_never_appears_in_an_error():
    """A malformed later field must not echo an earlier field's credential."""
    from freesky.drm_providers import DRMProviderError
    secret = "sup3rs3cret-token"
    with pytest.raises(DRMProviderError) as e:
        _import(f"https://a.test|Authorization={secret}|WHAT|R")
    assert secret not in str(e.value)


# --- request wrapping ------------------------------------------------------

@pytest.mark.parametrize("spec", ["", "R", "R{SSM}", "{SSM}"])
def test_raw_request_specs(spec):
    rec = _import(f"https://a.test||{spec}|R")
    assert rec["request_wrap"] == "raw"


@pytest.mark.parametrize("spec", ["b", "b{SSM}", "{SSMB64}"])
def test_base64_request_specs(spec):
    rec = _import(f"https://a.test||{spec}|R")
    assert rec["request_wrap"] == "base64"


def test_json_template_maps_to_named_field():
    rec = _import('https://a.test||{"challenge":"{SSMB64}","v":1}|R')
    assert rec["request_wrap"] == "json:challenge"


def test_json_template_with_raw_placeholder():
    rec = _import('https://a.test||{"payload":"{SSM}"}|R')
    assert rec["request_wrap"] == "json:payload"


def test_unrecognised_request_spec_rejected():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError):
        _import("https://a.test||NONSENSE|R")


# --- response unwrapping ---------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("", "raw"), ("R", "raw"), ("B", "base64"),
    ("JBlicense", "json:license"), ("Jlicense", "json:license"),
])
def test_response_specs(spec, expected):
    rec = _import(f"https://a.test||R{{SSM}}|{spec}")
    assert rec["response_unwrap"] == expected


def test_nested_response_path_rejected_with_guidance():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError) as e:
        _import("https://a.test||R{SSM}|JBdata.license")
    assert "manually" in str(e.value)


def test_response_spec_naming_no_field_rejected():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError):
        _import("https://a.test||R{SSM}|JB")


# --- integration with validation -------------------------------------------

def test_result_passes_provider_validation_and_saves():
    from freesky import drm_providers
    rec = _import(
        "https://lic.example.test/w|Authorization=Bearer%20abc|b{SSM}|JBlicense",
        name="myservice",
    )
    saved = drm_providers.upsert_provider(rec)
    assert saved["name"] == "myservice"
    assert drm_providers.get_provider("myservice")["response_unwrap"] == "json:license"


def test_bad_manifest_url_rejected_by_validation():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError):
        _import("https://a.test||R{SSM}|R", manifest_url="javascript:alert(1)")


def test_non_http_license_url_rejected_by_validation():
    from freesky.drm_providers import DRMProviderError
    with pytest.raises(DRMProviderError):
        _import("file:///etc/passwd||R{SSM}|R")
