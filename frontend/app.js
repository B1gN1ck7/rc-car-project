const BACKEND_BASE = window.location.origin;
const MIC_SAMPLE_RATE = 48000;
const PI_MIC_SAMPLE_RATE = 48000;

const cameraStreamEl = document.getElementById('camera-stream');
if (cameraStreamEl) {
  cameraStreamEl.src = '/video_feed';
}

// --- Volume Bar Logic ---
const volumeBar = document.querySelector('.volume-bar');
if (volumeBar) {
  volumeBar.addEventListener('input', () => {
    const gain = parseFloat(volumeBar.value) / 100;
    if (piMicGainNode) {
      piMicGainNode.gain.value = gain;
    }
    if (socket && socket.connected) {
      socket.emit('volume_update', { volume: gain });
    }
  });
}

async function ensurePiMicAudioContext() {
  if (!piMicAudioContext) {
    piMicAudioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: PI_MIC_SAMPLE_RATE });
    piMicGainNode = piMicAudioContext.createGain();
    const initialGain = volumeBar ? (parseFloat(volumeBar.value) / 100) : 1;
    piMicGainNode.gain.value = initialGain;
    piMicGainNode.connect(piMicAudioContext.destination);
    piMicNextTime = piMicAudioContext.currentTime;
  }
  if (piMicAudioContext.state !== 'running') {
    await piMicAudioContext.resume();
  }
}

function decodePayloadToInt16(payload) {
  if (payload instanceof ArrayBuffer) return new Int16Array(payload);
  if (ArrayBuffer.isView(payload)) return new Int16Array(payload.buffer, payload.byteOffset, Math.floor(payload.byteLength / 2));
  return null;
}

function playPiMicChunk(payload) {
  if (!piMicAudioContext || !piMicGainNode) return;
  const pcm = decodePayloadToInt16(payload);
  if (!pcm || pcm.length === 0) return;
  const floatData = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) {
    floatData[i] = pcm[i] / 32768;
  }
  const audioBuffer = piMicAudioContext.createBuffer(1, floatData.length, PI_MIC_SAMPLE_RATE);
  audioBuffer.copyToChannel(floatData, 0);
  const src = piMicAudioContext.createBufferSource();
  src.buffer = audioBuffer;
  src.connect(piMicGainNode);
  const now = piMicAudioContext.currentTime;
  const startAt = Math.max(now + 0.02, piMicNextTime);
  src.start(startAt);
  piMicNextTime = startAt + audioBuffer.duration;
}
// --- Mic Toggle Logic ---
const micToggle = document.querySelector('.mic-panel input[type="checkbox"]');
let micStream = null;
let micAudioContext = null;
let micSourceNode = null;
let micProcessorNode = null;
let micMuteNode = null;
let piMicAudioContext = null;
let piMicGainNode = null;
let piMicNextTime = 0;

function floatToInt16Buffer(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out.buffer;
}

async function startMicStream() {
  if (!navigator.mediaDevices) return;
  try {
    if (!socket || !socket.connected) {
      throw new Error('Backend socket not connected');
    }
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micAudioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: MIC_SAMPLE_RATE });
    await micAudioContext.resume();
    micSourceNode = micAudioContext.createMediaStreamSource(micStream);
    micProcessorNode = micAudioContext.createScriptProcessor(2048, 1, 1);
    micMuteNode = micAudioContext.createGain();
    micMuteNode.gain.value = 0;

    micProcessorNode.onaudioprocess = (event) => {
      if (!socket || !socket.connected) return;
      const input = event.inputBuffer.getChannelData(0);
      socket.emit('mic_pcm', floatToInt16Buffer(input));
    };

    micSourceNode.connect(micProcessorNode);
    micProcessorNode.connect(micMuteNode);
    micMuteNode.connect(micAudioContext.destination);
  } catch (err) {
    console.error('Mic start failed:', err);
    stopMicStream();
    if (micToggle) micToggle.checked = false;
  }
}

function stopMicStream() {
  if (micProcessorNode) micProcessorNode.disconnect();
  if (micSourceNode) micSourceNode.disconnect();
  if (micMuteNode) micMuteNode.disconnect();
  if (micStream) micStream.getTracks().forEach(track => track.stop());
  if (micAudioContext) micAudioContext.close();
  micAudioContext = null;
  micSourceNode = null;
  micProcessorNode = null;
  micMuteNode = null;
  micStream = null;
}

if (micToggle) {
  micToggle.addEventListener('change', (e) => {
    if (micToggle.checked) {
      startMicStream();
    } else {
      stopMicStream();
    }
  });
}
// --- Camera Pan Controls ---
const camPanValueEl = document.getElementById('cam-pan-value');
const camPanLeftBtn = document.querySelector('.cam-pan-btn.cam-pan-left');
const camPanRightBtn = document.querySelector('.cam-pan-btn.cam-pan-right');
let camPanSetting = 0;
function updateCamPanUI() {
  if (camPanValueEl) camPanValueEl.textContent = camPanSetting;
  if (camPanLeftBtn) camPanLeftBtn.classList.toggle('active', camPanSetting < 0);
  if (camPanRightBtn) camPanRightBtn.classList.toggle('active', camPanSetting > 0);
}
if (camPanLeftBtn) {
  camPanLeftBtn.addEventListener('click', () => {
    if (camPanSetting > -3) camPanSetting--;
    updateCamPanUI();
    state.cameraAngle = camPanSetting;
  });
}
if (camPanRightBtn) {
  camPanRightBtn.addEventListener('click', () => {
    if (camPanSetting < 3) camPanSetting++;
    updateCamPanUI();
    state.cameraAngle = camPanSetting;
  });
}
updateCamPanUI();

// --- Steering via Navigation Panel ---
const steerValueEl = document.getElementById('steer-value');
const navLeftBtn = document.querySelector('.nav-btn.nav-left');
const navRightBtn = document.querySelector('.nav-btn.nav-right');
const navZeroBtn = document.querySelector('.nav-btn.nav-zero');
let steerSetting = 0;
function updateSteerUI() {
  if (steerValueEl) steerValueEl.textContent = steerSetting;
  if (navLeftBtn) navLeftBtn.classList.toggle('active', steerSetting < 0);
  if (navRightBtn) navRightBtn.classList.toggle('active', steerSetting > 0);
}
if (navLeftBtn) {
  navLeftBtn.addEventListener('click', () => {
    if (steerSetting > -3) steerSetting--;
    updateSteerUI();
    state.steering = steerSetting;
  });
}
if (navRightBtn) {
  navRightBtn.addEventListener('click', () => {
    if (steerSetting < 3) steerSetting++;
    updateSteerUI();
    state.steering = steerSetting;
  });
}
if (navZeroBtn) {
  navZeroBtn.addEventListener('click', () => {
    steerSetting = 0;
    updateSteerUI();
    state.steering = steerSetting;
  });
}
updateSteerUI();
// Camera stream is provided by <img id="camera-stream"> in index.html via /video_feed.
// Sensor update handling for all configured zones (8 zones in backend SENSOR_PINS).

const zoneElements = {
  zone_extra_front: document.querySelector('.zone.zone-extra-front'),
  zone_front: document.querySelector('.zone.zone-front'),
  zone_front_right: document.querySelector('.zone.zone-front-right'),
  zone_front_left: document.querySelector('.zone.zone-front-left'),
  zone_back_right: document.querySelector('.zone.zone-back-right'),
  zone_back: document.querySelector('.zone.zone-back'),
  zone_back_left: document.querySelector('.zone.zone-back-left'),
  zone_extra_back: document.querySelector('.zone.zone-extra-back'),
};

function setZoneColor(zone, value) {
  const el = zoneElements[zone];
  if (!el) return;
  el.classList.remove('zone-red', 'zone-yellow');
  if (value === 1) {
    el.style.background = '';
  } else {
    if (zone === 'zone_extra_front' || zone === 'zone_extra_back') {
      el.classList.add('zone-yellow');
    } else {
      el.classList.add('zone-red');
    }
  }
}
// Backward button press/release handling.
const backwardBtn = document.querySelector('.nav-btn.nav-back');
if (backwardBtn) {
  // Mouse input
  backwardBtn.addEventListener('mousedown', () => {
    requestReverseActive(true);
  });
  backwardBtn.addEventListener('mouseup', () => {
    requestReverseActive(false);
  });
  backwardBtn.addEventListener('mouseleave', () => {
    requestReverseActive(false);
  });
  backwardBtn.addEventListener('mouseout', () => {
    requestReverseActive(false);
  });
  // Touch input (mobile)
  backwardBtn.addEventListener('touchstart', (e) => {
    e.preventDefault();
    requestReverseActive(true);
  });
  backwardBtn.addEventListener('touchend', (e) => {
    e.preventDefault();
    requestReverseActive(false);
  });
}
// --- Speed control logic ---
const speedLabel = document.querySelector('.nav-speed-label');
const upBtn = document.querySelector('.nav-btn.nav-up');
const downBtn = document.querySelector('.nav-btn.nav-down');
const speedSteps = [0.25, 0.5, 0.75, 1];
let speedIndex = 0;
if (speedLabel) speedLabel.textContent = (speedIndex + 1).toString();

function updateSpeedLabel() {
  if (speedLabel) speedLabel.textContent = (speedIndex + 1).toString();
}

if (upBtn) {
  upBtn.addEventListener('click', () => {
    if (speedIndex < speedSteps.length - 1) {
      speedIndex++;
      updateSpeedLabel();
    }
  });
}
if (downBtn) {
  downBtn.addEventListener('click', () => {
    if (speedIndex > 0) {
      speedIndex--;
      updateSpeedLabel();
    }
  });
}


// Central state object
const state = {
  driveMode: false,
  throttle: 0,
  throttleLimit: 0,
  steering: 0,
  steeringTrim: 0,
  cameraAngle: 0,
  forwardActive: false,
  reverseActive: false,
  emergencyStop: false
};

function emitStateNow() {
  if (socket && socket.connected) {
    socket.emit('state_update', state);
  }
}

function requestForwardActive(active) {
  state.emergencyStop = false;
  state.forwardActive = active;
  if (active) state.reverseActive = false;
  emitStateNow();
}

function requestReverseActive(active) {
  state.emergencyStop = false;
  state.reverseActive = active;
  if (active) state.forwardActive = false;
  emitStateNow();
}

// Expose the selected speed multiplier as a state property sent to the backend.
Object.defineProperty(state, 'speedFraction', {
  get() { return speedSteps[speedIndex]; },
  enumerable: true
});

function clamp(val, min, max) {
  return Math.max(min, Math.min(max, val));
}

function toggleDriveMode() {
  state.driveMode = !state.driveMode;
  document.body.classList.toggle("drive-mode", state.driveMode);
  if (state.driveMode) {
    document.getElementById("camera").requestPointerLock();
  } else {
    document.exitPointerLock();
    state.forwardActive = false;
    state.reverseActive = false;
  }
}

function emergencyStop() {
  state.throttle = 0;
  state.forwardActive = false;
  state.reverseActive = false;
  state.emergencyStop = true;
  emitStateNow();
  document.body.classList.add("emergency-flash");
  setTimeout(() => {
    document.body.classList.remove("emergency-flash");
    state.emergencyStop = false;
  }, 500);
}

const emergencyBtn = document.getElementById("emergencyStopBtn");
if (emergencyBtn) {
  emergencyBtn.addEventListener("click", emergencyStop);
}

document.addEventListener("keydown", (e) => {
  if (e.code === "Space") emergencyStop();
  if (e.code === "KeyF") toggleDriveMode();
  if (e.code === "Escape" && state.driveMode) toggleDriveMode();
  // Additional keybinds can be added here.
});

document.addEventListener("mousemove", (e) => {
  if (!state.driveMode) return;
  state.steering += e.movementX * 0.5;
  state.steering = clamp(state.steering, -100, 100);
});

document.addEventListener("wheel", (e) => {
  if (!state.driveMode) return;
  state.throttleLimit += e.deltaY < 0 ? 5 : -5;
  state.throttleLimit = clamp(state.throttleLimit, 0, 100);
});

function updateControl() {
  const accelRate = 3;
  const decayRate = 4;
  if (state.forwardActive) {
    state.throttle += accelRate;
  } else if (state.reverseActive) {
    state.throttle -= accelRate;
  } else {
    if (state.throttle > 0) state.throttle -= decayRate;
    if (state.throttle < 0) state.throttle += decayRate;
  }
  state.throttle = clamp(state.throttle, -state.throttleLimit, state.throttleLimit);
}


// Socket.IO client setup
let socket;
function connectSocket() {
  // Connect using the same origin as the frontend (proxied to backend).
  socket = io(BACKEND_BASE, { path: '/socket.io' });
  socket.on('connect', () => {
    console.log('Connected to backend');
    ensurePiMicAudioContext().catch(() => {});
  });
  socket.on('disconnect', () => {
    console.log('Disconnected from backend');
  });
    // Sensor update event handler
    socket.on('sensor_update', (data) => {
      // Update each zone color from latest backend state.
      for (const zone in zoneElements) {
        if (zone in data) {
          setZoneColor(zone, data[zone]);
        }
      }
    });
    socket.on('pi_mic_pcm', (payload) => {
      playPiMicChunk(payload);
    });
}

connectSocket();

document.addEventListener('pointerdown', () => {
  ensurePiMicAudioContext().catch(() => {});
}, { once: true });

setInterval(() => {
  updateControl();
  if (socket && socket.connected) {
    socket.emit('state_update', state);
  }
}, 20);


// Forward button press/release handling.
const forwardBtn = document.querySelector('.nav-btn.nav-fwd');
if (forwardBtn) {
  // Mouse input
  forwardBtn.addEventListener('mousedown', () => {
    requestForwardActive(true);
  });
  forwardBtn.addEventListener('mouseup', () => {
    requestForwardActive(false);
  });
  forwardBtn.addEventListener('mouseleave', () => {
    requestForwardActive(false);
  });
  forwardBtn.addEventListener('mouseout', () => {
    requestForwardActive(false);
  });
  // Touch input (mobile)
  forwardBtn.addEventListener('touchstart', (e) => {
    e.preventDefault();
    requestForwardActive(true);
  });
  forwardBtn.addEventListener('touchend', (e) => {
    e.preventDefault();
    requestForwardActive(false);
  });
}


