import {
  row,
  doubleRow,
  trendRow,
  setRows,
  renderBar,
  bipolarBar,
  sparkline,
  fmt,
  fmtRelativeSeconds,
} from './dom.js';
import { renderFaceOverlay } from './camera.js';
import { renderVoice } from './voice.js';
import { renderCost } from './cost.js';
import { updateVoiceTimeline } from './voice-timeline.js';
import { updateVoiceTurnStats } from './voice-turn-stats.js';

const HISTORY_LENGTH = 48;

const history = {
  pack_voltage: [],
  left_current: [],
  right_current: [],
  left_actual: [],
  right_actual: [],
  left_error: [],
  right_error: [],
};
let maxCurrentAmps = 0;
let maxAbsSpeedQpps = 1;

export function connectTelemetry() {
  const events = new EventSource('/events');
  events.addEventListener('message', (event) => {
    let snapshot;
    try {
      snapshot = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    render(snapshot);
  });
  events.addEventListener('error', () => {
    setTelemetryStatus('disconnected', 'err');
  });
}

function render(snapshot) {
  const sources = snapshot.sources || {};
  const gamepadStale = (sources.gamepad_teleop || {}).stale !== false;
  const motionSource = sources.robot_motion || {};
  const motionStale = motionSource.stale !== false;
  const systemStale = (sources.system || {}).stale !== false;
  // Battery/wheels/link/drive are owned by robot_motion once it has published.
  // gamepad_teleop is only the startup/old-snapshot fallback.
  const motionOwnsMotor = motionSource.last_seen != null || motionSource.stale === false;
  const motorStale = motionOwnsMotor ? motionStale : gamepadStale;

  recordHistory(snapshot, !motorStale);

  const controller = snapshot.controller || {};
  const wheels = snapshot.wheels || {};
  const battery = snapshot.motor_battery || {};
  const motorRail = snapshot.motor_rail || {};
  const linkLoop = snapshot.link_loop || {};
  const driveStatusPayload = snapshot.drive_status || {};
  const pi = snapshot.pi || {};

  renderHud(motorStale, systemStale, controller, wheels, battery, pi, driveStatusPayload);
  renderPi(pi);
  renderBattery(battery, motorRail, sources.motor_rail || {});
  renderController(controller);
  renderWheels(wheels);
  renderLink(linkLoop, driveStatusPayload);
  renderVoice(snapshot, sources);
  renderCost(snapshot);
  renderSensors(snapshot, sources);
  updateVoiceTimeline((snapshot.voice || {}).timeline);
  updateVoiceTurnStats((snapshot.voice || {}).timeline);
  renderFaceOverlay(snapshot, sources);
  setTelemetryStatus(gamepadStale ? 'stale' : 'live', gamepadStale ? 'warn' : 'ok');
}

function recordHistory(snapshot, gamepadLive) {
  if (!gamepadLive) return;
  const wheels = snapshot.wheels || {};
  const battery = snapshot.motor_battery || {};
  pushHistory('pack_voltage', battery.pack_voltage);
  pushHistory('left_current', wheels.left_current_amps);
  pushHistory('right_current', wheels.right_current_amps);
  ['left', 'right'].forEach((side) => {
    const data = wheelData(wheels, side);
    pushHistory(`${side}_actual`, data.actual);
    pushHistory(`${side}_error`, data.error);
    [data.target, data.actual].forEach((value) => {
      if (value != null) maxAbsSpeedQpps = Math.max(maxAbsSpeedQpps, Math.abs(value));
    });
  });
  [wheels.left_current_amps, wheels.right_current_amps].forEach((value) => {
    if (value != null) maxCurrentAmps = Math.max(maxCurrentAmps, Math.abs(value));
  });
}

function pushHistory(key, value) {
  history[key].push(value == null ? null : value);
  if (history[key].length > HISTORY_LENGTH) history[key].shift();
}

function renderHud(motorStale, systemStale, controller, wheels, battery, pi, driveStatusPayload) {
  const status = driveStatus(motorStale, systemStale, controller, wheels, battery, pi, driveStatusPayload);
  const driveEl = document.getElementById('drive-status');
  driveEl.textContent = status.label.toUpperCase();
  driveEl.className = `value ${status.cls}`;

  const voltage = battery.pack_voltage;
  const voltageEl = document.getElementById('battery-voltage');
  voltageEl.textContent = voltage != null ? `${voltage.toFixed(1)}V` : '--V';
  voltageEl.className = `value ${batteryClass(battery.status)}`;
}

function driveStatus(motorStale, systemStale, controller, wheels, battery, pi, driveStatusPayload) {
  const batteryStatus = battery.status || 'unknown';
  const throttled = pi.throttled_flags;
  const state = driveStatusPayload.state;

  if (state === 'waiting_for_controller' || state === 'waiting_for_roboclaw') {
    return { label: state.replaceAll('_', ' '), cls: 'warn' };
  }
  if (motorStale) return { label: 'hold', cls: 'err' };
  if (state === 'motor_command_failed' || state === 'controller_lost') return { label: 'hold', cls: 'err' };
  if (batteryStatus === 'critical' || batteryStatus === 'unknown') return { label: 'hold', cls: 'err' };
  if (!controller.connected) return { label: 'hold', cls: 'err' };

  const cautions = [];
  if (systemStale) cautions.push('system stale');
  if (batteryStatus === 'low') cautions.push('battery low');
  if (throttled && throttled !== '0x0' && throttled !== '0') cautions.push('throttled');
  if (!wheels.read_ok) cautions.push('wheel readback');

  if (cautions.length) return { label: 'caution', cls: 'warn' };
  return { label: 'ready', cls: 'ok' };
}

function batteryClass(status) {
  if (status === 'ok') return 'ok';
  if (status === 'low' || status === 'stale') return 'warn';
  return 'err';
}

function setTelemetryStatus(label, cls) {
  const el = document.getElementById('telemetry-freshness');
  if (!el) return;
  el.textContent = label;
  el.className = `value ${cls}`;
}

function renderPi(pi) {
  const memUsed = pi.memory_used_mb;
  const memTotal = pi.memory_total_mb;
  const throttled = pi.throttled_flags;
  const throttleOk = throttled == null || throttled === '0x0' || throttled === '0';
  const tempCls = pi.soc_temp_c == null ? 'muted' : (pi.soc_temp_c < 70 ? 'ok' : pi.soc_temp_c < 80 ? 'warn' : 'err');

  setRows('pi-rows', [
    row('uptime', '', fmt(pi.uptime_seconds, 's', 0)),
    row('load', renderBar(pi.load_1m, 4), fmt(pi.load_1m, '', 2)),
    row('memory', renderBar(memUsed, memTotal), memTotal ? `${Math.round(memUsed)}/${Math.round(memTotal)}MB` : '--'),
    row('disk', renderBar(pi.disk_used_percent, 100), fmt(pi.disk_used_percent, '%', 1)),
    row('soc temp', '', fmt(pi.soc_temp_c, '\u00B0C', 1), tempCls),
    row('throttle', '', throttled || '0x0', throttleOk ? 'ok' : 'warn'),
  ]);
}

function renderBattery(battery, motorRail, motorRailSource) {
  const v = battery.pack_voltage;
  const cell = battery.cell_voltage;
  const status = battery.status || 'unknown';
  const railState = motorRailSource.stale === false ? (motorRail.state || 'unknown') : 'stale';
  const railVoltage = motorRail.last_pack_voltage;
  const railReason = motorRail.reason || '--';

  setRows('power-rows', [
    row('rail', '', railState.toUpperCase().replaceAll('_', ' '), railClass(railState)),
    row('rail reason', '', railReason.replaceAll('_', ' '), railReason === '--' ? 'muted' : ''),
    row('pack', v != null ? renderBar(v - 9.0, 3.6, '', false) : '', fmt(v, 'V', 2), 'strong'),
    row('cell est', '', fmt(cell, 'V', 2)),
    row('last rail v', '', fmt(railVoltage, 'V', 2)),
    row('trend', sparkline(history.pack_voltage), ''),
    row('peak amps', '', fmt(maxCurrentAmps, 'A', 2)),
    row('status', '', status.toUpperCase(), batteryClass(status)),
  ]);
}

function railClass(state) {
  if (state === 'on') return 'ok';
  if (state === 'warning') return 'warn';
  if (state === 'low_battery_cutoff') return 'err';
  if (state === 'off' || state === 'stale') return 'muted';
  return 'err';
}

function renderController(controller) {
  if (!controller.connected) {
    setRows('controller-rows', [row('link', '', 'OFFLINE', 'err')]);
    return;
  }
  const buttons = controller.buttons || {};
  const pressed = Object.entries(buttons).filter(([_, value]) => value).map(([key]) => key.toUpperCase());

  setRows('controller-rows', [
    row('link', '', 'LINKED', 'ok'),
    row('left x', bipolarBar(controller.left_stick_x), fmt(controller.left_stick_x, '', 2)),
    row('left y', bipolarBar(controller.left_stick_y), fmt(controller.left_stick_y, '', 2)),
    row('right x', bipolarBar(controller.right_stick_x), fmt(controller.right_stick_x, '', 2)),
    row('right y', bipolarBar(controller.right_stick_y), fmt(controller.right_stick_y, '', 2)),
    row('left trig', renderBar(controller.left_trigger, 1), fmt(controller.left_trigger, '', 2)),
    row('right trig', renderBar(controller.right_trigger, 1), fmt(controller.right_trigger, '', 2)),
    row('buttons', '', pressed.length ? pressed.join(' ') : '--', pressed.length ? '' : 'muted'),
  ]);
}

function renderWheels(wheels) {
  if (!wheels.read_ok) {
    setRows('wheels-rows', [row('encoder', '', 'READ FAIL', 'err')]);
    return;
  }
  const left = wheelData(wheels, 'left');
  const right = wheelData(wheels, 'right');

  setRows('wheels-rows', [
    row('encoder', '', 'OK', 'ok'),
    doubleRow('cmd', bipolarBar(left.command), bipolarBar(right.command)),
    doubleRow('target', fmt(left.target, '', 0), fmt(right.target, '', 0)),
    doubleRow('actual', fmt(left.actual, '', 0), fmt(right.actual, '', 0)),
    doubleRow('max qpps', fmt(left.max, '', 0), fmt(right.max, '', 0)),
    doubleRow('error', fmt(left.error, '', 0), fmt(right.error, '', 0), left.errorCls, right.errorCls),
    doubleRow('amps', fmt(left.amps, 'A', 2), fmt(right.amps, 'A', 2)),
    doubleRow('load', renderBar(left.amps, 5), renderBar(right.amps, 5)),
    trendRow('speed trend', sparkline(history.left_actual, maxAbsSpeedQpps, true), sparkline(history.right_actual, maxAbsSpeedQpps, true)),
    trendRow('amp trend', sparkline(history.left_current), sparkline(history.right_current)),
  ]);
}

function wheelData(wheels, side) {
  const target = fixWraparound(wheels[`${side}_target_qpps`]);
  const actual = fixWraparound(wheels[`${side}_actual_qpps`]);
  const rawError = fixWraparound(wheels[`${side}_error_qpps`]);
  const error = (target != null && actual != null) ? target - actual : rawError;
  let errorCls = 'ok';
  if (error != null && target != null) {
    const ratio = Math.abs(error) / Math.max(Math.abs(target), 1);
    if (ratio > 0.25) errorCls = 'err';
    else if (ratio > 0.1) errorCls = 'warn';
  }
  return {
    command: wheels[`${side}_command`],
    target,
    actual,
    max: fixWraparound(wheels[`${side}_max_qpps`]),
    error,
    errorCls,
    amps: wheels[`${side}_current_amps`],
  };
}

function renderLink(linkLoop, driveStatusPayload) {
  const status = linkStatus(linkLoop);
  const successRate = linkLoop.read_success_rate;
  const successText = successRate != null ? `${Math.round(successRate * 100)}% ok` : '--';
  const failures = linkLoop.consecutive_read_failures;
  const failureText = failures === 0 ? 'none' : (failures != null ? `${failures} streak` : '--');
  const latency = linkLoop.telemetry_latency_ms;
  const loopHz = linkLoop.command_loop_hz;
  const latencyHealth = latency != null ? 100 - latency : null;
  const motorFailures = driveStatusPayload.consecutive_motor_command_failures;
  const motorOk = driveStatusPayload.motor_command_ok;
  const publishFailures = driveStatusPayload.telemetry_publish_failures;
  const publishOk = driveStatusPayload.last_telemetry_publish_ok;

  setRows('link-rows', [
    row('status', '', status.label.toUpperCase(), status.cls),
    row('drive', '', driveStatusPayload.state || '--'),
    row('motor cmd', '', motorOk === true ? 'ok' : (motorOk === false ? 'fail' : '--'), motorOk === true ? 'ok' : (motorOk === false ? 'err' : '')),
    row('cmd ack', '', fmtRelativeSeconds(driveStatusPayload.last_motor_command_ack_age_seconds)),
    row('cmd fails', renderBar(motorFailures, 5, motorFailures > 0 ? 'warn' : ''), motorFailures === 0 ? 'none' : (motorFailures != null ? `${motorFailures} streak` : '--'), motorFailures > 0 ? 'warn' : ''),
    row('roboclaw', renderBar(successRate, 1), successText, status.cls),
    row('last good', '', fmtRelativeSeconds(linkLoop.last_good_read_age_seconds)),
    row('failures', renderBar(failures, 10, failures > 0 ? 'warn' : ''), failureText, failures > 0 ? 'warn' : ''),
    row('latency', renderBar(latencyHealth, 100, '', false), latency != null ? `${Math.round(latency)}ms` : '--'),
    row('drive loop', renderBar(loopHz, 20), loopHz != null ? `${loopHz.toFixed(1)} Hz` : '--'),
    row('pub drops', '', publishFailures != null ? publishFailures : '--', publishFailures > 0 ? 'warn' : ''),
    row('last pub', '', publishOk === true ? 'ok' : (publishOk === false ? 'fail' : '--'), publishOk === true ? 'ok' : (publishOk === false ? 'warn' : '')),
  ]);
}

function renderSensors(snapshot, sources) {
  const panel = document.getElementById('sensors-panel');
  if (!panel) return;

  const sensors = snapshot.sensors;
  const source = sources.sensors || {};
  const readings = sensors?.readings || [];
  const hasLiveReadings = source.stale === false
    && sensors?.status === 'polling'
    && readings.some((reading) => reading.ok);

  if (!hasLiveReadings) {
    panel.classList.add('hidden');
    return;
  }

  panel.classList.remove('hidden');
  const rows = readings.map((reading) => {
    const label = reading.name || '--';
    const value = reading.ok ? `${reading.distance_mm} mm` : 'FAIL';
    const cls = reading.ok ? 'ok' : 'err';
    return row(label, '', value, cls);
  });
  rows.unshift(row('status', '', sensors.status || '--', 'ok'));
  setRows('sensors-rows', rows);
}

function fixWraparound(value) {
  if (value == null) return null;
  const max = (2 ** 31) - 1;
  const min = -(2 ** 31);
  const span = 2 ** 32;
  if (value > max) return value - span;
  if (value < min) return value + span;
  return value;
}

function linkStatus(linkLoop) {
  if (linkLoop.status === 'stale') return { label: 'stale', cls: 'err' };
  const successRate = linkLoop.read_success_rate;
  const failures = linkLoop.consecutive_read_failures;
  const lastGoodAge = linkLoop.last_good_read_age_seconds;
  const latency = linkLoop.telemetry_latency_ms;

  if (successRate == null) return { label: 'stale', cls: 'err' };
  if (successRate < 0.5 || (failures != null && failures >= 5) || (lastGoodAge != null && lastGoodAge >= 5)) {
    return { label: 'stale', cls: 'err' };
  }
  if (successRate < 0.9 || (failures != null && failures > 0) || (latency != null && latency > 100)) {
    return { label: 'degraded', cls: 'warn' };
  }
  return { label: 'live', cls: 'ok' };
}
