const MAX_ROWS = 20;
const PENDING_TTL_SECS = 60;

const records = new Map();
const finalizedRows = [];

let listEl = null;
let tallyEl = null;

export function initVoiceTurnStats() {
  listEl = document.getElementById('voice-turn-stats-list');
  tallyEl = document.getElementById('voice-turn-stats-tally');
}

export function updateVoiceTurnStats(timeline) {
  if (!timeline || !listEl) return;
  const ref = Number(timeline.ref) || 0;
  const events = Array.isArray(timeline.events) ? timeline.events : [];

  for (const event of events) {
    ingestEvent(event);
  }

  expirePending(ref);
  render();
}

function ingestEvent(event) {
  const turnId = event.turn_id;
  if (turnId == null) return;
  const t = Number(event.t) || 0;
  let record = records.get(turnId);
  if (!record) {
    record = { turnId, finalized: false };
    records.set(turnId, record);
  }
  if (record.finalized) return;

  switch (event.type) {
    case 'turn_start':
      if (record.start_t == null) {
        record.start_t = t;
        record.speculative = Boolean(event.speculative);
      }
      break;
    case 'turn_first_token':
      if (record.first_token_t == null) record.first_token_t = t;
      break;
    case 'turn_committed':
      if (record.committed_t == null) {
        record.committed_t = t;
        record.from_speculative = Boolean(event.from_speculative);
        finalize(record);
      }
      break;
    case 'turn_cancel':
      if (record.cancelled_t == null) {
        record.cancelled_t = t;
        record.cancel_reason = String(event.reason || '');
        finalize(record);
      }
      break;
  }
}

function finalize(record) {
  record.finalized = true;
  const row = classify(record);
  finalizedRows.unshift(row);
  if (finalizedRows.length > MAX_ROWS) finalizedRows.length = MAX_ROWS;
}

function classify(record) {
  const { turnId, start_t, first_token_t, committed_t, cancelled_t, speculative, from_speculative, cancel_reason } = record;
  if (committed_t != null) {
    if (speculative && from_speculative) {
      const commitDelta = (committed_t - start_t) * 1000;
      const tokenDelta = first_token_t != null ? (first_token_t - start_t) * 1000 : commitDelta;
      const savings_ms = Math.round(Math.min(commitDelta, tokenDelta));
      return { turnId, outcome: 'kept', savings_ms };
    }
    return { turnId, outcome: 'absent', savings_ms: 0 };
  }
  if (cancelled_t != null && speculative) {
    const cost_ms = first_token_t != null ? Math.round((cancelled_t - first_token_t) * 1000) : null;
    return { turnId, outcome: 'replaced', savings_ms: null, cost_ms, reason: cancel_reason };
  }
  return { turnId, outcome: 'cancelled', savings_ms: null, reason: cancel_reason };
}

function expirePending(now) {
  for (const [turnId, record] of records) {
    if (record.finalized) continue;
    const anchor = record.start_t ?? record.first_token_t ?? 0;
    if (anchor && now - anchor > PENDING_TTL_SECS) records.delete(turnId);
  }
}

function render() {
  const kept = finalizedRows.filter((row) => row.outcome === 'kept');
  const replaced = finalizedRows.filter((row) => row.outcome === 'replaced');
  const absent = finalizedRows.filter((row) => row.outcome === 'absent');
  const keptMedian = median(kept.map((row) => row.savings_ms));
  const replacedMedian = median(replaced.map((row) => row.cost_ms).filter((value) => value != null));

  const tallyParts = [
    `kept ${kept.length}${keptMedian != null ? ` (median +${keptMedian}ms)` : ''}`,
    `replaced ${replaced.length}${replacedMedian != null ? ` (median ${replacedMedian}ms cost)` : ''}`,
    `absent ${absent.length}`,
  ];
  tallyEl.textContent = tallyParts.join(' · ');

  listEl.innerHTML = '';
  for (const row of finalizedRows) {
    const line = document.createElement('div');
    line.className = `turn-stat-row turn-stat-${row.outcome}`;
    line.textContent = formatRow(row);
    listEl.appendChild(line);
  }
}

function formatRow(row) {
  const id = `turn ${row.turnId}`.padEnd(9);
  const outcome = row.outcome.padEnd(9);
  if (row.outcome === 'kept') return `${id}  ${outcome}  saved ${row.savings_ms}ms`;
  if (row.outcome === 'absent') return `${id}  ${outcome}  —`;
  if (row.outcome === 'replaced') {
    const cost = row.cost_ms != null ? `cost ${row.cost_ms}ms` : 'cost ?';
    return `${id}  ${outcome}  ${cost}${row.reason ? ` (${row.reason})` : ''}`;
  }
  return `${id}  ${outcome}${row.reason ? `  (${row.reason})` : ''}`;
}

function median(values) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : Math.round((sorted[mid - 1] + sorted[mid]) / 2);
}
