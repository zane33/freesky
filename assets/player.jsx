import React from 'react';
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

export function Player({ title, src }) {
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
        // Enhanced buffering for better live stream performance
        preferNativeHLS={false}
        // Reduce rebuffering by allowing larger buffer
        storage={{
          hlsLiveBackBufferLength: 30,  // Keep 30s of buffer behind playhead
          hlsLiveSyncDurationCount: 3,  // Stay closer to live edge
          hlsLiveMaxLatencyDurationCount: 10,  // Max latency before sync
          maxBufferLength: 60,  // Total buffer size: 60 seconds
          maxMaxBufferLength: 120,  // Emergency buffer: 2 minutes
          manifestLoadingTimeOut: 10000,  // 10s timeout for manifests
          manifestLoadingMaxRetry: 3,
          levelLoadingTimeOut: 10000,  // 10s timeout for segments
          levelLoadingMaxRetry: 2,
          fragLoadingTimeOut: 20000,  // 20s timeout for fragments
          fragLoadingMaxRetry: 3
        }}
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