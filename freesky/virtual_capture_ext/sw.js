// FreeSky tab-capture extension: service worker.
//
// The backend drives this worker through Playwright
// (`context.service_workers[0].evaluate(...)`), which is why the entry points
// are plain globals rather than a message protocol: startCapture(opts),
// stopCapture() and captureStatus(). Nothing here runs on its own.
//
// Flow: getMediaStreamId() turns a tab id into a capture handle, an offscreen
// document opens that handle with getUserMedia (a service worker has no DOM
// and cannot hold a MediaStream), and offscreen.js records and ships it.
//
// `chrome.tabCapture.getMediaStreamId` normally demands that the user invoked
// the extension for that tab. Chromium is launched with
// --allowlisted-extension-id=<this id>, which lifts that requirement; without
// the flag the call fails with "Extension has not been invoked for the current
// page". The id is pinned by the `key` in manifest.json so the flag can name it.

async function ensureOffscreen() {
  if (await chrome.offscreen.hasDocument()) return;
  await chrome.offscreen.createDocument({
    url: 'offscreen.html',
    reasons: ['USER_MEDIA'],
    justification: 'Encode the captured tab for restreaming',
  });
}

async function pickTab(opts) {
  const tabs = await chrome.tabs.query({});
  const real = tabs.filter(t => t.url && !t.url.startsWith('chrome-extension://'));
  if (opts.tabId) {
    const hit = real.find(t => t.id === opts.tabId);
    if (hit) return hit;
  }
  return real.find(t => t.active) || real[0];
}

self.startCapture = async (opts) => {
  const tab = await pickTab(opts || {});
  if (!tab) throw new Error('no tab to capture');
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  await ensureOffscreen();
  const reply = await chrome.runtime.sendMessage({ type: 'start', streamId, tabId: tab.id, ...opts });
  if (reply && reply.error) throw new Error(reply.error);
  return reply;
};

self.stopCapture = async () => {
  if (!(await chrome.offscreen.hasDocument())) return { stopped: false };
  const reply = await chrome.runtime.sendMessage({ type: 'stop' });
  await chrome.offscreen.closeDocument();
  return reply;
};

self.captureStatus = async () => {
  if (!(await chrome.offscreen.hasDocument())) return { running: false };
  return chrome.runtime.sendMessage({ type: 'status' });
};
