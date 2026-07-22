"""Channel enable/disable settings.

Runs in the same process as the FastAPI backend, so it reads and writes
`channel_prefs` directly instead of going back out over HTTP.
"""
import reflex as rx
from typing import List

from rxconfig import api_url

from freesky import backend, channel_prefs, users, app_settings
from freesky.free_sky import Channel
from freesky.components import navbar
from freesky.auth_state import require_admin
from freesky.free_sky_hybrid import StepDaddyHybrid

# "Auto" is a sentinel in the dropdown, stored as "" (no pin) on disk. The real
# options come from the resolver so the two can't drift apart.
AUTO_SOURCE = "Auto (failover)"
SOURCE_OPTIONS = [AUTO_SOURCE] + list(StepDaddyHybrid.PLAYER_PATHS)


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
        url = f"{api_url}/playlist.m3u8?token={token}"
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
                spacing="4",
                width="100%",
            ),
            padding_top="7rem",
            padding_bottom="2rem",
            max_width="900px",
        ),
    )
