import { on } from './dom.js';

let logsPaused = false;
let followBottom = true;

function logsAtBottom(output) {
  return output.scrollHeight - output.scrollTop - output.clientHeight <= 4;
}

export function appendLog(line) {
  if (logsPaused) return;
  const output = document.getElementById('logs-output');
  output.textContent += `${line}\n`;
  if (followBottom) {
    output.scrollTop = output.scrollHeight;
  }
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

export function bindLogScroll(bindOn) {
  bindOn('logs-output', 'scroll', () => {
    const output = document.getElementById('logs-output');
    if (output) followBottom = logsAtBottom(output);
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
