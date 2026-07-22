import reflex as rx
from pygments.styles.dracula import background

from rxconfig import config
from freesky.auth_state import AuthState


def navbar_icons_item(text: str, icon: str, url: str, external: bool = False) -> rx.Component:
    return rx.link(
        rx.hstack(
            rx.icon(icon, color="white"),
            rx.text(text, size="4", weight="medium", color="white"),
        ),
        href=url,
        is_external=external,
    )


def navbar_icons_menu_item(text: str, icon: str, url: str, external: bool = False) -> rx.Component:
    return rx.link(
        rx.hstack(
            rx.icon(icon, size=24, color="white"),
            rx.text(text, size="3", weight="medium", color="white"),
        ),
        href=url,
        is_external=external,
        padding="0.5em",
    )


def navbar(search=None) -> rx.Component:
    return rx.box(
        rx.card(
            # Desktop navbar
            rx.hstack(
                rx.vstack(
                    rx.text(
                        config.app_name, size="8", weight="bold", key="navbar-title"
                    ),
                    rx.box(
                        background_color="#fa5252",
                        width="100%",
                        padding="2.5px",
                    ),
                    align_items="center",
                    gap="0",
                    on_click=rx.redirect("/")
                ),
                rx.cond(
                    search,
                    search,
                    rx.text(
                        "Watch ",
                        rx.code("live"),
                        " TV channels",
                        align="center",
                        size="4",
                        padding="5px",
                    ),
                ),
                rx.hstack(
                    navbar_icons_item("Schedule", "calendar-sync", "/schedule"),
                    navbar_icons_item("playlist.m3u8", "file-down", "/playlist"),
                    # Settings is admin-only; hiding it is cosmetic, the page
                    # itself enforces the role in on_load.
                    rx.cond(
                        AuthState.is_admin,
                        navbar_icons_item("Settings", "settings", "/settings"),
                        rx.fragment(),
                    ),
                    rx.cond(
                        AuthState.is_authenticated,
                        rx.hstack(
                            rx.text(AuthState.username, size="2", color="gray"),
                            rx.button(
                                rx.icon("log-out", size=16),
                                on_click=AuthState.logout,
                                size="1",
                                variant="soft",
                                color_scheme="gray",
                            ),
                            align="center",
                            spacing="2",
                        ),
                        # Trusted-network visitors are not signed in, so without
                        # this there is no way to reach the admin login at all.
                        navbar_icons_item("Sign in", "log-in", "/login"),
                    ),
                    spacing="6",
                    class_name="desktop-nav-items",
                ),
                justify="between",
                align_items="center",
                width="100%",
                class_name="desktop-navbar",
            ),
            # Mobile navbar
            rx.vstack(
                rx.hstack(
                    rx.vstack(
                        rx.text(
                            config.app_name, size="6", weight="bold", key="navbar-title-mobile"
                        ),
                        rx.box(
                            background_color="#fa5252",
                            width="100%",
                            padding="2.5px",
                        ),
                        align_items="center",
                        gap="0",
                        on_click=rx.redirect("/")
                    ),
                    rx.menu.root(
                        rx.menu.trigger(
                            rx.icon("menu", size=24)
                        ),
                        rx.menu.content(
                            navbar_icons_menu_item("Schedule", "calendar-sync", "/schedule"),
                            navbar_icons_menu_item("playlist.m3u8", "file-down", "/playlist"),
                            rx.cond(
                                AuthState.is_admin,
                                navbar_icons_menu_item("Settings", "settings", "/settings"),
                                rx.fragment(),
                            ),
                            rx.cond(
                                AuthState.is_authenticated,
                                rx.menu.item("Sign out", on_click=AuthState.logout),
                                navbar_icons_menu_item("Sign in", "log-in", "/login"),
                            ),
                            navbar_icons_menu_item("Github", "github", "https://github.com/gookie-dev/freesky", True),
                        ),
                        justify="end",
                    ),
                    justify="between",
                    align_items="center",
                    width="100%",
                ),
                rx.cond(
                    search,
                    rx.box(
                        search,
                        width="100%",
                    ),
                    rx.fragment(),
                ),
                class_name="mobile-navbar",
            ),
            padding="1em",
            width="100%",
        ),
        padding="1rem",
        position="fixed",
        top="0px",
        z_index="2",
        width="100%",
        key="main-navbar",
        style={
            ".desktop-navbar": {
                "display": "flex",
                "@media (max-width: 768px)": {
                    "display": "none",
                }
            },
            ".mobile-navbar": {
                "display": "none",
                "@media (max-width: 768px)": {
                    "display": "flex",
                }
            },
            ".desktop-nav-items": {
                "display": "flex",
                "@media (max-width: 768px)": {
                    "display": "none",
                }
            }
        }
    )
