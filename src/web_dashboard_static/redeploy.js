import { appendLog } from './logs.js';
import { refreshCameraStream } from './camera.js';
import { on } from './dom.js';

const DEBUG = new URLSearchParams(location.search).has('debug');
const REDEPLOY_RELOAD_DELAY_MS = 1500;

let redeployArmedUntil = 0;
let redeployRunning = false;
let redeployWork = Promise.resolve();
let redeployWorkSerial = 0;
let lastRedeployButtonLabel = '';
let redeployDisarmTimer = null;
let redeployResultsSeen = false;
let seenResultSerial = 0;
let redeployReloadTimer = null;

function redeployDbg(step, detail) {
  if (DEBUG) console.log('[dashboard redeploy]', step, detail || '');
}

function redeploySnapshot() {
  return {
    armedUntil: redeployArmedUntil,
    armed: Date.now() <= redeployArmedUntil,
    running: redeployRunning,
    workSerial: redeployWorkSerial,
    msUntilDisarm: Math.max(0, redeployArmedUntil - Date.now()),
    seenResultSerial,
  };
}

function hideRedeployAlert() {
  clearTimeout(redeployReloadTimer);
  redeployReloadTimer = null;
  const alert = document.getElementById('redeploy-alert');
  const dismiss = document.getElementById('redeploy-alert-dismiss');
  if (!alert) return;
  alert.className = 'redeploy-alert hidden';
  if (dismiss) dismiss.classList.add('hidden');
}

function showRedeployAlert(kind, message) {
  const alert = document.getElementById('redeploy-alert');
  const text = document.getElementById('redeploy-alert-text');
  const dismiss = document.getElementById('redeploy-alert-dismiss');
  if (!alert || !text) return;
  clearTimeout(redeployReloadTimer);
  redeployReloadTimer = null;
  const prefix = kind === 'success' ? 'Redeploy succeeded: ' : 'Redeploy failed: ';
  text.textContent = `${prefix}${message}`;
  alert.className = `redeploy-alert ${kind === 'success' ? 'ok' : 'err'}`;
  if (dismiss) dismiss.classList.toggle('hidden', kind === 'success');
}

function scheduleRedeployReload() {
  clearTimeout(redeployReloadTimer);
  redeployReloadTimer = setTimeout(() => location.reload(), REDEPLOY_RELOAD_DELAY_MS);
}

function onRedeployResult(status) {
  if (status.last_result === 'success') {
    showRedeployAlert('success', status.last_message || 'Redeploy complete.');
    scheduleRedeployReload();
    return;
  }
  if (status.last_result === 'failed') {
    showRedeployAlert('failed', status.last_message || 'Unknown error.');
  }
}

function onRedeploy() {
  redeployDbg('click', { before: redeploySnapshot() });
  if (redeployRunning) {
    redeployDbg('click ignored', 'redeployRunning is true');
    return;
  }
  if (Date.now() > redeployArmedUntil) {
    redeployArmedUntil = Date.now() + 10000;
    redeployDbg('click -> arm (local)', { after: redeploySnapshot() });
    scheduleRedeployDisarm();
    updateRedeployButton();
    const job = ++redeployWorkSerial;
    redeployWork = redeployWork.then(() => postRedeployArm(job)).catch((err) => {
      redeployDbg('arm chain error', { job, err: String(err), ...redeploySnapshot() });
      redeployArmedUntil = 0;
      scheduleRedeployDisarm();
      updateRedeployButton();
      showRedeployAlert('failed', String(err));
      appendLog(`redeploy arm failed: ${err}`);
    });
    redeployDbg('arm queued', { job, ...redeploySnapshot() });
    return;
  }
  redeployArmedUntil = 0;
  redeployRunning = true;
  hideRedeployAlert();
  scheduleRedeployDisarm();
  redeployDbg('click -> run (local)', { after: redeploySnapshot() });
  updateRedeployButton();
  const job = ++redeployWorkSerial;
  redeployWork = redeployWork.then(() => postRedeployRun(job)).catch((err) => {
    redeployDbg('run chain error', { job, err: String(err), ...redeploySnapshot() });
    redeployRunning = false;
    updateRedeployButton();
    showRedeployAlert('failed', String(err));
    appendLog(`redeploy run failed: ${err}`);
  });
  redeployDbg('run queued', { job, ...redeploySnapshot() });
}

async function postRedeployArm(job) {
  redeployDbg('arm fetch start', { job, ...redeploySnapshot() });
  const response = await fetch('/redeploy/arm', { method: 'POST' });
  const status = await response.json();
  redeployDbg('arm fetch done', { job, ok: response.ok, status, ...redeploySnapshot() });
  applyRedeployStatus(status, 'arm-response');
}

async function postRedeployRun(job) {
  redeployDbg('run fetch start', { job, ...redeploySnapshot() });
  const response = await fetch('/redeploy/run', { method: 'POST' });
  const status = await response.json();
  redeployDbg('run fetch done', { job, ok: response.ok, status, ...redeploySnapshot() });
  applyRedeployStatus(status, 'run-response');
}

async function syncRedeployFromServer() {
  try {
    const response = await fetch('/redeploy/status');
    if (!response.ok) return;
    applyRedeployStatus(await response.json(), 'sync');
  } catch (err) {
    redeployDbg('sync error', String(err));
  }
}

async function refreshRedeployStatus() {
  if (!redeployRunning && Date.now() > redeployArmedUntil) return;
  await syncRedeployFromServer();
}

function applyRedeployStatus(status, source) {
  const before = redeploySnapshot();
  const wasRunning = redeployRunning;
  redeployRunning = status.running === true;
  const resultSerial = status.result_serial ?? 0;
  if (!redeployResultsSeen) {
    redeployResultsSeen = true;
    seenResultSerial = resultSerial;
  } else if (resultSerial > seenResultSerial) {
    seenResultSerial = resultSerial;
    onRedeployResult(status);
  }
  redeployDbg('apply status', { source, status, before, after: redeploySnapshot() });
  if (wasRunning && !redeployRunning) {
    refreshCameraStream();
    syncRedeployFromServer();
  }
  updateRedeployButton();
}

function scheduleRedeployDisarm() {
  clearTimeout(redeployDisarmTimer);
  redeployDisarmTimer = null;
  const delay = redeployArmedUntil - Date.now();
  if (delay > 0) {
    redeployDisarmTimer = setTimeout(() => {
      redeployDisarmTimer = null;
      updateRedeployButton();
    }, delay + 10);
  }
}

function updateRedeployButton() {
  const button = document.getElementById('redeploy-button');
  if (!button) return;
  if (button.disabled) button.disabled = false;
  let text = 'Redeploy';
  let className = '';
  if (redeployRunning) {
    text = 'Redeploying...';
    className = 'is-busy';
  } else if (Date.now() <= redeployArmedUntil) {
    text = 'Redeploy Armed';
    className = 'armed';
  }
  if (button.textContent !== text) button.textContent = text;
  if (button.className !== className) button.className = className;
  const stateLabel = `${text}|${className}`;
  if (stateLabel !== lastRedeployButtonLabel) {
    lastRedeployButtonLabel = stateLabel;
    redeployDbg('button', {
      text,
      className,
      disabled: button.disabled,
      ...redeploySnapshot(),
    });
  }
}

export function bindRedeployHandlers(bindOn) {
  bindOn('redeploy-button', 'click', onRedeploy);
  on('redeploy-alert-dismiss', 'click', hideRedeployAlert);
}

export function initRedeploy() {
  setInterval(refreshRedeployStatus, 1000);
  syncRedeployFromServer();
  setInterval(syncRedeployFromServer, 5000);
}
