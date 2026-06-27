import { on, updateSession } from './dom.js';
import { setupCameraStream } from './camera.js';
import { connectTelemetry } from './telemetry.js';
import { connectLogs, bindLogToolbar, bindLogScroll } from './logs.js';
import { initRedeploy, bindRedeployHandlers } from './redeploy.js';
import { bindVoiceHandlers } from './voice.js';
import { initVoiceTimeline } from './voice-timeline.js';
import { initVoiceTurnStats } from './voice-turn-stats.js';
import { initPathHistory } from './path-history.js';
import { bindConfigHandlers } from './config.js';

setupCameraStream();
bindLogToolbar(on);
bindLogScroll(on);
bindVoiceHandlers(on);
initVoiceTimeline();
initVoiceTurnStats();
initPathHistory();
bindRedeployHandlers(on);
bindConfigHandlers(on);
connectTelemetry();
connectLogs();
initRedeploy();

const sessionStart = Date.now();
setInterval(() => updateSession(sessionStart), 1000);
updateSession(sessionStart);
