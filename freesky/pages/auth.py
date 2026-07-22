"""The /login page. Auth state and guards live in freesky/auth_state.py."""
import reflex as rx

from freesky.auth_state import AuthState
from freesky.components import navbar


@rx.page("/login")
def login() -> rx.Component:
    return rx.box(
        navbar(),
        rx.center(
            rx.card(
                rx.form(
                    rx.vstack(
                        rx.heading("Sign in", size="6"),
                        rx.text("Enter your credentials to continue.", color="gray", size="2"),
                        rx.input(
                            name="username",
                            placeholder="Username",
                            required=True,
                            size="3",
                            width="100%",
                        ),
                        rx.input(
                            name="password",
                            placeholder="Password",
                            type="password",
                            required=True,
                            size="3",
                            width="100%",
                        ),
                        rx.cond(
                            AuthState.error != "",
                            rx.callout(
                                AuthState.error,
                                icon="triangle_alert",
                                color_scheme="red",
                                size="1",
                                width="100%",
                            ),
                            rx.fragment(),
                        ),
                        rx.button("Sign in", type="submit", size="3", width="100%"),
                        spacing="4",
                        width="100%",
                    ),
                    on_submit=AuthState.login,
                    reset_on_submit=False,
                ),
                width="100%",
                max_width="360px",
                padding="2rem",
            ),
            # Centre in the viewport rather than hanging off a fixed top padding,
            # which left the card stranded near the top of a tall window.
            width="100%",
            min_height="100vh",
            padding="1rem",
        ),
        width="100%",
    )
