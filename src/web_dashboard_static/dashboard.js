(function () {
  'use strict';

  const HISTORY_LENGTH = 48;
  const SPARK_BLOCKS = '▁▂▃▄▅▆▇█';

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
  let redeployArmedUntil = 0;
  let redeployRunning = false;
  let logsPaused = false;

  // Build the camera URL from the page hostname so a remote browser
  // (e.g. MacBook) loads MJPEG from the Pi, not from its own loopback.
  document.getElementById('camera-stream').src = `http://${window.location.hostname}:8081/stream.mjpg`;

  const sessionStart = Date.now();
  setInterval(updateSession, 1000);
  setInterval(updateRedeployButton, 250);
  updateSession();
  bindActions();
  connectTelemetry();
  connectLogs();

  function bindActions() {
    document.getElementById('redeploy-button').addEventListener('click', onRedeploy);
    document.getElementById('config-button').addEventListener('click', openConfig);
    document.getElementById('config-cancel').addEventListener('click', closeConfig);
    document.getElementById('config-modal').addEventListener('click', (event) => {
      if (event.target.id === 'config-modal') closeConfig();
    });
    document.getElementById('config-form').addEventListener('submit', applyConfig);
    document.getElementById('logs-clear').addEventListener('click', () => {
      document.getElementById('logs-output').textContent = '';
    });
    document.getElementById('logs-pause').addEventListener('click', () => {
      logsPaused = !logsPaused;
      document.getElementById('logs-pause').textContent = logsPaused ? 'Resume' : 'Pause';
    });
  }

  function connectTelemetry() {
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

  function connectLogs() {
    const events = new EventSource('/logs/events');
    events.addEventListener('message', (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      appendLog(payload.line || '');
    });
    events.addEventListener('error', () => {
      appendLog('log stream disconnected');
    });
  }

  function render(snapshot) {
    const sources = snapshot.sources || {};
    const gamepadStale = (sources.gamepad_teleop || {}).stale !== false;
    const systemStale = (sources.system || {}).stale !== false;
    const gamepadLive = !gamepadStale;

    recordHistory(snapshot, gamepadLive);

    const controller = gamepadLive ? (snapshot.controller || {}) : {};
    const wheels = gamepadLive ? (snapshot.wheels || {}) : {};
    const battery = gamepadLive ? (snapshot.motor_battery || {}) : { status: 'stale' };
    const linkLoop = gamepadLive ? (snapshot.link_loop || {}) : { status: 'stale' };
    const pi = snapshot.pi || {};

    renderHud(gamepadStale, systemStale, controller, wheels, battery, pi);
    renderPi(pi);
    renderBattery(battery);
    renderController(controller);
    renderWheels(wheels);
    renderLink(linkLoop);
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
      row('memory', renderBar(memUsed, memTotal), memTotal ? `${Math.round(memUsed)}/${Math.round(memTotal)}MB` : '--'),
      row('disk', renderBar(pi.disk_used_percent, 100), fmt(pi.disk_used_percent, '%', 1)),
      row('soc temp', '', fmt(pi.soc_temp_c, '\u00B0C', 1), tempCls),
      row('throttle', '', throttled || '0x0', throttleOk ? 'ok' : 'warn'),
    ]);
  }

  function renderBattery(battery) {
    const v = battery.pack_voltage;
    const cell = battery.cell_voltage;
    const status = battery.status || 'unknown';

    setRows('power-rows', [
      row('pack', v != null ? renderBar(v - 9.0, 3.6, '', false) : '', fmt(v, 'V', 2), 'strong'),
      row('cell est', '', fmt(cell, 'V', 2)),
      row('trend', sparkline(history.pack_voltage, 14), ''),
      row('peak amps', '', fmt(maxCurrentAmps, 'A', 2)),
      row('status', '', status.toUpperCase(), batteryClass(status)),
    ]);
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
      doubleRow('speed trend', sparkline(history.left_actual, 13, maxAbsSpeedQpps, true), sparkline(history.right_actual, 13, maxAbsSpeedQpps, true)),
      doubleRow('amp trend', sparkline(history.left_current, 13), sparkline(history.right_current, 13)),
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

  function renderLink(linkLoop) {
    const status = linkStatus(linkLoop);
    const successRate = linkLoop.read_success_rate;
    const successText = successRate != null ? `${Math.round(successRate * 100)}% ok` : '--';
    const failures = linkLoop.consecutive_read_failures;
    const failureText = failures === 0 ? 'none' : (failures != null ? `${failures} streak` : '--');
    const latency = linkLoop.telemetry_latency_ms;
    const loopHz = linkLoop.command_loop_hz;
    const latencyHealth = latency != null ? 100 - latency : null;

    setRows('link-rows', [
      row('status', '', status.label.toUpperCase(), status.cls),
      row('roboclaw', renderBar(successRate, 1), successText, status.cls),
      row('last good', '', fmtRelativeSeconds(linkLoop.last_good_read_age_seconds)),
      row('failures', renderBar(failures, 10, failures > 0 ? 'warn' : ''), failureText, failures > 0 ? 'warn' : ''),
      row('latency', renderBar(latencyHealth, 100, '', false), latency != null ? `${Math.round(latency)}ms` : '--'),
      row('drive loop', renderBar(loopHz, 20), loopHz != null ? `${loopHz.toFixed(1)} Hz` : '--'),
    ]);
  }

  async function onRedeploy() {
    const endpoint = Date.now() <= redeployArmedUntil ? '/redeploy/run' : '/redeploy/arm';
    const response = await fetch(endpoint, { method: 'POST' });
    const status = await response.json();
    applyRedeployStatus(status);
  }

  function applyRedeployStatus(status) {
    redeployRunning = status.running === true;
    redeployArmedUntil = status.armed ? Date.now() + (status.armed_seconds_remaining * 1000) : 0;
    updateRedeployButton();
  }

  function updateRedeployButton() {
    const button = document.getElementById('redeploy-button');
    if (redeployRunning) {
      button.textContent = 'Redeploying...';
      button.disabled = true;
      button.classList.remove('armed');
      return;
    }
    button.disabled = false;
    if (Date.now() <= redeployArmedUntil) {
      button.textContent = 'Redeploy Armed';
      button.classList.add('armed');
    } else {
      button.textContent = 'Redeploy';
      button.classList.remove('armed');
    }
  }

  async function openConfig() {
    const modal = document.getElementById('config-modal');
    const error = document.getElementById('config-error');
    error.textContent = '';
    modal.classList.remove('hidden');

    const response = await fetch('/config/drive');
    const payload = await response.json();
    if (payload.error) error.textContent = payload.error;
    renderConfigFields(payload.fields, payload.values);
  }

  function renderConfigFields(fields, values) {
    document.getElementById('config-fields').innerHTML = fields.map((field) => {
      const value = values[field.key];
      return `
        <div class="field">
          <label for="config-${field.key}">${escapeHtml(field.label)}</label>
          <input id="config-${field.key}" name="${field.key}" value="${Number(value).toFixed(2)}" inputmode="decimal">
          <span class="help">${escapeHtml(field.help)}</span>
        </div>
      `;
    }).join('');
  }

  function closeConfig() {
    document.getElementById('config-modal').classList.add('hidden');
  }

  async function applyConfig(event) {
    event.preventDefault();
    const error = document.getElementById('config-error');
    error.textContent = '';
    const values = {};
    new FormData(event.currentTarget).forEach((value, key) => {
      values[key] = Number(value);
    });

    const response = await fetch('/config/drive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(values),
    });
    const payload = await response.json();
    if (!response.ok) {
      error.textContent = payload.error || 'Drive tuning apply failed.';
      return;
    }
    closeConfig();
  }

  function appendLog(line) {
    if (logsPaused) return;
    const output = document.getElementById('logs-output');
    output.textContent += `${line}\n`;
    output.scrollTop = output.scrollHeight;
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

  function doubleRow(label, leftVal, rightVal, leftCls = '', rightCls = '') {
    return `
      <div class="row">
        <span class="label">${escapeHtml(label)}</span>
        <span class="bar-cell value ${leftCls}">L ${leftVal}</span>
        <span class="value ${rightCls}">R ${rightVal}</span>
      </div>
    `;
  }

  function row(label, bar, value, cls = '') {
    return `<div class="row"><span class="label">${escapeHtml(label)}</span><span class="bar-cell">${bar || ''}</span><span class="value ${cls}">${escapeHtml(value)}</span></div>`;
  }

  function setRows(id, rows) {
    document.getElementById(id).innerHTML = Array.isArray(rows) ? rows.join('') : rows;
  }

  function renderBar(value, limit, cls = '', absolute = true) {
    if (value == null || limit == null || limit <= 0) return '<span class="bar"></span>';
    const scaled = absolute ? Math.abs(value) : Math.max(0, value);
    const ratio = Math.max(0, Math.min(1, scaled / limit));
    return `<span class="bar ${cls}"><span class="fill" style="width: ${(ratio * 100).toFixed(1)}%"></span></span>`;
  }

  function bipolarBar(value) {
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

  function sparkline(values, width = 12, limit = null, absolute = false) {
    const clean = values.filter((value) => value != null).map((value) => absolute ? Math.abs(value) : value);
    if (!clean.length) return '<span class="sparkline">────────────</span>';
    const recent = clean.slice(-width);
    if (recent.length < 2) return `<span class="sparkline">${'─'.repeat(width)}</span>`;
    const low = limit == null ? Math.min(...recent) : 0;
    const high = limit == null ? Math.max(...recent) : limit;
    if (high === low) return `<span class="sparkline">${SPARK_BLOCKS[4].repeat(recent.length)}${'─'.repeat(width - recent.length)}</span>`;
    const text = recent.map((value) => {
      const ratio = Math.max(0, Math.min(1, (value - low) / (high - low)));
      return SPARK_BLOCKS[Math.floor(ratio * (SPARK_BLOCKS.length - 1))];
    }).join('');
    return `<span class="sparkline">${text}${'─'.repeat(width - recent.length)}</span>`;
  }

  function fmt(value, suffix, digits) {
    if (value == null) return '--';
    if (typeof value === 'number') return value.toFixed(digits) + (suffix || '');
    return value + (suffix || '');
  }

  function fmtRelativeSeconds(seconds) {
    if (seconds == null) return 'never';
    if (seconds < 10) return `${seconds.toFixed(1)}s ago`;
    return `${Math.floor(seconds)}s ago`;
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

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
})();
