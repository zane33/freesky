"""Instance-wide settings that admins change at runtime.

Same file-backed pattern as channel_prefs.py. Currently just the trusted-network
allowlist: requests from these CIDRs skip authentication entirely, so a LAN can
watch without logging in while the internet-facing side still requires it.
"""
import ipaddress
import json
import os
import threading
from typing import List

SETTINGS_FILE = os.environ.get(
    "APP_SETTINGS_FILE",
    os.path.join(os.path.dirname(__file__), "app_settings.json"),
)

_write_lock = threading.Lock()

# Seed value for a fresh install, e.g. "192.168.3.0/24,10.0.0.0/8".
_ENV_DEFAULT = os.environ.get("TRUSTED_NETWORKS", "")


def _load() -> dict:
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def _save(data: dict) -> None:
    with _write_lock:
        os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
        tmp = f"{SETTINGS_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)


def trusted_networks() -> List[str]:
    """CIDRs allowed in without credentials. Falls back to TRUSTED_NETWORKS env."""
    data = _load()
    if "trusted_networks" in data:
        return list(data["trusted_networks"])
    return [n.strip() for n in _ENV_DEFAULT.split(",") if n.strip()]


def set_trusted_networks(networks: List[str]) -> List[str]:
    """Validate and store. Invalid entries are rejected rather than silently kept,
    since a typo'd CIDR that never matches would look like auth is broken."""
    cleaned = []
    for n in networks:
        n = str(n).strip()
        if not n:
            continue
        ipaddress.ip_network(n, strict=False)  # raises on nonsense
        cleaned.append(n)
    data = _load()
    data["trusted_networks"] = cleaned
    _save(data)
    return cleaned


def is_trusted_ip(ip: str) -> bool:
    """True when this client may skip login.

    Unparseable or empty IPs are never trusted — failing closed matters more here
    than being lenient about a proxy that didn't forward an address.
    """
    if not ip:
        return False
    nets = trusted_networks()
    if not nets:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    for n in nets:
        try:
            if addr in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip_from_headers(headers, fallback: str = "") -> str:
    """Real client IP behind Caddy.

    Caddy terminates the connection, so request.client.host is the proxy. The
    left-most X-Forwarded-For entry is the original client. Only meaningful
    because the reverse proxy is the sole ingress — if the backend port is
    reachable directly, a client can forge this header, so don't expose it.
    """
    xri = headers.get("x-real-ip") or headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return fallback


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        SETTINGS_FILE = os.path.join(d, "s.json")
        assert trusted_networks() == []
        assert not is_trusted_ip("192.168.3.5"), "no networks => nothing trusted"
        set_trusted_networks(["192.168.3.0/24", " 10.0.0.0/8 "])
        assert is_trusted_ip("192.168.3.5") and is_trusted_ip("10.1.2.3")
        assert not is_trusted_ip("8.8.8.8")
        assert not is_trusted_ip("garbage") and not is_trusted_ip("")
        # a single host works too
        set_trusted_networks(["192.168.3.148/32"])
        assert is_trusted_ip("192.168.3.148") and not is_trusted_ip("192.168.3.149")
        try:
            set_trusted_networks(["not-a-cidr"])
            raise AssertionError("should have rejected")
        except ValueError:
            pass
        assert client_ip_from_headers({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}) == "1.2.3.4"
        assert client_ip_from_headers({"x-real-ip": "9.9.9.9"}) == "9.9.9.9"
        assert client_ip_from_headers({}, "7.7.7.7") == "7.7.7.7"
        print("app_settings ok")
