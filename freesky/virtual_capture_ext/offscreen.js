// FreeSky tab-capture extension: the recorder.
//
// Opens the tab's capture handle as a MediaStream, encodes it with
// MediaRecorder and pushes the resulting WebM byte stream to the backend over
// a loopback WebSocket, where it is written straight into ffmpeg's stdin.
//
// Why this is smoother than grabbing the X display:
//   * frames come from Chromium's own compositor with the timestamp of the
//     frame they are, not sampled off a screen by a wall-clock timer that
//     drifts and bursts when the host is busy;
//   * audio comes from the same tab and is muxed against the same clock, so
//     lip-sync cannot drift over a long session;
//   * Chromium encodes the H.264 itself, so ffmpeg only remuxes to HLS and
//     costs almost nothing.
//
// The codec is negotiated by the backend (`mime`): H.264/Opus in WebM is what
// Playwright's Chromium supports and what lets ffmpeg use `-c:v copy`.

let state = null;

async function start(o) {
  if (state) await stop();
  const mandatory = { chromeMediaSource: 'tab', chromeMediaSourceId: o.streamId };
  const video = { ...mandatory, minFrameRate: o.fps, maxFrameRate: o.fps };
  if (o.width && o.height) {
    // Pin the capture size: the encoder needs even dimensions and a kiosk
    // window can be a pixel short of the nominal screen size.
    Object.assign(video, { minWidth: o.width, maxWidth: o.width,
                           minHeight: o.height, maxHeight: o.height });
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: o.audio ? { mandatory } : false,
    video: { mandatory: video },
  });

  const ws = new WebSocket(o.ws);
  ws.binaryType = 'arraybuffer';
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error('capture WebSocket failed to connect'));
  });

  const opts = { mimeType: o.mime };
  if (o.vbps) opts.videoBitsPerSecond = o.vbps;
  if (o.abps) opts.audioBitsPerSecond = o.abps;
  // One keyframe per HLS segment, so every segment starts on an IDR and
  // EXT-X-INDEPENDENT-SEGMENTS holds without re-encoding.
  if (o.keyMs) opts.videoKeyFrameIntervalDuration = o.keyMs;
  const rec = new MediaRecorder(stream, opts);

  const st = {
    running: true, chunks: 0, bytes: 0, buffered: 0, errors: [],
    started: Date.now(), mime: rec.mimeType,
    settings: stream.getVideoTracks()[0].getSettings(),
  };
  // Blob.arrayBuffer() is asynchronous; chaining keeps chunks in order.
  let chain = Promise.resolve();
  rec.ondataavailable = (e) => {
    if (!e.data || !e.data.size) return;
    chain = chain.then(() => e.data.arrayBuffer()).then((buf) => {
      if (ws.readyState !== 1) return;
      ws.send(buf);
      st.chunks += 1;
      st.bytes += buf.byteLength;
      st.buffered = ws.bufferedAmount;
    }).catch((err) => st.errors.push(String(err)));
  };
  rec.onerror = (e) => st.errors.push(String((e && e.error) || e));
  for (const track of stream.getTracks()) {
    track.onended = () => { st.errors.push(track.kind + ' track ended'); st.running = false; };
  }
  ws.onclose = () => { if (st.running) st.errors.push('capture WebSocket closed'); st.running = false; };

  rec.start(o.timeslice || 250);
  state = { rec, ws, stream, st };
  return { ok: true, mime: rec.mimeType, settings: st.settings, tabId: o.tabId };
}

async function stop() {
  if (!state) return { stopped: false };
  const { rec, ws, stream, st } = state;
  state = null;
  try { rec.stop(); } catch (e) { /* already stopped */ }
  for (const track of stream.getTracks()) track.stop();
  // Let the final ondataavailable flush before the socket goes away.
  await new Promise((r) => setTimeout(r, 300));
  try { ws.close(); } catch (e) { /* already closed */ }
  st.running = false;
  return { stopped: true, chunks: st.chunks, bytes: st.bytes, errors: st.errors };
}

function status() {
  if (!state) return { running: false };
  return { ...state.st, buffered: state.ws.bufferedAmount, wsState: state.ws.readyState };
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  let run;
  if (msg.type === 'start') run = start(msg);
  else if (msg.type === 'stop') run = stop();
  else run = Promise.resolve(status());
  run.then(reply, (err) => reply({ error: String((err && err.message) || err) }));
  return true;  // reply is asynchronous
});
