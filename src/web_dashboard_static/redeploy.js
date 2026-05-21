import { appendLog } from './logs.js';
import { refreshCameraStream } from './camera.js';

const DEBUG = new URLSearchParams(location.search).has('debug');

let redeployArmedUntil = 0;
let redeployRunning = false;
let redeployWork = Promise.resolve();
let redeployWorkSerial = 0;
let lastRedeployButtonLabel = '';
let redeployDisarmTimer = null;

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
  };
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
      appendLog(`redeploy arm failed: ${err}`);
    });
    redeployDbg('arm queued', { job, ...redeploySnapshot() });
    return;
  }
  redeployArmedUntil = 0;
  redeployRunning = true;
  scheduleRedeployDisarm();
  redeployDbg('click -> run (local)', { after: redeploySnapshot() });
  updateRedeployButton();
  const job = ++redeployWorkSerial;
  redeployWork = redeployWork.then(() => postRedeployRun(job)).catch((err) => {
    redeployDbg('run chain error', { job, err: String(err), ...redeploySnapshot() });
    redeployRunning = false;
    updateRedeployButton();
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
  redeployDbg('apply status', { source, status, before, after: redeploySnapshot() });
  if (wasRunning && !redeployRunning) refreshCameraStream();
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
}

export function initRedeploy() {
  setInterval(refreshRedeployStatus, 1000);
  syncRedeployFromServer();
  setInterval(syncRedeployFromServer, 5000);
}
