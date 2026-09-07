"""Channel enable/disable settings.

Runs in the same process as the FastAPI backend, so it reads and writes
`channel_prefs` directly instead of going back out over HTTP.
"""
import reflex as rx
from urllib.parse import urlparse
from typing import List

from rxconfig import api_url

from freesky import backend, channel_prefs, users, app_settings, virtual_channels
from freesky.free_sky import Channel
from freesky.components import navbar
from freesky.auth_state import AuthState, require_admin
from freesky.free_sky_hybrid import StepDaddyHybrid

# "Auto" is a sentinel in the dropdown, stored as "" (no pin) on disk. The real
# options come from the resolver so the two can't drift apart.
AUTO_SOURCE = "Auto (failover)"
SOURCE_OPTIONS = [AUTO_SOURCE] + list(StepDaddyHybrid.PLAYER_PATHS)

RESOLUTION_OPTIONS = list(virtual_channels.RESOLUTIONS)
FRAMERATE_OPTIONS = [str(f) for f in virtual_channels.FRAMERATES]
PRESET_OPTIONS = list(virtual_channels.PRESETS)

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

    # --- virtual channels ---------------------------------------------------
    # A virtual channel is a web page restreamed as live HLS by a headless
    # browser session. Nothing in these records is secret, so the whole record
    # can be round-tripped to the browser.
    vc_list: List[dict] = []
    vc_error: str = ""
    # The admin's own token, kept so a list refresh outside on_load can rebuild
    # each row's control_url.
    vc_token: str = ""
    vc_confirm_delete: str = ""
    # Running sessions, refreshed on demand rather than polled: each entry costs
    # a browser and an encoder, so there are never many, and a poll would keep
    # the Reflex socket busy for a panel nobody is looking at.
    vc_sessions: List[dict] = []
    # Missing binaries from the capture preflight. Turns "the stream won't
    # start" into "ffmpeg is not installed" without an admin reading logs.
    vc_missing: List[str] = []

    # Form. vc_editing is "" when adding, otherwise the name being edited.
    # Numeric fields are strings because rx.input hands back strings; they are
    # coerced once, in save_vc, by virtual_channels.validate_channel.
    vc_editing: str = ""
    vc_name: str = ""
    vc_title: str = ""
    vc_url: str = ""
    vc_resolution: str = virtual_channels.DEFAULT_RESOLUTION
    vc_framerate: str = str(virtual_channels.DEFAULT_FRAMERATE)
    vc_preset: str = virtual_channels.DEFAULT_PRESET
    vc_video_bitrate: str = "2500"
    vc_audio: bool = True
    vc_audio_bitrate: str = "128"
    vc_warmup: str = "6"
    vc_idle_timeout: str = "120"
    vc_click_selectors: str = ""
    vc_hide_selectors: str = ""
    vc_logo: str = ""
    vc_tags: str = "Virtual"
    vc_enabled: bool = True

    # --- virtual channels ---------------------------------------------------

    # Explicit setters: Reflex no longer generates implicit set_<var> events, so
    # every field bound with on_change needs one by hand.
    @rx.event
    def set_vc_name(self, value: str):
        self.vc_name = value

    @rx.event
    def set_vc_title(self, value: str):
        self.vc_title = value

    @rx.event
    def set_vc_url(self, value: str):
        self.vc_url = value

    @rx.event
    def set_vc_resolution(self, value: str):
        self.vc_resolution = value

    @rx.event
    def set_vc_framerate(self, value: str):
        self.vc_framerate = value

    @rx.event
    def set_vc_preset(self, value: str):
        self.vc_preset = value

    @rx.event
    def set_vc_video_bitrate(self, value: str):
        self.vc_video_bitrate = value

    @rx.event
    def set_vc_audio_bitrate(self, value: str):
        self.vc_audio_bitrate = value

    @rx.event
    def set_vc_warmup(self, value: str):
        self.vc_warmup = value

    @rx.event
    def set_vc_idle_timeout(self, value: str):
        self.vc_idle_timeout = value

    @rx.event
    def set_vc_click_selectors(self, value: str):
        self.vc_click_selectors = value

    @rx.event
    def set_vc_hide_selectors(self, value: str):
        self.vc_hide_selectors = value

    @rx.event
    def set_vc_logo(self, value: str):
        self.vc_logo = value

    @rx.event
    def set_vc_tags(self, value: str):
        self.vc_tags = value

    @rx.event
    def set_vc_audio(self, value: bool):
        self.vc_audio = value

    @rx.event
    def set_vc_enabled(self, value: bool):
        self.vc_enabled = value

    def _load_virtual(self, token: str = ""):
        """Re-read the store into the list the page renders.

        `control_url` is baked into each row so the Control button can be a
        plain link. It used to be a window.open() issued from an event handler,
        which browsers block as an unrequested popup: the call arrives over the
        Reflex socket, so it is not in the user-gesture call stack and the
        browser has no reason to trust it.
        """
        try:
            rows = virtual_channels.list_channels()
        except Exception as exc:  # a corrupt store must not blank the page
            self.vc_list = []
            self.vc_error = f"Could not read virtual channels: {exc}"
            return
        suffix = f"?token={token}" if token else ""
        for row in rows:
            row["control_url"] = f"/api/virtual-control/{row['name']}/panel{suffix}"
        self.vc_list = rows

    @rx.event
    def reset_vc_form(self):
        """Back to a blank 'add channel' form."""
        self.vc_editing = ""
        self.vc_name = ""
        self.vc_title = ""
        self.vc_url = ""
        self.vc_resolution = virtual_channels.DEFAULT_RESOLUTION
        self.vc_framerate = str(virtual_channels.DEFAULT_FRAMERATE)
        self.vc_preset = virtual_channels.DEFAULT_PRESET
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
        self.vc_error = ""

    @rx.event
    def edit_vc(self, name: str):
        """Load a stored channel into the form."""
        record = virtual_channels.get_channel(name)
        if record is None:
            self.vc_error = f"No virtual channel named {name}."
            return
        self.vc_editing = record["name"]
        self.vc_name = record["name"]
        self.vc_title = record["title"]
        self.vc_url = record["url"]
        self.vc_resolution = record["resolution"]
        self.vc_framerate = str(record["framerate"])
        self.vc_preset = record["preset"]
        self.vc_video_bitrate = str(record["video_bitrate"])
        self.vc_audio = record["audio"]
        self.vc_audio_bitrate = str(record["audio_bitrate"])
        self.vc_warmup = str(record["warmup"])
        self.vc_idle_timeout = str(record["idle_timeout"])
        self.vc_click_selectors = "\n".join(record["click_selectors"])
        self.vc_hide_selectors = "\n".join(record["hide_selectors"])
        self.vc_logo = record["logo"]
        self.vc_tags = ", ".join(record["tags"])
        self.vc_enabled = record["enabled"]
        self.vc_error = ""

    @rx.event
    def save_vc(self):
        """Validate and persist the form.

        A rename is a distinct operation, not an upsert: the name IS the channel
        id, so upserting under a new name would leave the old record behind as a
        duplicate channel in the playlist.
        """
        record = {
            "name": self.vc_name,
            "title": self.vc_title,
            "url": self.vc_url,
            "resolution": self.vc_resolution,
            "framerate": self.vc_framerate,
            "preset": self.vc_preset,
            "video_bitrate": self.vc_video_bitrate,
            "audio": self.vc_audio,
            "audio_bitrate": self.vc_audio_bitrate,
            "warmup": self.vc_warmup,
            "idle_timeout": self.vc_idle_timeout,
            "click_selectors": self.vc_click_selectors,
            "hide_selectors": self.vc_hide_selectors,
            "logo": self.vc_logo,
            "tags": self.vc_tags,
            "enabled": self.vc_enabled,
        }
        try:
            if self.vc_editing and self.vc_editing != self.vc_name.strip().lower():
                virtual_channels.upsert_channel({**record, "name": self.vc_editing})
                saved = virtual_channels.rename_channel(self.vc_editing, self.vc_name)
            else:
                saved = virtual_channels.upsert_channel(record)
        except virtual_channels.VirtualChannelError as exc:
            self.vc_error = str(exc)
            return
        self._load_virtual(self.vc_token)
        self.reset_vc_form()
        # Edits to geometry or URL only take effect on a fresh session, and an
        # admin who just changed the bitrate expects the next play to use it.
        yield SettingsState.stop_vc_session(saved["name"])
        yield rx.toast(f"Saved virtual channel '{saved['name']}'")

    @rx.event
    def toggle_vc_enabled(self, name: str):
        """Flip one channel on or off without opening the edit form."""
        record = virtual_channels.get_channel(name)
        if record is None:
            return
        record["enabled"] = not record["enabled"]
        try:
            virtual_channels.upsert_channel(record)
        except virtual_channels.VirtualChannelError as exc:
            self.vc_error = str(exc)
            return
        self._load_virtual(self.vc_token)
        if not record["enabled"]:
            yield SettingsState.stop_vc_session(name)

    @rx.event
    def ask_delete_vc(self, name: str):
        self.vc_confirm_delete = name

    @rx.event
    def cancel_delete_vc(self):
        self.vc_confirm_delete = ""

    @rx.event
    def confirm_delete_vc(self, name: str):
        virtual_channels.delete_channel(name)
        self.vc_confirm_delete = ""
        self._load_virtual(self.vc_token)
        yield SettingsState.stop_vc_session(name)
        yield rx.toast(f"Deleted virtual channel '{name}'")

    @rx.event
    async def refresh_vc_sessions(self):
        """Read the live session table and the capture preflight.

        The settings page runs in the same process as the backend, so this talks
        to the session manager directly rather than going back out over HTTP.
        The import is deferred because it pulls in Playwright, which an install
        that never uses this feature should not pay for.
        """
        from freesky import virtual_session

        self.vc_missing = virtual_session.preflight()
        suffix = f"?token={self.vc_token}" if self.vc_token else ""
        rows = virtual_session.manager.statuses()
        for row in rows:
            row["control_url"] = f"/api/virtual-control/{row['name']}/panel{suffix}"
        self.vc_sessions = rows

    @rx.event
    async def stop_vc_session(self, name: str):
        """Force a session down. The next request starts a fresh one."""
        from freesky import virtual_session

        await virtual_session.manager.stop(name)
        yield SettingsState.refresh_vc_sessions

    @rx.var
    def vc_form_title(self) -> str:
        return f"Edit '{self.vc_editing}'" if self.vc_editing else "Add a virtual channel"

    @rx.var
    def vc_preflight_message(self) -> str:
        if not self.vc_missing:
            return ""
        return (
            "Virtual channels cannot start: "
            + ", ".join(self.vc_missing)
            + " not installed in this container. Rebuild the image — these are "
            "installed by the Dockerfile."
        )

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
        self.vc_token = auth.stream_token
        self._load_virtual(self.vc_token)

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


def virtual_channel_row(record: dict) -> rx.Component:
    """One stored virtual channel: status, its page URL, and the row's actions."""
    confirming = SettingsState.vc_confirm_delete == record["name"]
    return rx.vstack(
        rx.hstack(
            rx.switch(
                # Explicit cast: a value indexed out of a dict Var is untyped,
                # and rx.switch needs a boolean Var.
                checked=record["enabled"].to(bool),
                on_change=lambda _: SettingsState.toggle_vc_enabled(record["name"]),
            ),
            rx.text(record["title"], size="3", weight="medium"),
            rx.badge(record["resolution"], variant="soft"),
            rx.badge(f"{record['framerate']}fps", variant="soft", color_scheme="gray"),
            rx.cond(
                record["audio"].to(bool),
                rx.badge(rx.icon("volume-2", size=12), "audio", variant="soft", color_scheme="green"),
                rx.badge(rx.icon("volume-x", size=12), "silent", variant="soft", color_scheme="gray"),
            ),
            rx.spacer(),
            rx.link(
                rx.button(
                    rx.icon("mouse-pointer-click", size=14),
                    "Control",
                    size="1",
                    variant="soft",
                    title="Open the browser session and drive it with mouse and keyboard",
                ),
                href=record["control_url"],
                is_external=True,
            ),
            rx.button(
                rx.icon("pencil", size=14),
                on_click=lambda: SettingsState.edit_vc(record["name"]),
                size="1",
                variant="soft",
                title="Edit this channel",
            ),
            rx.cond(
                confirming,
                rx.hstack(
                    rx.button(
                        "Delete",
                        on_click=lambda: SettingsState.confirm_delete_vc(record["name"]),
                        size="1",
                        color_scheme="red",
                    ),
                    rx.button(
                        "Cancel",
                        on_click=SettingsState.cancel_delete_vc,
                        size="1",
                        variant="soft",
                    ),
                    spacing="1",
                ),
                rx.button(
                    rx.icon("trash-2", size=14),
                    on_click=lambda: SettingsState.ask_delete_vc(record["name"]),
                    size="1",
                    variant="soft",
                    color_scheme="red",
                    title="Delete this channel",
                ),
            ),
            align="center",
            spacing="2",
            width="100%",
        ),
        rx.text(
            record["url"],
            size="1",
            color="gray",
            no_of_lines=1,
            font_family="mono",
            width="100%",
        ),
        spacing="1",
        width="100%",
        padding_y="0.4rem",
        border_bottom="1px solid var(--gray-4)",
    )


def virtual_session_row(session: dict) -> rx.Component:
    """One running capture: how long it has been up and whether it is healthy."""
    return rx.hstack(
        rx.cond(
            session["alive"].to(bool),
            rx.badge("live", color_scheme="green", variant="soft"),
            rx.badge("stalled", color_scheme="red", variant="soft"),
        ),
        rx.text(session["name"], size="2", weight="medium"),
        rx.text(session["resolution"], size="1", color="gray"),
        rx.text(f"up {session['uptime']}s", size="1", color="gray"),
        rx.text(f"idle {session['idle']}s", size="1", color="gray"),
        rx.spacer(),
        rx.link(
            rx.button("Control", size="1", variant="soft"),
            href=session["control_url"],
            is_external=True,
        ),
        rx.button(
            "Stop",
            on_click=lambda: SettingsState.stop_vc_session(session["name"]),
            size="1",
            variant="soft",
            color_scheme="red",
        ),
        align="center",
        spacing="2",
        width="100%",
        padding_y="0.3rem",
    )


def virtual_form() -> rx.Component:
    """Add/edit form for a virtual channel."""
    return rx.vstack(
        rx.heading(SettingsState.vc_form_title, size="3"),
        rx.hstack(
            rx.vstack(
                rx.text("Name (id)", size="1", color="gray"),
                rx.input(
                    placeholder="bbc-news",
                    value=SettingsState.vc_name,
                    on_change=SettingsState.set_vc_name,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text("Display name", size="1", color="gray"),
                rx.input(
                    placeholder="BBC News",
                    value=SettingsState.vc_title,
                    on_change=SettingsState.set_vc_title,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
        rx.vstack(
            rx.text("Page URL", size="1", color="gray"),
            rx.input(
                placeholder="https://example.com/live",
                value=SettingsState.vc_url,
                on_change=SettingsState.set_vc_url,
                width="100%",
            ),
            spacing="1",
            width="100%",
        ),
        rx.hstack(
            rx.vstack(
                rx.text("Resolution", size="1", color="gray"),
                rx.select(
                    RESOLUTION_OPTIONS,
                    value=SettingsState.vc_resolution,
                    on_change=SettingsState.set_vc_resolution,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text("Frame rate", size="1", color="gray"),
                rx.select(
                    FRAMERATE_OPTIONS,
                    value=SettingsState.vc_framerate,
                    on_change=SettingsState.set_vc_framerate,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text("Encoder preset", size="1", color="gray"),
                rx.select(
                    PRESET_OPTIONS,
                    value=SettingsState.vc_preset,
                    on_change=SettingsState.set_vc_preset,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text("Video kbps", size="1", color="gray"),
                rx.input(
                    value=SettingsState.vc_video_bitrate,
                    on_change=SettingsState.set_vc_video_bitrate,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
        rx.hstack(
            rx.vstack(
                rx.text("Audio kbps", size="1", color="gray"),
                rx.input(
                    value=SettingsState.vc_audio_bitrate,
                    on_change=SettingsState.set_vc_audio_bitrate,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text("Warm-up seconds", size="1", color="gray"),
                rx.input(
                    value=SettingsState.vc_warmup,
                    on_change=SettingsState.set_vc_warmup,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text("Idle timeout (s)", size="1", color="gray"),
                rx.input(
                    value=SettingsState.vc_idle_timeout,
                    on_change=SettingsState.set_vc_idle_timeout,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
        rx.hstack(
            rx.vstack(
                rx.text(
                    "Hide these (CSS selectors, one per line)",
                    size="1", color="gray",
                ),
                rx.text_area(
                    placeholder=".cookie-banner\n#consent-overlay",
                    value=SettingsState.vc_hide_selectors,
                    on_change=SettingsState.set_vc_hide_selectors,
                    rows="3",
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text(
                    "Click these to start playback (one per line)",
                    size="1", color="gray",
                ),
                rx.text_area(
                    placeholder="button.play\n.vjs-big-play-button",
                    value=SettingsState.vc_click_selectors,
                    on_change=SettingsState.set_vc_click_selectors,
                    rows="3",
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
        rx.hstack(
            rx.vstack(
                rx.text("Logo URL (optional)", size="1", color="gray"),
                rx.input(
                    value=SettingsState.vc_logo,
                    on_change=SettingsState.set_vc_logo,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            rx.vstack(
                rx.text("Tags (comma separated)", size="1", color="gray"),
                rx.input(
                    value=SettingsState.vc_tags,
                    on_change=SettingsState.set_vc_tags,
                    width="100%",
                ),
                spacing="1",
                width="100%",
            ),
            spacing="2",
            width="100%",
        ),
        rx.hstack(
            rx.hstack(
                rx.switch(
                    checked=SettingsState.vc_audio,
                    on_change=SettingsState.set_vc_audio,
                ),
                rx.text("Capture audio", size="2"),
                align="center",
                spacing="2",
            ),
            rx.hstack(
                rx.switch(
                    checked=SettingsState.vc_enabled,
                    on_change=SettingsState.set_vc_enabled,
                ),
                rx.text("Enabled", size="2"),
                align="center",
                spacing="2",
            ),
            rx.spacer(),
            rx.button("Cancel", on_click=SettingsState.reset_vc_form, variant="soft", size="2"),
            rx.button("Save", on_click=SettingsState.save_vc, size="2"),
            align="center",
            spacing="3",
            width="100%",
        ),
        spacing="3",
        width="100%",
    )


def virtual_section() -> rx.Component:
    """Virtual channel configuration.

    A virtual channel is a web page shown in a headless browser and restreamed
    as live HLS, so anything a browser can display becomes a channel — including
    sites that have no stream URL to proxy.
    """
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.heading("Virtual Channels", size="5"),
                rx.spacer(),
                rx.button(
                    rx.icon("refresh-cw", size=14),
                    "Sessions",
                    on_click=SettingsState.refresh_vc_sessions,
                    variant="soft",
                    size="1",
                ),
                align="center",
                width="100%",
            ),
            rx.text(
                "Restream a web page as a live channel. FreeSky opens the page in "
                "a browser on a private display, records the screen and the "
                "browser's audio, and serves the result as HLS — so it appears in "
                "playlist.m3u8 like any other channel. A session starts when "
                "someone tunes in and stops after the idle timeout. Use Control "
                "to open the live browser and drive it with mouse and keyboard - "
                "to sign into a site, dismiss a consent dialog, or set up the "
                "page before it goes out. Any number of channels can run at "
                "once; each costs roughly 1.5-2 CPU cores and ~900MB at 720p.",
                color="gray",
                size="2",
            ),
            rx.cond(
                SettingsState.vc_preflight_message != "",
                rx.callout(SettingsState.vc_preflight_message, icon="triangle_alert",
                           color_scheme="amber", size="1", width="100%"),
            ),
            rx.cond(
                SettingsState.vc_error != "",
                rx.callout(SettingsState.vc_error, icon="triangle_alert",
                           color_scheme="red", size="1", width="100%"),
            ),
            rx.cond(
                SettingsState.vc_list.length() > 0,
                rx.vstack(
                    rx.foreach(SettingsState.vc_list, virtual_channel_row),
                    spacing="0",
                    width="100%",
                ),
                rx.text("No virtual channels configured yet.", size="2", color="gray"),
            ),
            rx.cond(
                SettingsState.vc_sessions.length() > 0,
                rx.vstack(
                    rx.divider(),
                    rx.text("Running sessions", size="2", weight="bold"),
                    rx.foreach(SettingsState.vc_sessions, virtual_session_row),
                    spacing="1",
                    width="100%",
                ),
            ),
            rx.divider(),
            virtual_form(),
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
                virtual_section(),
                spacing="4",
                width="100%",
            ),
            padding_top="7rem",
            padding_bottom="2rem",
            max_width="900px",
        ),
    )
