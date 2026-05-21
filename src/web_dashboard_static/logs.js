import { on } from './dom.js';

let logsPaused = false;

export function appendLog(line) {
  if (logsPaused) return;
  const output = document.getElementById('logs-output');
  output.textContent += `${line}\n`;
  output.scrollTop = output.scrollHeight;
}

export function connectLogs() {
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

export function bindLogToolbar(bindOn) {
  bindOn('logs-clear', 'click', () => {
    const output = document.getElementById('logs-output');
    if (output) output.textContent = '';
  });
  bindOn('logs-pause', 'click', () => {
    logsPaused = !logsPaused;
    const button = document.getElementById('logs-pause');
    if (button) button.textContent = logsPaused ? 'Resume' : 'Pause';
  });
}
