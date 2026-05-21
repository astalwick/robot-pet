import { on, updateSession } from './dom.js';
import { setupCameraStream } from './camera.js';
import { connectTelemetry } from './telemetry.js';
import { connectLogs, bindLogToolbar } from './logs.js';
import { initRedeploy, bindRedeployHandlers } from './redeploy.js';
import { bindVoiceHandlers } from './voice.js';
import { bindConfigHandlers } from './config.js';

setupCameraStream();
bindLogToolbar(on);
bindVoiceHandlers(on);
bindRedeployHandlers(on);
bindConfigHandlers(on);
connectTelemetry();
connectLogs();
initRedeploy();

const sessionStart = Date.now();
setInterval(() => updateSession(sessionStart), 1000);
updateSession(sessionStart);
