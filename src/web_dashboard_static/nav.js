import { row, setRows, fmt, escapeHtml } from './dom.js';
import { appendLog } from './logs.js';

// The map: the live occupancy grid from the nav bridge with the robot, the
// saved places, the barriers and the navigation goal drawn on it. It shares
// the camera's box (the Camera / Map switch picks one) and clicking it sends
// the robot there, unless a map tool is on: Draw barrier takes two clicks for
// a wall Nav2 must not cross, Erase deletes the barrier or place clicked. The
// Map panel holds the mode / map / place controls.

// Same as robot_nav.BARRIER_HALF_WIDTH_METERS: how far each side of a barrier
// line the bridge paints forbidden.
const BARRIER_HALF_WIDTH_M = 0.3;
// ?debug shows the judge's numbers and the Record button for the replay tool
const DEBUG = new URLSearchParams(location.search).has('debug');

// How sure the robot is of its position, for the panel. Mapping has no state.
const POSITION_TEXT = {
  tracking: ['sure', 'ok'],
  checking: ['checking my position, sure after a short drive', 'warn'],
  searching: ['finding my place, moving me by hand helps', 'err'],
  off_map: ["can't find my place here, put me somewhere I know or start mapping below", 'err'],
  stale: ['lidar off', 'muted'],
};

// What a held trip is waiting for.
const WAITING_TEXT = {
  lidar: 'the lidar',
  position: 'my position',
  nav2: 'the planner',
};

// While someone has the robot in hand, instead of the position text.
const HANDLED_TEXT = {
  'picked up': ["picked up, I'll check my place once I'm put down", 'warn'],
  pushed: ["pushed, I'll check my place once I'm still", 'warn'],
};

let mapImage = null;
let mapSerial = null;
let mapInfo = null;
let latestNav = null;
let navStale = true;
let mapTool = null; // 'barrier' | 'erase' | null (click drives there)
let barrierStart = null; // the first click of a barrier, in map metres

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
    row('position', '', ...(HANDLED_TEXT[nav.handled] || POSITION_TEXT[nav.state] || ['mapping', 'muted'])),
    row('activity', '', nav.activity || 'idle', nav.activity ? 'warn' : 'muted'),
    row('pose', '', pose ? `${fmt(pose.x, ' m', 2)}, ${fmt(pose.y, ' m', 2)}, ${fmt((pose.yaw * 180) / Math.PI, '°', 0)}` : '--'),
    row('places', '', String(Object.keys(nav.places || {}).length)),
    row('goal', '', goalText(nav.goal), goalClass(nav.goal)),
  ];
  if (DEBUG) {
    const check = nav.check;
    rows.push(
      row('state', '', `${nav.state || '--'}${nav.localized ? ', localized' : ''}`),
      row('judge', '', check ? `agree ${check.agree}, through ${check.through}, opinion ${check.opinion}${check.self_check == null ? '' : `, self-check ${check.self_check}`}` : '--', check ? (check.passes ? 'ok' : 'err') : 'muted'),
      row('search', '', searchText(nav.search), nav.search && nav.search.rival === 'stands' ? 'err' : 'muted'),
      row('recording', '', nav.recording || 'no', nav.recording ? 'warn' : 'muted'),
    );
  }
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
  if (goal.state === 'held') {
    return `${target}, waiting for ${WAITING_TEXT[goal.waiting_for] || 'a moment'}`;
  }
  return `${goal.state} ${target}${goal.reason ? ` (${goal.reason})` : ''}`;
}

// the last search: how long it took, its best place, and a winner or rival
function searchText(search) {
  if (!search) return '--';
  const best = search.best ? `best ${search.best.agree} on ${search.best.map}` : 'nothing';
  const found = search.winner ? ', clear winner' : '';
  const rival = search.rival ? `, better place ${search.rival}` : '';
  return `${search.seconds} s, ${best}${found}${rival}`;
}

function goalClass(goal) {
  if (!goal) return 'muted';
  return { held: 'warn', navigating: 'warn', arrived: 'ok', failed: 'err' }[goal.state] || 'muted';
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
  if (!canvas || !mapImage || !mapInfo || !latestNav) return;
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
  // the trip's goal: a cross while driving, a ring while held
  if (nav.goal && nav.goal.map === nav.map && (nav.goal.state === 'navigating' || nav.goal.state === 'held')) {
    const [px, py] = toPixel(nav.goal.x, nav.goal.y);
    ctx.strokeStyle = '#4ade80';
    ctx.lineWidth = 2;
    ctx.beginPath();
    if (nav.goal.state === 'held') {
      ctx.arc(px, py, 5, 0, Math.PI * 2);
    } else {
      ctx.moveTo(px - 5, py - 5);
      ctx.lineTo(px + 5, py + 5);
      ctx.moveTo(px - 5, py + 5);
      ctx.lineTo(px + 5, py - 5);
    }
    ctx.stroke();
  }
  for (const barrier of barriersOnMap(nav)) {
    const [x1, y1] = toPixel(barrier.x1, barrier.y1);
    const [x2, y2] = toPixel(barrier.x2, barrier.y2);
    ctx.strokeStyle = 'rgba(248, 113, 113, 0.35)';
    ctx.lineWidth = (2 * BARRIER_HALF_WIDTH_M) / mapInfo.resolution;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.strokeStyle = '#f87171';
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  if (barrierStart) {
    const [px, py] = toPixel(barrierStart.x, barrierStart.y);
    ctx.fillStyle = '#f87171';
    ctx.beginPath();
    ctx.arc(px, py, 3, 0, Math.PI * 2);
    ctx.fill();
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
  // off the map the pose could be anywhere, so it is not drawn
  if (nav.pose && nav.state !== 'off_map') {
    const [px, py] = toPixel(nav.pose.x, nav.pose.y);
    const size = 6;
    ctx.save();
    ctx.translate(px, py);
    ctx.rotate(-nav.pose.yaw);
    ctx.beginPath();
    ctx.moveTo(size, 0);
    ctx.lineTo(-size, size * 0.7);
    ctx.lineTo(-size, -size * 0.7);
    ctx.closePath();
    // solid when sure, soft while checking, grey with the lidar off, hollow when lost
    if (nav.state === 'searching') {
      ctx.strokeStyle = '#f87171';
      ctx.lineWidth = 2;
      ctx.stroke();
    } else if (nav.state === 'stale') {
      ctx.fillStyle = '#8a8a8a';
      ctx.fill();
    } else {
      ctx.fillStyle = nav.state === 'tracking' || nav.mode === 'mapping' ? '#00d4ff' : 'rgba(0, 212, 255, 0.45)';
      ctx.fill();
    }
    ctx.restore();
  }
}

function barriersOnMap(nav) {
  return (nav.barriers || {})[nav.map] || [];
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
  // a place saved on an unconfirmed position could be off by a room
  setDisabled('nav-save-place-button', !(localize && nav.state === 'tracking' && nav.pose));
  setDisabled('nav-record-button', !localize || nav.recording != null);
  setDisabled('nav-cancel-button', !(nav && nav.goal && (nav.goal.state === 'navigating' || nav.goal.state === 'held')));

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
  bindOn('view-camera-button', 'click', () => showMap(false));
  bindOn('view-map-button', 'click', () => showMap(true));
  bindOn('nav-draw-barrier-button', 'click', () => setMapTool('barrier'));
  bindOn('nav-erase-button', 'click', () => setMapTool('erase'));
  bindOn('nav-record-button', 'click', onRecord);
  document.getElementById('nav-debug-actions').classList.toggle('hidden', !DEBUG);
}

// A tool stays on until clicked again (or the other one is picked).
function setMapTool(tool) {
  mapTool = mapTool === tool ? null : tool;
  barrierStart = null;
  document.getElementById('nav-draw-barrier-button').classList.toggle('active', mapTool === 'barrier');
  document.getElementById('nav-erase-button').classList.toggle('active', mapTool === 'erase');
  drawMap();
}

function showMap(show) {
  document.getElementById('camera-section').classList.toggle('show-map', show);
  document.getElementById('view-camera-button').classList.toggle('active', !show);
  document.getElementById('view-map-button').classList.toggle('active', show);
}

// A click on the map drives the robot to that spot, or draws or erases when a
// tool is on. The canvas fills the camera box with object-fit: contain, so
// find where the image actually sits, go from CSS pixels back to image
// pixels, then undo drawMap's toPixel.
function onMapClick(event) {
  if (!mapInfo || navStale) return;
  const canvas = event.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const scale = Math.min(rect.width / canvas.width, rect.height / canvas.height);
  const left = rect.left + (rect.width - canvas.width * scale) / 2;
  const top = rect.top + (rect.height - canvas.height * scale) / 2;
  const px = (event.clientX - left) / scale;
  const py = (event.clientY - top) / scale;
  if (px < 0 || py < 0 || px > canvas.width || py > canvas.height) return;
  const x = Number((px * mapInfo.resolution + mapInfo.origin_x).toFixed(2));
  const y = Number(((mapInfo.height - py) * mapInfo.resolution + mapInfo.origin_y).toFixed(2));
  if (mapTool === 'barrier') {
    if (!barrierStart) {
      barrierStart = { x, y };
    } else {
      sendNav({ cmd: 'create_barrier', x1: barrierStart.x, y1: barrierStart.y, x2: x, y2: y });
      barrierStart = null;
    }
    drawMap();
  } else if (mapTool === 'erase') {
    eraseAt(x, y);
  } else {
    sendNav({ cmd: 'navigate_to', x, y });
  }
}

// Delete whatever was clicked: a place within 0.3 m, or a barrier whose band
// covers the point. The closest one wins.
function eraseAt(x, y) {
  const nav = latestNav;
  let closest = { distance: BARRIER_HALF_WIDTH_M, cmd: null };
  for (const [name, place] of Object.entries(nav.places || {})) {
    if (place.map !== nav.map) continue;
    const distance = Math.hypot(place.x - x, place.y - y);
    if (distance <= closest.distance) closest = { distance, cmd: 'delete_place', name };
  }
  barriersOnMap(nav).forEach((barrier, index) => {
    const distance = distanceToSegment(x, y, barrier);
    if (distance <= closest.distance) closest = { distance, cmd: 'delete_barrier', index };
  });
  if (!closest.cmd) return;
  const { distance, ...message } = closest;
  sendNav(message);
}

function distanceToSegment(x, y, barrier) {
  const dx = barrier.x2 - barrier.x1;
  const dy = barrier.y2 - barrier.y1;
  const lengthSq = dx * dx + dy * dy;
  const along = lengthSq === 0 ? 0 : Math.min(1, Math.max(0, ((x - barrier.x1) * dx + (y - barrier.y1) * dy) / lengthSq));
  return Math.hypot(x - (barrier.x1 + along * dx), y - (barrier.y1 + along * dy));
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

// Save the next few seconds of scans for scripts/localization-replay.py,
// labelled with where the robot is ("cross 2") or what just went wrong.
function onRecord() {
  const input = document.getElementById('nav-record-label');
  const label = input.value.trim();
  if (!label) return;
  const seconds = Number(document.getElementById('nav-record-seconds').value);
  sendNav({ cmd: 'record', label, seconds }).then((ok) => { if (ok) input.value = ''; });
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
