const MAX_ROWS = 20;
const PENDING_TTL_SECS = 60;

const records = new Map();
const finalizedRows = [];

let listEl = null;
let tallyEl = null;
let lastEventT = -1;
let highestTurnId = 0;
let sessionEpoch = 0;
let latencySummary = null;

export function initVoiceTurnStats() {
  listEl = document.getElementById('voice-turn-stats-list');
  tallyEl = document.getElementById('voice-turn-stats-tally');
}

export function updateVoiceTurnStats(timeline) {
  if (!timeline || !listEl) return;
  const ref = Number(timeline.ref) || 0;
  const events = Array.isArray(timeline.events) ? timeline.events : [];
  latencySummary = timeline.latency || latencySummary;

  // Server process restart: monotonic ref jumped backwards.
  if (lastEventT > 0 && ref + 1 < lastEventT) {
    resetAll();
  }

  for (const event of events) {
    const t = Number(event.t) || 0;
    if (t <= lastEventT) continue;
    lastEventT = t;
    ingestEvent(event, t);
  }

  expirePending(ref);
  render();
}

function ingestEvent(event, t) {
  const turnId = event.turn_id;
  if (turnId == null) return;

  if (event.type === 'turn_start' && turnId <= highestTurnId) {
    beginNewSession();
  }
  if (event.type === 'turn_start') {
    highestTurnId = turnId;
  }

  const key = `${sessionEpoch}:${turnId}`;
  let record = records.get(key);
  if (!record) {
    record = { key, turnId, finalized: false };
    records.set(key, record);
  }

  switch (event.type) {
    case 'turn_start':
      if (record.finalized) return;
      if (record.start_t == null) {
        record.start_t = t;
        record.speculative = Boolean(event.speculative);
      }
      break;
    case 'turn_first_token':
      if (record.first_token_t == null) record.first_token_t = t;
      updateFinalizedRow(record);
      break;
    case 'assistant_start':
      if (record.first_audio_t == null) record.first_audio_t = t;
      updateFinalizedRow(record);
      break;
    case 'turn_committed':
      if (record.finalized) return;
      if (record.committed_t == null) {
        record.committed_t = t;
        record.from_speculative = Boolean(event.from_speculative);
        finalize(record);
      }
      break;
    case 'turn_cancel':
      if (record.finalized) return;
      if (record.cancelled_t == null) {
        record.cancelled_t = t;
        record.cancel_reason = String(event.reason || '');
        finalize(record);
      }
      break;
  }
}

function beginNewSession() {
  sessionEpoch++;
  highestTurnId = 0;
  pushRow({ outcome: 'separator' });
}

function resetAll() {
  records.clear();
  finalizedRows.length = 0;
  highestTurnId = 0;
  sessionEpoch = 0;
  lastEventT = -1;
}

function finalize(record) {
  record.finalized = true;
  record.row = classify(record);
  pushRow(record.row);
}

function updateFinalizedRow(record) {
  if (!record.finalized || !record.row) return;
  Object.assign(record.row, turnTiming(record));
}

function pushRow(row) {
  finalizedRows.unshift(row);
  if (finalizedRows.length > MAX_ROWS) finalizedRows.length = MAX_ROWS;
}

function classify(record) {
  const { turnId, start_t, first_token_t, committed_t, cancelled_t, speculative, from_speculative, cancel_reason } = record;
  const timing = turnTiming(record);
  if (committed_t != null) {
    if (speculative && from_speculative) {
      const commitDelta = (committed_t - start_t) * 1000;
      const tokenDelta = first_token_t != null ? (first_token_t - start_t) * 1000 : commitDelta;
      const savings_ms = Math.round(Math.min(commitDelta, tokenDelta));
      return { turnId, outcome: 'kept', savings_ms, ...timing };
    }
    return { turnId, outcome: 'absent', savings_ms: 0, ...timing };
  }
  if (cancelled_t != null && speculative) {
    if (first_token_t == null) {
      return { turnId, outcome: 'replaced', cost_ms: 0, no_audible: true, reason: cancel_reason, ...timing };
    }
    return { turnId, outcome: 'replaced', cost_ms: Math.round((cancelled_t - first_token_t) * 1000), reason: cancel_reason, ...timing };
  }
  return { turnId, outcome: 'cancelled', reason: cancel_reason, ...timing };
}

function turnTiming(record) {
  const timing = {};
  if (record.start_t != null && record.first_token_t != null) {
    timing.first_token_ms = Math.round((record.first_token_t - record.start_t) * 1000);
  }
  if (record.start_t != null && record.first_audio_t != null) {
    timing.first_audio_ms = Math.round((record.first_audio_t - record.start_t) * 1000);
  }
  return timing;
}

function expirePending(now) {
  for (const [key, record] of records) {
    if (record.finalized) continue;
    const anchor = record.start_t ?? record.first_token_t ?? 0;
    if (anchor && now - anchor > PENDING_TTL_SECS) records.delete(key);
  }
}

function render() {
  const visibleRows = finalizedRows.filter((row) => row.outcome !== 'separator');
  const kept = visibleRows.filter((row) => row.outcome === 'kept');
  const replaced = visibleRows.filter((row) => row.outcome === 'replaced');
  const absent = visibleRows.filter((row) => row.outcome === 'absent');
  const keptMedian = median(kept.map((row) => row.savings_ms));
  const replacedMedian = median(replaced.map((row) => row.cost_ms).filter((value) => value != null));
  const latency = latestLatency();

  const tallyParts = [
    `kept ${kept.length}${keptMedian != null ? ` (median +${keptMedian}ms)` : ''}`,
    `replaced ${replaced.length}${replacedMedian != null ? ` (median ${replacedMedian}ms cost)` : ''}`,
    `absent ${absent.length}`,
  ];
  if (latency) tallyParts.push(latency);
  tallyEl.textContent = visibleRows.length ? tallyParts.join(' · ') : 'no turns yet';

  listEl.innerHTML = '';
  for (const row of finalizedRows) {
    const line = document.createElement('div');
    if (row.outcome === 'separator') {
      line.className = 'turn-stat-separator';
      line.textContent = '— new session —';
    } else {
      line.className = `turn-stat-row turn-stat-${row.outcome}`;
      line.textContent = formatRow(row);
    }
    listEl.appendChild(line);
  }
}

function formatRow(row) {
  const id = `turn ${row.turnId}`.padEnd(9);
  const outcome = row.outcome.padEnd(9);
  if (row.outcome === 'kept') return `${id}  ${outcome}  saved ${row.savings_ms}ms${formatTiming(row)}`;
  if (row.outcome === 'absent') return `${id}  ${outcome}  —${formatTiming(row)}`;
  if (row.outcome === 'replaced') {
    const cost = row.no_audible ? 'no audible cost' : `cost ${row.cost_ms}ms`;
    return `${id}  ${outcome}  ${cost}${formatTiming(row)}${row.reason ? ` (${row.reason})` : ''}`;
  }
  return `${id}  ${outcome}${formatTiming(row)}${row.reason ? `  (${row.reason})` : ''}`;
}

function formatTiming(row) {
  const parts = [];
  if (row.first_token_ms != null) parts.push(`token ${row.first_token_ms}ms`);
  if (row.first_audio_ms != null) parts.push(`audio ${row.first_audio_ms}ms`);
  return parts.length ? `  ${parts.join(' ')}` : '';
}

function median(values) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : Math.round((sorted[mid - 1] + sorted[mid]) / 2);
}

function latestLatency() {
  if (!latencySummary) return null;
  const last = latencySummary.last || null;
  const parts = [];
  if (last && last.input_to_audio_ms != null) parts.push(`last audio ${last.input_to_audio_ms}ms`);
  if (latencySummary.median_input_to_audio_ms != null) {
    parts.push(`median audio ${latencySummary.median_input_to_audio_ms}ms`);
  }
  return parts.length ? parts.join(' / ') : null;
}
