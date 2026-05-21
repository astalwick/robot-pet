import { escapeHtml } from './dom.js';
import { configStore, loadAll } from './config-store.js';

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

function renderConfigFields() {
  const driveHtml = configStore.drive.fields.map((field) => fieldHtml(field, configStore.drive.server, 'drive')).join('');
  const visionHtml = configStore.vision.fields.map((field) => fieldHtml(field, configStore.vision.server, 'vision')).join('');
  const voiceHtml = configStore.voice.fields.map((field) => fieldHtml(field, configStore.voice.server, 'voice')).join('');
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

  const results = await loadAll();
  const messages = [];
  if (results[0].error) messages.push(`Drive: ${results[0].error}`);
  if (results[1].error) messages.push(`Vision: ${results[1].error}`);
  if (results[2].error) messages.push(`Voice: ${results[2].error}`);
  error.textContent = messages.join('\n');

  renderConfigFields();
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
  const visionResult = await configStore.vision.apply(visionValues);
  if (!visionResult.ok) messages.push(`Vision: ${visionResult.error}`);
  const voiceResult = await configStore.voice.apply(voiceValues);
  if (!voiceResult.ok) messages.push(`Voice: ${voiceResult.error}`);
  const driveResult = await configStore.drive.apply(driveValues);
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
