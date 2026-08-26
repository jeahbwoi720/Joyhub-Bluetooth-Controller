# Joyhub Bluetooth Device Controller (GUI & CLI) 🎮✨

A modern, standalone Windows Desktop GUI and CLI application for connecting to and controlling **Joyhub** Bluetooth Low Energy (BLE) interactive hardware.

---

## 🌟 Key Features

### 🖥️ Modern Desktop GUI App (joyhub_gui.py / Joyhub_GUI.exe)
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
* **🛑 Big Red Emergency Stop**: Instantly stops all motors, heating, lighting, and suction.

---

### 💻 Terminal CLI Controller (joyhub_controller.py / Joyhub_Controller.exe)
For terminal users, scripts, and headless control:
* Interactive command shell: ibe <0-100>, pulse, heat on/off, light on/off, suck 1-5, squeeze 1-5, pump on/off, stop, 
aw <hex>.

---

## 🚀 Quick Start

### Option 1: Standalone Windows App (No Python Required)
Download the latest pre-compiled bundle from **[GitHub Releases](https://github.com/jeahbwoi720/Joyhub-Bluetooth-Controller/releases)** and run **Joyhub_GUI.exe**.

### Option 2: Run via Python
1. Install requirements:
   `powershell
   pip install -r requirements.txt
   `
2. Run the GUI:
   `powershell
   python joyhub_gui.py
   `
   *Or double-click JoyhubGUI.bat.*

---

## 📄 License
MIT License.
