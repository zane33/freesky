import reflex as rx
from reflex.components.component import NoSSRComponent


class MediaPlayer(NoSSRComponent):
    # ponytail: "$/public/player", not "/public/player". NoSSRComponent strips the
    # static import under the "$"-prefixed key but reflex registers it under the raw
    # one, so a bare "/..." leaves both `import {Player}` and `const Player = ...` in
    # the generated module -> rolldown PARSE_ERROR "Player has already been declared".
    library = "$/public/player"
    # hls.js is imported directly by player.jsx: Chrome's canPlayType lies about native
    # HLS support, so we cannot rely on vidstack lazy-loading it from a CDN.
    lib_dependencies: list[str] = ["@vidstack/react@next", "hls.js"]
    tag = "Player"
    title: rx.Var[str]
    src: rx.Var[str]
    autoplay: bool = True
