# RC Car Project (Senior Design)

This repository contains the software and supporting project artifacts for the Raspberry Pi based remote-control inspection car.

## Project structure

- `backend/` - Flask + Flask-SocketIO application, GPIO/PWM control, sensor polling, camera stream endpoint.
- `frontend/` - Browser HMI (HTML/CSS/JS) and Node frontend server/proxy.
- `SETUP_PI.md` - Raspberry Pi setup and run instructions.
- `ecosystem.config.js` - PM2 process configuration.
- `sensor_zone_pinout_chart.html` - Sensor pinout reference chart.

## Core features

- Browser-based remote control (forward/reverse, steering, camera pan).
- Real-time zone updates from IR obstacle sensors.
- Live camera streaming to the operator interface.
- GPIO/PWM integration for ESC and steering servo control.
- HTTPS-capable frontend service with backend proxying.

## Run (typical)

1. Install Python dependencies:
   - `pip install -r backend/requirements.txt`
2. Install Node dependencies:
   - `npm install`
3. Start with PM2 (recommended) or run backend/frontend directly.

See `SETUP_PI.md` for full Raspberry Pi setup details.

