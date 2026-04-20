
import time
import RPi.GPIO as GPIO
import json

# Pin mappings for Raspberry Pi 5 (BCM numbering).
# This is a standalone legacy test script; names differ from backend/app.py.
PINOUTS = {
    'motor_forward': 5,    # BCM 5, physical 29
    'motor_reverse': 6,    # BCM 6, physical 31
    'steering_pwm': 26,    # BCM 26, physical 37
    'camera_pan_pwm': 19,  # BCM 19, physical 35
}



STATE_FILE = "state.json"

GPIO.setmode(GPIO.BCM)
# Configure output pins for this local test script.
for name, pin in PINOUTS.items():
    GPIO.setup(pin, GPIO.OUT)



    try:
        while True:
            # Example test: drive forward continuously.
            GPIO.output(PINOUTS['motor_forward'], GPIO.HIGH)
            GPIO.output(PINOUTS['motor_reverse'], GPIO.LOW)
            # Sensors are not read in this standalone output test.
            time.sleep(0.5)
    finally:
        GPIO.cleanup()

if __name__ == "__main__":
    control_loop()
