const HORIZON_DEFAULT = 30.0;

const COLORS = {
  bg: '#020a10',
  grid: '#0a2538',
  gridStrong: '#143a52',
  tick: '#3a6080',
  text: '#c0e8f0',
  textDim: '#6a8a9a',
  accent: '#00d4ff',
  mic: '#00d4ff',
  playback: '#c084fc',
  threshold: '#facc15',
  gateOpen: '#4ade80',
  gateClosed: '#163040',
  border: '#0a4f6a',
};

const STATE_COLORS = {
  listening: '#00d4ff',
  hearing: '#facc15',
  thinking: '#7dd3fc',
  starting: '#facc15',
  reconnecting: '#f87171',
  speaking: '#c084fc',
  error: '#f87171',
  disabled: '#3a4a55',
};

const EVENT_STYLES = {
  barge_in_fired:      { color: '#f87171', glyph: '●', label: 'BARGE' },
  barge_in_considered: { color: '#facc15', glyph: '○', label: 'check' },
  echo_suppressed:     { color: '#c084fc', glyph: '×',  label: 'echo' },
  turn_start:          { color: '#7dd3fc', glyph: '▶', label: 'turn' },
  turn_cancel:         { color: '#f87171', glyph: '■', label: 'cancel' },
  commit:              { color: '#4ade80', glyph: '◆', label: 'commit' },
  commit_decision:     { color: '#4ade80', glyph: '◆', label: 'commit' },
  partial:             { color: '#6a8a9a', glyph: '·',  label: 'partial' },
  state:               { color: '#6a8a9a', glyph: '│', label: 'state' },
};

const LANE_RATIOS = {
  audio: 0.40,
  state: 0.10,
  gate:  0.07,
  events: 0.28,
  transcript: 0.15,
};

let canvas = null;
let ctx = null;
let wrap = null;
let hoverEl = null;
let pauseButton = null;
let paused = false;
let dpr = 1;
let viewW = 0;
let viewH = 0;

let serverRef = 0;
let browserRefAt = 0;
let frozenRef = null;
let horizon = HORIZON_DEFAULT;
let levels = [];
let events = [];
let hoverX = null;
let rafToken = null;

export function initVoiceTimeline() {
  canvas = document.getElementById('voice-timeline-canvas');
  wrap = document.getElementById('voice-timeline-canvas-wrap');
  hoverEl = document.getElementById('voice-timeline-hover');
  pauseButton = document.getElementById('voice-timeline-pause');
  if (!canvas || !wrap) return;
  ctx = canvas.getContext('2d');

  resize();
  window.addEventListener('resize', resize);
  if (window.ResizeObserver) new ResizeObserver(resize).observe(wrap);

  canvas.addEventListener('mousemove', onHover);
  canvas.addEventListener('mouseleave', () => { hoverX = null; hoverEl.classList.add('hidden'); });
  if (pauseButton) pauseButton.addEventListener('click', onPause);

  scheduleFrame();
}

export function updateVoiceTimeline(timeline) {
  if (!timeline) return;
  serverRef = Number(timeline.ref) || 0;
  browserRefAt = performance.now() / 1000;
  horizon = Number(timeline.horizon_secs) || HORIZON_DEFAULT;
  levels = Array.isArray(timeline.levels) ? timeline.levels : [];
  events = Array.isArray(timeline.events) ? timeline.events : [];
}

function onPause() {
  paused = !paused;
  if (paused) {
    frozenRef = effectiveRef();
    pauseButton.classList.add('armed', 'timeline-paused');
    pauseButton.textContent = 'Resume';
  } else {
    frozenRef = null;
    pauseButton.classList.remove('armed', 'timeline-paused');
    pauseButton.textContent = 'Pause';
  }
}

function effectiveRef() {
  if (paused && frozenRef != null) return frozenRef;
  return serverRef + (performance.now() / 1000 - browserRefAt);
}

function resize() {
  if (!canvas || !wrap) return;
  dpr = window.devicePixelRatio || 1;
  const rect = wrap.getBoundingClientRect();
  viewW = Math.max(1, Math.floor(rect.width));
  viewH = Math.max(1, Math.floor(rect.height));
  canvas.width = Math.floor(viewW * dpr);
  canvas.height = Math.floor(viewH * dpr);
  canvas.style.width = `${viewW}px`;
  canvas.style.height = `${viewH}px`;
}

function scheduleFrame() {
  if (rafToken != null) return;
  const tick = () => {
    rafToken = null;
    render();
    rafToken = requestAnimationFrame(tick);
  };
  rafToken = requestAnimationFrame(tick);
}

function timeToX(t, ref) {
  const age = ref - t;
  return viewW * (1 - age / horizon);
}

function laneRects() {
  const total = LANE_RATIOS.audio + LANE_RATIOS.state + LANE_RATIOS.gate + LANE_RATIOS.events + LANE_RATIOS.transcript;
  const usable = viewH - 18;
  const scale = usable / total;
  let y = 4;
  const make = (h) => { const r = { y, h: h * scale }; y += r.h + 2; return r; };
  return {
    audio: make(LANE_RATIOS.audio),
    state: make(LANE_RATIOS.state),
    gate: make(LANE_RATIOS.gate),
    events: make(LANE_RATIOS.events),
    transcript: make(LANE_RATIOS.transcript),
    axisY: viewH - 14,
  };
}

function render() {
  if (!ctx) return;
  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.fillStyle = COLORS.bg;
  ctx.fillRect(0, 0, viewW, viewH);

  const ref = effectiveRef();
  const lanes = laneRects();

  drawTimeAxis(ref, lanes.axisY);
  drawAudioLane(ref, lanes.audio);
  drawStateLane(ref, lanes.state);
  drawGateLane(ref, lanes.gate);
  drawEventsLane(ref, lanes.events);
  drawTranscriptLane(ref, lanes.transcript);
  if (paused) drawPausedBadge();
  if (hoverX != null) drawCrosshair(ref, hoverX, lanes);

  ctx.restore();
}

function drawTimeAxis(ref, y) {
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  ctx.fillStyle = COLORS.textDim;
  ctx.font = '10px "JetBrains Mono", monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (let secs = 0; secs <= horizon; secs += 5) {
    const x = viewW * (1 - secs / horizon);
    ctx.beginPath();
    ctx.moveTo(x, y - 2);
    ctx.lineTo(x, y + 3);
    ctx.stroke();
    ctx.fillText(secs === 0 ? 'now' : `-${secs}s`, x, y + 4);
  }
}

function drawAudioLane(ref, rect) {
  drawLaneBackground(rect, 'AUDIO');
  if (levels.length < 2) return;

  let maxObserved = 500;
  for (const sample of levels) {
    if (sample[1] > maxObserved) maxObserved = sample[1];
    if (sample[2] > maxObserved) maxObserved = sample[2];
    if (sample[3] > maxObserved) maxObserved = sample[3];
  }
  const max = maxObserved * 1.15;

  drawAudioGrid(rect, max);

  drawSeries(levels, rect, ref, max, 2, COLORS.playback, true);
  drawSeries(levels, rect, ref, max, 1, COLORS.mic, false);
  drawSeries(levels, rect, ref, max, 3, COLORS.threshold, false, true);

  ctx.fillStyle = COLORS.textDim;
  ctx.font = '9px "JetBrains Mono", monospace';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  ctx.fillText(`max ${Math.round(max)}`, 4, rect.y + 2);
}

function drawAudioGrid(rect, max) {
  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = rect.y + (rect.h * i) / 4;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(viewW, y);
    ctx.stroke();
  }
}

function drawSeries(samples, rect, ref, max, idx, color, filled, dashed = false) {
  ctx.beginPath();
  let started = false;
  for (const sample of samples) {
    const t = sample[0];
    const value = sample[idx];
    const x = timeToX(t, ref);
    if (x < -2 || x > viewW + 2) continue;
    const y = rect.y + rect.h - (value / max) * rect.h;
    if (!started) { ctx.moveTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  }
  if (!started) return;
  if (filled) {
    ctx.lineTo(timeToX(samples[samples.length - 1][0], ref), rect.y + rect.h);
    ctx.lineTo(timeToX(samples[0][0], ref), rect.y + rect.h);
    ctx.closePath();
    ctx.fillStyle = withAlpha(color, 0.18);
    ctx.fill();
    ctx.beginPath();
    started = false;
    for (const sample of samples) {
      const x = timeToX(sample[0], ref);
      if (x < -2 || x > viewW + 2) continue;
      const y = rect.y + rect.h - (sample[idx] / max) * rect.h;
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
  }
  ctx.lineWidth = dashed ? 1.2 : 1.4;
  ctx.strokeStyle = color;
  if (dashed) ctx.setLineDash([3, 3]);
  ctx.stroke();
  if (dashed) ctx.setLineDash([]);
}

function drawStateLane(ref, rect) {
  drawLaneBackground(rect, 'STATE');
  const segments = stateSegments(ref);
  for (const seg of segments) {
    const x1 = Math.max(0, timeToX(seg.start, ref));
    const x2 = Math.min(viewW, timeToX(seg.end, ref));
    if (x2 <= x1) continue;
    ctx.fillStyle = STATE_COLORS[seg.state] || STATE_COLORS.disabled;
    ctx.globalAlpha = 0.7;
    ctx.fillRect(x1, rect.y + 4, x2 - x1, rect.h - 6);
    ctx.globalAlpha = 1;
    if (x2 - x1 > 50) {
      ctx.fillStyle = '#020a10';
      ctx.font = '9px "JetBrains Mono", monospace';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText(seg.state.toUpperCase(), x1 + 4, rect.y + rect.h / 2);
    }
  }
}

function stateSegments(ref) {
  const stateEvents = events.filter((e) => e.type === 'state' && e.state);
  if (!stateEvents.length) return [];
  stateEvents.sort((a, b) => a.t - b.t);
  const segments = [];
  for (let i = 0; i < stateEvents.length; i++) {
    const start = stateEvents[i].t;
    const end = i + 1 < stateEvents.length ? stateEvents[i + 1].t : ref;
    segments.push({ state: String(stateEvents[i].state), start, end });
  }
  return segments;
}

function drawGateLane(ref, rect) {
  drawLaneBackground(rect, 'GATE');
  if (levels.length < 2) return;
  ctx.fillStyle = COLORS.gateClosed;
  ctx.fillRect(0, rect.y + 4, viewW, rect.h - 6);
  for (let i = 0; i < levels.length - 1; i++) {
    if (!levels[i][4]) continue;
    const x1 = Math.max(0, timeToX(levels[i][0], ref));
    const x2 = Math.min(viewW, timeToX(levels[i + 1][0], ref));
    if (x2 <= x1) continue;
    ctx.fillStyle = COLORS.gateOpen;
    ctx.fillRect(x1, rect.y + 4, x2 - x1, rect.h - 6);
  }
}

function drawEventsLane(ref, rect) {
  drawLaneBackground(rect, 'EVENTS');
  const cutoff = ref - horizon;
  const visible = events.filter((e) => e.t >= cutoff && e.type !== 'state' && e.type !== 'partial');
  visible.sort((a, b) => a.t - b.t);

  const lanes = [];
  ctx.font = '10px "JetBrains Mono", monospace';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';

  for (const event of visible) {
    const style = EVENT_STYLES[event.type] || { color: COLORS.textDim, glyph: '●', label: event.type };
    const x = timeToX(event.t, ref);
    const labelWidth = ctx.measureText(style.label).width + 12;
    let laneIdx = 0;
    while (laneIdx < lanes.length && lanes[laneIdx] > x) laneIdx++;
    lanes[laneIdx] = x + labelWidth;
    const laneCount = Math.max(3, lanes.length);
    const laneH = (rect.h - 8) / laneCount;
    const y = rect.y + 4 + laneIdx * laneH + laneH / 2;

    ctx.strokeStyle = withAlpha(style.color, 0.4);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, rect.y + 4);
    ctx.lineTo(x, rect.y + rect.h - 4);
    ctx.stroke();

    ctx.fillStyle = style.color;
    ctx.fillText(style.glyph + ' ' + style.label, x + 3, y);
  }
}

function drawTranscriptLane(ref, rect) {
  drawLaneBackground(rect, 'TRANSCRIPT');
  const cutoff = ref - horizon;
  const visible = events.filter((e) => (e.type === 'commit' || e.type === 'partial') && e.t >= cutoff && e.text);
  if (!visible.length) return;
  ctx.font = '10px "JetBrains Mono", monospace';
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';

  let lastY = rect.y + rect.h / 2;
  let lastEndX = -Infinity;
  let row = 0;
  for (const event of visible) {
    const x = timeToX(event.t, ref);
    const text = (event.type === 'commit' ? '"' : '…') + String(event.text).slice(0, 40) + (event.type === 'commit' ? '"' : '');
    const width = ctx.measureText(text).width + 10;
    if (x < lastEndX) {
      row = (row + 1) % 2;
    } else {
      row = 0;
    }
    lastY = rect.y + 4 + row * ((rect.h - 8) / 2) + ((rect.h - 8) / 4);
    lastEndX = x + width;

    ctx.fillStyle = event.type === 'commit' ? withAlpha(COLORS.accent, 0.18) : withAlpha(COLORS.textDim, 0.18);
    const boxX = x;
    const boxY = lastY - 7;
    ctx.fillRect(boxX, boxY, width, 14);
    ctx.strokeStyle = event.type === 'commit' ? COLORS.accent : COLORS.textDim;
    ctx.lineWidth = 1;
    ctx.strokeRect(boxX + 0.5, boxY + 0.5, width - 1, 13);
    ctx.fillStyle = event.type === 'commit' ? COLORS.text : COLORS.textDim;
    ctx.fillText(text, boxX + 4, lastY);
  }
}

function drawLaneBackground(rect, label) {
  ctx.strokeStyle = COLORS.gridStrong;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, rect.y);
  ctx.lineTo(viewW, rect.y);
  ctx.stroke();

  ctx.fillStyle = COLORS.textDim;
  ctx.font = '9px "JetBrains Mono", monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'top';
  ctx.fillText(label, viewW - 4, rect.y + 2);
}

function drawPausedBadge() {
  ctx.fillStyle = 'rgba(0, 212, 255, 0.18)';
  ctx.fillRect(viewW - 64, 2, 60, 16);
  ctx.strokeStyle = COLORS.accent;
  ctx.strokeRect(viewW - 64 + 0.5, 2.5, 59, 15);
  ctx.fillStyle = COLORS.accent;
  ctx.font = 'bold 10px "JetBrains Mono", monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('PAUSED', viewW - 34, 10);
}

function drawCrosshair(ref, x, lanes) {
  ctx.strokeStyle = withAlpha(COLORS.accent, 0.6);
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(x, lanes.audio.y);
  ctx.lineTo(x, lanes.transcript.y + lanes.transcript.h);
  ctx.stroke();
  ctx.setLineDash([]);
}

function onHover(event) {
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  hoverX = x;
  const ref = effectiveRef();
  const t = ref - horizon * (1 - x / viewW);

  const sample = nearestSample(t);
  const nearbyEvents = events.filter((e) => Math.abs(e.t - t) < 0.4);
  const lines = [];
  lines.push(`t  -${(ref - t).toFixed(1)}s`);
  if (sample) {
    lines.push(`mic       ${sample[1]}`);
    lines.push(`playback  ${sample[2]}`);
    lines.push(`threshold ${sample[3]}`);
    lines.push(`gate      ${sample[4] ? 'open' : 'closed'}`);
  }
  for (const ev of nearbyEvents.slice(-4)) {
    lines.push(`> ${ev.type}${ev.reason ? ' ' + ev.reason : ''}${ev.state ? ' ' + ev.state : ''}`);
  }
  hoverEl.textContent = lines.join('\n');
  hoverEl.classList.remove('hidden');
  const wrapRect = wrap.getBoundingClientRect();
  const left = Math.min(wrapRect.width - 200, Math.max(8, x + 12));
  hoverEl.style.left = `${left}px`;
}

function nearestSample(t) {
  if (!levels.length) return null;
  let best = null;
  let bestDiff = Infinity;
  for (const sample of levels) {
    const d = Math.abs(sample[0] - t);
    if (d < bestDiff) { bestDiff = d; best = sample; }
  }
  return bestDiff < 0.25 ? best : null;
}

function withAlpha(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
