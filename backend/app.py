import asyncio
import websockets
import threading as th
import threading
import os
import logging
import subprocess
import shutil
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
logging.getLogger("werkzeug").setLevel(logging.ERROR)

volume_level = 1.0  # Default volume (0.0 to 1.0)
COMMAND_TIMEOUT_S = float(os.getenv("COMMAND_TIMEOUT_S", "0.5"))
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
PI_MIC_RETRY_LOG_INTERVAL_S = float(os.getenv("PI_MIC_RETRY_LOG_INTERVAL_S", "15.0"))
mic_audio = {
    "proc": None,
    "rate": None,
    "channels": None,
    "device": None,
}
_last_pi_mic_open_fail_log_ts = 0.0


def run_pi_mic_capture():
    global _last_pi_mic_open_fail_log_ts
    if not PI_MIC_ENABLE:
        return
    if shutil.which("arecord") is None:
        print("Pi mic capture disabled: arecord not found (install alsa-utils)")
        return
    candidate_devices = [PI_MIC_ALSA_DEVICE, "default"]
    if PI_MIC_ALSA_DEVICE.startswith("hw:"):
        candidate_devices.insert(0, "plughw:" + PI_MIC_ALSA_DEVICE.split(":", 1)[1])
    candidate_channels = [PI_MIC_CHANNELS, 1, 2]
    candidate_rates = [PI_MIC_SAMPLE_RATE, 48000, 44100, 16000]

    while True:
        proc = None
        try:
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
                now = time.monotonic()
                if now - _last_pi_mic_open_fail_log_ts >= PI_MIC_RETRY_LOG_INTERVAL_S:
                    print("Pi mic capture could not open any ALSA input config")
                    _last_pi_mic_open_fail_log_ts = now
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
## zone_back_right    | 25  |    22    | Input     | IR obstacle — back-right
## zone_back          | 12  |    32    | Input     | IR obstacle — back
## zone_back_left     | 16  |    36    | Input     | IR obstacle — back-left
## zone_front_left    | 20  |    38    | Input     | IR obstacle — front-left
## zone_extra_front   | 21  |    40    | Input     | IR obstacle — extra front
## zone_extra_back    | 24  |    18    | Input     | IR obstacle — extra back
## (BCM inputs used by this project: 12,16,18,20,21,23,24,25)
##
## --- OUTPUTS (PINOUTS): must not reuse sensor BCM lines above ---
## Name                   | BCM | Physical | Direction | Description
## -----------------------+-----+----------+-----------+----------------------------------
## motor_forward          |  5  |    29    | Output    | Motor drive PWM / forward line
## forward_backward_switch|  6  |    31    | Output    | HIGH = forward intent, LOW = reverse/idle
## steering_pwm           | 26  |    37    | Output    | Steering servo PWM (~50 Hz)
## camera_pan_pwm         | 19  |    35    | Output    | Camera pan servo PWM
## backward_signal        | 22  |    15    | Output    | HIGH only when reverse active
PINOUTS = {
    'motor_forward': 5,    # BCM 5, physical 29
    'forward_backward_switch': 6,  # BCM 6, physical 31 (HIGH=fwd, LOW=rev/idle)
    'steering_pwm': 26,    # BCM 26, physical 37
    'camera_pan_pwm': 19,  # BCM 19, physical 35
    'backward_signal': 22,  # BCM 22, physical 15 (HIGH when reverseActive)
}
SENSOR_PINS = {
    'zone_front': 18,        # BCM 18, physical 12
    'zone_front_right': 23,  # BCM 23, physical 16
    'zone_back_right': 25,   # BCM 25, physical 22
    'zone_back': 12,         # BCM 12, physical 32
    'zone_back_left': 16,    # BCM 16, physical 36
    'zone_front_left': 20,   # BCM 20, physical 38
    'zone_extra_front': 21,  # BCM 21, physical 40
    'zone_extra_back': 24,   # BCM 24, physical 18
}
sensor_state = {k: 1 for k in SENSOR_PINS}

if ON_PI:
    try:
        GPIO.setmode(GPIO.BCM)
    except RuntimeError as e:
        print(f"GPIO unavailable, disabling ON_PI mode: {e}")
        ON_PI = False

def poll_sensors():
    global ON_PI
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
            socketio.emit('sensor_update', dict(sensor_state))
        time.sleep(0.1)

@socketio.on('connect')
def handle_connect():
    # Send current sensor state on connect
    socketio.emit('sensor_update', dict(sensor_state))


@socketio.on('disconnect')
def handle_disconnect():
    global active_control_sid
    sid = request.sid
    if sid == active_control_sid:
        active_control_sid = None
        force_safe_state(reason="controller disconnected")
        apply_gpio_outputs_from_state()

state = {
    "driveMode": False,
    "throttle": 0,
    "throttleLimit": 0,
    "steering": 0,
    "steeringTrim": 0,
    "cameraAngle": 0,
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


def force_safe_state(reason="unknown"):
    """Server-side fail-safe: neutralize motion commands."""
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

        # Any explicit drive direction command clears a stale emergency-stop latch.
        if requested_dir != 0:
            state['emergencyStop'] = False

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
except ImportError:
    cv2 = None
try:
    from picamera2 import Picamera2
except ImportError:
    Picamera2 = None
try:
    from PIL import Image
except ImportError:
    Image = None

CAMERA_SATURATION = float(os.getenv("CAMERA_SATURATION", "1.05"))
CAMERA_CONTRAST = float(os.getenv("CAMERA_CONTRAST", "1.03"))
CAMERA_BRIGHTNESS = float(os.getenv("CAMERA_BRIGHTNESS", "0.0"))
CAMERA_AWB_ENABLE = os.getenv("CAMERA_AWB_ENABLE", "1") == "1"


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

def gen_camera():
    # Pi5-friendly order: Picamera2 -> OpenCV -> guaranteed dummy frames.
    if ON_PI and Picamera2 is not None:
        picam2 = None
        try:
            picam2 = Picamera2()
            config = picam2.create_video_configuration(main={"size": (1280, 720)})
            picam2.configure(config)
            picam2.start()
            time.sleep(0.2)
            # Apply conservative defaults to reduce blue cast indoors.
            picam2.set_controls({
                "AwbEnable": CAMERA_AWB_ENABLE,
                "AeEnable": True,
                "Saturation": CAMERA_SATURATION,
                "Contrast": CAMERA_CONTRAST,
                "Brightness": CAMERA_BRIGHTNESS,
            })
            while True:
                frame = picam2.capture_array()
                if frame is None:
                    time.sleep(0.02)
                    continue
                if cv2 is not None:
                    ret, jpeg = cv2.imencode('.jpg', frame)
                    if ret:
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
                        continue
                dummy = encode_dummy_frame()
                if dummy:
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + dummy + b'\r\n')
                time.sleep(0.03)
        except Exception as e:
            print(f"Picamera2 failed, falling back to OpenCV/dummy stream: {e}")
        finally:
            if picam2 is not None:
                try:
                    picam2.stop()
                except Exception:
                    pass

    if cv2 is not None:
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        try:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.02)
                    continue
                ret, jpeg = cv2.imencode('.jpg', frame)
                if not ret:
                    time.sleep(0.02)
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        finally:
            cap.release()

    while True:
        dummy = encode_dummy_frame()
        if dummy:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + dummy + b'\r\n')
        time.sleep(0.1)

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
