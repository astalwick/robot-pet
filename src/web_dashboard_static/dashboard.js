(function () {
  'use strict';

  const HISTORY_LENGTH = 48;
  const SPARK_BLOCKS = '▁▂▃▄▅▆▇█';
  const VISION_STALE_SECONDS = 2.0;

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
  let redeployRequestInFlight = false;
  let logsPaused = false;
  let cameraRetry = null;
  let latestVoice = null;
  let voiceRowsReady = false;
  let voiceTargetEnabled = null;
  let voiceSaveInFlight = false;
  let voiceSaveAgain = false;
  const voicePendingPatch = {};
  const voiceGainSaveTimers = {};

  // Build the camera URL from the page hostname so a remote browser
  // (e.g. MacBook) loads MJPEG from the Pi, not from its own loopback.
  setupCameraStream();

  const sessionStart = Date.now();
  setInterval(updateSession, 1000);
  setInterval(updateRedeployButton, 250);
  setInterval(refreshRedeployStatus, 1000);
  updateSession();
  bindActions();
  connectTelemetry();
  connectLogs();

  function setupCameraStream() {
    const camera = document.getElementById('camera-stream');
    if (!camera) return;
    camera.addEventListener('error', scheduleCameraReconnect);
    refreshCameraStream();
  }

  function refreshCameraStream() {
    const camera = document.getElementById('camera-stream');
    if (!camera) return;
    camera.src = `http://${window.location.hostname}:8081/stream.mjpg?t=${Date.now()}`;
  }

  function scheduleCameraReconnect() {
    if (cameraRetry != null) return;
    cameraRetry = setTimeout(() => {
      cameraRetry = null;
      refreshCameraStream();
    }, 2000);
  }

  function bindActions() {
    on('voice-toggle-button', 'click', onVoiceToggle);
    on('redeploy-button', 'click', onRedeploy);
    on('config-button', 'click', openConfig);
    on('config-cancel', 'click', closeConfig);
    on('config-modal', 'click', (event) => {
      if (event.target.id === 'config-modal') closeConfig();
    });
    on('config-form', 'submit', applyConfig);
    on('logs-clear', 'click', () => {
      const output = document.getElementById('logs-output');
      if (output) output.textContent = '';
    });
    on('logs-pause', 'click', () => {
      logsPaused = !logsPaused;
      const button = document.getElementById('logs-pause');
      if (button) button.textContent = logsPaused ? 'Resume' : 'Pause';
    });
    on('voice-rows', 'input', onVoiceGainInput);
    on('voice-rows', 'change', onVoiceGainCommit);
  }

  function on(id, eventName, handler) {
    const element = document.getElementById(id);
    if (element) element.addEventListener(eventName, handler);
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
    const driveStatusPayload = gamepadLive ? (snapshot.drive_status || {}) : { state: 'stale' };
    const pi = snapshot.pi || {};

    renderHud(gamepadStale, systemStale, controller, wheels, battery, pi, driveStatusPayload);
    renderPi(pi);
    renderBattery(battery);
    renderController(controller);
    renderWheels(wheels);
    renderLink(linkLoop, driveStatusPayload);
    renderVoice(snapshot, sources);
    renderFaceOverlay(snapshot, sources);
    setTelemetryStatus(gamepadStale ? 'stale' : 'live', gamepadStale ? 'warn' : 'ok');
  }

  function renderVoice(snapshot, sources) {
    const voiceSource = sources.voice || {};
    const voice = snapshot.voice || {};
    latestVoice = voice;
    clearAppliedVoicePatch(voice);
    const stale = voiceSource.stale !== false;
    const displayVoice = { ...voice, ...voicePendingPatch };
    const lastError = voice.last_error;
    const cardStatus = voiceCardStatus(voice, stale, lastError);

    ensureVoiceRows();
    setVoiceValue('status', cardStatus.text, cardStatus.cls);
    setVoiceValue('listen', displayVoice.enabled ? 'enabled' : 'disabled', displayVoice.enabled ? 'ok' : 'muted');
    setVoiceValue('input', displayVoice.input_device || '--');
    setVoiceValue('output', displayVoice.output_device || '--');
    setVoiceValue('channel', displayVoice.capture_channel_index != null ? String(displayVoice.capture_channel_index) : '--');
    updateGainControl('input_gain', displayVoice.input_gain);
    updateGainControl('output_gain', displayVoice.output_gain);
    const transcript = displayVoice.last_committed_transcript || displayVoice.partial_transcript || '--';
    setVoiceValue('transcript', transcript, transcript === '--' ? 'muted' : '');
    setVoiceValue('error', lastError || '--', lastError ? 'err' : 'muted');
    updateVoiceToggleButton();
  }

  function voiceEffectiveEnabled() {
    if (voiceTargetEnabled !== null) return voiceTargetEnabled;
    return !!(latestVoice && latestVoice.enabled);
  }

  function voiceToggleInTransition() {
    if (voiceSaveInFlight) return true;
    if (voiceTargetEnabled === null || !latestVoice) return false;
    return latestVoice.enabled !== voiceTargetEnabled;
  }

  function voiceCardStatus(voice, stale, lastError) {
    if (stale) return { text: 'STALE', cls: 'err' };
    if (voiceToggleInTransition()) {
      return { text: voiceEffectiveEnabled() ? 'STARTING' : 'STOPPING', cls: 'warn' };
    }
    const status = voice.status || 'unknown';
    return { text: status.toUpperCase(), cls: voiceStatusClass(status, lastError) };
  }

  function updateVoiceToggleButton() {
    const button = document.getElementById('voice-toggle-button');
    if (!button) return;
    const label = button.querySelector('.voice-toggle-label');
    const targetOn = voiceEffectiveEnabled();
    let text;
    if (voiceToggleInTransition()) {
      text = targetOn ? 'Starting' : 'Stopping';
    } else {
      text = targetOn ? 'Voice On' : 'Voice Off';
    }
    button.classList.remove('ok', 'warn', 'err', 'muted');
    if (text === 'Voice On') button.classList.add('ok');
    else if (text === 'Voice Off') button.classList.add('muted');
    else button.classList.add('warn');
    label.textContent = text;
    button.setAttribute('aria-label', text);
  }

  function ensureVoiceRows() {
    if (voiceRowsReady) return;
    document.getElementById('voice-rows').innerHTML = [
      voiceActivityRow(),
      voiceValueRow('listen'),
      voiceValueRow('input'),
      voiceValueRow('output'),
      voiceValueRow('channel'),
      gainControlRow('mic gain', 'input_gain'),
      gainControlRow('speaker', 'output_gain'),
      voiceValueRow('transcript'),
      voiceValueRow('error'),
    ].join('');
    voiceRowsReady = true;
  }

  function voiceActivityRow() {
    return `
      <div class="row voice-activity-row">
        <span class="label">activity</span>
        <span class="bar-cell"></span>
        <span class="value muted voice-activity" data-voice-value="status">--</span>
      </div>
    `;
  }

  function voiceValueRow(key) {
    return `
      <div class="row">
        <span class="label">${escapeHtml(key)}</span>
        <span class="bar-cell"></span>
        <span class="value muted" data-voice-value="${key}">--</span>
      </div>
    `;
  }

  function gainControlRow(label, key) {
    return `
      <div class="row control-row">
        <span class="label">${escapeHtml(label)}</span>
        <span class="bar-cell">
          <input type="range" min="0" max="3" step="0.1" value="1.0" data-voice-key="${key}" aria-label="${escapeHtml(label)}">
        </span>
        <span class="value" data-voice-value="${key}">1.0</span>
      </div>
    `;
  }

  function setVoiceValue(key, value, cls = '') {
    const element = document.querySelector(`[data-voice-value="${key}"]`);
    if (!element) return;
    element.textContent = value;
    const activity = key === 'status' ? ' voice-activity' : '';
    element.className = `value${activity} ${cls}`.trim();
  }

  function updateGainControl(key, value) {
    const gain = Number(value == null ? 1.0 : value);
    const input = document.querySelector(`input[data-voice-key="${key}"]`);
    if (input && document.activeElement !== input) input.value = gain.toFixed(1);
    setVoiceValue(key, gain.toFixed(1));
  }

  function clearAppliedVoicePatch(voice) {
    ['input_gain', 'output_gain'].forEach((key) => {
      if (voicePendingPatch[key] != null && Number(voice[key]).toFixed(1) === Number(voicePendingPatch[key]).toFixed(1)) {
        delete voicePendingPatch[key];
      }
    });
    if (voiceTargetEnabled !== null && voice.enabled === voiceTargetEnabled && !voiceSaveInFlight) {
      voiceTargetEnabled = null;
    }
  }

  function voiceStatusClass(status, lastError) {
    if (lastError || status === 'error' || status === 'stale') return 'err';
    if (status === 'starting' || status === 'reconnecting' || status === 'hearing' || status === 'thinking') return 'warn';
    if (status === 'listening' || status === 'speaking') return 'ok';
    return 'muted';
  }

  function renderFaceOverlay(snapshot, sources) {
    const overlay = document.getElementById('face-overlay');
    const visionSource = sources.vision || {};
    const vision = snapshot.vision;

    if (!vision || visionSource.stale === true) {
      overlay.innerHTML = '';
      return;
    }

    const lastDetection = vision.last_detection_time;
    const snapshotTime = snapshot.time;
    if (lastDetection != null && snapshotTime != null
        && (snapshotTime - lastDetection) > VISION_STALE_SECONDS) {
      overlay.innerHTML = '';
      return;
    }

    const imageWidth = vision.image_width;
    const imageHeight = vision.image_height;
    if (!imageWidth || !imageHeight) {
      overlay.innerHTML = '';
      return;
    }

    const cameraSection = document.getElementById('camera-section');
    const rect = containedImageRect(
      cameraSection.clientWidth, cameraSection.clientHeight, imageWidth, imageHeight,
    );
    const faces = vision.faces || [];
    overlay.innerHTML = faces.map((face) => faceBoxHtml(face, rect)).join('');
  }

  function containedImageRect(containerW, containerH, sourceW, sourceH) {
    if (containerW <= 0 || containerH <= 0 || sourceW <= 0 || sourceH <= 0) {
      return { left: 0, top: 0, width: 0, height: 0 };
    }
    const sourceAspect = sourceW / sourceH;
    const containerAspect = containerW / containerH;
    if (containerAspect > sourceAspect) {
      const height = containerH;
      const width = height * sourceAspect;
      return { left: (containerW - width) / 2, top: 0, width, height };
    }
    const width = containerW;
    const height = width / sourceAspect;
    return { left: 0, top: (containerH - height) / 2, width, height };
  }

  function faceBoxHtml(face, rect) {
    const left = rect.left + face.x * rect.width;
    const top = rect.top + face.y * rect.height;
    const width = face.width * rect.width;
    const height = face.height * rect.height;
    return `<div class="face-box" style="left: ${left.toFixed(1)}px; top: ${top.toFixed(1)}px; width: ${width.toFixed(1)}px; height: ${height.toFixed(1)}px;"></div>`;
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

  function renderHud(gamepadStale, systemStale, controller, wheels, battery, pi, driveStatusPayload) {
    const status = driveStatus(gamepadStale, systemStale, controller, wheels, battery, pi, driveStatusPayload);
    const driveEl = document.getElementById('drive-status');
    driveEl.textContent = status.label.toUpperCase();
    driveEl.className = `value ${status.cls}`;

    const voltage = battery.pack_voltage;
    const voltageEl = document.getElementById('battery-voltage');
    voltageEl.textContent = voltage != null ? `${voltage.toFixed(1)}V` : '--V';
    voltageEl.className = `value ${batteryClass(battery.status)}`;
  }

  function driveStatus(gamepadStale, systemStale, controller, wheels, battery, pi, driveStatusPayload) {
    const batteryStatus = battery.status || 'unknown';
    const throttled = pi.throttled_flags;
    const state = driveStatusPayload.state;

    if (gamepadStale) return { label: 'hold', cls: 'err' };
    if (state === 'motor_command_failed' || state === 'controller_lost') return { label: 'hold', cls: 'err' };
    if (state === 'waiting_for_controller' || state === 'waiting_for_roboclaw') return { label: state.replaceAll('_', ' '), cls: 'warn' };
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

  function updateSession() {
    const seconds = Math.floor((Date.now() - sessionStart) / 1000);
    const uptime = document.getElementById('session-uptime');
    if (uptime) uptime.textContent = formatDuration(seconds);
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

  async function onRedeploy() {
    if (redeployRequestInFlight) return;
    const endpoint = Date.now() <= redeployArmedUntil ? '/redeploy/run' : '/redeploy/arm';
    const previous = {
      redeployArmedUntil,
      redeployRunning,
    };
    if (endpoint === '/redeploy/arm') {
      redeployArmedUntil = Date.now() + 10000;
    } else {
      redeployRunning = true;
      redeployArmedUntil = 0;
    }
    updateRedeployButton();
    redeployRequestInFlight = true;
    try {
      const response = await fetch(endpoint, { method: 'POST' });
      const status = await response.json();
      applyRedeployStatus(status);
    } catch (err) {
      redeployArmedUntil = previous.redeployArmedUntil;
      redeployRunning = previous.redeployRunning;
      updateRedeployButton();
      appendLog(`redeploy request failed: ${err}`);
    } finally {
      redeployRequestInFlight = false;
    }
  }

  function onVoiceToggle() {
    voiceTargetEnabled = !voiceEffectiveEnabled();
    updateVoiceToggleButton();
    void saveVoiceTarget();
  }

  async function saveVoiceTarget() {
    if (voiceSaveInFlight) {
      voiceSaveAgain = true;
      return;
    }
    voiceSaveInFlight = true;
    updateVoiceToggleButton();
    try {
      do {
        voiceSaveAgain = false;
        if (voiceTargetEnabled === null) break;
        const target = voiceTargetEnabled;
        const result = await updateVoiceConfig({ enabled: target });
        if (!result.ok) {
          appendLog(`voice config update failed: ${result.error}`);
          voiceTargetEnabled = !!(latestVoice && latestVoice.enabled);
          break;
        }
      } while (voiceSaveAgain);
    } finally {
      voiceSaveInFlight = false;
      updateVoiceToggleButton();
    }
  }

  function onVoiceGainInput(event) {
    const input = event.target;
    if (!input.dataset.voiceKey) return;
    const key = input.dataset.voiceKey;
    voicePendingPatch[key] = Number(input.value);
    setVoiceValue(key, Number(input.value).toFixed(1));
    clearTimeout(voiceGainSaveTimers[key]);
    voiceGainSaveTimers[key] = setTimeout(() => saveVoiceGain(key), 350);
  }

  function onVoiceGainCommit(event) {
    const input = event.target;
    if (!input.dataset.voiceKey) return;
    clearTimeout(voiceGainSaveTimers[input.dataset.voiceKey]);
    saveVoiceGain(input.dataset.voiceKey);
  }

  async function saveVoiceGain(key) {
    const value = voicePendingPatch[key];
    if (value == null) return;
    const result = await updateVoiceConfig({ [key]: value });
    if (!result.ok) appendLog(`voice config update failed: ${result.error}`);
  }

  async function fetchVoiceValues() {
    try {
      const response = await fetch('/config/voice');
      if (!response.ok) return null;
      const payload = await response.json();
      return payload.values || null;
    } catch (err) {
      appendLog(`voice config fetch failed: ${err}`);
      return null;
    }
  }

  async function updateVoiceConfig(patch) {
    try {
      const values = await fetchVoiceValues();
      if (!values) return { ok: false, error: 'could not load voice config' };
      return await postConfig('/config/voice', { ...values, ...patch });
    } catch (err) {
      return { ok: false, error: String(err) };
    }
  }

  async function refreshRedeployStatus() {
    if (!redeployRunning && Date.now() > redeployArmedUntil) return;
    try {
      const response = await fetch('/redeploy/status');
      if (!response.ok) return;
      applyRedeployStatus(await response.json());
    } catch (err) {
      // The web dashboard restarts during redeploy; the next poll will reconnect.
    }
  }

  function applyRedeployStatus(status) {
    const wasRunning = redeployRunning;
    redeployRunning = status.running === true;
    if (status.armed) {
      redeployArmedUntil = Date.now() + (status.armed_seconds_remaining * 1000);
    } else if (!redeployRequestInFlight) {
      redeployArmedUntil = 0;
    }
    if (wasRunning && !redeployRunning) refreshCameraStream();
    updateRedeployButton();
  }

  function updateRedeployButton() {
    const button = document.getElementById('redeploy-button');
    if (!button) return;
    button.disabled = redeployRunning || redeployRequestInFlight;
    if (redeployRunning) {
      button.textContent = 'Redeploying...';
      button.classList.remove('armed');
      return;
    }
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

    const [driveResp, visionResp, voiceResp] = await Promise.all([
      fetch('/config/drive'),
      fetch('/config/vision'),
      fetch('/config/voice'),
    ]);
    const drive = await driveResp.json();
    const vision = await visionResp.json();
    const voice = await voiceResp.json();

    const messages = [];
    if (drive.error) messages.push(`Drive: ${drive.error}`);
    if (vision.error) messages.push(`Vision: ${vision.error}`);
    if (voice.error) messages.push(`Voice: ${voice.error}`);
    error.textContent = messages.join('\n');

    renderConfigFields(drive, vision, voice);
  }

  function renderConfigFields(drive, vision, voice) {
    const driveHtml = drive.fields.map((field) => fieldHtml(field, drive.values, 'drive')).join('');
    const visionHtml = vision.fields.map((field) => fieldHtml(field, vision.values, 'vision')).join('');
    const voiceHtml = voice.fields.map((field) => fieldHtml(field, voice.values, 'voice')).join('');
    document.getElementById('config-fields').innerHTML = `
      <h3 class="config-section">Drive</h3>
      ${driveHtml}
      <h3 class="config-section">Vision</h3>
      ${visionHtml}
      <h3 class="config-section">Voice</h3>
      ${voiceHtml}
    `;
  }

  function fieldHtml(field, values, section) {
    const value = values[field.key];
    const inputId = `config-${section}-${field.key}`;
    if (field.type === 'boolean') {
      return `
        <div class="field">
          <label for="${inputId}">${escapeHtml(field.label)}</label>
          <input type="checkbox" id="${inputId}" data-section="${section}" data-key="${field.key}" ${value ? 'checked' : ''}>
          <span class="help">${escapeHtml(field.help)}</span>
        </div>
      `;
    }
    if (field.type === 'number') {
      const minAttr = field.min !== undefined ? ` min="${field.min}"` : '';
      const maxAttr = field.max !== undefined ? ` max="${field.max}"` : '';
      const stepAttr = field.step !== undefined ? ` step="${field.step}"` : ' step="any"';
      return `
        <div class="field">
          <label for="${inputId}">${escapeHtml(field.label)}</label>
          <input type="number" id="${inputId}" data-section="${section}" data-key="${field.key}" value="${Number(value)}"${minAttr}${maxAttr}${stepAttr}>
          <span class="help">${escapeHtml(field.help)}</span>
        </div>
      `;
    }
    if (field.type === 'text') {
      return `
        <div class="field">
          <label for="${inputId}">${escapeHtml(field.label)}</label>
          <input type="text" id="${inputId}" data-section="${section}" data-key="${field.key}" value="${escapeHtml(value || '')}">
          <span class="help">${escapeHtml(field.help)}</span>
        </div>
      `;
    }
    return `
      <div class="field">
        <label for="${inputId}">${escapeHtml(field.label)}</label>
        <input id="${inputId}" data-section="${section}" data-key="${field.key}" value="${Number(value).toFixed(2)}" inputmode="decimal">
        <span class="help">${escapeHtml(field.help)}</span>
      </div>
    `;
  }

  function closeConfig() {
    document.getElementById('config-modal').classList.add('hidden');
  }

  async function applyConfig(event) {
    event.preventDefault();
    const error = document.getElementById('config-error');
    error.textContent = '';

    const driveValues = {};
    const visionValues = {};
    const voiceValues = {};
    event.currentTarget.querySelectorAll('input[data-section]').forEach((input) => {
      const target = input.dataset.section === 'drive' ? driveValues : (input.dataset.section === 'vision' ? visionValues : voiceValues);
      if (input.type === 'checkbox') {
        target[input.dataset.key] = input.checked;
      } else if (input.type === 'text') {
        target[input.dataset.key] = input.value;
      } else {
        target[input.dataset.key] = Number(input.value);
      }
    });

    const messages = [];
    const visionResult = await postConfig('/config/vision', visionValues);
    if (!visionResult.ok) messages.push(`Vision: ${visionResult.error}`);
    const voiceResult = await postConfig('/config/voice', voiceValues);
    if (!voiceResult.ok) messages.push(`Voice: ${voiceResult.error}`);
    const driveResult = await postConfig('/config/drive', driveValues);
    if (!driveResult.ok) messages.push(`Drive: ${driveResult.error}`);

    if (messages.length) {
      error.textContent = messages.join('\n');
      return;
    }
    closeConfig();
  }

  async function postConfig(url, values) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(values),
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch (err) {
      // Non-JSON body — fall through to generic error.
    }
    if (!response.ok) {
      return { ok: false, error: payload.error || `${url} apply failed (${response.status})` };
    }
    return { ok: true };
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
