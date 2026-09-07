import React from 'react';
import Hls from 'hls.js';
import '@vidstack/react/player/styles/default/theme.css';
import '@vidstack/react/player/styles/default/layouts/audio.css';
import '@vidstack/react/player/styles/default/layouts/video.css';
import { MediaPlayer, MediaProvider, Poster, Captions } from "@vidstack/react"
import { DefaultVideoLayout, defaultLayoutIcons } from '@vidstack/react/player/layouts/default';


function InjectCSS() {
  const css = `
    .media-player[data-view-type="video"] {
      aspect-ratio: 16 / 9;
    }

    .vds-video-layout {
      --video-brand: hsl(0, 0%, 96%);
    }

    .vds-audio-layout {
      --audio-brand: hsl(0, 0%, 96%);
    }

    .plyr {
      --plyr-color-main: hsl(198, 100%, 50%);
    }
    
    .vds-slider-chapters {
      display: none;
    }
    
    .rt-Container {
      align-self: center;
    }
  `;

  return <style dangerouslySetInnerHTML={{ __html: css }} />;
}

// hls.js tuning for live streams. This has to be applied to the provider — vidstack's
// `storage` prop is for persisted player state (volume, quality) and silently swallowed
// this config, leaving the player spinning after the manifest loaded.
const hlsConfig = {
  // Live-edge targeting.
  //
  // liveSyncDuration / liveMaxLatencyDuration were REMOVED here, not merely
  // retuned. hls.js documents that liveSyncDuration takes precedence over
  // liveSyncDurationCount when both are set, so `liveSyncDuration: 2` pinned
  // playback to 2s behind the live edge — with 2s segments that is less than
  // one segment of headroom, so the player was permanently chasing a fragment
  // that had barely been written, and stalled and rebuffered its way through
  // playback. That reads as "laggy" but is a buffering bug, not a bitrate one.
  //
  // 3 is hls.js's default and its documented floor: "decreasing this value is
  // likely to cause playback stalls".
  liveSyncDurationCount: 3,
  liveMaxLatencyDurationCount: 10,
  // The important one. Instead of seeking when it drifts behind, the player
  // speeds up slightly until it is back at the target. Default is 1, i.e.
  // disabled, which leaves drift to be corrected by a visible jump.
  maxLiveSyncPlaybackRate: 1.5,
  // 180s of forward buffer on a live stream delays startup and pins memory for
  // nothing: segments ahead of the live edge do not exist yet.
  maxBufferLength: 30,
  maxMaxBufferLength: 60,
  // backBufferLength supersedes the deprecated liveBackBufferLength, and caps
  // memory growth on a channel left playing for hours.
  backBufferLength: 30,
  manifestLoadingTimeOut: 5000,
  manifestLoadingMaxRetry: 2,
  levelLoadingTimeOut: 8000,
  levelLoadingMaxRetry: 1,
  // A virtual channel's first segment can take a while on a cold start, and an
  // upstream channel may fail over between feeds.
  fragLoadingTimeOut: 20000,
  fragLoadingMaxRetry: 3,
  startFragPrefetch: true,
  testBandwidth: false,
  startLevel: -1,
  capLevelToPlayerSize: false,
  maxStarvationDelay: 4,
  maxLoadingDelay: 4,
};

export function Player({ title, src }) {
  const hlsRef = React.useRef(null);
  const playerRef = React.useRef(null);

  const handleProviderChange = (provider) => {
    if (provider?.type === 'hls') {
      provider.config = { ...provider.config, ...hlsConfig };
    }
  };

  // ponytail: Chrome answers "maybe" to canPlayType('application/vnd.apple.mpegurl')
  // while being unable to actually play HLS. vidstack trusts that probe, picks its
  // native video provider even with preferNativeHLS={false}, and the player spins
  // forever. When we land on the native provider for an HLS source, drive hls.js
  // ourselves. Remove this once vidstack's detection stops trusting "maybe".
  const handleProviderSetup = (provider) => {
    if (provider?.type !== 'video' || !src || !src.includes('.m3u8')) return;
    if (!Hls.isSupported()) return;

    const video = provider.video;
    if (!video) return;

    if (hlsRef.current) hlsRef.current.destroy();
    const hls = new Hls(hlsConfig);
    hlsRef.current = hls;
    hls.loadSource(src);
    hls.attachMedia(video);
  };

  React.useEffect(() => () => {
    if (hlsRef.current) {
      hlsRef.current.destroy();
      hlsRef.current = null;
    }
  }, []);

  const handleCanPlay = () => {
    console.log('Video can start playing');
  };

  const handleWaiting = () => {
    console.log('Video is buffering/waiting');
  };

  const handleError = (event) => {
    console.error('Video error:', event);
  };

  const handleStalled = () => {
    console.log('Video playback stalled');
  };

  // Browsers only allow autoplay WITH sound once the origin has enough user
  // interaction; otherwise play() rejects. Hardcoding `muted` bought reliable
  // autoplay at the cost of every stream starting silent. Instead start unmuted
  // and only fall back to muted when the browser actually refuses — arriving here
  // by clicking a channel is usually gesture enough to keep the sound.
  const handleAutoPlayFail = () => {
    const player = playerRef.current;
    if (!player) return;
    console.log('Unmuted autoplay blocked; retrying muted');
    player.muted = true;
    player.play().catch(() => {});
  };

  return (
    <>
      <InjectCSS />
      <MediaPlayer
        ref={playerRef}
        title={title}
        src={src}
        viewType='video'
        streamType='live'
        logLevel='warn'
        playsInline
        autoplay
        load='eager'
        preload='auto'
        crossorigin='anonymous'
        preferNativeHLS={false}
        onProviderChange={handleProviderChange}
        onProviderSetup={handleProviderSetup}
        onCanPlay={handleCanPlay}
        onWaiting={handleWaiting}
        onError={handleError}
        onStalled={handleStalled}
        onAutoPlayFail={handleAutoPlayFail}
      >
        <MediaProvider>
          <Poster className="vds-poster" />
        </MediaProvider>
        <DefaultVideoLayout
          icons={defaultLayoutIcons}
        />
          <Captions className="vds-captions" />
      </MediaPlayer>
    </>
  );
}