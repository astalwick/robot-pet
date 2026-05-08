(function () {
  'use strict';

  // Build the camera URL from the page hostname so a remote browser
  // (e.g. MacBook) loads MJPEG from the Pi, not from its own loopback.
  const cameraUrl = `http://${window.location.hostname}:8081/stream.mjpg`;
  document.getElementById('camera-stream').src = cameraUrl;

  const sessionStart = Date.now();
  setInterval(updateSession, 1000);
  updateSession();

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

  function render(snapshot) {
    const sources = snapshot.sources || {};
    const gamepadStale = (sources.gamepad_teleop || {}).stale !== false;
    const systemStale = (sources.system || {}).stale !== false;

    const controller = !gamepadStale ? (snapshot.controller || {}) : {};
    const wheels = !gamepadStale ? (snapshot.wheels || {}) : {};
    const battery = !gamepadStale ? (snapshot.motor_battery || {}) : { status: 'stale' };
    const linkLoop = !gamepadStale ? (snapshot.link_loop || {}) : { status: 'stale' };
    const pi = snapshot.pi || {};

    renderHud(gamepadStale, systemStale, controller, wheels, battery, pi);
    renderPi(pi);
    renderBattery(battery);
    renderController(controller);
    renderWheels(wheels);
    renderLink(linkLoop);
    setTelemetryStatus(gamepadStale ? 'stale' : 'live', gamepadStale ? 'warn' : 'ok');
  }

  function renderHud(gamepadStale, systemStale, controller, wheels, battery, pi) {
    const status = driveStatus(gamepadStale, systemStale, controller, wheels, battery, pi);
    const driveEl = document.getElementById('drive-status');
    driveEl.textContent = status.label.toUpperCase();
    driveEl.className = `value ${status.cls}`;

    const voltage = battery.pack_voltage;
    const voltageEl = document.getElementById('battery-voltage');
    voltageEl.textContent = voltage != null ? `${voltage.toFixed(1)}V` : '--V';
    voltageEl.className = `value ${batteryClass(battery.status)}`;
  }

  function driveStatus(gamepadStale, systemStale, controller, wheels, battery, pi) {
    const batteryStatus = battery.status || 'unknown';
    const throttled = pi.throttled_flags;

    if (gamepadStale) return { label: 'hold', cls: 'err' };
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
    el.textContent = label;
    el.className = `value ${cls}`;
  }

  function updateSession() {
    const seconds = Math.floor((Date.now() - sessionStart) / 1000);
    document.getElementById('session-uptime').textContent = formatDuration(seconds);
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
      row('memory', renderBar(memUsed, memTotal),
        memTotal ? `${Math.round(memUsed)}/${Math.round(memTotal)}MB` : '--'),
      row('disk', '', fmt(pi.disk_used_percent, '%', 1)),
      row('soc temp', '', fmt(pi.soc_temp_c, '\u00B0C', 1), tempCls),
      row('throttle', '', throttled || '0x0', throttleOk ? 'ok' : 'warn'),
    ]);
  }

  function renderBattery(battery) {
    const v = battery.pack_voltage;
    const cell = battery.cell_voltage;
    const status = battery.status || 'unknown';
    const voltageBar = v != null ? renderBar(v - 9.0, 3.6) : '<span class="bar"></span>';

    setRows('power-rows', [
      row('pack', voltageBar, fmt(v, 'V', 2), 'strong'),
      row('cell est', '', fmt(cell, 'V', 2)),
      row('status', '', status.toUpperCase(), batteryClass(status)),
    ]);
  }

  function renderController(controller) {
    if (!controller.connected) {
      setRows('controller-rows', [row('link', '', 'OFFLINE', 'err')]);
      return;
    }
    const buttons = controller.buttons || {};
    const pressed = Object.entries(buttons).filter(([_, v]) => v).map(([k]) => k);

    setRows('controller-rows', [
      row('link', '', 'LINKED', 'ok'),
      row('left stick', '', `x ${fmt(controller.left_stick_x, '', 2)}  y ${fmt(controller.left_stick_y, '', 2)}`),
      row('right stick', '', `x ${fmt(controller.right_stick_x, '', 2)}  y ${fmt(controller.right_stick_y, '', 2)}`),
      row('triggers', '', `L ${fmt(controller.left_trigger, '', 2)}  R ${fmt(controller.right_trigger, '', 2)}`),
      row('buttons', '', pressed.length ? pressed.join(' ').toUpperCase() : '--', pressed.length ? '' : 'muted'),
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
      doubleRow('cmd', left.cmd, right.cmd),
      doubleRow('target', fmt(left.target, '', 0), fmt(right.target, '', 0)),
      doubleRow('actual', fmt(left.actual, '', 0), fmt(right.actual, '', 0)),
      doubleRow('error', fmt(left.error, '', 0), fmt(right.error, '', 0), left.errorCls, right.errorCls),
      doubleRow('amps', fmt(left.amps, 'A', 2), fmt(right.amps, 'A', 2)),
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
      cmd: fmt(wheels[`${side}_command`], '', 2),
      target,
      actual,
      error,
      errorCls,
      amps: wheels[`${side}_current_amps`],
    };
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

  function doubleRow(label, leftVal, rightVal, leftCls = '', rightCls = '') {
    const leftSpan = leftCls ? `<span class="value ${leftCls}">${leftVal}</span>` : leftVal;
    const rightSpan = rightCls ? `<span class="value ${rightCls}">${rightVal}</span>` : rightVal;
    return `<div class="row"><span class="label">${label}</span><span class="bar-cell"></span><span class="value">L ${leftSpan} &nbsp; R ${rightSpan}</span></div>`;
  }

  function renderLink(linkLoop) {
    const status = linkStatus(linkLoop);
    const successRate = linkLoop.read_success_rate;
    const successText = successRate != null ? `${Math.round(successRate * 100)}% ok` : '--';
    const failures = linkLoop.consecutive_read_failures;
    const failureText = failures === 0 ? 'none' : (failures != null ? `${failures} streak` : '--');
    const latency = linkLoop.telemetry_latency_ms;
    const loopHz = linkLoop.command_loop_hz;

    setRows('link-rows', [
      row('status', '', status.label.toUpperCase(), status.cls),
      row('roboclaw', '', successText, status.cls),
      row('failures', '', failureText, failures > 0 ? 'warn' : ''),
      row('latency', '', latency != null ? `${Math.round(latency)}ms` : '--'),
      row('drive loop', '', loopHz != null ? `${loopHz.toFixed(1)} Hz` : '--'),
    ]);
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

  function row(label, bar, value, cls = '') {
    return `<div class="row"><span class="label">${label}</span><span class="bar-cell">${bar || ''}</span><span class="value ${cls}">${value}</span></div>`;
  }

  function setRows(id, rows) {
    document.getElementById(id).innerHTML = Array.isArray(rows) ? rows.join('') : rows;
  }

  function renderBar(value, limit, cls = '') {
    if (value == null || limit == null || limit <= 0) return '<span class="bar"></span>';
    const ratio = Math.max(0, Math.min(1, value / limit));
    return `<span class="bar ${cls}"><span class="fill" style="width: ${(ratio * 100).toFixed(1)}%"></span></span>`;
  }

  function fmt(value, suffix, digits) {
    if (value == null) return '--';
    if (typeof value === 'number') return value.toFixed(digits) + (suffix || '');
    return value + (suffix || '');
  }

  function formatDuration(seconds) {
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
})();
