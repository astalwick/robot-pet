import { escapeHtml } from './dom.js';
import { appendLog } from './logs.js';
import { configStore } from './config-store.js';

const DEBUG = new URLSearchParams(location.search).has('debug');

const GAIN_KEYS = ['input_gain', 'output_gain'];

let latestVoice = null;
let voiceRowsReady = false;
let voiceWantEnabled = false;
let voiceTelemetryEnabled = false;
let voiceHydrated = false;
let lastVoiceDbgKey = '';
let lastVoiceButtonLabel = '';
let lastBargeInEventCount = null;
let bargeInFlashUntil = 0;

function voiceDbg(step, detail) {
  if (DEBUG) console.log('[dashboard voice]', step, detail || '');
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

function updateGainControl(key) {
  const raw = configStore.voice.get(key);
  const gain = Number(raw == null ? 1.0 : raw);
  const input = document.querySelector(`input[data-voice-key="${key}"]`);
  if (input) input.value = gain.toFixed(1);
  setVoiceValue(key, gain.toFixed(1));
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
    voiceDbg('button', { label: text, disabled: button.disabled });
  }
}

function displayEnabled() {
  if (configStore.voice.hasLocal('enabled')) {
    return !!configStore.voice.get('enabled');
  }
  return !!latestVoice && !!latestVoice.enabled;
}

export function renderVoice(snapshot, sources) {
  const voiceSource = sources.voice || {};
  const voice = snapshot.voice || {};
  latestVoice = voice;
  configStore.voice.ingestServer(voice);

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
    });
  }

  const stale = voiceSource.stale !== false;
  const lastError = voice.last_error;
  const cardStatus = voiceCardStatus(voice, stale, lastError);
  const enabled = displayEnabled();

  ensureVoiceRows();
  setVoiceValue('status', cardStatus.text, cardStatus.cls);
  setVoiceValue('listen', enabled ? 'enabled' : 'disabled', enabled ? 'ok' : 'muted');
  setVoiceValue('input', voice.input_device || '--');
  setVoiceValue('output', voice.output_device || '--');
  setVoiceValue('channel', voice.capture_channel_index != null ? String(voice.capture_channel_index) : '--');
  updateGainControl('input_gain');
  updateGainControl('output_gain');
  updateBargeInEvent(voice);
  setVoiceValue('barge_in_gate', formatBargeInGate(voice), formatBargeInGateClass(voice));
  setVoiceValue('barge_in_reason', voice.barge_in_last_reason || '--', voice.barge_in_last_reason ? 'warn' : 'muted');
  const transcript = voice.partial_transcript || voice.last_committed_transcript || '--';
  setVoiceValue('transcript', transcript, transcript === '--' ? 'muted' : '');
  setVoiceValue('error', lastError || '--', lastError ? 'err' : 'muted');
  updateVoiceToggleButton();
}

async function handleSaveError(result) {
  if (!result || result.ok) return;
  appendLog(`voice config update failed: ${result.error}`);
}

function onVoiceToggle() {
  voiceWantEnabled = !voiceWantEnabled;
  updateVoiceToggleButton();
  configStore.voice.set({ enabled: voiceWantEnabled });
  configStore.voice.flush().then((result) => {
    handleSaveError(result);
    if (result && !result.ok) {
      voiceWantEnabled = voiceTelemetryEnabled;
      updateVoiceToggleButton();
    }
  });
}

function onVoiceGainInput(event) {
  const input = event.target;
  if (!input.dataset.voiceKey) return;
  if (!GAIN_KEYS.includes(input.dataset.voiceKey)) return;
  configStore.voice.set({ [input.dataset.voiceKey]: Number(input.value) });
}

function onVoiceGainCommit(event) {
  const input = event.target;
  if (!input.dataset.voiceKey) return;
  if (!GAIN_KEYS.includes(input.dataset.voiceKey)) return;
  configStore.voice.set({ [input.dataset.voiceKey]: Number(input.value) });
  configStore.voice.flush().then(handleSaveError);
}

export function bindVoiceHandlers(bindOn) {
  bindOn('voice-toggle-button', 'click', onVoiceToggle);
  bindOn('voice-rows', 'input', onVoiceGainInput);
  bindOn('voice-rows', 'change', onVoiceGainCommit);
}
