import { escapeHtml } from './dom.js';

export async function postConfig(url, values) {
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

export async function fetchVoiceValues() {
  const response = await fetch('/config/voice');
  if (!response.ok) return null;
  const payload = await response.json();
  return payload.values || null;
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

function closeConfig() {
  document.getElementById('config-modal').classList.add('hidden');
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

export function bindConfigHandlers(bindOn) {
  bindOn('config-button', 'click', openConfig);
  bindOn('config-cancel', 'click', closeConfig);
  bindOn('config-modal', 'click', (event) => {
    if (event.target.id === 'config-modal') closeConfig();
  });
  bindOn('config-form', 'submit', applyConfig);
}
