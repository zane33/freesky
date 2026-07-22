"""Server-side user store: login, roles, and per-user stream tokens.

Same file-backed, atomic-write pattern as channel_prefs.py. Passwords are hashed
with stdlib scrypt (no new dependency). Two roles only: "admin" and "standard".

The per-user `token` is what authenticates the m3u8/segment URLs for a
non-interactive consumer like Dispatcharr, which has no way to send a cookie or
Basic-auth — the token rides in the URL and can be rotated to revoke access.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import Optional

USERS_FILE = os.environ.get(
    "USERS_FILE",
    os.path.join(os.path.dirname(__file__), "users.json"),
)

# scrypt work factors. N=2**15 is ~50ms/hash on a modern core — enough to make
# guessing expensive without stalling logins. Bump N if the host has headroom.
# maxmem must exceed 128*N*r bytes (=32MB here) or OpenSSL refuses the call.
_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024

# A fixed hash computed for unknown usernames so a login attempt spends the same
# CPU whether or not the account exists — no timing signal for enumeration.
_DUMMY_SALT = b"\x00" * 16
_DUMMY_HASH = hashlib.scrypt(b"decoy", salt=_DUMMY_SALT, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)

VALID_ROLES = ("admin", "standard")

_write_lock = threading.Lock()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM)


def _load() -> dict:
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def _save(users: dict) -> None:
    with _write_lock:
        os.makedirs(os.path.dirname(USERS_FILE) or ".", exist_ok=True)
        tmp = f"{USERS_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(users, f, indent=2)
        os.replace(tmp, USERS_FILE)


def list_users() -> list:
    """Public view — never leaks salt/hash/token."""
    return [
        {"username": name, "email": u.get("email", ""), "role": u.get("role", "standard")}
        for name, u in sorted(_load().items())
    ]


def token_for(username: str) -> str:
    """That user's stream token, for building their personal playlist URL."""
    return _load().get(username, {}).get("token", "")


def verify(username: str, password: str) -> Optional[dict]:
    """Return the user record on success, else None.

    Runs one scrypt regardless of whether the user exists, and compares in
    constant time, so an attacker can't tell "no such user" from "wrong
    password" by timing or by response.
    """
    user = _load().get(username)
    if user is None:
        # Spend the same work on a decoy so timing doesn't reveal absence.
        hmac.compare_digest(_DUMMY_HASH, _hash(password, _DUMMY_SALT))
        return None
    candidate = _hash(password, bytes.fromhex(user["salt"]))
    if hmac.compare_digest(bytes.fromhex(user["hash"]), candidate):
        return {"username": username, **user}
    return None


def user_by_token(token: str) -> Optional[dict]:
    """Resolve a stream/session token to its user, in constant time per entry."""
    if not token:
        return None
    for name, u in _load().items():
        if hmac.compare_digest(u.get("token", ""), token):
            return {"username": name, **u}
    return None


def add_user(username: str, email: str, password: str, role: str = "standard") -> None:
    username = username.strip()
    if not username or not password:
        raise ValueError("username and password required")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    users = _load()
    salt = secrets.token_bytes(16)
    users[username] = {
        "email": email.strip(),
        "role": role,
        "salt": salt.hex(),
        "hash": _hash(password, salt).hex(),
        "token": secrets.token_urlsafe(24),
    }
    _save(users)


def delete_user(username: str) -> None:
    users = _load()
    if username in users:
        del users[username]
        _save(users)


def set_password(username: str, password: str) -> None:
    users = _load()
    if username not in users:
        raise KeyError(username)
    salt = secrets.token_bytes(16)
    users[username]["salt"] = salt.hex()
    users[username]["hash"] = _hash(password, salt).hex()
    _save(users)


def rotate_token(username: str) -> str:
    """New token = old m3u8 URLs stop working. This is how you revoke a user."""
    users = _load()
    if username not in users:
        raise KeyError(username)
    tok = secrets.token_urlsafe(24)
    users[username]["token"] = tok
    _save(users)
    return tok


def ensure_admin() -> Optional[str]:
    """Bootstrap a first admin so a fresh deploy is reachable.

    Only runs when there are no users at all — never overwrites an existing one,
    so ADMIN_PASS sitting in the environment can't silently reset a password the
    admin changed later.

    If ADMIN_PASS isn't set we generate a random one and return it to be logged,
    rather than shipping a guessable default. This app is meant to be exposed,
    and "admin/admin" on an exposed host is an open door.

    Returns the generated password (once, at creation) or None.
    """
    if _load():
        return None
    user = os.environ.get("ADMIN_USER", "admin")
    pw = os.environ.get("ADMIN_PASS", "")
    generated = None
    if not pw:
        pw = secrets.token_urlsafe(12)
        generated = pw
    add_user(user, os.environ.get("ADMIN_EMAIL", ""), pw, "admin")
    return generated


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        USERS_FILE = os.path.join(d, "users.json")
        assert list_users() == []
        add_user("alice", "a@x.com", "hunter2", "admin")
        assert verify("alice", "hunter2")["role"] == "admin"
        # wrong password and unknown user must both return None (no enumeration)
        assert verify("alice", "wrong") is None
        assert verify("ghost", "hunter2") is None
        # token roundtrip + rotation revokes
        tok = _load()["alice"]["token"]
        assert user_by_token(tok)["username"] == "alice"
        new = rotate_token("alice")
        assert user_by_token(tok) is None and user_by_token(new)["username"] == "alice"
        # public view leaks no secrets
        assert set(list_users()[0]) == {"username", "email", "role"}
        set_password("alice", "newpass")
        assert verify("alice", "newpass") and verify("alice", "hunter2") is None
        delete_user("alice")
        assert list_users() == []
        # bootstrap
        os.environ["ADMIN_USER"], os.environ["ADMIN_PASS"] = "root", "toor"
        ensure_admin()
        assert verify("root", "toor")["role"] == "admin"
        ensure_admin()  # idempotent — must not throw or duplicate
        assert len(list_users()) == 1
        print("users store ok")
