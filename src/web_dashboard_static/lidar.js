import { row, setRows, fmt, fmtRelativeSeconds } from './dom.js';
import { appendLog } from './logs.js';

// foxglove_bridge (robot-foxglove.service) listens here on the robot.
const FOXGLOVE_PORT = 8765;

const STATUS_CLASSES = {
  ok: 'ok',
  warning: 'warn',
  fault: 'err',
  no_scans: 'err',
  off: 'muted',
  unplugged: 'muted',
};

// diagnostic_msgs/DiagnosticStatus levels: OK, WARN, ERROR, STALE
const DIAGNOSTIC_CLASSES = ['ok', 'warn', 'err', 'muted'];

let lidarServiceActive = false;
let lidarPowerBusy = false;

export function renderLidar(snapshot, sources) {
  const lidar = snapshot.lidar;
  const powerButton = document.getElementById('lidar-power-button');
  if (!lidar || (sources.lidar || {}).stale !== false) {
    setRows('lidar-rows', [row('status', '', 'STALE', 'err')]);
    powerButton.disabled = true;
    return;
  }

  lidarServiceActive = lidar.service_active;
  powerButton.textContent = lidar.service_active ? 'Turn off' : 'Turn on';
  powerButton.disabled = lidarPowerBusy || !lidar.device_present;

  const rows = [
    row('status', '', lidar.status.toUpperCase(), STATUS_CLASSES[lidar.status]),
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

export function bindLidarHandlers(bindOn) {
  bindOn('lidar-power-button', 'click', onLidarPower);
  bindOn('lidar-foxglove-button', 'click', onOpenFoxglove);
}

async function onLidarPower(event) {
  const button = event.currentTarget;
  const verb = lidarServiceActive ? 'stop' : 'start';
  lidarPowerBusy = true;
  button.disabled = true;
  try {
    const response = await fetch('/lidar/power', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on: !lidarServiceActive }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      appendLog(`lidar ${verb} failed: ${body.error || response.statusText}`);
    }
  } catch (exc) {
    appendLog(`lidar ${verb} failed: ${exc}`);
  } finally {
    lidarPowerBusy = false;
  }
}

// Opens the Foxglove desktop app (or app.foxglove.dev if it isn't installed)
// already connected to the robot, using the same host as this dashboard.
function onOpenFoxglove() {
  const bridgeUrl = encodeURIComponent(`ws://${window.location.hostname}:${FOXGLOVE_PORT}`);
  window.open(`https://app.foxglove.dev/~/view?ds=foxglove-websocket&ds.url=${bridgeUrl}&openIn=desktop`, '_blank');
}
