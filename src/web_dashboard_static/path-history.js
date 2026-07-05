// Estimated local path: a short breadcrumb trail dead-reckoned in the browser
// from IMU yaw and cumulative wheel distances. This drifts and is deliberately
// local-only; it is a debugging/intuition tool, not a map. History lives in
// browser memory and starts empty on every dashboard refresh.

const HISTORY_SECONDS = 180;
const MIN_SPAN_M = 1.0; // smallest visible world span so tiny moves don't jump
const TAU = Math.PI * 2;
const GRID_STEPS_M = [0.25, 0.5, 1, 2, 5, 10, 20];

const COLORS = {
  bg: '#020a10',
  grid: '#0a2538',
  trail: '#00d4ff',
  robot: '#4ade80',
  text: '#c0e8f0',
  textDim: '#6a8a9a',
};

let xMeters = 0;
let yMeters = 0;
let previousLeft = null;
let previousRight = null;
let lastSnapshotTime = null;
let points = [];
let status = 'awaiting odometry';

let canvas = null;
let ctx = null;
let wrap = null;
let section = null;
let statusEl = null;
let maximizeButton = null;
let resetButton = null;
let closeButton = null;
let maximized = false;
let dpr = 1;
let viewW = 0;
let viewH = 0;
let rafToken = null;

export function initPathHistory() {
  canvas = document.getElementById('path-canvas');
  wrap = document.getElementById('path-canvas-wrap');
  section = document.getElementById('path-section');
  statusEl = document.getElementById('path-status');
  maximizeButton = document.getElementById('path-maximize');
  resetButton = document.getElementById('path-reset');
  closeButton = document.getElementById('path-close');
  if (!canvas || !wrap) return;
  ctx = canvas.getContext('2d');

  resize();
  window.addEventListener('resize', resize);
  if (window.ResizeObserver) new ResizeObserver(resize).observe(wrap);

  if (maximizeButton) maximizeButton.addEventListener('click', () => setMaximized(true));
  if (closeButton) closeButton.addEventListener('click', () => setMaximized(false));
  if (resetButton) resetButton.addEventListener('click', resetPathHistory);
}

export function updatePathHistory(snapshot) {
  const sources = snapshot.sources || {};
  const motionStale = (sources.robot_motion || {}).stale !== false;
  const sensors = snapshot.sensors;
  // Match the IMU panel's liveness rule: a fresh sensors source, actively
  // polling, with a good IMU reading. The snapshot keeps the last sensors
  // payload even when the source goes stale, so imu.ok alone is not enough.
  const imuLive = (sources.sensors || {}).stale === false
    && sensors?.status === 'polling'
    && sensors?.imu?.ok;
  const yaw = imuLive ? sensors.imu.yaw_degrees : null;
  const odometry = snapshot.odometry;

  // Any missing/stale input freezes the trail. We also drop the odometry
  // baseline so the next valid frame re-anchors at the current pose instead
  // of integrating everything that happened during the gap with one yaw.
  if (motionStale) {
    freeze('stale telemetry');
    return;
  }
  if (yaw == null) {
    freeze('awaiting imu');
    return;
  }
  if (!odometry || odometry.left_distance_m == null || odometry.right_distance_m == null) {
    freeze('awaiting odometry');
    return;
  }

  const time = Number(snapshot.time) || performance.now() / 1000;
  if (time === lastSnapshotTime) {
    setStatus('live');
    return;
  }
  lastSnapshotTime = time;

  const left = odometry.left_distance_m;
  const right = odometry.right_distance_m;
  if (previousLeft === null) {
    // First valid frame just anchors the baseline at the current pose.
    previousLeft = left;
    previousRight = right;
    points.push({ time, x_m: xMeters, y_m: yMeters, yaw_degrees: yaw });
    setStatus('live');
    return;
  }

  const travel = ((left - previousLeft) + (right - previousRight)) / 2;
  previousLeft = left;
  previousRight = right;

  const yawRad = (yaw * Math.PI) / 180;
  xMeters += travel * Math.cos(yawRad);
  yMeters += travel * Math.sin(yawRad);
  points.push({ time, x_m: xMeters, y_m: yMeters, yaw_degrees: yaw });

  expireOldPoints(time);
  setStatus('live');
}

export function resetPathHistory() {
  xMeters = 0;
  yMeters = 0;
  // Drop the baseline so the next valid frame re-anchors instead of jumping.
  previousLeft = null;
  previousRight = null;
  lastSnapshotTime = null;
  points = [];
  setStatus('awaiting odometry');
}

function freeze(statusText) {
  // Keep the visible trail but invalidate the baseline so movement during the
  // gap is never back-filled onto the next good frame.
  previousLeft = null;
  previousRight = null;
  setStatus(statusText);
}

function expireOldPoints(now) {
  const cutoff = now - HISTORY_SECONDS;
  // Points are appended in time order, so drop from the front until fresh.
  let firstFresh = 0;
  while (firstFresh < points.length && points[firstFresh].time < cutoff) firstFresh += 1;
  if (firstFresh > 0) points = points.slice(firstFresh);
}

function setStatus(next) {
  status = next;
  if (statusEl) statusEl.textContent = next;
}

function setMaximized(next) {
  maximized = next;
  // The path view only ever appears as a full overlay, so it stays hidden
  // until the IMU panel button opens it.
  if (section) {
    section.classList.toggle('hidden', !maximized);
    section.classList.toggle('maximized', maximized);
  }
  if (maximized) {
    resize();
    startFrames();
  } else {
    stopFrames();
  }
}

function startFrames() {
  if (rafToken != null) return;
  const tick = () => {
    render();
    rafToken = requestAnimationFrame(tick);
  };
  rafToken = requestAnimationFrame(tick);
}

function stopFrames() {
  if (rafToken != null) cancelAnimationFrame(rafToken);
  rafToken = null;
}

function resize() {
  if (!canvas || !wrap) return;
  dpr = window.devicePixelRatio || 1;
  const nextW = Math.max(1, wrap.clientWidth);
  const nextH = Math.max(1, wrap.clientHeight);
  if (nextW === viewW && nextH === viewH) return;
  viewW = nextW;
  viewH = nextH;
  canvas.width = Math.floor(viewW * dpr);
  canvas.height = Math.floor(viewH * dpr);
}

function render() {
  if (!ctx) return;
  ctx.save();
  ctx.scale(dpr, dpr);
  ctx.fillStyle = COLORS.bg;
  ctx.fillRect(0, 0, viewW, viewH);

  if (points.length === 0) {
    ctx.fillStyle = COLORS.textDim;
    ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(status, viewW / 2, viewH / 2);
    ctx.restore();
    return;
  }

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const point of points) {
    minX = Math.min(minX, point.x_m);
    maxX = Math.max(maxX, point.x_m);
    minY = Math.min(minY, point.y_m);
    maxY = Math.max(maxY, point.y_m);
  }
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  const spanX = Math.max(maxX - minX, MIN_SPAN_M);
  const spanY = Math.max(maxY - minY, MIN_SPAN_M);
  // One uniform meters-per-pixel keeps the top-down view square.
  const pxPerMeter = Math.min(viewW / (spanX * 1.25), viewH / (spanY * 1.25));
  const toScreenX = (x) => viewW / 2 + (x - centerX) * pxPerMeter;
  const toScreenY = (y) => viewH / 2 - (y - centerY) * pxPerMeter;

  const step = drawGrid(pxPerMeter, centerX, centerY, toScreenX, toScreenY);
  drawTrail(toScreenX, toScreenY);
  drawRobot(toScreenX, toScreenY);
  drawScaleBar(step, pxPerMeter);

  ctx.restore();
}

function drawGrid(pxPerMeter, centerX, centerY, toScreenX, toScreenY) {
  let step = GRID_STEPS_M[GRID_STEPS_M.length - 1];
  for (const candidate of GRID_STEPS_M) {
    if (candidate * pxPerMeter >= 45) { step = candidate; break; }
  }
  const leftM = centerX - viewW / 2 / pxPerMeter;
  const rightM = centerX + viewW / 2 / pxPerMeter;
  const bottomM = centerY - viewH / 2 / pxPerMeter;
  const topM = centerY + viewH / 2 / pxPerMeter;

  ctx.strokeStyle = COLORS.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let gx = Math.ceil(leftM / step) * step; gx <= rightM; gx += step) {
    const screenX = toScreenX(gx);
    ctx.moveTo(screenX, 0);
    ctx.lineTo(screenX, viewH);
  }
  for (let gy = Math.ceil(bottomM / step) * step; gy <= topM; gy += step) {
    const screenY = toScreenY(gy);
    ctx.moveTo(0, screenY);
    ctx.lineTo(viewW, screenY);
  }
  ctx.stroke();
  return step;
}

function drawTrail(toScreenX, toScreenY) {
  const now = points[points.length - 1].time;

  ctx.strokeStyle = COLORS.trail;
  ctx.globalAlpha = 0.25;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(toScreenX(points[0].x_m), toScreenY(points[0].y_m));
  for (const point of points) ctx.lineTo(toScreenX(point.x_m), toScreenY(point.y_m));
  ctx.stroke();

  ctx.fillStyle = COLORS.trail;
  for (const point of points) {
    ctx.globalAlpha = Math.max(0.06, 1 - (now - point.time) / HISTORY_SECONDS);
    ctx.beginPath();
    ctx.arc(toScreenX(point.x_m), toScreenY(point.y_m), 2, 0, TAU);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
}

function drawRobot(toScreenX, toScreenY) {
  const last = points[points.length - 1];
  const screenX = toScreenX(last.x_m);
  const screenY = toScreenY(last.y_m);
  const yawRad = (last.yaw_degrees * Math.PI) / 180;
  // World heading (cos, sin); screen y is flipped so sin negates.
  const headingX = Math.cos(yawRad);
  const headingY = -Math.sin(yawRad);
  const perpX = -headingY;
  const perpY = headingX;
  const size = 9;

  ctx.fillStyle = COLORS.robot;
  ctx.beginPath();
  ctx.moveTo(screenX + headingX * size, screenY + headingY * size);
  ctx.lineTo(screenX - headingX * size * 0.6 + perpX * size * 0.6, screenY - headingY * size * 0.6 + perpY * size * 0.6);
  ctx.lineTo(screenX - headingX * size * 0.6 - perpX * size * 0.6, screenY - headingY * size * 0.6 - perpY * size * 0.6);
  ctx.closePath();
  ctx.fill();
}

function drawScaleBar(step, pxPerMeter) {
  const barLength = step * pxPerMeter;
  const x0 = 12;
  const y0 = viewH - 14;
  ctx.strokeStyle = COLORS.text;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x0 + barLength, y0);
  ctx.stroke();

  ctx.fillStyle = COLORS.text;
  ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
  ctx.textAlign = 'left';
  ctx.fillText(`${step} m`, x0, y0 - 4);
}
