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
        // Aggressive buffering for optimal live stream performance
        preferNativeHLS={false}
        // Large buffers to prevent rebuffering under network fluctuations
        storage={{
          hlsLiveBackBufferLength: 60,  // Keep 60s of buffer behind playhead
          hlsLiveSyncDurationCount: 5,  // More segments for smoother playback
          hlsLiveMaxLatencyDurationCount: 15,  // Higher latency tolerance
          maxBufferLength: 180,  // Large buffer: 3 minutes
          maxMaxBufferLength: 300,  // Emergency buffer: 5 minutes
          manifestLoadingTimeOut: 5000,  // Faster manifest timeout for quicker retries
          manifestLoadingMaxRetry: 2,   // Fewer retries for faster failover
          levelLoadingTimeOut: 8000,    // Faster segment timeout
          levelLoadingMaxRetry: 1,      // Single retry for faster failover
          fragLoadingTimeOut: 15000,    // Reduced fragment timeout
          fragLoadingMaxRetry: 2,       // Fewer fragment retries
          startFragPrefetch: true,      // Enable fragment prefetching
          testBandwidth: false,         // Disable bandwidth testing for faster startup
          startLevel: -1,               // Let player choose best quality automatically
          capLevelToPlayerSize: false,  // Don't limit quality based on player size
          maxStarvationDelay: 4,        // Quick starvation recovery
          maxLoadingDelay: 4,           // Quick loading recovery
          liveSyncDuration: 2,          // Faster sync to live edge
          liveMaxLatencyDuration: 8     // Max latency before seeking to live
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