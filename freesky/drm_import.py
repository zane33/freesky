"""Import a Kodi-style DRM license key string into a FreeSky provider record.

Kodi DRM playback is configured through `inputstream.adaptive`'s `license_key`
property, a pipe-separated string of four fields::

    <license_url>|<headers>|<post_data_spec>|<response_spec>

That format is the public interface every Kodi DRM add-on ultimately speaks, so
parsing it lets a user move a working configuration into FreeSky by pasting one
string instead of filling six form fields by hand.

The two spec fields exist because providers disagree about how the CDM challenge
travels: some want the raw bytes, some want base64, some want it wrapped in JSON.
That is precisely the variability `drm_providers.request_wrap` and
`response_unwrap` already model, so this module is a translator, not a new
schema.

Nothing here contacts a provider or embeds any provider's settings; it only
converts a string the user supplies.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple
from urllib.parse import unquote, unquote_plus

from freesky.drm_providers import DRMProviderError, validate_provider

logger = logging.getLogger(__name__)

# Placeholders inputstream.adaptive substitutes with the CDM challenge.
_RAW_CHALLENGE = "{SSM}"
_B64_CHALLENGE = "{SSMB64}"


def _split_license_key(license_key: str) -> Tuple[str, str, str, str]:
    """Split a license key into its four fields, tolerating omitted trailing ones.

    Args:
        license_key: The raw `license_key` string.

    Returns:
        A 4-tuple of (url, headers, post_data, response), each possibly empty.

    Raises:
        DRMProviderError: If the string is empty or has more than four fields.
    """
    text = (license_key or "").strip()
    if not text:
        raise DRMProviderError("Paste a license key string to import.")

    parts = text.split("|")
    if len(parts) > 4:
        raise DRMProviderError(
            f"A license key has at most 4 pipe-separated fields; found {len(parts)}. "
            "If a header value contains a '|', URL-encode it as %7C."
        )
    parts += [""] * (4 - len(parts))
    return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()


def parse_headers(blob: str) -> Dict[str, str]:
    """Parse the `&`-joined, URL-encoded header field.

    Values are decoded with `unquote_plus` so that `+` means a space, matching
    how add-ons build these strings; names use plain `unquote` because a `+` is
    legal in a header name and must survive.

    Args:
        blob: The second license-key field, e.g. ``A=1&B=2``.

    Returns:
        A mapping of header name to value. Empty when `blob` is empty.

    Raises:
        DRMProviderError: If a pair has no `=` or an empty name.
    """
    headers: Dict[str, str] = {}
    if not blob:
        return headers

    for pair in blob.split("&"):
        if not pair:
            continue
        if "=" not in pair:
            raise DRMProviderError(
                f"Header {pair!r} is missing '='. Headers look like 'Name=value&Other=value'."
            )
        raw_name, raw_value = pair.split("=", 1)
        name = unquote(raw_name).strip()
        if not name:
            raise DRMProviderError("A header name is empty in the headers field.")
        headers[name] = unquote_plus(raw_value).strip()
    return headers


def parse_request_wrap(spec: str) -> str:
    """Translate the post-data spec into a `request_wrap` value.

    Recognised forms:
        - empty or ``R``/``R{SSM}`` -> ``raw`` (challenge sent as raw bytes)
        - ``b``/``b{SSM}``/anything using ``{SSMB64}`` -> ``base64``
        - a JSON template containing a challenge placeholder -> ``json:<field>``

    Args:
        spec: The third license-key field.

    Returns:
        A `request_wrap` value understood by the license proxy.

    Raises:
        DRMProviderError: If a JSON template has no recognisable field, or the
            spec is not one of the known shapes.
    """
    text = (spec or "").strip()
    if not text or text in ("R", _RAW_CHALLENGE, "R" + _RAW_CHALLENGE):
        return "raw"
    if text in ("b", "B", _B64_CHALLENGE, "b" + _RAW_CHALLENGE, "B" + _RAW_CHALLENGE):
        return "base64"

    # A JSON template such as {"challenge":"{SSMB64}","token":"x"}. We only need
    # the name of the field the challenge goes into; the proxy rebuilds the rest.
    if text.lstrip().startswith("{") and (_RAW_CHALLENGE in text or _B64_CHALLENGE in text):
        field = _json_field_for_placeholder(text)
        if field:
            return f"json:{field}"
        raise DRMProviderError(
            "Could not tell which JSON field carries the challenge. "
            "Set the request wrapping manually."
        )

    raise DRMProviderError(
        f"Unrecognised post-data spec {spec!r}. Expected empty, 'R{{SSM}}', "
        "'b{SSM}', or a JSON template containing {SSM} or {SSMB64}."
    )


def _json_field_for_placeholder(template: str) -> str:
    """Find the JSON key whose value is the challenge placeholder.

    Deliberately a light scan rather than a JSON parse: the template is not valid
    JSON until the placeholder is substituted, so `json.loads` would reject it.

    Args:
        template: A JSON-shaped string containing `{SSM}` or `{SSMB64}`.

    Returns:
        The key name, or "" if it cannot be determined.
    """
    for placeholder in (_B64_CHALLENGE, _RAW_CHALLENGE):
        idx = template.find(placeholder)
        if idx == -1:
            continue
        head = template[:idx]
        # Walk back over the opening quote/colon to the key's closing quote.
        colon = head.rfind(":")
        if colon == -1:
            continue
        key_part = head[:colon].rstrip()
        if len(key_part) >= 2 and key_part.endswith(('"', "'")):
            quote = key_part[-1]
            start = key_part.rfind(quote, 0, len(key_part) - 1)
            if start != -1:
                return key_part[start + 1:-1]
    return ""


def parse_response_unwrap(spec: str) -> str:
    """Translate the response spec into a `response_unwrap` value.

    Recognised forms:
        - empty or ``R`` -> ``raw``
        - ``B`` -> ``base64``
        - ``J<field>`` / ``JB<field>`` -> ``json:<field>``

    `JB` means "parse JSON, take the field, then base64-decode it", which is what
    the proxy's ``json:<field>`` mode already does, so both map to the same value.

    Args:
        spec: The fourth license-key field.

    Returns:
        A `response_unwrap` value understood by the license proxy.

    Raises:
        DRMProviderError: If the spec is not one of the known shapes.
    """
    text = (spec or "").strip()
    if not text or text == "R":
        return "raw"
    if text == "B":
        return "base64"
    if text.startswith("J"):
        field = text[2:] if text.startswith("JB") else text[1:]
        field = field.strip().strip(";").strip()
        if not field:
            raise DRMProviderError(
                f"Response spec {spec!r} names no JSON field. Expected e.g. 'JBlicense'."
            )
        # Add-ons sometimes write a dotted path; the proxy reads a single field.
        if "." in field:
            raise DRMProviderError(
                f"Response spec {spec!r} uses a nested path. The proxy reads one "
                "top-level field; set the response unwrapping manually."
            )
        return f"json:{field}"

    raise DRMProviderError(
        f"Unrecognised response spec {spec!r}. Expected empty, 'R', 'B', or 'JBfield'."
    )


def parse_license_key(
    license_key: str,
    *,
    name: str,
    manifest_url: str,
    manifest_type: str = "dash",
    key_system: str = "com.widevine.alpha",
    enabled: bool = True,
) -> dict:
    """Build a validated provider record from a pasted license key string.

    Args:
        license_key: The `inputstream.adaptive` license key string.
        name: Slug for the new provider record.
        manifest_url: Manifest URL; the license key does not carry one.
        manifest_type: "dash" or "hls".
        key_system: DRM key system identifier.
        enabled: Whether the provider is active on save.

    Returns:
        A normalised provider dict, already through `validate_provider`.

    Raises:
        DRMProviderError: On any malformed field, with a message written for the
            settings form rather than a log line.
    """
    url, header_blob, post_spec, response_spec = _split_license_key(license_key)
    if not url:
        raise DRMProviderError("The license key's first field (the licence URL) is empty.")

    record = {
        "name": name,
        "key_system": key_system,
        "license_url": url,
        "license_headers": parse_headers(header_blob),
        "request_wrap": parse_request_wrap(post_spec),
        "response_unwrap": parse_response_unwrap(response_spec),
        "manifest_url": manifest_url,
        "manifest_type": manifest_type,
        "enabled": enabled,
    }
    validated = validate_provider(record)
    logger.info(
        "Imported DRM provider %r: wrap=%s unwrap=%s headers=%s",
        validated["name"], validated["request_wrap"], validated["response_unwrap"],
        sorted(validated["license_headers"]),  # names only — values are credentials
    )
    return validated
