"""Reflex wrapper around the shaka-player based Widevine player.

See ``assets/shaka_player.jsx`` for the component itself.
"""

import reflex as rx
from reflex.components.component import NoSSRComponent


def _on_drm_error_spec(error: rx.Var[dict]) -> tuple[rx.Var[str], rx.Var[str]]:
    """Narrow the JS error object down to serialisable fields.

    The JSX side emits ``{code, message}``; declaring the spec explicitly keeps the
    raw shaka error (which carries non-serialisable data) out of the websocket
    payload and gives the handler a stable two-argument signature.

    Args:
        error: The ``{code, message}`` object emitted by the JSX component.

    Returns:
        A ``(code, message)`` tuple of Vars passed to the Python event handler.
    """
    return (
        rx.Var(f"{error!s}.code").to(str),
        rx.Var(f"{error!s}.message").to(str),
    )


class ShakaPlayer(NoSSRComponent):
    """Widevine-capable video player using shaka-player and standard EME.

    Decryption is performed entirely by the browser's own licensed Content
    Decryption Module. This component only points that CDM at a same-origin
    FreeSky licence proxy and forwards the CDM's opaque challenge unmodified;
    it never has access to content keys, licences in cleartext, or plaintext
    media. No DRM circumvention is involved at any point.

    Props:
        src: URL of the DASH or HLS manifest to play.
        license_url: Same-origin FreeSky licence proxy endpoint.
        session_token: FreeSky session token, sent as the ``X-Freesky-Token``
            header on licence requests only.
        manifest_type: ``"dash"`` or ``"hls"``; hints the manifest MIME type when
            the URL carries no usable extension.

    Event triggers:
        on_drm_error: Fired with ``(code, message)`` when playback cannot start.
            ``code`` is either a shaka error code or one of the synthetic codes
            ``INSECURE_CONTEXT`` / ``BROWSER_UNSUPPORTED``.
    """

    # ponytail: "$/public/shaka_player", not "/public/shaka_player". NoSSRComponent
    # strips the static import under the "$"-prefixed key but reflex registers it
    # under the raw one, so a bare "/..." leaves both `import {ShakaPlayer}` and
    # `const ShakaPlayer = ...` in the generated module -> rolldown PARSE_ERROR
    # "ShakaPlayer has already been declared". See media_player.py for the same trap.
    library = "$/public/shaka_player"
    # Pinned exactly: shaka's DRM error codes are renumbered between major versions
    # and the mapping in shaka_player.jsx is written against 5.x.
    lib_dependencies: list[str] = ["shaka-player@5.2.9"]
    tag = "ShakaPlayer"

    src: rx.Var[str]
    license_url: rx.Var[str]
    session_token: rx.Var[str]
    manifest_type: rx.Var[str]

    on_drm_error: rx.EventHandler[_on_drm_error_spec]
