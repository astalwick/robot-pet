import { escapeHtml } from './dom.js';
import { appendLog } from './logs.js';
import { fetchVoiceValues, postConfig } from './config.js';

const DEBUG = new URLSearchParams(location.search).has('debug');

let latestVoice = null;
let voiceRowsReady = false;
let voiceWantEnabled = false;
let voiceTelemetryEnabled = false;
let voiceHydrated = false;
let voicePersistWork = Promise.resolve();
const voicePendingPatch = {};
const voiceGainSaveTimers = {};
let voicePersistSerial = 0;
let lastVoiceDbgKey = '';
let lastVoiceButtonLabel = '';
let lastBargeInEventCount = null;
let bargeInFlashUntil = 0;

function voiceDbg(step, detail) {
  if (DEBUG) console.log('[dashboard voice]', step, detail || '');
}

function voiceSnapshot() {
  return {
    want: voiceWantEnabled,
    telemetry: voiceTelemetryEnabled,
    pending: voiceUiPending(),
    hydrated: voiceHydrated,
    persistSerial: voicePersistSerial,
    status: latestVoice ? latestVoice.status : null,
    enabled: latestVoice ? latestVoice.enabled : null,
  };
}

function voiceUiPending() {
  return voiceWantEnabled !== voiceTelemetryEnabled;
}

function voiceCardStatus(voice, stale, lastError) {
  if (stale) return { text: 'STALE', cls: 'err' };
  if (voiceUiPending()) {
    return { text: voiceWantEnabled ? 'STARTING' : 'STOPPING', cls: 'warn' };
  }
  const status = voice.status || 'unknown';
  return { text: status.toUpperCase(), cls: voiceStatusClass(status, lastError) };
}

function voiceStatusClass(status, lastError) {
  if (lastError || status === 'error' || status === 'stale') return 'err';
  if (status === 'starting' || status === 'reconnecting' || status === 'hearing' || status === 'thinking') return 'warn';
  if (status === 'listening' || status === 'speaking') return 'ok';
  return 'muted';
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

function voiceValueRow(key, label = key) {
  return `
      <div class="row">
        <span class="label">${escapeHtml(label)}</span>
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

function voiceToggleRow(label, key) {
  return `
      <div class="row control-row">
        <span class="label">${escapeHtml(label)}</span>
        <span class="bar-cell">
          <input type="checkbox" data-voice-key="${key}" aria-label="${escapeHtml(label)}">
        </span>
        <span class="value" data-voice-value="${key}">on</span>
      </div>
    `;
}

function voiceSliderRow(label, key, min, max, step, fallback) {
  return `
      <div class="row control-row">
        <span class="label">${escapeHtml(label)}</span>
        <span class="bar-cell">
          <input type="range" min="${min}" max="${max}" step="${step}" value="${fallback}" data-voice-key="${key}" aria-label="${escapeHtml(label)}">
        </span>
        <span class="value" data-voice-value="${key}">${fallback}</span>
      </div>
    `;
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
    voiceToggleRow('barge-in', 'barge_in_enabled'),
    voiceSliderRow('barge min rms', 'barge_in_min_rms', 100, 5000, 50, 700),
    voiceSliderRow('barge sustain ms', 'barge_in_sustain_ms', 0, 1500, 50, 350),
    voiceSliderRow('playback ratio', 'barge_in_playback_leakage_ratio', 0.5, 5, 0.1, 1.8),
    voiceValueRow('barge_in_event', 'barge-in'),
    voiceValueRow('barge_in_gate', 'interrupt gate'),
    voiceValueRow('barge_in_reason', 'gate status'),
    voiceValueRow('transcript'),
    voiceValueRow('error'),
  ].join('');
  voiceRowsReady = true;
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

function updateVoiceSlider(key, value, fallback) {
  const numeric = Number(value == null ? fallback : value);
  const input = document.querySelector(`input[data-voice-key="${key}"]`);
  const decimals = key === 'barge_in_playback_leakage_ratio' ? 1 : 0;
  if (input && document.activeElement !== input) input.value = numeric.toFixed(decimals);
  setVoiceValue(key, numeric.toFixed(decimals));
}

function updateVoiceToggle(key, value, fallback = true) {
  const enabled = value == null ? fallback : !!value;
  const input = document.querySelector(`input[data-voice-key="${key}"]`);
  if (input && document.activeElement !== input) input.checked = enabled;
  setVoiceValue(key, enabled ? 'on' : 'off', enabled ? 'ok' : 'muted');
}

function formatBargeInGate(voice) {
  if (voice.barge_in_gate_open == null) return '--';
  const mic = voice.barge_in_mic_rms != null ? voice.barge_in_mic_rms : '?';
  const threshold = voice.barge_in_threshold_rms != null ? voice.barge_in_threshold_rms : '?';
  const open = voice.barge_in_gate_open ? 'open' : 'closed';
  return `${open} (${mic}/${threshold})`;
}

function formatBargeInGateClass(voice) {
  if (voice.barge_in_gate_open == null) return 'muted';
  return voice.barge_in_gate_open ? 'ok' : 'warn';
}

function updateBargeInEvent(voice) {
  const count = Number(voice.barge_in_event_count || 0);
  if (lastBargeInEventCount == null) {
    lastBargeInEventCount = count;
  } else if (count > lastBargeInEventCount) {
    lastBargeInEventCount = count;
    bargeInFlashUntil = Date.now() + 5000;
  }

  if (voice.assistant_speaking && voice.partial_transcript) {
    setVoiceValue('barge_in_event', 'HEARING STT', 'ok');
    return;
  }

  const event = voice.barge_in_last_event || '';
  if (Date.now() < bargeInFlashUntil && event) {
    setVoiceValue('barge_in_event', `JUST NOW: ${event}`, 'err');
  } else if (event) {
    setVoiceValue('barge_in_event', `last: ${event}`, 'muted');
  } else {
    setVoiceValue('barge_in_event', 'none', 'muted');
  }
}

function clearAppliedVoicePatch(voice) {
  const gainKeys = ['input_gain', 'output_gain'];
  const sliderKeys = ['barge_in_min_rms', 'barge_in_sustain_ms', 'barge_in_playback_leakage_ratio'];
  gainKeys.forEach((key) => {
    if (voicePendingPatch[key] != null && Number(voice[key]).toFixed(1) === Number(voicePendingPatch[key]).toFixed(1)) {
      delete voicePendingPatch[key];
    }
  });
  sliderKeys.forEach((key) => {
    if (voicePendingPatch[key] == null) return;
    const decimals = key === 'barge_in_playback_leakage_ratio' ? 1 : 0;
    if (Number(voice[key]).toFixed(decimals) === Number(voicePendingPatch[key]).toFixed(decimals)) {
      delete voicePendingPatch[key];
    }
  });
  if (voicePendingPatch.barge_in_enabled != null && !!voice.barge_in_enabled === !!voicePendingPatch.barge_in_enabled) {
    delete voicePendingPatch.barge_in_enabled;
  }
}

function updateVoiceToggleButton() {
  const button = document.getElementById('voice-toggle-button');
  if (!button) return;
  const label = button.querySelector('.voice-toggle-label');
  let text;
  if (voiceUiPending()) {
    text = voiceWantEnabled ? 'Starting' : 'Stopping';
  } else {
    text = voiceWantEnabled ? 'Voice On' : 'Voice Off';
  }
  let cls = 'warn';
  if (text === 'Voice On') cls = 'ok';
  else if (text === 'Voice Off') cls = 'muted';
  const className = `voice-toggle ${cls}`;
  if (button.className !== className) button.className = className;
  if (label.textContent !== text) label.textContent = text;
  if (button.getAttribute('aria-label') !== text) button.setAttribute('aria-label', text);
  if (text !== lastVoiceButtonLabel) {
    lastVoiceButtonLabel = text;
    voiceDbg('button', { label: text, disabled: button.disabled, ...voiceSnapshot() });
  }
}

export function renderVoice(snapshot, sources) {
  const voiceSource = sources.voice || {};
  const voice = snapshot.voice || {};
  latestVoice = voice;
  const prevTelemetry = voiceTelemetryEnabled;
  voiceTelemetryEnabled = !!voice.enabled;
  if (!voiceHydrated) {
    voiceWantEnabled = voiceTelemetryEnabled;
    voiceHydrated = true;
    voiceDbg('hydrate', { fromTelemetry: voiceTelemetryEnabled, status: voice.status });
  }
  const dbgKey = `${voiceWantEnabled}|${voiceTelemetryEnabled}|${voice.status}|${voiceUiPending()}`;
  if (dbgKey !== lastVoiceDbgKey) {
    lastVoiceDbgKey = dbgKey;
    voiceDbg('telemetry', {
      telemetryChanged: prevTelemetry !== voiceTelemetryEnabled,
      prevTelemetry,
      now: voiceSnapshot(),
    });
  }
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
  updateVoiceToggle('barge_in_enabled', displayVoice.barge_in_enabled, true);
  updateVoiceSlider('barge_in_min_rms', displayVoice.barge_in_min_rms, 700);
  updateVoiceSlider('barge_in_sustain_ms', displayVoice.barge_in_sustain_ms, 350);
  updateVoiceSlider('barge_in_playback_leakage_ratio', displayVoice.barge_in_playback_leakage_ratio, 1.8);
  updateBargeInEvent(displayVoice);
  setVoiceValue('barge_in_gate', formatBargeInGate(displayVoice), formatBargeInGateClass(displayVoice));
  setVoiceValue('barge_in_reason', displayVoice.barge_in_last_reason || '--', displayVoice.barge_in_last_reason ? 'warn' : 'muted');
  const transcript = displayVoice.partial_transcript || displayVoice.last_committed_transcript || '--';
  setVoiceValue('transcript', transcript, transcript === '--' ? 'muted' : '');
  setVoiceValue('error', lastError || '--', lastError ? 'err' : 'muted');
  updateVoiceToggleButton();
}

async function updateVoiceConfig(patch) {
  voiceDbg('config update start', patch);
  try {
    const values = await fetchVoiceValues();
    if (!values) return { ok: false, error: 'could not load voice config' };
    const merged = { ...values, ...patch };
    voiceDbg('config post start', { enabled: merged.enabled });
    const result = await postConfig('/config/voice', merged);
    voiceDbg('config post done', result);
    return result;
  } catch (err) {
    voiceDbg('config update error', String(err));
    return { ok: false, error: String(err) };
  }
}

function onVoiceToggle() {
  const before = voiceSnapshot();
  voiceDbg('click', { before });
  voiceWantEnabled = !voiceWantEnabled;
  voiceDbg('click -> want updated', { after: voiceSnapshot() });
  updateVoiceToggleButton();
  queueVoicePersist();
}

function queueVoicePersist() {
  const job = ++voicePersistSerial;
  voiceDbg('persist queued', { job, ...voiceSnapshot() });
  voicePersistWork = voicePersistWork.then(() => persistVoiceWant(job)).catch((err) => {
    voiceDbg('persist chain error', { job, err: String(err), ...voiceSnapshot() });
    appendLog(`voice config save failed: ${err}`);
    voiceWantEnabled = voiceTelemetryEnabled;
    voiceDbg('persist reverted want to telemetry', voiceSnapshot());
    updateVoiceToggleButton();
  });
}

async function persistVoiceWant(job) {
  const want = voiceWantEnabled;
  voiceDbg('persist start', { job, want, ...voiceSnapshot() });
  const result = await updateVoiceConfig({ enabled: want });
  voiceDbg('persist post done', { job, want, result, ...voiceSnapshot() });
  if (!result.ok) {
    appendLog(`voice config update failed: ${result.error}`);
    voiceWantEnabled = voiceTelemetryEnabled;
    voiceDbg('persist failed -> reverted want', voiceSnapshot());
    updateVoiceToggleButton();
    return;
  }
  if (voiceWantEnabled !== want) {
    voiceDbg('persist want changed during save, saving again', { job, saved: want, now: voiceWantEnabled });
    await persistVoiceWant(job);
    return;
  }
  voiceDbg('persist complete', { job, ...voiceSnapshot() });
}

function onVoiceGainInput(event) {
  const input = event.target;
  if (!input.dataset.voiceKey) return;
  const key = input.dataset.voiceKey;
  if (input.type === 'checkbox') {
    voicePendingPatch[key] = input.checked;
    updateVoiceToggle(key, input.checked);
    clearTimeout(voiceGainSaveTimers[key]);
    voiceGainSaveTimers[key] = setTimeout(() => saveVoiceSetting(key), 350);
    return;
  }
  voicePendingPatch[key] = Number(input.value);
  if (key === 'barge_in_playback_leakage_ratio' || key.startsWith('barge_in_')) {
    updateVoiceSlider(key, Number(input.value), Number(input.value));
  } else {
    setVoiceValue(key, Number(input.value).toFixed(1));
  }
  clearTimeout(voiceGainSaveTimers[key]);
  voiceGainSaveTimers[key] = setTimeout(() => saveVoiceSetting(key), 350);
}

function onVoiceGainCommit(event) {
  const input = event.target;
  if (!input.dataset.voiceKey) return;
  clearTimeout(voiceGainSaveTimers[input.dataset.voiceKey]);
  saveVoiceSetting(input.dataset.voiceKey);
}

async function saveVoiceSetting(key) {
  const value = voicePendingPatch[key];
  if (value == null) return;
  const result = await updateVoiceConfig({ [key]: value });
  if (!result.ok) appendLog(`voice config update failed: ${result.error}`);
}

export function bindVoiceHandlers(bindOn) {
  bindOn('voice-toggle-button', 'click', onVoiceToggle);
  bindOn('voice-rows', 'input', onVoiceGainInput);
  bindOn('voice-rows', 'change', onVoiceGainCommit);
}
