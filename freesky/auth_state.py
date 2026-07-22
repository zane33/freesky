"""Auth state and page guards.

Kept separate from pages/auth.py because the navbar needs AuthState while the
login page needs the navbar — importing across those two directly would be a
cycle.
"""
import reflex as rx

from rxconfig import api_url

from freesky import users, app_settings


class AuthState(rx.State):
    # Cookie survives restarts; the token is re-validated against users.json on
    # every use, so deleting a user or rotating their token locks them out
    # immediately regardless of what cookie the browser still holds.
    session_token: str = rx.Cookie(name="fs_session", max_age=60 * 60 * 24 * 30, same_site="lax")
    error: str = ""

    @rx.var
    def current_user(self) -> dict:
        u = users.user_by_token(self.session_token) if self.session_token else None
        # Only what the UI needs — never salt or hash.
        return {"username": u["username"], "role": u["role"], "token": u["token"]} if u else {}

    @rx.var
    def client_ip(self) -> str:
        """Caller's address.

        Reflex already unrolls x-forwarded-for into session.client_ip (app.py),
        so take that rather than re-parsing headers — router.headers is a typed
        object, not a dict, and coercing it was silently yielding "".
        """
        try:
            return self.router.session.client_ip or ""
        except Exception:
            return ""

    @rx.var
    def is_trusted_network(self) -> bool:
        """On a whitelisted subnet — may browse and watch without signing in."""
        return app_settings.is_trusted_ip(self.client_ip)

    @rx.var
    def is_authenticated(self) -> bool:
        return bool(self.current_user)

    @rx.var
    def has_access(self) -> bool:
        """Signed in, or on the LAN. Gates viewing, never administration."""
        return self.is_authenticated or self.is_trusted_network

    @rx.var
    def is_admin(self) -> bool:
        return self.current_user.get("role") == "admin"

    @rx.var
    def username(self) -> str:
        return self.current_user.get("username", "")

    @rx.var
    def stream_token(self) -> str:
        return self.current_user.get("token", "")

    @rx.var
    def playlist_url(self) -> str:
        """This user's personal playlist URL — paste straight into Dispatcharr."""
        token = self.stream_token
        return f"{api_url}/playlist.m3u8" + (f"?token={token}" if token else "")

    @rx.var
    def epg_url(self) -> str:
        token = self.stream_token
        return f"{api_url}/epg.xml" + (f"?token={token}" if token else "")

    @rx.event
    def login(self, form: dict):
        user = users.verify(form.get("username", ""), form.get("password", ""))
        if not user:
            # One message for every failure mode — wrong password and unknown
            # account are indistinguishable, and users.verify spends equal time
            # on both so timing doesn't leak either.
            self.error = "Invalid username or password"
            return
        self.session_token = user["token"]
        self.error = ""
        return rx.redirect("/")

    @rx.event
    def logout(self):
        self.session_token = ""
        return rx.redirect("/login")


async def require_login(state: rx.State):
    """Redirect to /login unless signed in, or coming from a trusted subnet.

    Reflex renders the page shell before on_load runs, so there's a brief flash
    of empty layout before the redirect — but channel data only loads after this
    returns None, so no content leaks.
    """
    auth = await state.get_state(AuthState)
    if auth.is_authenticated or auth.is_trusted_network:
        return None
    return rx.redirect("/login")


async def require_admin(state: rx.State):
    """Redirect non-admins away. Standard users may watch, never configure.

    The trusted-subnet bypass deliberately does NOT grant admin: being on the LAN
    lets you watch without logging in, it does not let you manage users. An admin
    must actually sign in to change settings.
    """
    auth = await state.get_state(AuthState)
    if not auth.is_authenticated:
        return rx.redirect("/login")
    if not auth.is_admin:
        return rx.redirect("/")
    return None
