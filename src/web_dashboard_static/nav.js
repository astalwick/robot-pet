import { row, setRows, fmt, escapeHtml } from './dom.js';
import { appendLog } from './logs.js';

// The map panel: the live occupancy grid from the nav bridge with the robot,
// the saved places and the navigation goal drawn on it, plus the mode / map /
// place controls. Clicking the map sends the robot there.

let mapImage = null;
let mapSerial = null;
let mapInfo = null;
let latestNav = null;
let navStale = true;

export function renderNav(snapshot, sources) {
  const nav = snapshot.nav;
  navStale = !nav || (sources.nav || {}).stale !== false;
  if (navStale) {
    setRows('nav-rows', [row('status', '', 'STALE', 'err')]);
    latestNav = null;
    updateNavControls();
    return;
  }
  latestNav = nav;

  const pose = nav.pose;
  const rows = [
    row('mode', '', nav.mode, nav.mode === 'localize' ? 'ok' : ''),
    row('map', '', nav.map || (nav.mode === 'mapping' ? 'new map' : '--')),
    row('localized', '', nav.localized ? 'yes' : 'no', nav.localized ? 'ok' : 'muted'),
    row('activity', '', nav.activity || 'idle', nav.activity ? 'warn' : 'muted'),
    row('pose', '', pose ? `${fmt(pose.x, ' m', 2)}, ${fmt(pose.y, ' m', 2)}, ${fmt((pose.yaw * 180) / Math.PI, '°', 0)}` : '--'),
    row('places', '', String(Object.keys(nav.places || {}).length)),
    row('goal', '', goalText(nav.goal), goalClass(nav.goal)),
  ];
  setRows('nav-rows', rows);

  if (nav.map_info && nav.map_info.serial !== mapSerial) loadMap(nav.map_info);
  mapInfo = nav.map_info;
  drawMap();
  updateNavControls();
}

function goalText(goal) {
  if (!goal) return '--';
  const target = goal.target || `${fmt(goal.x, '', 1)}, ${fmt(goal.y, '', 1)}`;
  if (goal.state === 'navigating') {
    return `${target}, ${goal.remaining_m == null ? '?' : fmt(goal.remaining_m, ' m', 1)} left`;
  }
  return `${goal.state} ${target}${goal.reason ? ` (${goal.reason})` : ''}`;
}

function goalClass(goal) {
  if (!goal) return 'muted';
  return { navigating: 'warn', arrived: 'ok', failed: 'err' }[goal.state] || 'muted';
}

function loadMap(info) {
  mapSerial = info.serial;
  const image = new Image();
  image.onload = () => {
    mapImage = image;
    drawMap();
  };
  image.src = `/map.png?serial=${info.serial}`;
}

function drawMap() {
  const canvas = document.getElementById('nav-map');
  if (!canvas || !mapImage || !mapInfo) return;
  if (canvas.width !== mapImage.width || canvas.height !== mapImage.height) {
    canvas.width = mapImage.width;
    canvas.height = mapImage.height;
  }
  const ctx = canvas.getContext('2d');
  ctx.drawImage(mapImage, 0, 0);

  // Map frame (meters, y up) to image pixels (y down).
  const toPixel = (x, y) => [
    (x - mapInfo.origin_x) / mapInfo.resolution,
    mapInfo.height - (y - mapInfo.origin_y) / mapInfo.resolution,
  ];
  const nav = latestNav;
  ctx.font = '11px sans-serif';
  ctx.textBaseline = 'middle';
  if (nav.goal && nav.goal.state === 'navigating') {
    const [px, py] = toPixel(nav.goal.x, nav.goal.y);
    ctx.strokeStyle = '#4ade80';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(px - 5, py - 5);
    ctx.lineTo(px + 5, py + 5);
    ctx.moveTo(px - 5, py + 5);
    ctx.lineTo(px + 5, py - 5);
    ctx.stroke();
  }
  for (const [name, place] of Object.entries(nav.places || {})) {
    if (place.map !== nav.map) continue;
    const [px, py] = toPixel(place.x, place.y);
    ctx.fillStyle = '#facc15';
    ctx.beginPath();
    ctx.arc(px, py, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillText(name, px + 6, py);
  }
  if (nav.pose) {
    const [px, py] = toPixel(nav.pose.x, nav.pose.y);
    const size = 6;
    ctx.save();
    ctx.translate(px, py);
    ctx.rotate(-nav.pose.yaw);
    ctx.fillStyle = nav.localized || nav.mode === 'mapping' ? '#00d4ff' : '#f87171';
    ctx.beginPath();
    ctx.moveTo(size, 0);
    ctx.lineTo(-size, size * 0.7);
    ctx.lineTo(-size, -size * 0.7);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }
}

// Like the lidar button: write the DOM only when something changed, so a
// re-render never swallows a click, and never disable a button for being busy.
function updateNavControls() {
  const nav = latestNav;
  const mapping = nav != null && nav.mode === 'mapping';
  const localize = nav != null && nav.mode === 'localize';
  setDisabled('nav-start-mapping-button', navStale || mapping);
  setDisabled('nav-save-map-button', !mapping);
  setDisabled('nav-localize-button', !localize);
  setDisabled('nav-save-place-button', !(localize && nav.localized && nav.pose));
  setDisabled('nav-cancel-button', !(nav && nav.goal && nav.goal.state === 'navigating'));

  const select = document.getElementById('nav-map-select');
  const maps = nav ? nav.maps : [];
  const options = maps.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
  if (select.innerHTML !== options) {
    select.innerHTML = options;
    if (nav && nav.map) select.value = nav.map;
  }
}

function setDisabled(id, disabled) {
  const button = document.getElementById(id);
  if (button.disabled !== disabled) button.disabled = disabled;
}

export function bindNavHandlers(bindOn) {
  bindOn('nav-start-mapping-button', 'click', () => sendNav({ cmd: 'set_mode', mode: 'mapping' }));
  bindOn('nav-save-map-button', 'click', onSaveMap);
  bindOn('nav-localize-button', 'click', onLocalize);
  bindOn('nav-save-place-button', 'click', onSavePlace);
  bindOn('nav-cancel-button', 'click', () => sendNav({ cmd: 'nav_cancel' }));
  bindOn('nav-map', 'click', onMapClick);
}

// A click on the map drives the robot to that spot. The canvas is scaled to
// the panel width, so go from CSS pixels back to image pixels, then undo
// drawMap's toPixel.
function onMapClick(event) {
  if (!mapInfo || navStale) return;
  const canvas = event.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const px = ((event.clientX - rect.left) * canvas.width) / rect.width;
  const py = ((event.clientY - rect.top) * canvas.height) / rect.height;
  const x = px * mapInfo.resolution + mapInfo.origin_x;
  const y = (mapInfo.height - py) * mapInfo.resolution + mapInfo.origin_y;
  sendNav({ cmd: 'navigate_to', x: Number(x.toFixed(2)), y: Number(y.toFixed(2)) });
}

function onSaveMap() {
  const input = document.getElementById('nav-map-name');
  const name = input.value.trim();
  if (!name) return;
  sendNav({ cmd: 'save_map', name }).then((ok) => { if (ok) input.value = ''; });
}

function onLocalize() {
  const map = document.getElementById('nav-map-select').value;
  if (latestNav && map && map !== latestNav.map) {
    sendNav({ cmd: 'set_mode', mode: 'localize', map });
    return;
  }
  sendNav({ cmd: 'localize' });
}

function onSavePlace() {
  const input = document.getElementById('nav-place-name');
  const name = input.value.trim();
  if (!name) return;
  sendNav({ cmd: 'create_place', name }).then((ok) => { if (ok) input.value = ''; });
}

async function sendNav(message) {
  let error = null;
  try {
    const response = await fetch('/nav/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(message),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      error = body.error || response.statusText;
    }
  } catch (exc) {
    error = exc;
  }
  if (error) appendLog(`nav ${message.cmd} failed: ${error}`);
  return error === null;
}
