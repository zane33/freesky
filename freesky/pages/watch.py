import reflex as rx
from urllib.parse import urlparse
from rxconfig import config
from freesky import backend
from freesky.components import navbar, MediaPlayer
from freesky.free_sky import Channel
from freesky.free_sky_hybrid import StepDaddyHybrid
from freesky.auth_state import require_login, AuthState

media_player = MediaPlayer.create

# The upstream feeds a viewer can force from the watch page. Kept in sync with
# the resolver's player list so the switcher never offers a feed that can't resolve.
FEEDS = list(StepDaddyHybrid.PLAYER_PATHS)




class WatchState(rx.State):
    is_loaded: bool = False
    _cache_buster: int = 0
    url: str = ""
    # Manual feed override; "" = Auto (audio-aware failover picks the feed).
    player: str = ""
    _base: str = ""
    _token: str = ""

    def _build_url(self):
        """Compose the stream URL from the stored base/token plus any feed pick."""
        params = []
        if self._token:
            params.append(f"token={self._token}")
        if self.player:
            params.append(f"player={self.player}")
        query = ("?" + "&".join(params)) if params else ""
        self.url = f"{self._base}/api/stream/{self.route_channel_id}.m3u8{query}"
        self._cache_buster += 1

    @rx.event
    async def on_load(self):
        """Initialize watch page state on load."""
        redirect = await require_login(self)
        if redirect is not None:
            return redirect
        self.is_loaded = False
        self.player = ""  # every fresh load starts on Auto

        # Build the stream URL from the address the browser actually used, and
        # carry the viewer's token. The old version used the build-time
        # config.api_url with no token, so through NAT or a hostname the player
        # requested an unreachable LAN address and, off the trusted network, would
        # have been rejected as unauthenticated anyway.
        origin = ""
        try:
            parsed = urlparse(str(self.router.url))
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass
        self._base = (origin or config.api_url).rstrip("/")
        try:
            self._token = (await self.get_state(AuthState)).stream_token
        except Exception:
            self._token = ""
        self._build_url()

    @rx.event
    def set_feed(self, player: str):
        """Switch the player to a specific upstream feed (or "" for Auto).

        Reloads the stream URL with ?player=, which the backend re-resolves
        against that feed, bypassing the shared cache.
        """
        self.player = player
        self._build_url()

    @rx.var
    def route_channel_id(self) -> str:
        return self.router.page.params.get("channel_id", "")

    @rx.var
    def channel(self) -> Channel | None:
        self.is_loaded = False
        channel = backend.get_channel(self.route_channel_id)
        self.is_loaded = True
        return channel


    def copy_url_to_clipboard(self):
        """Copy URL to clipboard using JavaScript."""
        url = self.url
        return rx.call_script(f"""
            async function copyToClipboard() {{
                const text = '{url}';
                try {{
                    if (navigator.clipboard && window.isSecureContext) {{
                        await navigator.clipboard.writeText(text);
                        return true;
                    }} else {{
                        const textArea = document.createElement('textarea');
                        textArea.value = text;
                        textArea.style.position = 'fixed';
                        textArea.style.left = '-999999px';
                        textArea.style.top = '-999999px';
                        document.body.appendChild(textArea);
                        textArea.focus();
                        textArea.select();
                        const result = document.execCommand('copy');
                        document.body.removeChild(textArea);
                        return result;
                    }}
                }} catch (err) {{
                    console.error('Failed to copy text: ', err);
                    return false;
                }}
            }}
            copyToClipboard();
        """)


def _feed_button(label, value) -> rx.Component:
    return rx.button(
        label,
        size="1",
        variant=rx.cond(WatchState.player == value, "solid", "soft"),
        color_scheme=rx.cond(WatchState.player == value, "green", "gray"),
        on_click=lambda: WatchState.set_feed(value),
    )


def feed_selector() -> rx.Component:
    """Let the viewer force a specific upstream feed — the quick way to escape a
    source that's video-only or otherwise broken without leaving the page."""
    return rx.card(
        rx.vstack(
            rx.hstack(
                rx.icon("radio", size=16),
                rx.text("Feed", weight="bold", size="2"),
                rx.text("switch source if audio or video is missing",
                        size="1", color_scheme="gray"),
                align="center",
                spacing="2",
            ),
            rx.hstack(
                _feed_button("Auto", ""),
                rx.foreach(FEEDS, lambda f: _feed_button(f, f)),
                wrap="wrap",
                spacing="2",
            ),
            spacing="2",
        ),
        margin_top="0.75rem",
        width="100%",
    )


def uri_card() -> rx.Component:
    return rx.card(
        rx.hstack(
            rx.button(
                rx.text(WatchState.url),
                rx.icon("link-2", size=20),
                on_click=[
                    WatchState.copy_url_to_clipboard,
                    rx.toast("URI copied to clipboard"),
                ],
                size="1",
                variant="surface",
                radius="full",
                color_scheme="gray"
            ),
            rx.button(
                rx.text("VLC"),
                rx.icon("external-link", size=15),
                on_click=[
                    WatchState.copy_url_to_clipboard,
                    rx.toast("Stream URL copied! Open VLC → Media → Open Network Stream and paste the URL"),
                ],
                size="1",
                color_scheme="orange",
                variant="soft",
                high_contrast=True,
            ),
            rx.button(
                rx.text("MPV"),
                rx.icon("external-link", size=15),
                on_click=rx.redirect(f"mpv://{WatchState.url}", is_external=True),
                size="1",
                color_scheme="purple",
                variant="soft",
                high_contrast=True,
            ),
            rx.button(
                rx.text("Pot"),
                rx.icon("external-link", size=15),
                on_click=rx.redirect(f"potplayer://{WatchState.url}", is_external=True),
                size="1",
                color_scheme="yellow",
                variant="soft",
                high_contrast=True,
            ),
            # width="100%",
            wrap="wrap",
        ),
        margin_top="1rem",
    )


@rx.page("/watch/[channel_id]", on_load=WatchState.on_load)
def watch() -> rx.Component:
    return rx.fragment(
        navbar(),
        rx.container(
            rx.cond(
                config.proxy_content,
                rx.fragment(),
                rx.card(
                    rx.hstack(
                        rx.icon(
                            "info",
                        ),
                        rx.text(
                            "Proxy content is disabled on this instance. Web Player won't work due to CORS.",
                        ),
                    ),
                    width="100%",
                    margin_bottom="1rem",
                    background_color=rx.color("accent", 7),
                ),
            ),
            rx.center(
                rx.card(
                    rx.cond(
                        WatchState.channel.name,
                        rx.hstack(
                            rx.box(
                                rx.hstack(
                                    rx.card(
                                        rx.image(
                                            src=WatchState.channel.logo,
                                            width="60px",
                                            height="60px",
                                            object_fit="contain",
                                        ),
                                        padding="0",
                                    ),
                                    rx.box(
                                        rx.heading(WatchState.channel.name, margin_bottom="0.3rem", padding_top="0.2rem"),
                                        rx.box(
                                            rx.hstack(
                                                rx.cond(
                                                    WatchState.channel.tags,
                                                    rx.foreach(
                                                        WatchState.channel.tags,
                                                        lambda tag: rx.badge(tag, variant="surface", color_scheme="gray")
                                                    ),
                                                ),
                                            ),
                                        ),
                                        overflow="hidden",
                                        text_overflow="ellipsis",
                                        white_space="nowrap",
                                    ),
                                ),
                            ),
                            rx.tablet_and_desktop(
                                rx.box(
                                    rx.vstack(
                                        rx.button(
                                            rx.text(
                                                WatchState.url,
                                                overflow="hidden",
                                                text_overflow="ellipsis",
                                                white_space="nowrap",
                                            ),
                                            rx.icon("link-2", size=20),
                                            on_click=[
                                                WatchState.copy_url_to_clipboard,
                                                rx.toast("URI copied to clipboard"),
                                            ],
                                            size="1",
                                            variant="surface",
                                            radius="full",
                                            color_scheme="gray"
                                        ),
                                        rx.hstack(
                                            rx.button(
                                                rx.text("VLC"),
                                                rx.icon("external-link", size=15),
                                                on_click=[
                                                    WatchState.copy_url_to_clipboard,
                                                    rx.toast("Stream URL copied! Open VLC → Media → Open Network Stream and paste the URL"),
                                                ],
                                                size="1",
                                                color_scheme="orange",
                                                variant="soft",
                                                high_contrast=True,
                                            ),
                                            rx.button(
                                                rx.text("MPV"),
                                                rx.icon("external-link", size=15),
                                                on_click=rx.redirect(f"mpv://{WatchState.url}", is_external=True),
                                                size="1",
                                                color_scheme="purple",
                                                variant="soft",
                                                high_contrast=True,
                                            ),
                                            rx.button(
                                                rx.text("Pot"),
                                                rx.icon("external-link", size=15),
                                                on_click=rx.redirect(f"potplayer://{WatchState.url}", is_external=True),
                                                size="1",
                                                color_scheme="yellow",
                                                variant="soft",
                                                high_contrast=True,
                                            ),
                                            justify="end",
                                            width="100%",
                                        ),
                                    ),
                                ),
                            ),
                            justify="between",
                            padding_bottom="0.5rem",
                        ),
                    ),
                    rx.box(
                        rx.cond(
                            WatchState.route_channel_id != "",
                            media_player(
                                title=WatchState.channel.name,
                                src=WatchState.url,
                            ),
                            rx.center(
                                rx.spinner(size="3"),
                            ),
                        ),
                        width="100%",
                    ),
                    feed_selector(),
                    padding_bottom="0.3rem",
                    width="100%",
                ),
            ),
            rx.fragment(
                rx.mobile_only(
                    uri_card(),
                ),
                rx.cond(
                    WatchState.is_loaded & ~WatchState.channel.name,
                    rx.tablet_and_desktop(
                        uri_card(),
                    ),
                ),
            ),
            size="4",
            padding_top="10rem",
        ),
    )
