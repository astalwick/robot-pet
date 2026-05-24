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
  if (status === 'waiting') return 'muted';
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

function updateVoiceToggleButton() {
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
  for (const button of document.querySelectorAll('.voice-toggle')) {
    const label = button.querySelector('.voice-toggle-label');
    const timeline = button.classList.contains('voice-timeline-toggle');
    const nextClassName = timeline ? `${className} voice-timeline-toggle` : className;
    if (button.className !== nextClassName) button.className = nextClassName;
    if (label && label.textContent !== text) label.textContent = text;
    if (button.getAttribute('aria-label') !== text) button.setAttribute('aria-label', text);
  }
  if (text !== lastVoiceButtonLabel) {
    lastVoiceButtonLabel = text;
    voiceDbg('button', { label: text });
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
  const patch = voiceWantEnabled
    ? { enabled: true, wake_word_enabled: true }
    : { enabled: false };
  configStore.voice.set(patch);
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

async function onTalkNow() {
  try {
    const response = await fetch('/voice/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd: 'talk_now' }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      appendLog(`talk now failed: ${body.error || response.statusText}`);
    }
  } catch (exc) {
    appendLog(`talk now failed: ${exc}`);
  }
}

export function bindVoiceHandlers(bindOn) {
  bindOn('voice-toggle-button', 'click', onVoiceToggle);
  bindOn('voice-timeline-toggle-button', 'click', onVoiceToggle);
  bindOn('voice-talk-now-button', 'click', onTalkNow);
  bindOn('voice-rows', 'input', onVoiceGainInput);
  bindOn('voice-rows', 'change', onVoiceGainCommit);
}
