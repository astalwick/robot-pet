import { row, setRows, fmt, fmtRelativeSeconds } from './dom.js';
import { appendLog } from './logs.js';

// foxglove_bridge (robot-foxglove.service) listens here on the robot.
const FOXGLOVE_PORT = 8765;

const STATUS_CLASSES = {
  ok: 'ok',
  warning: 'warn',
  fault: 'err',
  no_scans: 'err',
  driver_stopped: 'err',
  off: 'muted',
  unplugged: 'muted',
};

// diagnostic_msgs/DiagnosticStatus levels: OK, WARN, ERROR, STALE
const DIAGNOSTIC_CLASSES = ['ok', 'warn', 'err', 'muted'];

// Like the voice toggle: a click flips what we want right away, and the button
// says "Turning on/off" until telemetry agrees. It stays clickable throughout.
let lidarWantActive = false;
let lidarTelemetryActive = false;
let lidarDriverRunning = false;

export function renderLidar(snapshot, sources) {
  const lidar = snapshot.lidar;
  if (!lidar || (sources.lidar || {}).stale !== false) {
    setRows('lidar-rows', [row('status', '', 'STALE', 'err')]);
    lidarDriverRunning = false;
    updateLidarPowerButton();
    return;
  }

  // When the lidar changes state, that's the new truth, whether or not a click
  // did it (a driver restart comes back on).
  if (lidar.lidar_active !== lidarTelemetryActive) lidarWantActive = lidar.lidar_active;
  lidarTelemetryActive = lidar.lidar_active;
  lidarDriverRunning = lidar.service_active;
  updateLidarPowerButton();

  const rows = [
    row('status', '', lidar.status.replace('_', ' ').toUpperCase(), STATUS_CLASSES[lidar.status]),
    row('device', '', lidar.device_present ? 'plugged in' : 'not found', lidar.device_present ? 'ok' : 'muted'),
    row('driver', '', lidar.service_active ? 'running' : 'stopped', lidar.service_active ? 'ok' : 'muted'),
    row('scan rate', '', fmt(lidar.scan_hz, ' Hz', 1)),
    row('points', '', lidar.points != null ? `${lidar.valid_points} / ${lidar.points}` : '--'),
    row('last scan', '', fmtRelativeSeconds(lidar.scan_age_seconds)),
    row('slam', '', lidar.slam_active ? 'running' : 'stopped', lidar.slam_active ? 'ok' : 'muted'),
  ];
  for (const status of lidar.diagnostics) {
    rows.push(row(status.name, '', status.message || '--', DIAGNOSTIC_CLASSES[status.level]));
    for (const [key, value] of Object.entries(status.values)) {
      // Device Info leads with a 32-character serial number that stretches the column.
      rows.push(row(key, '', value.replace(/S\/N: \S+ \| /, '')));
    }
  }
  setRows('lidar-rows', rows);
}

// The panel re-renders on every snapshot. Rewriting the button when nothing
// changed swaps its text node mid-click and the browser drops the click.
function updateLidarPowerButton() {
  const button = document.getElementById('lidar-power-button');
  let text = lidarWantActive ? 'Turn off' : 'Turn on';
  if (lidarWantActive !== lidarTelemetryActive) text = lidarWantActive ? 'Turning on' : 'Turning off';
  // On and off go through the driver, so it has to be running.
  const disabled = !lidarDriverRunning;
  if (button.textContent !== text) button.textContent = text;
  if (button.disabled !== disabled) button.disabled = disabled;
}

export function bindLidarHandlers(bindOn) {
  bindOn('lidar-power-button', 'click', onLidarPower);
  bindOn('lidar-foxglove-button', 'click', onOpenFoxglove);
}

async function onLidarPower() {
  lidarWantActive = !lidarWantActive;
  updateLidarPowerButton();
  const verb = lidarWantActive ? 'turn on' : 'turn off';
  let error = null;
  try {
    const response = await fetch('/lidar/power', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on: lidarWantActive }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      error = body.error || response.statusText;
    }
  } catch (exc) {
    error = exc;
  }
  if (error) {
    appendLog(`lidar ${verb} failed: ${error}`);
    lidarWantActive = lidarTelemetryActive;
    updateLidarPowerButton();
  }
}

// Opens the Foxglove desktop app (or app.foxglove.dev if it isn't installed)
// already connected to the robot, using the same host as this dashboard.
function onOpenFoxglove() {
  const bridgeUrl = encodeURIComponent(`ws://${window.location.hostname}:${FOXGLOVE_PORT}`);
  window.open(`https://app.foxglove.dev/~/view?ds=foxglove-websocket&ds.url=${bridgeUrl}&openIn=desktop`, '_blank');
}
