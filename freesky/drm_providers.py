"""DRM provider definitions for standards-compliant client-side EME playback.

A "provider" is the configuration FreeSky needs to hand a browser a DASH/HLS
manifest and to relay license requests to the provider's license server on the
client's behalf. Decryption happens entirely inside the browser's licensed CDM
(Widevine/PlayReady); nothing here extracts, derives, or stores content keys,
and the server never decrypts a stream.

Same file-backed pattern as channel_prefs.py / app_settings.py: one JSON file
under the ./data volume, a threading.Lock around writes, and os.replace so a
crash mid-write cannot truncate the file into a half-record. Path comes from
DRM_PROVIDERS_FILE.

SECURITY — headers are stored in plaintext at rest
--------------------------------------------------
`license_headers` routinely carries a bearer token or API key. This repo has no
at-rest encryption primitive: utils.py's XOR helper uses a per-process random
key, so anything it "encrypts" is unreadable after a restart and it is NOT a
secret store. Until a real key-management story exists (an OS keyring, a
passphrase-derived key, or an external secret manager), these values are written
as plaintext JSON. The mitigations in place are:

  * the file is created and kept at mode 0600 (owner read/write only);
  * header VALUES are never logged — only the header names;
  * `license_headers` is stripped from anything that reaches the browser (see
    `public_provider`), because the whole point of the server-side relay is that
    the credential never leaves the server.

Encrypting `license_headers` at rest is deliberate future work, tracked here so
it is not mistaken for an oversight.
"""
import json
import logging
import os
import re
import threading
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PROVIDERS_FILE = os.environ.get(
    "DRM_PROVIDERS_FILE",
    os.path.join(os.path.dirname(__file__), "drm_providers.json"),
)

_write_lock = threading.Lock()

# Only the two key systems a mainstream desktop/mobile browser actually ships a
# CDM for. FairPlay needs a different (non-EME-generic) flow, so it is out until
# that flow exists rather than half-supported.
KEY_SYSTEMS = ("com.widevine.alpha", "com.microsoft.playready")

MANIFEST_TYPES = ("dash", "hls")

# Slug: lowercase, used verbatim as the id prefix for this provider's channels,
# so it has to survive being embedded in a URL path and an M3U tvg-id.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# RFC 7230 token characters — anything else is not a legal header field name.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# "raw" and "base64" take no argument; "json:<field>" names one non-empty field.
_WRAP_RE = re.compile(r"^json:(.+)$")


class DRMProviderError(ValueError):
    """A provider record was rejected.

    Carries a message written for the person editing the form, not for a log
    line — the settings UI renders `str(exc)` directly.
    """


def _wrap_ok(value: str) -> bool:
    """True if `value` is a legal wrap/unwrap spec."""
    if value in ("raw", "base64"):
        return True
    match = _WRAP_RE.match(value)
    return bool(match and match.group(1).strip())


def _validate_url(value: str, label: str) -> str:
    """Return `value` stripped, or raise if it is not an absolute http(s) URL.

    Scheme is restricted so a provider record can never make the server fetch a
    file:// path or hand the browser a javascript: URL.
    """
    value = str(value or "").strip()
    if not value:
        raise DRMProviderError(f"{label} is required")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise DRMProviderError(f"{label} must start with http:// or https://")
    if not parsed.netloc:
        raise DRMProviderError(f"{label} is missing a host")
    return value


def _validate_headers(headers) -> Dict[str, str]:
    """Normalise `license_headers` to a str->str dict.

    Values are validated but never echoed into the error message or the log:
    a rejected header is usually a mistyped token, and repeating it in a log is
    how a credential ends up in a file with looser permissions than this one.
    """
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise DRMProviderError("License headers must be a set of name/value pairs")
    cleaned: Dict[str, str] = {}
    for name, value in headers.items():
        name = str(name).strip()
        if not name:
            continue
        if not _HEADER_NAME_RE.match(name):
            raise DRMProviderError(f"'{name}' is not a valid HTTP header name")
        value = str(value)
        # A newline in a value would let one header smuggle in another when the
        # relay builds the upstream request.
        if "\r" in value or "\n" in value:
            raise DRMProviderError(f"Header '{name}' must not contain a line break")
        cleaned[name] = value
    return cleaned


def validate_provider(record: dict) -> dict:
    """Validate and normalise one provider record.

    Args:
        record: Raw fields, typically straight off the settings form.

    Returns:
        A new dict with exactly the known keys, trimmed and type-coerced.

    Raises:
        DRMProviderError: With a message safe to show the user. Never includes a
            header value.
    """
    if not isinstance(record, dict):
        raise DRMProviderError("Provider must be a set of fields")

    name = str(record.get("name", "")).strip().lower()
    if not name:
        raise DRMProviderError("Name is required")
    if not _NAME_RE.match(name):
        raise DRMProviderError(
            "Name must be lowercase letters, digits, '-' or '_', start with a "
            "letter or digit, and be at most 64 characters"
        )

    key_system = str(record.get("key_system", "")).strip()
    if key_system not in KEY_SYSTEMS:
        raise DRMProviderError(f"Key system must be one of: {', '.join(KEY_SYSTEMS)}")

    manifest_type = str(record.get("manifest_type", "")).strip().lower()
    if manifest_type not in MANIFEST_TYPES:
        raise DRMProviderError(f"Manifest type must be one of: {', '.join(MANIFEST_TYPES)}")

    request_wrap = str(record.get("request_wrap", "raw")).strip()
    if not _wrap_ok(request_wrap):
        raise DRMProviderError(
            "Request wrap must be 'raw', 'base64', or 'json:<field>'"
        )

    response_unwrap = str(record.get("response_unwrap", "raw")).strip()
    if not _wrap_ok(response_unwrap):
        raise DRMProviderError(
            "Response unwrap must be 'raw', 'base64', or 'json:<field>'"
        )

    return {
        "name": name,
        "key_system": key_system,
        "license_url": _validate_url(record.get("license_url"), "License URL"),
        "license_headers": _validate_headers(record.get("license_headers")),
        "request_wrap": request_wrap,
        "response_unwrap": response_unwrap,
        "manifest_url": _validate_url(record.get("manifest_url"), "Manifest URL"),
        "manifest_type": manifest_type,
        "enabled": bool(record.get("enabled", True)),
    }


def _load() -> Dict[str, dict]:
    """All stored providers keyed by name. A missing or corrupt file reads as
    empty rather than raising — a bad file must not take the whole app down."""
    try:
        with open(PROVIDERS_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save(data: Dict[str, dict]) -> None:
    """Write the store atomically at mode 0600.

    The temp file is opened with 0600 from the start rather than chmod'd after,
    so there is no window where a token-bearing file is world-readable.
    """
    with _write_lock:
        os.makedirs(os.path.dirname(PROVIDERS_FILE) or ".", exist_ok=True)
        tmp = f"{PROVIDERS_FILE}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, PROVIDERS_FILE)
        # os.replace keeps the temp file's mode, but an operator may have
        # loosened an existing file; re-assert it on every write. Best-effort:
        # a filesystem without POSIX modes (a bind-mounted Windows path) is not
        # a reason to fail the save.
        try:
            os.chmod(PROVIDERS_FILE, 0o600)
        except OSError:
            pass


def list_providers() -> List[dict]:
    """Every stored provider, sorted by name.

    Records that no longer validate (hand-edited file, or a field this version
    tightened) are skipped rather than returned half-broken.
    """
    out: List[dict] = []
    for name, record in sorted(_load().items()):
        try:
            out.append(validate_provider({**record, "name": name}))
        except DRMProviderError as e:
            logger.warning("Skipping invalid DRM provider %r: %s", name, e)
    return out


def get_provider(name: str) -> Optional[dict]:
    """One provider by name, or None if it is not stored or no longer valid."""
    record = _load().get(str(name).strip().lower())
    if record is None:
        return None
    try:
        return validate_provider({**record, "name": str(name).strip().lower()})
    except DRMProviderError as e:
        logger.warning("Stored DRM provider %r is invalid: %s", name, e)
        return None


def upsert_provider(record: dict) -> dict:
    """Create or replace a provider, keyed by its name.

    Args:
        record: Raw fields; validated before anything is written.

    Returns:
        The normalised record as stored.

    Raises:
        DRMProviderError: If validation fails. Nothing is written in that case.
    """
    provider = validate_provider(record)
    data = _load()
    data[provider["name"]] = provider
    _save(data)
    # Names only — a value here would be the bearer token.
    logger.info(
        "Saved DRM provider %r (%s, %s) with headers: %s",
        provider["name"],
        provider["key_system"],
        provider["manifest_type"],
        ", ".join(sorted(provider["license_headers"])) or "none",
    )
    return provider


def delete_provider(name: str) -> bool:
    """Remove a provider. Returns False if there was nothing to remove."""
    name = str(name).strip().lower()
    data = _load()
    if name not in data:
        return False
    del data[name]
    _save(data)
    logger.info("Deleted DRM provider %r", name)
    return True


def public_provider(record: dict) -> dict:
    """The subset of a provider that is safe to send to a browser.

    `license_headers` is dropped entirely — the browser posts its license
    challenge to FreeSky, and FreeSky attaches the credential when it relays the
    request upstream. `has_license_headers` is kept so the UI can show that a
    credential is configured without revealing it.

    Anything rendering a provider client-side must go through this, not the raw
    record.
    """
    return {
        "name": record.get("name", ""),
        "key_system": record.get("key_system", ""),
        "manifest_url": record.get("manifest_url", ""),
        "manifest_type": record.get("manifest_type", ""),
        "enabled": bool(record.get("enabled", False)),
        "has_license_headers": bool(record.get("license_headers")),
    }


def public_providers() -> List[dict]:
    """`list_providers()` with every record run through `public_provider`."""
    return [public_provider(p) for p in list_providers()]
