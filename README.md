# Joyhub Bluetooth Device Controller (GUI & CLI) 🎮✨
**Author / Creator:** `jeahbwoi720` / `Remus`

A modern, standalone Windows Desktop GUI and CLI application for connecting to and controlling **Joyhub** Bluetooth Low Energy (BLE) interactive hardware.

---

## 🌟 Key Features

### 🖥️ Modern Desktop GUI App (`joyhub_gui.py` / `Joyhub_GUI.exe`)
* **Dark Theme Modern UI**: Clean Windows 11 dark mode interface built with CustomTkinter.
* **BLE Auto-Scanner & Memory**: Scans and lists nearby BLE devices with RSSI signal strength, remembers paired devices, and automatically connects.
* **Intelligent Auto-Reconnect**: Runs background reconnection on connection loss.
* **Vibration & Multi-Motor Control**:
  * Master Speed slider + individual Channel 1 to 4 motor sliders (0% to 100%).
  * Quick preset buttons (OFF, 25%, 50%, 75%, 100%).
* **Full Hardware Feature Controls**:
  * 🔥 **Heating Element** (On / Off)
  * 💡 **LED Light Show** (On / Off)
  * 🌀 **Suction Intensity** (Levels 0 – 5)
  * 🤏 **Squeezing / Clamping** (Levels 0 – 5)
  * 💦 **Fluid Pump** (On / Off)
* **Automated Pulse / Waveform Generator**:
  * Smooth real-time sine/pulsing wave generator with Min %, Max %, and Frequency (Hz) sliders.
  * Animated live pulsing visualizer bar.

---

### 👁️ AI Video Vision & Audio Sync (Global Browser / Video Player)
Synchronize your hardware with **ANY video playing globally on your PC** (Google Chrome, Edge, Firefox, VLC, Windows Media Player, YouTube, Twitter/X, Reddit, adult streaming platforms, or VaM VR desktop mirrors):
* **🧠 Real-Time Neural AI Pose & Semantic Act Recognition (DirectML GPU Acceleration)**:
  * **Ultra-Fast DirectML Hardware Acceleration**: Runs YOLOv8-Pose over DirectX 12 DirectML on modern GPUs at **sub-3.5 ms per frame (> 250+ FPS)**!
  * **17 COCO Keypoint Skeletal Tracking**: Tracks actor anatomy (pelvis, wrists, heads) in real time.
  * **Semantic Act Classifier**:
    * `👅 Oral / Sucking`: Head-to-pelvis proximity automatically commands Suction Level 2.
    * `🔥 Deep Thrusting`: Pelvis-to-pelvis proximity triggers intense vibration with sharp apex penetration impact pulses.
    * `🖐️ Hand Stroking`: Tracks wrist-to-pelvis strokes and oscillation frequencies.
    * `✨ Sensual Teasing`: Multi-person intimacy proximity detection.
  * **Camera Pan & Zoom Immunity**: Dynamically calculates an **Intimacy Interaction Bounding Box (ROI)** around contact keypoints. Optical flow is computed strictly within this box, completely ignoring browser UI, subtitles, and camera panning.
* **Dense Optical Flow Motion Detection (Farneback)**:
  * Analyzes frame-by-frame velocity vectors in real time (running at 30–60 FPS with < 7ms processing latency on modern multi-core systems).
  * Measures motion magnitude, active area coverage, and directional velocity.
  * **Stroke Rhythm & Frequency Tracking**: Automatically detects back-and-forth thrusting reversals, stroke apex inflection points, and calculates live **Stroke Frequency (Hz and BPM)**!
* **WASAPI Loopback Audio Reactive Analysis**:
  * Native Windows WASAPI system audio loopback capture (captures exactly what you hear in headphones/speakers).
  * FFT spectrum analysis isolating low-end bass transients (20 Hz – 250 Hz) and vocal/moan envelopes with beat transient detection.
* **3 Multi-Modal Fusion Modes**:
  * `👁️ + 🎵 Vision & Audio Blend`: Combines visual motion velocity and audio bass transients with dynamic beat accents.
  * `👁️ Vision Only (Motion Flow)`: Driven 100% by video motion and stroking speed.
  * `🎵 Audio Only (WASAPI Beat)`: Driven 100% by system audio and music beats.
* **Smart Adaptive Features**:
  * **⚡ Rhythm Stroke Pulse**: Modulates toy vibration amplitude in exact sync with detected stroke phase, letting you feel every single thrust and stroke directly!
  * **👅 Auto Oral Suction**: Automatically engages suction when oral acts are detected by neural vision.
  * **🔥 Thrust Apex Strike**: Delivers sharp haptic impact kicks right at the climax of each pelvic thrust.
  * **🌀 Auto Suction on Climax**: Automatically engages suction (Levels 1–2) during prolonged high-velocity action scenes.
  * **Flexible Target Tracking**:
    * `🌟 Auto: Active Foreground Window`: Automatically tracks Chrome, Edge, or whatever player is focused.
    * `🖥️ Full Screen`: Primary or secondary display capture.
    * `🪟 Specific Window`: Dropdown list of open browser tabs and video players with live `🔄 Refresh Windows`.

---

### 🎮 Integrated Virt-a-Mate (VaM) UDP Bridge
* Built-in high-speed UDP server listening on `127.0.0.1:8888`.
* Seamlessly receives real-time haptic collision & stroking data from VaM plugin (`JoyhubHaptics.cs`).
* **3 Flexible Control Modes**:
  * **🎮 VaM Priority**: VaM drives the hardware directly; sliders reflect live in-game haptics.
  * **🔀 Mix / Max Blend**: Blends manual slider/pulse engine baselines with incoming VaM touches (`max(VaM, Manual)`).
  * **🖐️ Manual Only**: Play exclusively with GUI sliders and waveforms while ignoring VaM packets.
* **Live Mirroring**: Watch sliders and toggles move in real-time as VR touches occur.
* **🛑 Big Red Emergency Stop**: Instantly stops all motors, heating, lighting, suction, and AI sync engines.

---

### 💻 Terminal CLI Controller (`joyhub_controller.py` / `Joyhub_Controller.exe`)
For terminal users, scripts, and headless control:
* Interactive command shell: `vibe <0-100>`, `pulse`, `heat on/off`, `light on/off`, `suck 1-5`, `squeeze 1-5`, `pump on/off`, `stop`, `raw <hex>`.

---

## 🚀 Quick Start

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 2. Run the GUI
```powershell
python joyhub_gui.py
```
*Or double-click `JoyhubGUI.bat`.*

---

## 📄 License
MIT License. Created by `jeahbwoi720` / `Remus`.
