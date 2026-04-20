# Raspberry Pi RC Car Project Setup Guide

## 1. Copy Files to Raspberry Pi
- Copy the entire project folder (including backend, frontend, requirements.txt, ecosystem.config.js, etc.) to your Raspberry Pi using a thumb drive or network transfer.

## 2. Create and Activate Python Virtual Environment (on the Pi)
cd RC_Car_Project
python3 -m venv .venv
source .venv/bin/activate

## 3. Install All Python Dependencies
pip install --upgrade pip
pip install -r backend/requirements.txt

## 4. Install Node.js Dependencies (for frontend, if needed)
cd frontend
npm install
cd ..

## 5. Start Backend and Frontend
# Start backend (from project root, with venv activated):
cd backend
python app.py
# Or use pm2 if configured:
pm2 start ../ecosystem.config.js

# Start frontend (from project root):
cd frontend
npm start

---

**Note:**
- Do NOT copy the .venv folder from Windows to the Pi. Always create the venv natively on the Pi for compatibility.
- If you need to install system-level packages (e.g., for PyAudio or GPIO), run:
sudo apt update
sudo apt install python3-dev python3-pip python3-numpy python3-opencv python3-pyaudio libasound2-dev libportaudio2 libportaudiocpp0 portaudio19-dev libatlas-base-dev libjpeg-dev libtiff5-dev libjasper-dev libpng-dev libopenjp2-7 libavcodec-dev libavformat-dev libswscale-dev libv4l-dev v4l-utils ffmpeg

- For camera support, enable the camera interface using `sudo raspi-config` if needed.

---

This guide ensures the fewest steps and maximum compatibility for running your project on the Raspberry Pi.
