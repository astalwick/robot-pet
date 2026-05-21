const VISION_STALE_SECONDS = 2.0;

let cameraRetry = null;

export function setupCameraStream() {
  const camera = document.getElementById('camera-stream');
  if (!camera) return;
  camera.addEventListener('error', scheduleCameraReconnect);
  refreshCameraStream();
}

export function refreshCameraStream() {
  const camera = document.getElementById('camera-stream');
  if (!camera) return;
  camera.src = `http://${window.location.hostname}:8081/stream.mjpg?t=${Date.now()}`;
}

function scheduleCameraReconnect() {
  if (cameraRetry != null) return;
  cameraRetry = setTimeout(() => {
    cameraRetry = null;
    refreshCameraStream();
  }, 2000);
}

export function renderFaceOverlay(snapshot, sources) {
  const overlay = document.getElementById('face-overlay');
  const visionSource = sources.vision || {};
  const vision = snapshot.vision;

  if (!vision || visionSource.stale === true) {
    overlay.innerHTML = '';
    return;
  }

  const lastDetection = vision.last_detection_time;
  const snapshotTime = snapshot.time;
  if (lastDetection != null && snapshotTime != null
      && (snapshotTime - lastDetection) > VISION_STALE_SECONDS) {
    overlay.innerHTML = '';
    return;
  }

  const imageWidth = vision.image_width;
  const imageHeight = vision.image_height;
  if (!imageWidth || !imageHeight) {
    overlay.innerHTML = '';
    return;
  }

  const cameraSection = document.getElementById('camera-section');
  const rect = containedImageRect(
    cameraSection.clientWidth, cameraSection.clientHeight, imageWidth, imageHeight,
  );
  const faces = vision.faces || [];
  overlay.innerHTML = faces.map((face) => faceBoxHtml(face, rect)).join('');
}

function containedImageRect(containerW, containerH, sourceW, sourceH) {
  if (containerW <= 0 || containerH <= 0 || sourceW <= 0 || sourceH <= 0) {
    return { left: 0, top: 0, width: 0, height: 0 };
  }
  const sourceAspect = sourceW / sourceH;
  const containerAspect = containerW / containerH;
  if (containerAspect > sourceAspect) {
    const height = containerH;
    const width = height * sourceAspect;
    return { left: (containerW - width) / 2, top: 0, width, height };
  }
  const width = containerW;
  const height = width / sourceAspect;
  return { left: 0, top: (containerH - height) / 2, width, height };
}

function faceBoxHtml(face, rect) {
  const left = rect.left + face.x * rect.width;
  const top = rect.top + face.y * rect.height;
  const width = face.width * rect.width;
  const height = face.height * rect.height;
  return `<div class="face-box" style="left: ${left.toFixed(1)}px; top: ${top.toFixed(1)}px; width: ${width.toFixed(1)}px; height: ${height.toFixed(1)}px;"></div>`;
}
