let gridEl = null;
let latestName = null;
let pollTimer = null;

function renderFrames(frames) {
  if (!gridEl) return;
  if (!frames.length) {
    gridEl.textContent = 'no frames yet';
    gridEl.classList.add('muted');
    return;
  }
  gridEl.classList.remove('muted');
  gridEl.innerHTML = frames
    .slice(0, 8)
    .map(
      (frame) =>
        `<a href="/model-frames/${encodeURIComponent(frame.name)}" target="_blank" rel="noopener">` +
        `<img src="/model-frames/${encodeURIComponent(frame.name)}" alt="${frame.name}" ` +
        `title="${frame.caption || frame.name}"></a>`,
    )
    .join('');
}

async function pollModelFrames() {
  if (document.hidden) return;
  try {
    const response = await fetch('/api/model-frames');
    if (!response.ok) return;
    const payload = await response.json();
    const frames = payload.frames || [];
    const newest = frames[0]?.name || null;
    if (newest === latestName) return;
    latestName = newest;
    renderFrames(frames);
  } catch (_exc) {
    // ignore transient dashboard fetch failures
  }
}

export function initModelFrames() {
  gridEl = document.getElementById('model-frames-grid');
  if (!gridEl) return;
  pollModelFrames();
  pollTimer = window.setInterval(pollModelFrames, 2000);
}
