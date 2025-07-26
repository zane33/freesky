import reflex as rx

def VidembedPlayer(title: str, vidembed_url: str):
    """Player component for vidembed URLs"""
    return rx.box(
        rx.iframe(
            src=vidembed_url,
            width="100%",
            height="100%",
            style={
                "border": "none",
                "border-radius": "8px",
                "min-height": "400px"
            },
            title=title,
            allow="autoplay; fullscreen; picture-in-picture",
            allow_full_screen=True,
        ),
        width="100%",
        height="100%",
        min_height="400px",
        border_radius="8px",
        overflow="hidden",
    ) 