const SPARK_POINTS = 24;

export function on(id, eventName, handler) {
  const element = document.getElementById(id);
  if (element) {
    element.addEventListener(eventName, handler);
  } else {
    console.error('[dashboard bind] MISSING ELEMENT', id, eventName);
  }
}

export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function row(label, bar, value, cls = '') {
  return `<div class="row"><span class="label">${escapeHtml(label)}</span><span class="bar-cell">${bar || ''}</span><span class="value ${cls}">${escapeHtml(value)}</span></div>`;
}

export function doubleRow(label, leftVal, rightVal, leftCls = '', rightCls = '') {
  return `
      <div class="row">
        <span class="label">${escapeHtml(label)}</span>
        <span class="bar-cell value ${leftCls}">L ${leftVal}</span>
        <span class="value ${rightCls}">R ${rightVal}</span>
      </div>
    `;
}

export function trendRow(label, leftSpark, rightSpark) {
  return `
      <div class="row trend-row">
        <span class="label">${escapeHtml(label)}</span>
        <span class="trend-pair">
          <span class="trend-cell"><span class="trend-side">L</span>${leftSpark}</span>
          <span class="trend-cell"><span class="trend-side">R</span>${rightSpark}</span>
        </span>
      </div>
    `;
}

export function setRows(id, rows) {
  document.getElementById(id).innerHTML = Array.isArray(rows) ? rows.join('') : rows;
}

export function renderBar(value, limit, cls = '', absolute = true) {
  if (value == null || limit == null || limit <= 0) return '<span class="bar"></span>';
  const scaled = absolute ? Math.abs(value) : Math.max(0, value);
  const ratio = Math.max(0, Math.min(1, scaled / limit));
  return `<span class="bar ${cls}"><span class="fill" style="width: ${(ratio * 100).toFixed(1)}%"></span></span>`;
}

export function bipolarBar(value) {
  const ratio = value == null ? 0 : Math.max(-1, Math.min(1, value));
  const leftWidth = ratio < 0 ? Math.abs(ratio) * 100 : 0;
  const rightWidth = ratio > 0 ? ratio * 100 : 0;
  return `
      <span class="bipolar">
        <span class="left"><span class="fill" style="width: ${leftWidth.toFixed(1)}%"></span></span>
        <span class="mid"></span>
        <span class="right"><span class="fill" style="width: ${rightWidth.toFixed(1)}%"></span></span>
      </span>
    `;
}

export function sparkline(values, limit = null, absolute = false) {
  const clean = values.filter((value) => value != null).map((value) => absolute ? Math.abs(value) : value);
  const points = clean.slice(-SPARK_POINTS);
  if (points.length < 2) return '<span class="sparkline sparkline-empty"></span>';

  const low = limit == null ? Math.min(...points) : 0;
  const high = limit == null ? Math.max(...points) : limit;
  const span = high - low;
  const stepX = 100 / (points.length - 1);
  const coords = points.map((value, index) => {
    const ratio = span === 0 ? 0.5 : Math.max(0, Math.min(1, (value - low) / span));
    return `${(index * stepX).toFixed(1)},${(100 - ratio * 100).toFixed(1)}`;
  }).join(' ');
  return `<svg class="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><polyline points="${coords}"></polyline></svg>`;
}

export function fmt(value, suffix, digits) {
  if (value == null) return '--';
  if (typeof value === 'number') return value.toFixed(digits) + (suffix || '');
  return value + (suffix || '');
}

export function fmtRelativeSeconds(seconds) {
  if (seconds == null) return 'never';
  if (seconds < 10) return `${seconds.toFixed(1)}s ago`;
  return `${Math.floor(seconds)}s ago`;
}

export function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}:${pad(m)}:${pad(s)}`;
  return `${pad(m)}:${pad(s)}`;
}

function pad(n) {
  return n.toString().padStart(2, '0');
}

export function updateSession(sessionStart) {
  const seconds = Math.floor((Date.now() - sessionStart) / 1000);
  const uptime = document.getElementById('session-uptime');
  if (uptime) uptime.textContent = formatDuration(seconds);
}
