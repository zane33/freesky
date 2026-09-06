"""Channel enable/disable settings.

Runs in the same process as the FastAPI backend, so it reads and writes
`channel_prefs` directly instead of going back out over HTTP.
"""
import httpx
import reflex as rx
from urllib.parse import urlparse
from typing import List

from rxconfig import api_url, backend_port

from freesky import backend, channel_prefs, users, app_settings, drm_providers
from freesky.free_sky import Channel
from freesky.components import navbar
from freesky.auth_state import AuthState, require_admin
from freesky.free_sky_hybrid import StepDaddyHybrid
from freesky.drm_providers import DRMProviderError
from freesky.drm_import import parse_license_key

# "Auto" is a sentinel in the dropdown, stored as "" (no pin) on disk. The real
# options come from the resolver so the two can't drift apart.
AUTO_SOURCE = "Auto (failover)"
SOURCE_OPTIONS = [AUTO_SOURCE] + list(StepDaddyHybrid.PLAYER_PATHS)

# Dropdown options for the DRM form. Taken from drm_providers so the UI can't
# offer a value the validator will then reject.
KEY_SYSTEM_OPTIONS = list(drm_providers.KEY_SYSTEMS)
MANIFEST_TYPE_OPTIONS = list(drm_providers.MANIFEST_TYPES)
WRAP_OPTIONS = ["raw", "base64", "json:license", "json:payload"]

# The test endpoint lives in the same process, but it is a FastAPI route rather
# than something importable from here, so the page dials it over loopback. Not
# api_url: that is the client-facing origin and may not resolve from inside the
# container.
BACKEND_ORIGIN = f"http://127.0.0.1:{backend_port}"


def _parse_header_lines(text: str) -> dict:
    """Turn the header textarea into a {name: value} dict.

    One "Name: value" per line; blank lines and lines without a colon are
    ignored. Deliberately lenient about whitespace — the strict check is
    drm_providers.validate_provider, which is the single source of truth.
    """
    headers = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        if name:
            headers[name] = value.strip()
    return headers


class SettingsState(rx.State):
    """Channel visibility settings, persisted server-side for every client."""

    channels: List[Channel] = []
    disabled: List[str] = []
    search: str = ""
    refreshing: bool = False

    # User management (admin only — the page itself is admin-gated)
    users: List[dict] = []
    user_error: str = ""

    # Subnets that may browse and stream without signing in
    trusted_networks: str = ""
    network_error: str = ""

    # Playlist URL revealed for one user at a time (see copy_playlist_url)
    revealed_user: str = ""
    revealed_url: str = ""

    # Per-channel upstream source pins, {channel_id: player}
    sources: dict = {}

    # Paging. Rendering all 900 rows at once made Reflex re-diff the whole list on
    # every state change, which is what made rows flicker and vanish.
    page: int = 0
    PAGE_SIZE: int = 50

    # --- DRM providers ------------------------------------------------------
    # Never holds license_headers. Header VALUES are secrets and this state is
    # serialised to the browser over the Reflex socket; only the header NAMES
    # travel, so the admin can see that a credential exists without it being
    # re-transmitted every time the page loads.
    drm_list: List[dict] = []
    drm_error: str = ""

    # Form. drm_editing is "" when adding, otherwise the name being edited.
    drm_editing: str = ""
    drm_name: str = ""
    drm_key_system: str = KEY_SYSTEM_OPTIONS[0]
    drm_license_url: str = ""
    drm_manifest_url: str = ""
    drm_manifest_type: str = MANIFEST_TYPE_OPTIONS[0]
    drm_request_wrap: str = "raw"
    drm_response_unwrap: str = "raw"
    drm_enabled: bool = True
    # Write-only: blank on an edit means "keep the stored headers".
    drm_headers_input: str = ""
    drm_clear_headers: bool = False

    # --- license-key import -------------------------------------------------
    # Parse and save are two steps: parsing must never write. The full parsed
    # record lives in a backend-only var (leading underscore => Reflex does not
    # serialise it), because it carries license_headers VALUES. Only the
    # projection in the drm_preview_* vars below reaches the browser, and that
    # deliberately carries header NAMES only.
    _drm_import_record: dict = {}

    drm_import_open: bool = False
    drm_import_key: str = ""
    drm_import_name: str = ""
    drm_import_manifest_url: str = ""
    drm_import_manifest_type: str = MANIFEST_TYPE_OPTIONS[0]
    drm_import_key_system: str = KEY_SYSTEM_OPTIONS[0]
    drm_import_error: str = ""

    drm_import_parsed: bool = False
    drm_preview_name: str = ""
    drm_preview_license_url: str = ""
    drm_preview_request_wrap: str = ""
    drm_preview_response_unwrap: str = ""
    drm_preview_header_names: str = ""

    drm_confirm_delete: str = ""
    drm_testing: str = ""
    drm_test_target: str = ""
    drm_test_ok: bool = False
    drm_test_message: str = ""

    @rx.var
    def matching(self) -> List[Channel]:
        """Channels matching the search box, before paging."""
        if not self.search:
            return self.channels
        q = self.search.lower()
        return [c for c in self.channels if q in c.name.lower()]

    @rx.var
    def page_count(self) -> int:
        total = len(self.matching)
        return max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    @rx.var
    def visible(self) -> List[Channel]:
        """The current page of channels."""
        start = self.page * self.PAGE_SIZE
        return self.matching[start:start + self.PAGE_SIZE]

    @rx.var
    def page_label(self) -> str:
        return f"Page {self.page + 1} of {self.page_count} ({len(self.matching)} channels)"

    @rx.var
    def enable_all_label(self) -> str:
        return f"Enable all {len(self.matching)}"

    @rx.var
    def disable_all_label(self) -> str:
        return f"Disable all {len(self.matching)}"

    @rx.var
    def enabled_count(self) -> int:
        # Count real channels, don't subtract list lengths: `disabled` can hold ids
        # that are no longer in the channel list, which produced counts like
        # "-893 of 5" when the backend was still loading.
        off = set(self.disabled)
        return sum(1 for c in self.channels if c.id not in off)

    @rx.var
    def summary(self) -> str:
        if not self.channels:
            return "Channels are still loading from upstream — try Refresh in a moment"
        return f"{self.enabled_count} of {len(self.channels)} channels in the playlist"

    @rx.event
    async def on_load(self):
        redirect = await require_admin(self)
        if redirect is not None:
            return redirect
        # Reflex re-runs on_load after a socket reconnect. Don't blank a populated
        # list if the backend momentarily returns nothing.
        channels = backend.get_channels()
        if channels:
            self.channels = channels
        self.disabled = sorted(channel_prefs.disabled_ids())
        self.users = users.list_users()
        self.trusted_networks = ", ".join(app_settings.trusted_networks())
        self.sources = channel_prefs.sources()
        self._load_drm()

    @rx.event
    async def refresh(self):
        """Re-scrape the channel list from upstream, then reload."""
        self.refreshing = True
        yield
        try:
            await backend.free_sky.load_channels()
        except Exception as e:
            print(f"Settings refresh failed: {e}")
        self.channels = backend.get_channels()
        self.refreshing = False
        yield rx.toast(f"{len(self.channels)} channels loaded")

    @rx.event
    def set_search(self, query: str):
        self.search = query
        self.page = 0  # otherwise a narrow search lands on an empty page

    @rx.event
    def next_page(self):
        if self.page + 1 < self.page_count:
            self.page += 1

    @rx.event
    def prev_page(self):
        if self.page > 0:
            self.page -= 1

    @rx.event
    def set_source(self, channel_id: str, player: str):
        """Pin a channel to one upstream source, or back to automatic."""
        value = "" if player == AUTO_SOURCE else player
        channel_prefs.set_source(channel_id, value)
        self.sources = channel_prefs.sources()

    @rx.event
    def toggle(self, channel_id: str):
        """Flip one channel. Saved immediately — a Save button would only add a
        way to lose the change."""
        if channel_id in self.disabled:
            self.disabled.remove(channel_id)
        else:
            self.disabled.append(channel_id)
        channel_prefs.set_disabled(self.disabled)

    @rx.event
    def add_user(self, form: dict):
        try:
            users.add_user(
                form.get("username", ""),
                form.get("email", ""),
                form.get("password", ""),
                form.get("role", "standard"),
            )
            self.user_error = ""
        except Exception as e:
            self.user_error = str(e)
            return
        self.users = users.list_users()

    @rx.event
    def remove_user(self, username: str):
        # Refuse to delete the last admin, which would lock everyone out of settings.
        remaining = [u for u in users.list_users()
                     if u["role"] == "admin" and u["username"] != username]
        if not remaining:
            self.user_error = "Cannot remove the last admin"
            return
        users.delete_user(username)
        self.user_error = ""
        self.users = users.list_users()

    @rx.event
    def rotate_user_token(self, username: str):
        """Invalidates that user's existing playlist URL everywhere."""
        users.rotate_token(username)
        self.users = users.list_users()
        return rx.toast(f"New playlist URL issued for {username}; the old one no longer works")

    @rx.event
    def copy_playlist_url(self, username: str):
        """Reveal the user's playlist URL, and copy it where that's possible.

        navigator.clipboard only exists in a secure context (HTTPS or localhost).
        This app is normally reached over plain HTTP on a LAN address, where the
        copy silently does nothing — so the URL is displayed for manual selection
        and the clipboard write is a bonus, not the mechanism.
        """
        token = users.token_for(username)
        if not token:
            self.revealed_user = ""
            self.revealed_url = ""
            return rx.toast("User not found")
        # Same origin the admin is browsing from, so the copied URL works for
        # whoever they're handing it to rather than naming the LAN address.
        origin = api_url
        try:
            parsed = urlparse(str(self.router.url))
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass
        url = f"{origin}/playlist.m3u8?token={token}"
        # Toggle off if the same row is clicked again.
        if self.revealed_user == username:
            self.revealed_user = ""
            self.revealed_url = ""
            return
        self.revealed_user = username
        self.revealed_url = url
        return rx.set_clipboard(url)

    @rx.event
    def hide_playlist_url(self):
        self.revealed_user = ""
        self.revealed_url = ""

    @rx.event
    def set_trusted_networks(self, value: str):
        self.trusted_networks = value

    @rx.event
    def save_trusted_networks(self):
        try:
            saved = app_settings.set_trusted_networks(self.trusted_networks.split(","))
        except ValueError as e:
            self.network_error = f"Not a valid network: {e}"
            return
        self.network_error = ""
        self.trusted_networks = ", ".join(saved)
        return rx.toast(
            f"{len(saved)} trusted network(s) saved" if saved
            else "Whitelist cleared — everyone must now sign in"
        )

    @rx.event
    def set_all(self, enabled: bool):
        """Enable/disable every channel matching the current filter.

        Acts on the whole filtered set, not just the visible page — but the
        buttons name the count so it can't silently wipe all 900 the way an
        unlabelled "Disable shown" did.
        """
        affected = {c.id for c in self.matching}
        disabled = set(self.disabled) - affected if enabled else set(self.disabled) | affected
        self.disabled = sorted(disabled)
        channel_prefs.set_disabled(self.disabled)
        return rx.toast(
            f"{'Enabled' if enabled else 'Disabled'} {len(affected)} channel(s)"
        )

    # --- DRM providers ------------------------------------------------------

    def _load_drm(self) -> None:
        """Refresh drm_list from disk, header values stripped.

        Underscore-prefixed on purpose. Reflex wraps every PUBLIC state method in
        an EventHandler, so a public `load_drm` would return an EventSpec when
        called as `self.load_drm()` from another handler — the body would never
        run and the list would silently stay empty.
        """
        rows = []
        for provider in drm_providers.list_providers():
            public = drm_providers.public_provider(provider)
            # license_url is not secret and the admin has to be able to see what
            # they typed; the headers are, so only their names come through.
            public["license_url"] = provider["license_url"]
            public["request_wrap"] = provider["request_wrap"]
            public["response_unwrap"] = provider["response_unwrap"]
            public["header_names"] = ", ".join(sorted(provider["license_headers"]))
            rows.append(public)
        self.drm_list = rows

    @rx.event
    def set_drm_name(self, value: str):
        self.drm_name = value

    @rx.event
    def set_drm_key_system(self, value: str):
        self.drm_key_system = value

    @rx.event
    def set_drm_license_url(self, value: str):
        self.drm_license_url = value

    @rx.event
    def set_drm_manifest_url(self, value: str):
        self.drm_manifest_url = value

    @rx.event
    def set_drm_manifest_type(self, value: str):
        self.drm_manifest_type = value

    @rx.event
    def set_drm_request_wrap(self, value: str):
        self.drm_request_wrap = value

    @rx.event
    def set_drm_response_unwrap(self, value: str):
        self.drm_response_unwrap = value

    @rx.event
    def set_drm_enabled(self, value: bool):
        self.drm_enabled = value

    @rx.event
    def set_drm_headers_input(self, value: str):
        self.drm_headers_input = value
        # Typing a replacement and also ticking "clear" is contradictory; the
        # typed value wins.
        if value.strip():
            self.drm_clear_headers = False

    @rx.event
    def set_drm_clear_headers(self, value: bool):
        self.drm_clear_headers = value
        if value:
            self.drm_headers_input = ""

    @rx.event
    def reset_drm_form(self):
        """Back to a blank 'add provider' form."""
        self.drm_editing = ""
        self.drm_name = ""
        self.drm_key_system = KEY_SYSTEM_OPTIONS[0]
        self.drm_license_url = ""
        self.drm_manifest_url = ""
        self.drm_manifest_type = MANIFEST_TYPE_OPTIONS[0]
        self.drm_request_wrap = "raw"
        self.drm_response_unwrap = "raw"
        self.drm_enabled = True
        self.drm_headers_input = ""
        self.drm_clear_headers = False
        self.drm_error = ""

    @rx.event
    def edit_drm(self, name: str):
        """Load a provider into the form. Header values are deliberately left
        blank — they are not sent to the browser, so they cannot be pre-filled."""
        provider = drm_providers.get_provider(name)
        if provider is None:
            self.drm_error = f"Provider '{name}' no longer exists"
            self._load_drm()
            return
        self.drm_editing = provider["name"]
        self.drm_name = provider["name"]
        self.drm_key_system = provider["key_system"]
        self.drm_license_url = provider["license_url"]
        self.drm_manifest_url = provider["manifest_url"]
        self.drm_manifest_type = provider["manifest_type"]
        self.drm_request_wrap = provider["request_wrap"]
        self.drm_response_unwrap = provider["response_unwrap"]
        self.drm_enabled = provider["enabled"]
        self.drm_headers_input = ""
        self.drm_clear_headers = False
        self.drm_error = ""

    @rx.event
    def save_drm(self):
        """Validate and persist the form.

        Header handling on an edit: a blank textarea keeps whatever is stored,
        because the form never received the values and submitting {} would
        silently drop the credential. "Clear stored headers" is the explicit way
        to remove them.
        """
        typed = _parse_header_lines(self.drm_headers_input)
        if typed:
            headers = typed
        elif self.drm_clear_headers or not self.drm_editing:
            headers = {}
        else:
            existing = drm_providers.get_provider(self.drm_editing)
            headers = existing["license_headers"] if existing else {}

        record = {
            "name": self.drm_name,
            "key_system": self.drm_key_system,
            "license_url": self.drm_license_url,
            "license_headers": headers,
            "request_wrap": self.drm_request_wrap,
            "response_unwrap": self.drm_response_unwrap,
            "manifest_url": self.drm_manifest_url,
            "manifest_type": self.drm_manifest_type,
            "enabled": self.drm_enabled,
        }
        try:
            saved = drm_providers.upsert_provider(record)
        except DRMProviderError as e:
            self.drm_error = str(e)
            return
        # Renaming means the old record is now orphaned — drop it so an edit
        # doesn't quietly leave a duplicate behind.
        if self.drm_editing and self.drm_editing != saved["name"]:
            drm_providers.delete_provider(self.drm_editing)
        self.reset_drm_form()
        self._load_drm()
        return rx.toast(f"Saved DRM provider '{saved['name']}'")

    @rx.event
    def toggle_drm_enabled(self, name: str):
        """Flip one provider on or off without opening the edit form."""
        provider = drm_providers.get_provider(name)
        if provider is None:
            self._load_drm()
            return
        provider["enabled"] = not provider["enabled"]
        try:
            drm_providers.upsert_provider(provider)
            self.drm_error = ""
        except DRMProviderError as e:
            self.drm_error = str(e)
        self._load_drm()

    # --- import from a Kodi license key string ------------------------------

    def _clear_drm_preview(self) -> None:
        """Drop the parsed record and its preview.

        Plain method, not an event: it exists so no code path can leave a stale
        record (and its header values) sitting in state after an error.
        """
        self._drm_import_record = {}
        self.drm_import_parsed = False
        self.drm_preview_name = ""
        self.drm_preview_license_url = ""
        self.drm_preview_request_wrap = ""
        self.drm_preview_response_unwrap = ""
        self.drm_preview_header_names = ""

    def _set_drm_preview(self, record: dict) -> None:
        """Project a parsed record into the browser-visible preview vars.

        Header NAMES only, for the same reason `_load_drm` strips them: this state
        is serialised over the Reflex socket, and a license header value is a
        credential.
        """
        self._drm_import_record = record
        self.drm_import_parsed = True
        self.drm_preview_name = record["name"]
        self.drm_preview_license_url = record["license_url"]
        self.drm_preview_request_wrap = record["request_wrap"]
        self.drm_preview_response_unwrap = record["response_unwrap"]
        self.drm_preview_header_names = (
            ", ".join(sorted(record["license_headers"])) or "none"
        )

    @rx.event
    def toggle_drm_import(self):
        """Show or hide the import box, discarding any pending parse on close."""
        self.drm_import_open = not self.drm_import_open
        if not self.drm_import_open:
            self.reset_drm_import()

    @rx.event
    def reset_drm_import(self):
        """Empty the import form and forget the parsed record."""
        self.drm_import_key = ""
        self.drm_import_name = ""
        self.drm_import_manifest_url = ""
        self.drm_import_manifest_type = MANIFEST_TYPE_OPTIONS[0]
        self.drm_import_key_system = KEY_SYSTEM_OPTIONS[0]
        self.drm_import_error = ""
        self._clear_drm_preview()

    @rx.event
    def set_drm_import_key(self, value: str):
        self.drm_import_key = value
        # The preview describes the string that was parsed; editing it makes the
        # preview a lie, so retract it and make them press Parse again.
        self._clear_drm_preview()

    @rx.event
    def set_drm_import_name(self, value: str):
        self.drm_import_name = value
        self._clear_drm_preview()

    @rx.event
    def set_drm_import_manifest_url(self, value: str):
        self.drm_import_manifest_url = value
        self._clear_drm_preview()

    @rx.event
    def set_drm_import_manifest_type(self, value: str):
        self.drm_import_manifest_type = value
        self._clear_drm_preview()

    @rx.event
    def set_drm_import_key_system(self, value: str):
        self.drm_import_key_system = value
        self._clear_drm_preview()

    @rx.event
    def parse_drm_import(self):
        """Parse the pasted license key into a preview. Writes nothing to disk.

        The parser validates the whole record, so a preview appearing at all
        means the subsequent save will be accepted.
        """
        try:
            record = parse_license_key(
                self.drm_import_key,
                name=self.drm_import_name,
                manifest_url=self.drm_import_manifest_url,
                manifest_type=self.drm_import_manifest_type,
                key_system=self.drm_import_key_system,
                enabled=True,
            )
        except DRMProviderError as e:
            self.drm_import_error = str(e)
            self._clear_drm_preview()
            return
        self.drm_import_error = ""
        self._set_drm_preview(record)

    @rx.event
    def save_drm_import(self):
        """Persist the previewed record. Only reachable once Parse has succeeded."""
        record = dict(self._drm_import_record)
        if not record:
            self.drm_import_error = "Parse the license key before saving."
            return
        try:
            saved = drm_providers.upsert_provider(record)
        except DRMProviderError as e:
            # The parser already validated, so this means the record was edited
            # underneath us — surface it rather than saving something partial.
            self.drm_import_error = str(e)
            return
        self.reset_drm_import()
        self.drm_import_open = False
        self._load_drm()
        return rx.toast(f"Imported DRM provider '{saved['name']}'")

    @rx.event
    def ask_delete_drm(self, name: str):
        """Arm the confirm buttons for one row. Deleting drops a stored license
        credential, so it isn't a single click."""
        self.drm_confirm_delete = name

    @rx.event
    def cancel_delete_drm(self):
        self.drm_confirm_delete = ""

    @rx.event
    def confirm_delete_drm(self, name: str):
        removed = drm_providers.delete_provider(name)
        self.drm_confirm_delete = ""
        if self.drm_editing == name:
            self.reset_drm_form()
        self._load_drm()
        return rx.toast(
            f"Deleted DRM provider '{name}'" if removed else f"'{name}' was already gone"
        )

    @rx.event
    async def test_drm(self, name: str):
        """Ask the backend to exercise this provider's manifest and license URLs.

        The endpoint answers {ok, code, message}; nothing about the exchange is
        rendered beyond that, so a license response can't leak into the page.
        The admin's own session token is forwarded because this call originates
        server-side and carries none of the browser's cookies.
        """
        self.drm_testing = name
        self.drm_test_target = name
        self.drm_test_ok = False
        self.drm_test_message = ""
        yield
        try:
            auth = await self.get_state(AuthState)
            token = auth.session_token or ""
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
                response = await client.post(
                    f"{BACKEND_ORIGIN}/api/drm/test/{name}",
                    params={"token": token} if token else None,
                    cookies={"fs_session": token} if token else None,
                )
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            self.drm_test_ok = bool(body.get("ok"))
            code = body.get("code", response.status_code)
            message = str(body.get("message") or f"HTTP {response.status_code}")
            self.drm_test_message = f"{code}: {message}" if code else message
        except Exception as e:
            # Includes the case where the endpoint isn't deployed yet.
            self.drm_test_ok = False
            self.drm_test_message = f"Test failed: {e}"
        finally:
            self.drm_testing = ""


def user_row(user: dict) -> rx.Component:
    revealed = SettingsState.revealed_user == user["username"]
    return rx.vstack(
        rx.hstack(
            rx.badge(
                user["role"],
                color_scheme=rx.cond(user["role"] == "admin", "red", "gray"),
                variant="soft",
            ),
            rx.text(user["username"], size="3", weight="medium"),
            rx.text(user["email"], size="1", color="gray", flex="1", no_of_lines=1),
            rx.button(
                rx.icon("link", size=14),
                rx.cond(revealed, "Hide URL", "Playlist URL"),
                on_click=lambda: SettingsState.copy_playlist_url(user["username"]),
                size="1",
                variant="soft",
            ),
            rx.button(
                rx.icon("rotate-cw", size=14),
                on_click=lambda: SettingsState.rotate_user_token(user["username"]),
                size="1",
                variant="soft",
                color_scheme="amber",
                title="Issue a new playlist URL and revoke the old one",
            ),
            rx.button(
                rx.icon("trash-2", size=14),
                on_click=lambda: SettingsState.remove_user(user["username"]),
                size="1",
                variant="soft",
                color_scheme="red",
            ),
            align="center",
            spacing="2",
            width="100%",
        ),
        # Shown rather than only copied: navigator.clipboard is unavailable over
        # plain HTTP on a LAN address, so the copy is silently a no-op there.
        rx.cond(
            revealed,
            rx.vstack(
                rx.text(
                    "Select and copy this into Dispatcharr:",
                    size="1",
                    color="gray",
                ),
                rx.input(
                    value=SettingsState.revealed_url,
                    read_only=True,
                    on_click=rx.call_script(
                        "document.activeElement && document.activeElement.select()"
                    ),
                    font_family="mono",
                    font_size="12px",
                    width="100%",
                ),
                spacing="1",
                width="100%",
                padding_bottom="0.5rem",
            ),
            rx.fragment(),
        ),
        spacing="1",
        width="100%",
        padding_y="0.4rem",
        border_bottom="1px solid var(--gray-4)",
    )


def access_section() -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.heading("Trusted networks", size="5"),
            rx.text(
                "Comma-separated CIDRs. Clients in these ranges reach the app and "
                "the playlist without signing in — useful for a LAN or for "
                "Dispatcharr on a fixed host. Everyone else must log in. Being on a "
                "trusted network never grants admin; managing settings always "
                "requires signing in. Leave empty to require login from everywhere.",
                color="gray",
                size="2",
            ),
            rx.cond(
                SettingsState.network_error != "",
                rx.callout(SettingsState.network_error, icon="triangle_alert",
                           color_scheme="red", size="1", width="100%"),
            ),
            rx.hstack(
                rx.input(
                    value=SettingsState.trusted_networks,
                    on_change=SettingsState.set_trusted_networks,
                    placeholder="192.168.3.0/24, 10.0.0.0/8",
                    flex="1",
                ),
                rx.button("Save", on_click=SettingsState.save_trusted_networks),
                width="100%",
                spacing="2",
            ),
            spacing="3",
            width="100%",
        ),
        width="100%",
    )


def users_section() -> rx.Component:
    return rx.card(
        rx.vstack(
            rx.heading("Users", size="5"),
            rx.text(
                "Admins can reach these settings. Standard users can sign in and "
                "watch channels only. Each user gets their own playlist URL that "
                "can be revoked independently.",
                color="gray",
                size="2",
            ),
            rx.cond(
                SettingsState.user_error != "",
                rx.callout(SettingsState.user_error, icon="triangle_alert",
                           color_scheme="red", size="1", width="100%"),
            ),
            rx.vstack(
                rx.foreach(SettingsState.users, user_row),
                spacing="0",
                width="100%",
            ),
            rx.divider(),
            rx.form(
                rx.hstack(
                    rx.input(name="username", placeholder="Username", required=True, flex="1"),
                    rx.input(name="email", placeholder="Email (optional)", flex="1"),
                    rx.input(name="password", placeholder="Password", type="password",
                             required=True, flex="1"),
                    rx.select(
                        ["standard", "admin"],
                        name="role",
                        default_value="standard",
                        width="130px",
                    ),
                    rx.button("Add user", type="submit"),
                    spacing="2",
                    width="100%",
                    align="center",
                ),
                on_submit=SettingsState.add_user,
                reset_on_submit=True,
                width="100%",
            ),
            spacing="3",
            width="100%",
        ),
        width="100%",
    )


def channel_row(channel: Channel) -> rx.Component:
    enabled = ~SettingsState.disabled.contains(channel.id)
    return rx.hstack(
        rx.switch(
            checked=enabled,
            on_change=lambda _: SettingsState.toggle(channel.id),
        ),
        rx.image(
            src=channel.logo,
            width="32px",
            height="32px",
            object_fit="contain",
            # Without this the browser asks for all 900 logos at once and a cold
            # cache means each one is an upstream round-trip.
            loading="lazy",
        ),
        rx.text(
            channel.name,
            size="3",
            weight="medium",
            color=rx.cond(enabled, "inherit", "gray"),
            flex="1",
            no_of_lines=1,
        ),
        rx.select(
            SOURCE_OPTIONS,
            value=SettingsState.sources.get(channel.id, AUTO_SOURCE),
            on_change=lambda v: SettingsState.set_source(channel.id, v),
            size="1",
            width="120px",
        ),
        rx.text(channel.id, size="1", color="gray", width="42px", text_align="right"),
        align="center",
        spacing="3",
        width="100%",
        padding_y="0.4rem",
        border_bottom="1px solid var(--gray-4)",
    )


def drm_provider_row(provider: dict) -> rx.Component:
    """One stored provider: status, endpoints, and the row's actions."""
    confirming = SettingsState.drm_confirm_delete == provider["name"]
    tested = SettingsState.drm_test_target == provider["name"]
    return rx.vstack(
        rx.hstack(
            rx.switch(
                # Explicit cast: a value indexed out of a dict Var is untyped,
                # and rx.switch needs a boolean Var.
                checked=provider["enabled"].to(bool),
                on_change=lambda _: SettingsState.toggle_drm_enabled(provider["name"]),
            ),
            rx.text(provider["name"], size="3", weight="medium"),
            rx.badge(provider["key_system"], variant="soft"),
            rx.badge(provider["manifest_type"], variant="soft", color_scheme="gray"),
            rx.cond(
                provider["has_license_headers"],
                rx.badge(
                    rx.icon("key-round", size=12),
                    provider["header_names"],
                    variant="soft",
                    color_scheme="amber",
                ),
                rx.badge("no headers", variant="soft", color_scheme="gray"),
            ),
            rx.spacer(),
            rx.button(
                rx.icon("flask-conical", size=14),
                "Test",
                on_click=lambda: SettingsState.test_drm(provider["name"]),
                loading=SettingsState.drm_testing == provider["name"],
                size="1",
                variant="soft",
            ),
            rx.button(
                rx.icon("pencil", size=14),
                on_click=lambda: SettingsState.edit_drm(provider["name"]),
                size="1",
                variant="soft",
                title="Edit this provider",
            ),
            rx.cond(
                confirming,
                rx.hstack(
                    rx.button(
                        "Delete",
                        on_click=lambda: SettingsState.confirm_delete_drm(provider["name"]),
                        size="1",
                        color_scheme="red",
                    ),
                    rx.button(
                        "Cancel",
                        on_click=SettingsState.cancel_delete_drm,
                        size="1",
                        variant="soft",
                    ),
                    spacing="1",
                ),
                rx.button(
                    rx.icon("trash-2", size=14),
                    on_click=lambda: SettingsState.ask_delete_drm(provider["name"]),
                    size="1",
                    variant="soft",
                    color_scheme="red",
                    title="Delete this provider",
                ),
            ),
            align="center",
            spacing="2",
            width="100%",
        ),
        rx.text(
            provider["manifest_url"],
            size="1",
            color="gray",
            no_of_lines=1,
            font_family="mono",
            width="100%",
        ),
        # The test result belongs beside the provider it was run against, not in
        # a toast that vanishes before it can be read.
        rx.cond(
            tested & (SettingsState.drm_test_message != ""),
            rx.callout(
                SettingsState.drm_test_message,
                icon=rx.cond(SettingsState.drm_test_ok, "circle_check", "triangle_alert"),
                color_scheme=rx.cond(SettingsState.drm_test_ok, "green", "red"),
                size="1",
                width="100%",
            ),
            rx.fragment(),
        ),
        spacing="1",
        width="100%",
        padding_y="0.4rem",
        border_bottom="1px solid var(--gray-4)",
    )


def drm_import_preview() -> rx.Component:
    """What the pasted license key resolved to, before anything is saved.

    Header names only — the values stay server-side (see `_set_drm_preview`).
    """
    def field(label: str, value) -> rx.Component:
        return rx.hstack(
            rx.text(label, size="1", color="gray", width="130px", flex_shrink="0"),
            rx.text(value, size="1", font_family="mono", no_of_lines=1, flex="1"),
            align="center",
            spacing="2",
            width="100%",
        )

    return rx.vstack(
        rx.hstack(
            rx.icon("circle_check", size=14, color="var(--green-9)"),
            rx.text("Parsed — nothing saved yet", size="2", weight="medium"),
            align="center",
            spacing="2",
        ),
        field("Name", SettingsState.drm_preview_name),
        field("License URL", SettingsState.drm_preview_license_url),
        field("Request wrap", SettingsState.drm_preview_request_wrap),
        field("Response unwrap", SettingsState.drm_preview_response_unwrap),
        field("Header names", SettingsState.drm_preview_header_names),
        rx.text(
            "Header values were parsed but are held on the server and never sent "
            "to this page. Save, then use Test on the new row to confirm the "
            "license server accepts them.",
            size="1",
            color="gray",
        ),
        rx.hstack(
            rx.spacer(),
            rx.button(
                "Discard",
                on_click=SettingsState.reset_drm_import,
                variant="soft",
                type="button",
            ),
            rx.button(
                rx.icon("save", size=14),
                "Save provider",
                on_click=SettingsState.save_drm_import,
            ),
            align="center",
            spacing="2",
            width="100%",
        ),
        spacing="2",
        width="100%",
        padding="0.75rem",
        border="1px solid var(--gray-5)",
        border_radius="var(--radius-3)",
        background="var(--gray-2)",
    )


def drm_import_box() -> rx.Component:
    """Collapsible "import from license key" area.

    Accepts the `license_key` string from `inputstream.adaptive` — the format
    Kodi DRM add-ons already use — so a working configuration can be moved over
    by pasting one string instead of filling six fields by hand.
    """
    return rx.vstack(
        rx.hstack(
            rx.button(
                rx.icon(
                    rx.cond(SettingsState.drm_import_open, "chevron-down", "chevron-right"),
                    size=14,
                ),
                rx.icon("clipboard-paste", size=14),
                "Import from license key",
                on_click=SettingsState.toggle_drm_import,
                variant="soft",
                size="1",
                type="button",
            ),
            align="center",
            width="100%",
        ),
        rx.cond(
            SettingsState.drm_import_open,
            rx.vstack(
                rx.text(
                    "Paste the inputstream.adaptive license_key string: "
                    "license_url|headers|post_data|response. The manifest URL is "
                    "not part of that string, so give it here.",
                    size="1",
                    color="gray",
                ),
                rx.cond(
                    SettingsState.drm_import_error != "",
                    rx.callout(SettingsState.drm_import_error, icon="triangle_alert",
                               color_scheme="red", size="1", width="100%"),
                ),
                rx.text_area(
                    value=SettingsState.drm_import_key,
                    on_change=SettingsState.set_drm_import_key,
                    placeholder=(
                        "https://lic.example.com/wv|Authorization=Bearer%20abc"
                        "&Content-Type=application/octet-stream|R{SSM}|JBlicense"
                    ),
                    rows="4",
                    width="100%",
                    font_family="mono",
                    font_size="12px",
                ),
                rx.hstack(
                    rx.vstack(
                        rx.text("Name", size="1", color="gray"),
                        rx.input(
                            value=SettingsState.drm_import_name,
                            on_change=SettingsState.set_drm_import_name,
                            placeholder="sky-uk",
                            width="100%",
                        ),
                        spacing="1",
                        flex="1",
                    ),
                    rx.vstack(
                        rx.text("Key system", size="1", color="gray"),
                        rx.select(
                            KEY_SYSTEM_OPTIONS,
                            value=SettingsState.drm_import_key_system,
                            on_change=SettingsState.set_drm_import_key_system,
                            width="100%",
                        ),
                        spacing="1",
                        flex="1",
                    ),
                    rx.vstack(
                        rx.text("Manifest type", size="1", color="gray"),
                        rx.select(
                            MANIFEST_TYPE_OPTIONS,
                            value=SettingsState.drm_import_manifest_type,
                            on_change=SettingsState.set_drm_import_manifest_type,
                            width="100%",
                        ),
                        spacing="1",
                        width="120px",
                    ),
                    spacing="2",
                    width="100%",
                    align="end",
                ),
                rx.vstack(
                    rx.text("Manifest URL", size="1", color="gray"),
                    rx.input(
                        value=SettingsState.drm_import_manifest_url,
                        on_change=SettingsState.set_drm_import_manifest_url,
                        placeholder="https://cdn.example.com/stream.mpd",
                        width="100%",
                    ),
                    spacing="1",
                    width="100%",
                ),
                rx.hstack(
                    rx.spacer(),
                    rx.button(
                        rx.icon("wand-sparkles", size=14),
                        "Parse",
                        on_click=SettingsState.parse_drm_import,
                        variant="soft",
                        type="button",
                    ),
                    align="center",
                    width="100%",
                ),
                rx.cond(
                    SettingsState.drm_import_parsed,
                    drm_import_preview(),
                    rx.fragment(),
                ),
                spacing="3",
                width="100%",
            ),
            rx.fragment(),
        ),
        spacing="2",
        width="100%",
    )


def drm_form() -> rx.Component:
    """Add/edit form. Header values are write-only — see the note in the state."""
    editing = SettingsState.drm_editing != ""
    return rx.vstack(
        drm_import_box(),
        rx.divider(),
        rx.heading(
            rx.cond(editing, "Edit provider", "Add provider"),
            size="3",
        ),
        rx.hstack(
            rx.vstack(
                rx.text("Name", size="1", color="gray"),
                rx.input(
                    value=SettingsState.drm_name,
                    on_change=SettingsState.set_drm_name,
                    placeholder="sky-uk",
                    width="100%",
                ),
                spacing="1",
                flex="1",
            ),
            rx.vstack(
                rx.text("Key system", size="1", color="gray"),
                rx.select(
                    KEY_SYSTEM_OPTIONS,
                    value=SettingsState.drm_key_system,
                    on_change=SettingsState.set_drm_key_system,
                    width="100%",
                ),
                spacing="1",
                flex="1",
            ),
            rx.vstack(
                rx.text("Manifest type", size="1", color="gray"),
                rx.select(
                    MANIFEST_TYPE_OPTIONS,
                    value=SettingsState.drm_manifest_type,
                    on_change=SettingsState.set_drm_manifest_type,
                    width="100%",
                ),
                spacing="1",
                width="120px",
            ),
            spacing="2",
            width="100%",
            align="end",
        ),
        rx.vstack(
            rx.text("Manifest URL", size="1", color="gray"),
            rx.input(
                value=SettingsState.drm_manifest_url,
                on_change=SettingsState.set_drm_manifest_url,
                placeholder="https://cdn.example.com/stream.mpd",
                width="100%",
            ),
            spacing="1",
            width="100%",
        ),
        rx.vstack(
            rx.text("License URL", size="1", color="gray"),
            rx.input(
                value=SettingsState.drm_license_url,
                on_change=SettingsState.set_drm_license_url,
                placeholder="https://license.example.com/widevine",
                width="100%",
            ),
            spacing="1",
            width="100%",
        ),
        rx.hstack(
            rx.vstack(
                rx.text("Request wrap", size="1", color="gray"),
                rx.select(
                    WRAP_OPTIONS,
                    value=SettingsState.drm_request_wrap,
                    on_change=SettingsState.set_drm_request_wrap,
                    width="100%",
                ),
                spacing="1",
                flex="1",
            ),
            rx.vstack(
                rx.text("Response unwrap", size="1", color="gray"),
                rx.select(
                    WRAP_OPTIONS,
                    value=SettingsState.drm_response_unwrap,
                    on_change=SettingsState.set_drm_response_unwrap,
                    width="100%",
                ),
                spacing="1",
                flex="1",
            ),
            spacing="2",
            width="100%",
            align="end",
        ),
        rx.vstack(
            rx.text(
                "License headers — one 'Name: value' per line", size="1", color="gray"
            ),
            rx.text_area(
                value=SettingsState.drm_headers_input,
                on_change=SettingsState.set_drm_headers_input,
                placeholder="Authorization: Bearer ...\nX-Api-Key: ...",
                rows="3",
                width="100%",
                font_family="mono",
                font_size="12px",
            ),
            rx.text(
                rx.cond(
                    editing,
                    "Stored values are never sent back to this page. Leave blank "
                    "to keep them; tick Clear to remove them.",
                    "Attached server-side when FreeSky relays the license request. "
                    "Never sent to the browser.",
                ),
                size="1",
                color="gray",
            ),
            spacing="1",
            width="100%",
        ),
        rx.hstack(
            rx.checkbox(
                "Clear stored headers",
                checked=SettingsState.drm_clear_headers,
                on_change=SettingsState.set_drm_clear_headers,
                disabled=~editing,
            ),
            rx.checkbox(
                "Enabled",
                checked=SettingsState.drm_enabled,
                on_change=SettingsState.set_drm_enabled,
            ),
            rx.spacer(),
            rx.cond(
                editing,
                rx.button(
                    "Cancel",
                    on_click=SettingsState.reset_drm_form,
                    variant="soft",
                    type="button",
                ),
                rx.fragment(),
            ),
            rx.button(
                rx.cond(editing, "Save changes", "Add provider"),
                on_click=SettingsState.save_drm,
            ),
            align="center",
            spacing="3",
            width="100%",
        ),
        spacing="3",
        width="100%",
    )


def drm_section() -> rx.Component:
    """DRM provider configuration.

    Playback itself happens in the browser's own CDM via EME; this section only
    tells FreeSky where a provider's manifest and license server live and what
    credential to attach when relaying a license request.
    """
    return rx.card(
        rx.vstack(
            rx.heading("DRM Providers", size="5"),
            rx.text(
                "Sources whose streams are protected by Widevine or PlayReady. "
                "The browser's own CDM does the decryption; FreeSky relays the "
                "license request so the credential below never reaches the "
                "client. Header values are stored in plain text on the server — "
                "the file is owner-only, but treat the host as holding them.",
                color="gray",
                size="2",
            ),
            rx.cond(
                SettingsState.drm_error != "",
                rx.callout(SettingsState.drm_error, icon="triangle_alert",
                           color_scheme="red", size="1", width="100%"),
            ),
            rx.cond(
                SettingsState.drm_list.length() > 0,
                rx.vstack(
                    rx.foreach(SettingsState.drm_list, drm_provider_row),
                    spacing="0",
                    width="100%",
                ),
                rx.text("No DRM providers configured yet.", size="2", color="gray"),
            ),
            rx.divider(),
            drm_form(),
            spacing="3",
            width="100%",
        ),
        width="100%",
    )


@rx.page("/settings", on_load=SettingsState.on_load)
def settings() -> rx.Component:
    return rx.box(
        navbar(),
        rx.container(
            rx.vstack(
                rx.heading("Channel Settings", size="7"),
                rx.text(
                    "Turn channels off to hide them from the app and drop them "
                    "from playlist.m3u8. Changes save immediately.",
                    color="gray",
                ),
                rx.hstack(
                    rx.text(SettingsState.summary, size="2", weight="bold"),
                    rx.spacer(),
                    rx.button(
                        rx.icon("refresh-cw", size=16),
                        "Refresh from source",
                        on_click=SettingsState.refresh,
                        loading=SettingsState.refreshing,
                        variant="soft",
                        size="2",
                    ),
                    width="100%",
                    align="center",
                ),
                rx.hstack(
                    rx.input(
                        rx.input.slot(rx.icon("search")),
                        placeholder="Filter channels...",
                        value=SettingsState.search,
                        on_change=SettingsState.set_search,
                        flex="1",
                    ),
                    rx.button(
                        SettingsState.enable_all_label,
                        on_click=lambda: SettingsState.set_all(True),
                        variant="soft",
                        color_scheme="green",
                    ),
                    rx.button(
                        SettingsState.disable_all_label,
                        on_click=lambda: SettingsState.set_all(False),
                        variant="soft",
                        color_scheme="red",
                    ),
                    width="100%",
                    spacing="2",
                ),
                # ponytail: no inner scroll container. Paging to 50 rows already
                # keeps the page short, and a nested scrollbar inside a scrolling
                # page is worse to use than just scrolling the page.
                rx.card(
                    rx.vstack(
                        rx.foreach(SettingsState.visible, channel_row),
                        spacing="0",
                        width="100%",
                    ),
                    width="100%",
                ),
                rx.hstack(
                    rx.button(
                        rx.icon("chevron-left", size=16),
                        "Previous",
                        on_click=SettingsState.prev_page,
                        disabled=SettingsState.page == 0,
                        variant="soft",
                        size="2",
                    ),
                    rx.spacer(),
                    rx.text(SettingsState.page_label, size="2", color="gray"),
                    rx.spacer(),
                    rx.button(
                        "Next",
                        rx.icon("chevron-right", size=16),
                        on_click=SettingsState.next_page,
                        disabled=SettingsState.page + 1 >= SettingsState.page_count,
                        variant="soft",
                        size="2",
                    ),
                    width="100%",
                    align="center",
                ),
                rx.divider(margin_y="1rem"),
                access_section(),
                users_section(),
                drm_section(),
                spacing="4",
                width="100%",
            ),
            padding_top="7rem",
            padding_bottom="2rem",
            max_width="900px",
        ),
    )
