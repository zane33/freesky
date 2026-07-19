import reflex as rx
from reflex.components.component import NoSSRComponent


class MediaPlayer(NoSSRComponent):
    # ponytail: "$/public/player", not "/public/player". NoSSRComponent strips the
    # static import under the "$"-prefixed key but reflex registers it under the raw
    # one, so a bare "/..." leaves both `import {Player}` and `const Player = ...` in
    # the generated module -> rolldown PARSE_ERROR "Player has already been declared".
    library = "$/public/player"
    lib_dependencies: list[str] = ["@vidstack/react@next"]
    tag = "Player"
    title: rx.Var[str]
    src: rx.Var[str]
    autoplay: bool = True
