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
  scribeGate: '#fb923c',
  gateOpen: '#4ade80',
  gateClosed: '#163040',
  border: '#0a4f6a',
};

const PHASE_ROWS = [
  { name: 'user_speech', color: '#4ade80', label: 'user' },
  { name: 'hearing',     color: '#facc15', label: 'hearing' },
  { name: 'thinking',    color: '#7dd3fc', label: 'thinking' },
  { name: 'speaking',    color: '#c084fc', label: 'speaking' },
];

const ASSISTANT_COLOR = '#c084fc';

const EVENT_STYLES = {
  barge_in_fired:      { color: '#f87171', glyph: '●', label: 'BARGE' },
  barge_in_considered: { color: '#facc15', glyph: '○', label: 'check' },
  echo_suppressed:     { color: '#c084fc', glyph: '×',  label: 'echo' },
  turn_start:          { color: '#7dd3fc', glyph: '▶', label: 'turn' },
  turn_cancel:         { color: '#f87171', glyph: '■', label: 'cancel' },
  commit:              { color: '#4ade80', glyph: '◆', label: 'commit' },
  commit_decision:     { color: '#4ade80', glyph: '◆', label: 'commit' },
  partial:             { color: '#6a8a9a', glyph: '·',  label: 'partial' },
};

const LANE_RATIOS_NORMAL = {
  audio: 0.32,
  state: 0.22,
  gate:  0.08,
  events: 0.22,
  transcript: 0.16,
};

const LANE_RATIOS_MAXIMIZED = {
  audio: 0.24,
  state: 0.16,
  gate:  0.07,
  events: 0.15,
  transcript: 0.38,
};

// Timeline level tuple: [t, mic_peak, playback_rms, threshold_rms, barge_gate, scribe_gate]
const IDX_MIC = 1;
const IDX_PLAYBACK = 2;
const IDX_THRESHOLD = 3;
const IDX_BARGE_GATE = 4;
const IDX_SCRIBE_GATE = 5;

let canvas = null;
let ctx = null;
let wrap = null;
let hoverEl = null;
let pauseButton = null;
let maximizeButton = null;
let section = null;
let paused = false;
let maximized = false;
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
  maximizeButton = document.getElementById('voice-timeline-maximize');
  section = document.getElementById('voice-timeline-section');
  if (!canvas || !wrap) return;
  ctx = canvas.getContext('2d');

  resize();
  window.addEventListener('resize', resize);
  if (window.ResizeObserver) new ResizeObserver(resize).observe(wrap);

  canvas.addEventListener('mousemove', onHover);
  canvas.addEventListener('mouseleave', () => { hoverX = null; hoverEl.classList.add('hidden'); });
  if (pauseButton) pauseButton.addEventListener('click', onPause);
  if (maximizeButton) maximizeButton.addEventListener('click', onMaximize);

  scheduleFrame();
}

export function updateVoiceTimeline(timeline) {
  if (!timeline) return;
  if (paused) return;
  const nextRef = Number(timeline.ref) || 0;
  if (nextRef === serverRef) return;
  serverRef = nextRef;
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

function onMaximize() {
  maximized = !maximized;
  if (section) section.classList.toggle('maximized', maximized);
  maximizeButton.textContent = maximized ? 'Minimize' : 'Maximize';
  resize();
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
  const ratios = maximized ? LANE_RATIOS_MAXIMIZED : LANE_RATIOS_NORMAL;
  const total = ratios.audio + ratios.state + ratios.gate + ratios.events + ratios.transcript;
  const usable = viewH - 18;
  const scale = usable / total;
  let y = 4;
  const make = (h) => { const r = { y, h: h * scale }; y += r.h + 2; return r; };
  return {
    audio: make(ratios.audio),
    state: make(ratios.state),
    gate: make(ratios.gate),
    events: make(ratios.events),
    transcript: make(ratios.transcript),
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

const MIC_AUDIO_SPLIT = 0.38;
const MIC_SCALE_FLOOR = 300;
const OUT_SCALE_FLOOR = 500;
// Keep in sync with MIC_SCRIBE_SEND_RMS_MIN in src/voice/elevenlabs_io.py
const MIC_SCRIBE_SEND_RMS_MIN = 100;

function drawAudioLane(ref, rect) {
  if (levels.length < 2) {
    drawLaneBackground(rect, 'AUDIO');
    return;
  }

  const gap = 1;
  const micH = Math.max(22, Math.floor((rect.h - gap) * MIC_AUDIO_SPLIT));
  const micRect = { y: rect.y, h: micH };
  const outRect = { y: rect.y + micH + gap, h: rect.h - micH - gap };

  drawLaneBackground(micRect, 'MIC');
  drawLaneBackground(outRect, 'OUT');

  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, outRect.y - 0.5);
  ctx.lineTo(viewW, outRect.y - 0.5);
  ctx.stroke();

  drawMicAudioLane(ref, micRect);
  drawOutAudioLane(ref, outRect);
}

function seriesScaleMax(samples, idx, floor) {
  let maxObserved = floor;
  for (const sample of samples) {
    if (sample[idx] > maxObserved) maxObserved = sample[idx];
  }
  return maxObserved * 1.15;
}

function seriesMax(samples, ...indices) {
  let maxObserved = 0;
  for (const sample of samples) {
    for (const idx of indices) {
      if (sample[idx] > maxObserved) maxObserved = sample[idx];
    }
  }
  return maxObserved;
}

function drawLevelLine(rect, max, level, color) {
  const y = rect.y + rect.h - (level / max) * rect.h;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.2;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(0, y);
  ctx.lineTo(viewW, y);
  ctx.stroke();
  ctx.setLineDash([]);
  return y;
}

function drawMicAudioLane(ref, rect) {
  const max = seriesScaleMax(levels, IDX_MIC, MIC_SCALE_FLOOR);
  drawAudioGrid(rect, max);
  drawSeries(levels, rect, ref, max, IDX_MIC, COLORS.mic, false);
  const gateY = drawLevelLine(rect, max, MIC_SCRIBE_SEND_RMS_MIN, COLORS.scribeGate);
  ctx.fillStyle = COLORS.textDim;
  ctx.font = '9px "JetBrains Mono", monospace';
  ctx.textAlign = 'left';
  ctx.textBaseline = 'top';
  ctx.fillText(`max ${Math.round(max)}`, 4, rect.y + 2);
  ctx.textAlign = 'right';
  ctx.textBaseline = 'bottom';
  ctx.fillStyle = COLORS.scribeGate;
  ctx.fillText(`scribe ${MIC_SCRIBE_SEND_RMS_MIN}`, viewW - 4, gateY - 2);
}

function drawOutAudioLane(ref, rect) {
  const max = Math.max(OUT_SCALE_FLOOR, seriesMax(levels, IDX_PLAYBACK, IDX_THRESHOLD)) * 1.15;
  drawAudioGrid(rect, max);
  drawSeries(levels, rect, ref, max, IDX_PLAYBACK, COLORS.playback, true);
  drawSeries(levels, rect, ref, max, IDX_THRESHOLD, COLORS.threshold, false, true);
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
  const innerY = rect.y + 4;
  const innerH = rect.h - 6;
  const rowH = innerH / PHASE_ROWS.length;
  for (let i = 0; i < PHASE_ROWS.length; i++) {
    const row = PHASE_ROWS[i];
    const y = innerY + i * rowH;
    ctx.fillStyle = '#0a1822';
    ctx.fillRect(0, y, viewW, rowH - 1);

    const intervals = phaseIntervals(row.name, ref);
    ctx.fillStyle = row.color;
    for (const [start, end] of intervals) {
      const x1 = Math.max(0, timeToX(start, ref));
      const x2 = Math.min(viewW, timeToX(end, ref));
      if (x2 <= x1) continue;
      ctx.globalAlpha = 0.75;
      ctx.fillRect(x1, y, x2 - x1, rowH - 1);
      ctx.globalAlpha = 1;
    }
    ctx.fillStyle = '#6a8a9a';
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(row.label, 4, y + rowH / 2);
  }
}

function phaseIntervals(name, ref) {
  const phaseEvents = events.filter((e) => e.type === 'phase' && e.name === name);
  if (!phaseEvents.length) return [];
  phaseEvents.sort((a, b) => a.t - b.t);
  const intervals = [];
  let openAt = null;
  for (const event of phaseEvents) {
    if (event.on && openAt == null) {
      openAt = event.t;
    } else if (!event.on && openAt != null) {
      intervals.push([openAt, event.t]);
      openAt = null;
    }
  }
  if (openAt != null) intervals.push([openAt, ref]);
  return intervals;
}

function drawGateLane(ref, rect) {
  drawLaneBackground(rect, 'GATES');
  if (levels.length < 2) return;

  const innerY = rect.y + 4;
  const innerH = rect.h - 6;
  const rowH = innerH / 2;
  const rows = [
    { label: 'scribe', idx: IDX_SCRIBE_GATE, open: COLORS.scribeGate, closed: '#2a1810' },
    { label: 'barge', idx: IDX_BARGE_GATE, open: COLORS.gateOpen, closed: COLORS.gateClosed },
  ];

  for (let rowIdx = 0; rowIdx < rows.length; rowIdx++) {
    const row = rows[rowIdx];
    const y = innerY + rowIdx * rowH;
    ctx.fillStyle = row.closed;
    ctx.fillRect(0, y, viewW, rowH - 1);
    for (let i = 0; i < levels.length - 1; i++) {
      if (!levels[i][row.idx]) continue;
      const x1 = Math.max(0, timeToX(levels[i][0], ref));
      const x2 = Math.min(viewW, timeToX(levels[i + 1][0], ref));
      if (x2 <= x1) continue;
      ctx.fillStyle = row.open;
      ctx.fillRect(x1, y, x2 - x1, rowH - 1);
    }
    ctx.fillStyle = COLORS.textDim;
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(row.label, 4, y + rowH / 2);
  }
}

function drawEventsLane(ref, rect) {
  drawLaneBackground(rect, 'EVENTS');
  const cutoff = ref - horizon;
  const visible = events.filter((e) => (
    e.t >= cutoff
    && EVENT_STYLES[e.type]
    && e.type !== 'partial'
    && e.type !== 'commit'
  ));
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
  const items = transcriptItems(ref, cutoff);
  if (!items.length) return;
  ctx.font = '11px "JetBrains Mono", monospace';
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'left';

  const rowH = 18;
  const rowCount = Math.max(2, Math.floor((rect.h - 8) / rowH));
  const maxChars = maximized ? 120 : 40;
  const rowEnds = new Array(rowCount).fill(-Infinity);

  for (const item of items) {
    const x = timeToX(item.t, ref);
    const display = item.text ? item.text.slice(0, maxChars) : '';
    const label = item.wrap ? item.wrap[0] + display + item.wrap[1] : display;
    const width = Math.max(item.minWidth || 0, ctx.measureText(label).width + 10);

    let rowIdx = 0;
    while (rowIdx < rowCount - 1 && rowEnds[rowIdx] > x) rowIdx++;
    rowEnds[rowIdx] = x + width;

    const y = rect.y + 4 + rowIdx * rowH + rowH / 2;
    const boxY = y - 8;
    if (item.fill) {
      ctx.fillStyle = withAlpha(item.color, 0.18);
      ctx.fillRect(x, boxY, width, 16);
    }
    ctx.strokeStyle = item.color;
    ctx.lineWidth = 1;
    if (item.dashed) ctx.setLineDash([3, 3]);
    ctx.strokeRect(x + 0.5, boxY + 0.5, width - 1, 15);
    if (item.dashed) ctx.setLineDash([]);
    if (label) {
      ctx.fillStyle = item.fill ? COLORS.text : item.color;
      ctx.fillText(label, x + 4, y);
    }
  }
}

function transcriptItems(ref, cutoff) {
  const assistantByTurn = new Map();
  for (const event of events) {
    if (event.type === 'assistant' && event.turn_id != null) {
      assistantByTurn.set(event.turn_id, event);
    }
  }
  const items = [];
  for (const event of events) {
    if (event.t < cutoff) continue;
    if (event.type === 'partial' && event.text) {
      items.push({ t: event.t, text: String(event.text), wrap: ['…', ''], color: COLORS.textDim, fill: true });
    } else if (event.type === 'commit' && event.text) {
      items.push({ t: event.t, text: String(event.text), wrap: ['"', '"'], color: COLORS.accent, fill: true });
    } else if (event.type === 'assistant_start') {
      const matched = assistantByTurn.get(event.turn_id);
      if (matched) {
        items.push({ t: event.t, text: String(matched.text || ''), wrap: ['‹', '›'], color: ASSISTANT_COLOR, fill: true });
      } else {
        items.push({ t: event.t, text: '', color: ASSISTANT_COLOR, fill: false, dashed: true, minWidth: 60 });
      }
    }
  }
  items.sort((a, b) => a.t - b.t);
  return items;
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
    lines.push(`mic peak  ${sample[IDX_MIC]}`);
    lines.push(`scribe    ${sample[IDX_SCRIBE_GATE] ? 'sending' : 'silence'}`);
    lines.push(`playback  ${sample[IDX_PLAYBACK]}`);
    lines.push(`threshold ${sample[IDX_THRESHOLD]}`);
    lines.push(`barge     ${sample[IDX_BARGE_GATE] ? 'open' : 'closed'}`);
  }
  for (const ev of nearbyEvents.slice(-4)) {
    const extra = ev.reason
      || (ev.type === 'phase' ? `${ev.name} ${ev.on ? 'on' : 'off'}` : '')
      || (ev.text ? String(ev.text).slice(0, 40) : '');
    lines.push(`> ${ev.type}${extra ? ' ' + extra : ''}`);
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
