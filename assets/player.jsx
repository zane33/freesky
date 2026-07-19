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
  liveBackBufferLength: 60,
  liveSyncDurationCount: 5,
  liveMaxLatencyDurationCount: 15,
  maxBufferLength: 180,
  maxMaxBufferLength: 300,
  manifestLoadingTimeOut: 5000,
  manifestLoadingMaxRetry: 2,
  levelLoadingTimeOut: 8000,
  levelLoadingMaxRetry: 1,
  fragLoadingTimeOut: 15000,
  fragLoadingMaxRetry: 2,
  startFragPrefetch: true,
  testBandwidth: false,
  startLevel: -1,
  capLevelToPlayerSize: false,
  maxStarvationDelay: 4,
  maxLoadingDelay: 4,
  liveSyncDuration: 2,
  liveMaxLatencyDuration: 8,
};

export function Player({ title, src }) {
  const hlsRef = React.useRef(null);

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

  return (
    <>
      <InjectCSS />
      <MediaPlayer
        title={title}
        src={src}
        viewType='video'
        streamType='live'
        logLevel='warn'
        playsInline
        autoplay
        muted
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