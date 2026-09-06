import React from 'react';
import shaka from 'shaka-player/dist/shaka-player.ui.js';
import 'shaka-player/dist/controls.css';

// Widevine-capable player.
//
// Playback here is plain standards-compliant EME: shaka hands the browser's own
// licensed CDM an opaque challenge, the CDM's licence response comes back through
// our same-origin proxy, and every decrypt happens inside the CDM. Nothing in this
// file ever sees a key or a frame of plaintext media.

// Error codes shaka reports are numbers with no user-facing meaning. Map the ones a
// FreeSky viewer can actually act on; anything else falls through to a generic
// message carrying the raw code so a bug report is still useful.
//
// NOTE: these are the shaka 5.x DRM codes. There is no 6000 — the "no key system"
// error is 6001. Do not renumber these against older shaka docs.
const DRM_ERROR_MESSAGES = {
  6001: "This channel needs Widevine DRM, which this browser can't provide. Safari and iOS can't play it — use Chrome, Firefox or Edge, and make sure the page is served over HTTPS.",
  6002: 'The browser could not start its content decryption module. Fully quit and reopen the browser, then try again.',
  6003: 'The decryption module could not attach to the video element. Fully quit and reopen the browser, then try again.',
  6004: 'The DRM server certificate was rejected. This channel is misconfigured on the server.',
  6005: 'The browser could not open a DRM session. Fully quit and reopen the browser, then try again.',
  6006: 'The browser could not build a licence request. Try reloading the page.',
  6007: 'The licence request failed to reach FreeSky. Check that FreeSky is reachable and try again.',
  6008: 'The licence server rejected the request. Your FreeSky session may have expired — reload the page to sign in again.',
  6010: 'This stream is encrypted but carries no DRM information, so it cannot be played.',
  6012: 'No licence server is configured for this channel.',
  6013: 'The saved DRM session for this channel is no longer available. Reload the page.',
  6014: 'The DRM licence for this channel has expired. Reload the page to fetch a new one.',
  6015: 'This channel requires a DRM server certificate that FreeSky did not supply.',
};

// Synthetic codes for the two failures shaka cannot report, because they happen
// before a player exists.
const INSECURE_CONTEXT = 'INSECURE_CONTEXT';
const BROWSER_UNSUPPORTED = 'BROWSER_UNSUPPORTED';

const SYNTHETIC_ERROR_MESSAGES = {
  [INSECURE_CONTEXT]:
    'DRM playback requires a secure page. Open FreeSky over HTTPS, or via http://localhost, and try again.',
  [BROWSER_UNSUPPORTED]:
    'This browser cannot play DRM-protected streams. Use an up-to-date Chrome, Firefox or Edge.',
};

const MANIFEST_MIME_TYPES = {
  dash: 'application/dash+xml',
  mpd: 'application/dash+xml',
  hls: 'application/x-mpegurl',
  m3u8: 'application/x-mpegurl',
};

function InjectCSS() {
  const css = `
    .freesky-shaka-container {
      width: 100%;
      aspect-ratio: 16 / 9;
      background: #000;
      border-radius: 8px;
      overflow: hidden;
    }

    .freesky-shaka-container video {
      width: 100%;
      height: 100%;
    }
  `;

  return <style dangerouslySetInnerHTML={{ __html: css }} />;
}

export function ShakaPlayer({ src, licenseUrl, sessionToken, manifestType, onDrmError }) {
  const videoRef = React.useRef(null);
  const containerRef = React.useRef(null);
  const playerRef = React.useRef(null);
  const uiRef = React.useRef(null);

  // onDrmError is a reflex event handler and gets a fresh identity every render;
  // holding it in a ref keeps it out of the effect's dependency list so a re-render
  // never tears down and re-attaches a working CDM session.
  const onDrmErrorRef = React.useRef(onDrmError);
  onDrmErrorRef.current = onDrmError;

  const emitError = React.useCallback((code, message, detail) => {
    console.error('Shaka DRM error', code, message, detail);
    const handler = onDrmErrorRef.current;
    if (handler) handler({ code: String(code), message });
  }, []);

  const emitShakaError = React.useCallback(
    (error) => {
      const code = error && error.code;
      const message =
        DRM_ERROR_MESSAGES[code] ||
        `Playback failed with error ${code ?? 'unknown'}. Try another channel or reload the page.`;
      emitError(code ?? 'UNKNOWN', message, error);
    },
    [emitError],
  );

  React.useEffect(() => {
    // Guard the secure-context requirement BEFORE touching shaka. EME simply does not
    // exist on an insecure origin, and shaka's own report of that is a bare 6001 that
    // is indistinguishable from "this browser has no Widevine" and reads to the user
    // as a hang. FreeSky is frequently opened on a plain-HTTP LAN address, so this is
    // the single most likely DRM failure in the wild and deserves its own message.
    if (typeof window === 'undefined') return undefined;
    if (!window.isSecureContext) {
      emitError(INSECURE_CONTEXT, SYNTHETIC_ERROR_MESSAGES[INSECURE_CONTEXT]);
      return undefined;
    }

    shaka.polyfill.installAll();

    if (!shaka.Player.isBrowserSupported()) {
      emitError(BROWSER_UNSUPPORTED, SYNTHETIC_ERROR_MESSAGES[BROWSER_UNSUPPORTED]);
      return undefined;
    }

    const video = videoRef.current;
    const container = containerRef.current;
    if (!video || !container || !src) return undefined;

    // attach() and load() are async; if the component unmounts (or src changes) while
    // they are in flight we must not keep operating on a player we have destroyed.
    let cancelled = false;
    const player = new shaka.Player();
    playerRef.current = player;

    player.addEventListener('error', (event) => {
      if (!cancelled) emitShakaError(event.detail);
    });

    player.configure({
      drm: {
        // Same-origin licence proxy. The CDM talks only to FreeSky; FreeSky forwards
        // the challenge upstream. Widevine and PlayReady share the endpoint — the
        // proxy routes on the key system it sees in the request.
        servers: {
          'com.widevine.alpha': licenseUrl,
          'com.microsoft.playready': licenseUrl,
        },
        advanced: {
          // Software robustness ONLY, deliberately. Desktop Chrome/Firefox/Edge are
          // Widevine L3 (software) and cannot satisfy HW_SECURE_*. Asking for a
          // hardware level we do not need turns a stream that plays fine into a hard
          // load() failure with no usable diagnostic. Do NOT "upgrade" these.
          'com.widevine.alpha': {
            videoRobustness: ['SW_SECURE_DECODE', 'SW_SECURE_CRYPTO'],
            audioRobustness: ['SW_SECURE_CRYPTO'],
          },
          'com.microsoft.playready': {
            videoRobustness: ['SW_SECURE_DECODE', 'SW_SECURE_CRYPTO'],
            audioRobustness: ['SW_SECURE_CRYPTO'],
          },
        },
      },
      streaming: {
        lowLatencyMode: true,
        rebufferingGoal: 4,
        bufferingGoal: 30,
      },
    });

    player.getNetworkingEngine().registerRequestFilter((type, request) => {
      if (type !== shaka.net.NetworkingEngine.RequestType.LICENSE) return;
      // Authenticate the licence call to our own proxy via a header. The request BODY
      // is the CDM's opaque challenge and must reach the licence server byte-for-byte
      // — wrapping, re-encoding or appending to it invalidates the challenge and the
      // licence server rejects it (surfacing as 6008). Headers only, never the body.
      if (sessionToken) request.headers['X-Freesky-Token'] = sessionToken;
    });

    const ui = new shaka.ui.Overlay(player, container, video);
    uiRef.current = ui;

    (async () => {
      try {
        await player.attach(video);
        if (cancelled) return;
        const mimeType = MANIFEST_MIME_TYPES[String(manifestType || '').toLowerCase()];
        await player.load(src, null, mimeType);
        if (cancelled) return;
        video.play().catch(() => {
          // Autoplay with sound may be refused until the origin has enough user
          // interaction; fall back to muted rather than leaving a frozen first frame.
          video.muted = true;
          video.play().catch(() => {});
        });
      } catch (error) {
        if (!cancelled) emitShakaError(error);
      }
    })();

    return () => {
      cancelled = true;
      const currentUi = uiRef.current;
      const currentPlayer = playerRef.current;
      uiRef.current = null;
      playerRef.current = null;
      // Destroy the UI first: it holds a reference to the player and reaches into it
      // during its own teardown. shaka.ui.Overlay.destroy() also destroys the player
      // it owns, so guard the second destroy against the already-destroyed case.
      if (currentUi) currentUi.destroy().catch(() => {});
      else if (currentPlayer) currentPlayer.destroy().catch(() => {});
    };
  }, [src, licenseUrl, sessionToken, manifestType, emitError, emitShakaError]);

  return (
    <>
      <InjectCSS />
      <div ref={containerRef} className="freesky-shaka-container" data-shaka-player-container>
        <video ref={videoRef} data-shaka-player playsInline autoPlay crossOrigin="anonymous" />
      </div>
    </>
  );
}
