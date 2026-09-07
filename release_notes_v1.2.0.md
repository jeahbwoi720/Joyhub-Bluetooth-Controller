## 🧠 What's New in v1.2.0

### 🚀 Real-Time Neural AI Pose & Semantic Act Recognition (DirectML + RTX 5060)
* **DirectML Hardware Acceleration**: Runs YOLOv8-Pose over DirectX 12 DirectML on modern NVIDIA RTX / AMD GPUs at **3.2 ms per frame (> 300 FPS clean GPU)**!
* **17 COCO Keypoint Intimacy Tracking**: Analyzes distance vectors between actor anatomy (pelvis, wrists, head/nose, ankles) in real time.
* **Semantic Act Classifier**:
  * **`👅 Oral / Sucking`**: Head-to-pelvis proximity automatically commands **Hardware Suction Level 2** and cleanly powers down when transitioning.
  * **`🔥 Deep Thrusting`**: Pelvis-to-pelvis proximity triggers intense vibration and fires **Apex Penetration Impact Kicks** at maximum stroke depth.
  * **`🖐️ Hand Stroking`**: Wrist-to-pelvis proximity tracks hand stroke cadence and oscillation rates.
  * **`✨ Sensual Teasing` & `👀 Scene Motion`**: Dynamic contextual fallbacks.
* **Camera Pan & Zoom Immunity**: Dynamically crops optical flow strictly to the **Intimacy Interaction ROI**, completely eliminating false motion triggers caused by camera movement, zooming, or browser UI.

---

### 👁️ Global Video Vision & WASAPI Audio Synchronization
* **Dense Optical Flow (Farneback)**: Analyzes frame-by-frame velocity vectors across any active window (Chrome, Edge, VLC, Windows Media Player, streaming platforms, or VR mirrors).
* **Stroke Rhythm & Frequency Tracking**: Automatically detects thrusting oscillation axes, reversal points, and displays live **Stroke Frequency (Hz and BPM)**.
* **WASAPI System Audio Loopback**: Low-latency loopback capture isolating low-end bass transients (20–250 Hz) and vocal envelopes with transient beat detection.
* **3 Multi-Modal Fusion Modes**:
  * `👁️ + 🎵 Blend` (Motion Flow + WASAPI Beat)
  * `👁️ Vision Only`
  * `🎵 Audio Only`
* **⚡ Rhythm Stroke Pulse**: Modulates motor intensity in exact phase with the detected video stroke rhythm.

---

### 🎮 Virt-a-Mate (VaM) UDP Bridge Integration
* Built-in high-speed UDP server on `127.0.0.1:8888` pairing with `JoyhubHaptics.cs`.
* **3 Control Modes**:
  * `🎮 VaM Priority`: In-game VR collisions drive the hardware directly.
  * `🔀 Mix / Max Blend`: Blends manual/pulse engine baselines with incoming in-game hits.
  * `🖐️ Manual Only`: Exclusive local slider control.
* **Live UI Mirroring**: Sliders and toggle states reflect in-game VR actions in real time.
* **Relative Displacement Filter**: Distinguishes resting contact ("holding") from active movement ("stroking").

---

### 📦 Downloads & Assets:
* `Joyhub_GUI.exe` — Standalone Desktop GUI with Neural Pose + VaM Bridge
* `Joyhub_Controller.exe` — Standalone CLI Controller with VaM Bridge
* `yolov8n-pose.onnx` — DirectML Neural Pose Model (320x320)
* `Joyhub-Bluetooth-Controller-v1.2.0-Windows.zip` — Complete portable standalone bundle
