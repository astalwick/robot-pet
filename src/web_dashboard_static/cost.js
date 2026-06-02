import { row, setRows, formatDuration } from './dom.js';

function usd(value) {
  if (value == null) return '--';
  return `$${value.toFixed(4)}`;
}

function tokens(value) {
  if (value == null) return '--';
  return value.toLocaleString();
}

export function renderCost(snapshot) {
  const cost = (snapshot.voice || {}).cost;
  if (!cost) {
    setRows('cost-rows', [row('usage', '', 'no data', 'muted')]);
    return;
  }
  const stt = cost.stt || {};
  const llm = cost.llm || {};
  const tts = cost.tts || {};
  const cached = llm.cached_input_tokens || 0;
  const inputLabel = cached > 0
    ? `${tokens(llm.input_tokens)} (${tokens(cached)} cached)`
    : tokens(llm.input_tokens);

  setRows('cost-rows', [
    row('STT model', '', stt.model || '--', 'muted'),
    row('STT audio', '', formatDuration(stt.audio_seconds || 0)),
    row('STT cost', '', usd(stt.usd)),
    row('LLM model', '', llm.model || '--', 'muted'),
    row('LLM input', '', inputLabel),
    row('LLM output', '', tokens(llm.output_tokens)),
    row('LLM cost', '', usd(llm.usd)),
    row('TTS model', '', tts.model || '--', 'muted'),
    row('TTS chars', '', tokens(tts.characters)),
    row('TTS cost', '', usd(tts.usd)),
    row('total', '', usd(cost.total_usd), 'strong'),
  ]);
}
