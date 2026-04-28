import asyncio
import websockets
import threading as th
import threading
import os
import sys
import logging
import subprocess
import shutil
import re
import numpy as np
import time
import json
from flask import Flask, render_template, request
from flask_socketio import SocketIO

try:
    import RPi.GPIO as GPIO
    ON_PI = True
except ImportError:
    ON_PI = False

app = Flask(__name__)
# Flask 3.x: forked Flask sessions are incompatible with default SocketIO session handling.
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    manage_session=False,
)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

volume_level = 1.0  # Default volume (0.0 to 1.0)
COMMAND_TIMEOUT_S = float(os.getenv("COMMAND_TIMEOUT_S", "0.5"))
# If 1, disconnect latches emergencyStop (old behavior). Default 0: only cut throttle/FWD/REV so Wi‑Fi blips do not lock the car until manual reset.
FAILSAFE_LATCH_ON_DISCONNECT = os.getenv("FAILSAFE_LATCH_ON_DISCONNECT", "0") == "1"
DIRECTION_NEUTRAL_GAP_S = float(os.getenv("DIRECTION_NEUTRAL_GAP_S", "0.2"))
MIC_SAMPLE_RATE = int(os.getenv("MIC_SAMPLE_RATE", "48000"))
MIC_ALSA_DEVICE = os.getenv("MIC_ALSA_DEVICE", "hw:2,0")
MIC_OUTPUT_CHANNELS = int(os.getenv("MIC_OUTPUT_CHANNELS", "2"))
MIC_GAIN = float(os.getenv("MIC_GAIN", "6.0"))
PI_MIC_ENABLE = os.getenv("PI_MIC_ENABLE", "1") == "1"
PI_MIC_ALSA_DEVICE = os.getenv("PI_MIC_ALSA_DEVICE", "hw:2,0")
PI_MIC_SAMPLE_RATE = int(os.getenv("PI_MIC_SAMPLE_RATE", "48000"))
PI_MIC_CHANNELS = int(os.getenv("PI_MIC_CHANNELS", "1"))
PI_MIC_GAIN = float(os.getenv("PI_MIC_GAIN", "2.0"))
mic_audio = {
    "proc": None,
    "rate": None,
    "channels": None,
    "device": None,
}


def _probe_alsa_capture_devices():
    """Discover ALSA capture devices to improve arecord compatibility."""
    devices = []
    seen = set()

    # Highest-priority user-configured device first.
    for base in [PI_MIC_ALSA_DEVICE, "default"]:
        if base and base not in seen:
            devices.append(base)
            seen.add(base)
        if base.startswith("hw:"):
            plug = "plughw:" + base.split(":", 1)[1]
            if plug not in seen:
                devices.append(plug)
                seen.add(plug)

    # Parse hardware capture cards from arecord -l (e.g. card 1, device 0 -> hw:1,0).
    try:
        out = subprocess.check_output(["arecord", "-l"], stderr=subprocess.STDOUT, text=True)
        for card, dev in re.findall(r"card\s+(\d+):.*?device\s+(\d+):", out):
            for d in (f"plughw:{card},{dev}", f"hw:{card},{dev}"):
                if d not in seen:
                    devices.append(d)
                    seen.add(d)
    except Exception:
        pass

    # Add named ALSA PCMs from arecord -L.
    try:
        out = subprocess.check_output(["arecord", "-L"], stderr=subprocess.STDOUT, text=True)
        for line in out.splitlines():
            name = line.strip()
            if not name or name.startswith(" "):
                continue
            if name in {"null"}:
                continue
            if name not in seen:
                devices.append(name)
                seen.add(name)
    except Exception:
        pass

    return devices


def run_pi_mic_capture():
    if not PI_MIC_ENABLE:
        return
    if shutil.which("arecord") is None:
        print("Pi mic capture disabled: arecord not found (install alsa-utils)")
        return
    candidate_devices = _probe_alsa_capture_devices()
    candidate_channels = [PI_MIC_CHANNELS, 1, 2]
    candidate_rates = [PI_MIC_SAMPLE_RATE, 48000, 44100, 16000]
    last_probe_refresh = 0.0

    while True:
        proc = None
        try:
            now = time.time()
            # Refresh periodically in case USB mic appears later.
            if (now - last_probe_refresh) > 10:
                candidate_devices = _probe_alsa_capture_devices()
                last_probe_refresh = now

            started = False
            for device in dict.fromkeys(candidate_devices):
                for channels in dict.fromkeys(candidate_channels):
                    for rate in dict.fromkeys(candidate_rates):
                        cmd = [
                            "arecord",
                            "-q",
                            "-D",
                            device,
                            "-f",
                            "S16_LE",
                            "-r",
                            str(rate),
                            "-c",
                            str(channels),
                            "-t",
                            "raw",
                        ]
                        proc = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                        )
                        # If capture can't start on this config, process exits immediately.
                        time.sleep(0.15)
                        if proc.poll() is not None:
                            continue
                        print(f"Pi mic capture using ALSA {device} at {rate}Hz, {channels}ch")
                        while True:
                            chunk = proc.stdout.read(4096) if proc.stdout else b""
                            if not chunk:
                                break
                            pcm = np.frombuffer(chunk, dtype=np.int16)
                            if pcm.size == 0:
                                continue
                            scaled = np.clip(pcm.astype(np.float32) * PI_MIC_GAIN, -32768, 32767).astype(np.int16)
                            socketio.emit("pi_mic_pcm", scaled.tobytes())
                        started = True
                        break
                    if started:
                        break
                if started:
                    break
            if not started:
                print("Pi mic capture could not open any ALSA input config "
                      f"(tried {len(candidate_devices)} device names)")
        except Exception as e:
            print(f"Pi mic capture error: {e}")
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
        time.sleep(1.0)

def set_volume(level):
    global volume_level
    volume_level = max(0.0, min(1.0, float(level)))


def ensure_mic_output_stream():
    proc = mic_audio["proc"]
    if proc is not None and proc.poll() is None and proc.stdin is not None:
        return proc.stdin
    if shutil.which("aplay") is None:
        raise RuntimeError("aplay not found; install alsa-utils")
    base_device = MIC_ALSA_DEVICE
    candidate_devices = [base_device]
    if base_device.startswith("hw:"):
        candidate_devices.insert(0, "plughw:" + base_device.split(":", 1)[1])
    candidate_channels = [MIC_OUTPUT_CHANNELS, 2, 1]
    candidate_rates = [MIC_SAMPLE_RATE, 48000, 44100]

    for device in dict.fromkeys(candidate_devices):
        for channels in dict.fromkeys(candidate_channels):
            for rate in dict.fromkeys(candidate_rates):
                cmd = [
                    "aplay",
                    "-q",
                    "-D",
                    device,
                    "-f",
                    "S16_LE",
                    "-r",
                    str(rate),
                    "-c",
                    str(channels),
                    "-t",
                    "raw",
                ]
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    if proc.stdin is None:
                        proc.terminate()
                        continue
                    mic_audio["proc"] = proc
                    mic_audio["rate"] = rate
                    mic_audio["channels"] = channels
                    mic_audio["device"] = device
                    print(f"Mic relay using ALSA {device} at {rate}Hz, {channels}ch")
                    return proc.stdin
                except Exception:
                    continue
    raise RuntimeError("Unable to open any ALSA playback config for mic relay")


@socketio.on('mic_pcm')
def handle_mic_pcm(payload):
    try:
        stream_stdin = ensure_mic_output_stream()
        if isinstance(payload, (bytes, bytearray)):
            raw = payload
        else:
            raw = bytes(payload)
        audio = np.frombuffer(raw, dtype=np.int16)
        if audio.size == 0:
            return
        scaled = audio.astype(np.float32) * volume_level * MIC_GAIN
        audio = np.clip(scaled, -32768, 32767).astype(np.int16)
        output_channels = mic_audio.get("channels") or MIC_OUTPUT_CHANNELS
        if output_channels == 2:
            # Upmix browser mono mic to stereo for USB devices requiring 2 channels.
            audio_out = np.repeat(audio[:, np.newaxis], 2, axis=1).reshape(-1)
        else:
            audio_out = audio
        stream_stdin.write(audio_out.tobytes())
        stream_stdin.flush()
    except BrokenPipeError:
        mic_audio["proc"] = None
    except Exception as e:
        print(f"Mic socketio relay error: {e}")


async def mic_ws_handler(websocket, *args):
    p = pyaudio.PyAudio()
    output_index = get_usb_output_device_index(p)
    output_rate = MIC_SAMPLE_RATE
    packet_count = 0
    print("Mic WS client connected")
    if output_index is not None:
        info = p.get_device_info_by_index(output_index)
        print(f"Mic relay using USB output: {info.get('name')}")
        default_rate = int(info.get("defaultSampleRate", MIC_SAMPLE_RATE) or MIC_SAMPLE_RATE)
        output_rate = int(os.getenv("MIC_SAMPLE_RATE", str(default_rate)))
    else:
        print("Mic relay using default output device (no USB match found)")
    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=output_rate,
            output=True,
            output_device_index=output_index,
        )
    except Exception as e:
        print(f"Mic output open failed at {output_rate}Hz, retrying default: {e}")
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=48000,
            output=True,
        )
    try:
        async for message in websocket:
            # message is raw PCM16 little-endian mono from browser.
            audio = np.frombuffer(message, dtype=np.int16)
            audio = (audio * volume_level).astype(np.int16)
            packet_count += 1
            if packet_count % 50 == 0:
                level = int(np.abs(audio).mean()) if audio.size else 0
                print(f"Mic relay packets={packet_count} avg_level={level}")
            stream.write(audio.tobytes())
    except Exception as e:
        print(f"Mic WS error: {e}")
    finally:
        print("Mic WS client disconnected")
        stream.stop_stream()
        stream.close()
        p.terminate()

MIC_WS_PORT = int(os.getenv("MIC_WS_PORT", "8765"))


def run_mic_ws():
    async def mic_ws_main():
        async with websockets.serve(
            mic_ws_handler,
            "0.0.0.0",
            MIC_WS_PORT,
            ping_interval=None,
            ping_timeout=None,
        ):
            await asyncio.Future()

    try:
        asyncio.run(mic_ws_main())
    except Exception as e:
        print(f"Mic WS server failed: {e}")

# Legacy dedicated mic websocket server (8765) retained for fallback debugging.
# Primary mic transport now uses Socket.IO event: 'mic_pcm'.

@socketio.on('volume_update')
def handle_volume_update(data):
    set_volume(data.get('volume', 1.0))


## --- SENSOR_ZONES (SENSOR_PINS): 8 inputs, BCM numbering, 40-pin header ---
## Name               | BCM | Physical | Direction | Description
## -------------------+-----+----------+-----------+---------------------------
## zone_front         | 18  |    12    | Input     | IR obstacle — front
## zone_front_right   | 23  |    16    | Input     | IR obstacle — front-right
## zone_back_right    | 24  |    18    | Input     | IR obstacle — back-right
## zone_back          | 12  |    32    | Input     | IR obstacle — back
## zone_back_left     | 16  |    36    | Input     | IR obstacle — back-left
## zone_front_left    | 20  |    38    | Input     | IR obstacle — front-left
## zone_extra_front   | 27  |    13    | Input     | IR obstacle — extra front
## zone_extra_back    | 17  |    11    | Input     | IR obstacle — extra back
## (BCM inputs used by this project: 12,16,17,18,20,23,24,27)
##
## --- OUTPUTS (PINOUTS): must not reuse sensor BCM lines above ---
## Name                   | BCM | Physical | Direction | Description
## -----------------------+-----+----------+-----------+----------------------------------
## motor_forward          |  5  |    29    | Output    | Motor drive PWM / forward line
## forward_backward_switch|  6  |    31    | Output    | HIGH = forward intent, LOW = reverse/idle
## steering_pwm           | 26  |    37    | Output    | Steering servo PWM (~50 Hz)
## camera_pan_pwm         | 19  |    35    | Output    | Camera pan servo PWM
## backward_signal        | 22  |    15    | Output    | HIGH only when reverse active
## light_toggle           |  4  |     7    | Output    | HIGH when light enabled
PINOUTS = {
    'motor_forward': 5,    # BCM 5, physical 29
    'forward_backward_switch': 6,  # BCM 6, physical 31 (HIGH=fwd, LOW=rev/idle)
    'steering_pwm': 26,    # BCM 26, physical 37
    'camera_pan_pwm': 19,  # BCM 19, physical 35
    'backward_signal': 22,  # BCM 22, physical 15 (HIGH when reverseActive)
    'light_toggle': 4,     # BCM 4, physical 7 (HIGH when lightOn=True)
}
SENSOR_PINS = {
    'zone_front': 18,        # BCM 18, physical 12
    'zone_front_right': 23,  # BCM 23, physical 16
    'zone_back_right': 24,   # BCM 24, physical 18
    'zone_back': 12,         # BCM 12, physical 32
    'zone_back_left': 16,    # BCM 16, physical 36
    'zone_front_left': 20,   # BCM 20, physical 38
    'zone_extra_front': 27,  # BCM 27, physical 13
    'zone_extra_back': 17,   # BCM 17, physical 11
}
sensor_state = {k: 1 for k in SENSOR_PINS}
bench_seq = 0

if ON_PI:
    try:
        GPIO.setmode(GPIO.BCM)
    except RuntimeError as e:
        print(f"GPIO unavailable, disabling ON_PI mode: {e}")
        ON_PI = False

def poll_sensors():
    global ON_PI, bench_seq
    if not ON_PI:
        return
    try:
        for pin in SENSOR_PINS.values():
            GPIO.setup(pin, GPIO.IN)
    except RuntimeError as e:
        print(f"Sensor GPIO setup failed, disabling ON_PI mode: {e}")
        ON_PI = False
        return
    global sensor_state
    while True:
        updated = False
        for zone, pin in SENSOR_PINS.items():
            val = GPIO.input(pin)
            if sensor_state[zone] != val:
                sensor_state[zone] = val
                updated = True
        if updated:
            bench_seq += 1
            payload = dict(sensor_state)
            socketio.emit('sensor_update', payload)
        time.sleep(0.1)

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    print(f"[SOCKET] connect sid={sid} ip={client_ip}")
    # Send current sensor state on connect
    socketio.emit('sensor_update', dict(sensor_state))


@socketio.on('disconnect')
def handle_disconnect():
    global active_control_sid
    sid = request.sid
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    idle_ms = int((time.monotonic() - last_state_update_ts) * 1000)
    was_active = sid == active_control_sid
    print(
        f"[SOCKET] disconnect sid={sid} ip={client_ip} "
        f"was_active={was_active} active_sid={active_control_sid} idle_ms={idle_ms}"
    )
    if sid == active_control_sid:
        active_control_sid = None
        if FAILSAFE_LATCH_ON_DISCONNECT:
            force_safe_state(reason="controller disconnected")
        else:
            neutralize_motion(reason="controller disconnected")
        apply_gpio_outputs_from_state()

state = {
    "driveMode": False,
    "throttle": 0,
    "throttleLimit": 0,
    "steering": 0,
    "steeringTrim": 0,
    "cameraAngle": 0,
    "lightOn": False,
    "forwardActive": False,
    "reverseActive": False,
    "emergencyStop": False
}

STATE_FILE = "state.json"
state_lock = threading.Lock()
last_state_update_ts = time.monotonic()
last_neutral_ts = last_state_update_ts
last_nonzero_dir = 0
active_control_sid = None

motor_pwm = None
steer_pwm = None
cam_pan_pwm = None

def map_to_pwm(value):
    # value: -100 to 100
    return int(1500 + (value * 5))

def save_state():
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def neutralize_motion(reason="unknown"):
    """Cut throttle and direction only; does not latch emergencyStop (recoverable after reconnect)."""
    global last_neutral_ts, last_nonzero_dir
    with state_lock:
        state["throttle"] = 0
        state["forwardActive"] = False
        state["reverseActive"] = False
        last_neutral_ts = time.monotonic()
        last_nonzero_dir = 0
        save_state()
    print(f"[FAILSAFE] Motion neutralized ({reason})")


def force_safe_state(reason="unknown"):
    """Server-side fail-safe: neutralize motion and latch emergency stop until the client clears it."""
    global last_neutral_ts, last_nonzero_dir
    with state_lock:
        state["throttle"] = 0
        state["forwardActive"] = False
        state["reverseActive"] = False
        state["emergencyStop"] = True
        last_neutral_ts = time.monotonic()
        last_nonzero_dir = 0
        save_state()
    print(f"[FAILSAFE] Entered safe state ({reason})")


def apply_gpio_outputs_from_state():
    """Drive GPIO outputs from current state, including emergency-stop behavior."""
    global ON_PI, motor_pwm, steer_pwm, cam_pan_pwm
    if not ON_PI:
        return
    try:
        GPIO.setup(PINOUTS['motor_forward'], GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(PINOUTS['forward_backward_switch'], GPIO.OUT)
        GPIO.setup(PINOUTS['backward_signal'], GPIO.OUT)
        GPIO.setup(PINOUTS['light_toggle'], GPIO.OUT)

        if state.get("emergencyStop", False):
            is_forward = False
            is_reverse = False
            speed_fraction = 0.0
        else:
            speed_fraction = state.get('speedFraction', 1)
            is_forward = state.get('forwardActive', False)
            is_reverse = state.get('reverseActive', False)

        GPIO.output(
            PINOUTS['forward_backward_switch'],
            GPIO.HIGH if is_forward else GPIO.LOW
        )
        GPIO.output(
            PINOUTS['backward_signal'],
            GPIO.HIGH if is_reverse else GPIO.LOW
        )
        GPIO.output(
            PINOUTS['light_toggle'],
            GPIO.HIGH if state.get('lightOn', False) else GPIO.LOW
        )
        if is_forward or is_reverse:
            if motor_pwm is None:
                motor_pwm = GPIO.PWM(PINOUTS['motor_forward'], 100)
                motor_pwm.start(0)
            duty = int(100 * float(speed_fraction))
            motor_pwm.ChangeDutyCycle(duty)
        else:
            if motor_pwm is not None:
                motor_pwm.ChangeDutyCycle(0)
                motor_pwm.stop()
                motor_pwm = None
            GPIO.output(PINOUTS['motor_forward'], GPIO.LOW)

        GPIO.setup(PINOUTS['steering_pwm'], GPIO.OUT)
        steer_val = int(state.get('steering', 0))
        steer_pwm_val = 7.5 + (steer_val * (2.5 / 3))
        if steer_pwm is None:
            steer_pwm = GPIO.PWM(PINOUTS['steering_pwm'], 50)
            steer_pwm.start(7.5)
        steer_pwm.ChangeDutyCycle(steer_pwm_val)

        GPIO.setup(PINOUTS['camera_pan_pwm'], GPIO.OUT)
        cam_pan_val = int(state.get('cameraAngle', 0))
        cam_pan_pwm_val = 7.5 + (cam_pan_val * (2.5 / 3))
        if cam_pan_pwm is None:
            cam_pan_pwm = GPIO.PWM(PINOUTS['camera_pan_pwm'], 50)
            cam_pan_pwm.start(7.5)
        cam_pan_pwm.ChangeDutyCycle(cam_pan_pwm_val)
    except RuntimeError as e:
        print(f"GPIO runtime error, disabling ON_PI mode: {e}")
        ON_PI = False


def command_watchdog():
    """Fail-safe for loss-of-control and RF-dropout scenarios."""
    while True:
        timed_out = False
        with state_lock:
            now = time.monotonic()
            moving = state.get("forwardActive", False) or state.get("reverseActive", False)
            timed_out = (now - last_state_update_ts) > COMMAND_TIMEOUT_S and moving
        if timed_out:
            force_safe_state(reason="command timeout")
            apply_gpio_outputs_from_state()
        time.sleep(0.05)

@app.route("/")
def index():
    return "RC Car Backend Running"

@socketio.on('state_update')
def handle_state_update(data):
    global active_control_sid, last_state_update_ts, last_neutral_ts, last_nonzero_dir
    sid = request.sid
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if active_control_sid != sid:
        print(
            f"[CONTROL] ownership sid={sid} ip={client_ip} previous={active_control_sid}"
        )
    active_control_sid = request.sid
    now = time.monotonic()
    with state_lock:
        state.update(data)

        # Sanitize contradictory commands from client.
        if state.get('forwardActive', False) and state.get('reverseActive', False):
            state['forwardActive'] = False
            state['reverseActive'] = False

        requested_dir = 0
        if state.get('forwardActive', False):
            requested_dir = 1
        elif state.get('reverseActive', False):
            requested_dir = -1

        # Require a brief neutral gap before allowing direction reversal.
        if requested_dir == 0:
            last_neutral_ts = now
        elif last_nonzero_dir != 0 and requested_dir != last_nonzero_dir:
            if (now - last_neutral_ts) < DIRECTION_NEUTRAL_GAP_S:
                requested_dir = 0
            else:
                last_nonzero_dir = requested_dir
        else:
            last_nonzero_dir = requested_dir

        state['forwardActive'] = requested_dir == 1
        state['reverseActive'] = requested_dir == -1

        # Emergency stop should always force neutral.
        if state.get('emergencyStop', False):
            state['throttle'] = 0
            state['forwardActive'] = False
            state['reverseActive'] = False
            last_neutral_ts = now
            last_nonzero_dir = 0

        last_state_update_ts = now
        save_state()

    apply_gpio_outputs_from_state()
    # Emit sensor state on every update (for redundancy)
    socketio.emit('sensor_update', sensor_state)
    # Apply steering trim
    steering_pwm = map_to_pwm(state["steering"] + state["steeringTrim"])
    throttle_pwm = map_to_pwm(state["throttle"])


# --- Camera streaming logic ---
import io
from flask import Response
try:
    import cv2
    try:
        # Avoid flooding stderr when probing non-capture /dev/video* nodes (common on Pi + libcamera).
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass
except ImportError:
    cv2 = None
try:
    from picamera2 import Picamera2
except ImportError:
    Picamera2 = None
try:
    from libcamera import Transform
except ImportError:
    Transform = None
try:
    from PIL import Image
except ImportError:
    Image = None

CAMERA_SATURATION = float(os.getenv("CAMERA_SATURATION", "1.05"))
CAMERA_CONTRAST = float(os.getenv("CAMERA_CONTRAST", "1.03"))
CAMERA_BRIGHTNESS = float(os.getenv("CAMERA_BRIGHTNESS", "0.0"))
CAMERA_AWB_ENABLE = os.getenv("CAMERA_AWB_ENABLE", "1") == "1"
CAMERA_FLIP_180 = os.getenv("CAMERA_FLIP_180", "1") == "1"
CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
# On Pi, OpenCV index 0 is often not a real capture device (libcamera exposes many nodes). Use Picamera2 or set CAMERA_OPENCV_FALLBACK=1 after choosing the right device.
CAMERA_OPENCV_FALLBACK = os.getenv("CAMERA_OPENCV_FALLBACK", "0") == "1"
CAMERA_CAPTURE_FPS = float(os.getenv("CAMERA_CAPTURE_FPS", "20"))

camera_state_lock = threading.Lock()
camera_thread_started = False
latest_camera_jpeg = None


def encode_dummy_frame():
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    if cv2 is not None:
        ret, jpeg = cv2.imencode('.jpg', frame)
        if ret:
            return jpeg.tobytes()
    if Image is not None:
        img = Image.fromarray(frame[:, :, ::-1])  # BGR -> RGB
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        return buf.getvalue()
    return b""


def encode_frame_jpeg(frame):
    """Encode numpy frame to JPEG bytes using OpenCV, with PIL fallback."""
    if frame is None:
        return None

    if cv2 is not None:
        try:
            ret, jpeg = cv2.imencode('.jpg', frame)
            if ret:
                return jpeg.tobytes()
        except Exception:
            pass

    if Image is not None:
        try:
            arr = frame
            # Picamera2 often provides RGB/RGBA; normalize for PIL.
            if hasattr(arr, "shape") and len(arr.shape) == 3:
                ch = arr.shape[2]
                if ch == 4:
                    img = Image.fromarray(arr, mode="RGBA").convert("RGB")
                else:
                    img = Image.fromarray(arr)
            else:
                img = Image.fromarray(arr)
            buf = io.BytesIO()
            img.save(buf, format='JPEG')
            return buf.getvalue()
        except Exception:
            return None

    return None

def _camera_capture_loop():
    """Single producer loop: one camera instance feeds all HTTP clients."""
    global latest_camera_jpeg

    picam2 = None
    cap = None
    using_dummy = False

    try:
        if ON_PI and Picamera2 is not None:
            try:
                picam2 = Picamera2(CAMERA_INDEX)
                print(f"Starting Picamera2 on camera index {CAMERA_INDEX}")
                if CAMERA_FLIP_180 and Transform is not None:
                    config = picam2.create_video_configuration(
                        main={"size": (1280, 720)},
                        transform=Transform(hflip=1, vflip=1),
                    )
                else:
                    config = picam2.create_video_configuration(main={"size": (1280, 720)})
                picam2.configure(config)
                picam2.start()
                time.sleep(0.2)
                picam2.set_controls({
                    "AwbEnable": CAMERA_AWB_ENABLE,
                    "AeEnable": True,
                    "Saturation": CAMERA_SATURATION,
                    "Contrast": CAMERA_CONTRAST,
                    "Brightness": CAMERA_BRIGHTNESS,
                })
            except Exception as e:
                print(f"Picamera2 failed, falling back to OpenCV/dummy stream: {e}")
                if ON_PI and not CAMERA_OPENCV_FALLBACK:
                    print(
                        "OpenCV V4L fallback skipped on Pi (noisy/wrong /dev/video*). "
                        "Fix Picamera2 or set CAMERA_OPENCV_FALLBACK=1 if using a USB cam."
                    )
                picam2 = None

        if picam2 is None:
            use_opencv = cv2 is not None and ((not ON_PI) or CAMERA_OPENCV_FALLBACK)
            if use_opencv:
                idx = os.getenv("CAMERA_OPENCV_INDEX", "0")
                if idx.isdigit():
                    cap_idx = int(idx)
                else:
                    cap_idx = idx
                if sys.platform == "linux" and hasattr(cv2, "CAP_V4L2"):
                    cap = cv2.VideoCapture(cap_idx, cv2.CAP_V4L2)
                else:
                    n = int(cap_idx) if isinstance(cap_idx, int) or (isinstance(cap_idx, str) and cap_idx.isdigit()) else 0
                    cap = cv2.VideoCapture(n)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                if not cap.isOpened():
                    cap = None

        if picam2 is None and cap is None:
            using_dummy = True
            print("Camera source unavailable; serving dummy frames.")

        period_s = max(0.01, 1.0 / CAMERA_CAPTURE_FPS)
        while True:
            jpeg_bytes = None

            if picam2 is not None:
                frame = picam2.capture_array()
                if frame is not None:
                    if CAMERA_FLIP_180 and Transform is None and cv2 is not None:
                        frame = cv2.rotate(frame, cv2.ROTATE_180)
                    jpeg_bytes = encode_frame_jpeg(frame)

            elif cap is not None:
                ret, frame = cap.read()
                if ret and frame is not None:
                    if CAMERA_FLIP_180:
                        frame = cv2.rotate(frame, cv2.ROTATE_180)
                    jpeg_bytes = encode_frame_jpeg(frame)

            if jpeg_bytes is None:
                jpeg_bytes = encode_dummy_frame()
                if not using_dummy:
                    using_dummy = True
                    print("Camera frame encode failed; switching to dummy frames.")
            elif using_dummy:
                using_dummy = False
                print("Camera frames recovered from dummy fallback.")

            with camera_state_lock:
                latest_camera_jpeg = jpeg_bytes
            time.sleep(period_s)

    except Exception as e:
        print(f"Camera capture loop crashed: {e}")
    finally:
        if picam2 is not None:
            try:
                picam2.stop()
            except Exception:
                pass
        if cap is not None:
            cap.release()


def _ensure_camera_thread():
    global camera_thread_started
    with camera_state_lock:
        if camera_thread_started:
            return
        camera_thread_started = True
    t = threading.Thread(target=_camera_capture_loop, daemon=True)
    t.start()


def gen_camera():
    _ensure_camera_thread()
    while True:
        with camera_state_lock:
            jpeg_bytes = latest_camera_jpeg
        if jpeg_bytes is None:
            jpeg_bytes = encode_dummy_frame()
        if jpeg_bytes:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + jpeg_bytes + b'\r\n')
        time.sleep(0.03)

@app.route('/video_feed')
def video_feed():
    return Response(gen_camera(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == "__main__":
    if ON_PI:
        t = threading.Thread(target=poll_sensors, daemon=True)
        t.start()
        t_mic = threading.Thread(target=run_pi_mic_capture, daemon=True)
        t_mic.start()
    t_watchdog = threading.Thread(target=command_watchdog, daemon=True)
    t_watchdog.start()
    socketio.run(app, host="0.0.0.0", port=8000, allow_unsafe_werkzeug=True)
