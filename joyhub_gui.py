import asyncio
import math
import sys
import time
import json
import threading
from pathlib import Path
import customtkinter as ctk
from bleak import BleakScanner, BleakClient

# ==================== Config Persistence ====================
CONFIG_FILE = Path(__file__).parent / "joyhub_config.json"

def load_saved_device():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def save_device(address: str, name: str):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"address": address, "name": name}, f, indent=2)
    except Exception:
        pass

def clear_saved_device():
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
        except Exception:
            pass

# ==================== Command Protocol Constants ====================
CMD_HEADER = "a0"
CMD_TRAILER_AA = "aa"
CMD_TRAILER_FF = "ff"

OP_MOTOR_WAVE = "03"
OP_HEATING = "04"
OP_SUCKING = "07"
OP_SQUEEZING = "0d"
OP_LIGHTING = "14"
OP_SQUIRTING = "24"

def build_vibe_packet(speeds: list[int]) -> bytes:
    padded = (speeds + [0, 0, 0, 0])[:4]
    hex_payload = "".join(f"{max(0, min(255, s)):02x}" for s in padded)
    return bytes.fromhex(f"{CMD_HEADER}{OP_MOTOR_WAVE}{hex_payload}{CMD_TRAILER_AA}")

def build_feature_packet(op: str, state_on: bool, level: int = 1) -> bytes:
    if state_on and level > 0:
        return bytes.fromhex(f"{CMD_HEADER}{op}0100{level:02x}{CMD_TRAILER_FF}")
    return bytes.fromhex(f"{CMD_HEADER}{op}00000000")

# ==================== BLE Background Engine ====================
class BleEngine:
    def __init__(self, on_status_change, on_telemetry):
        self.on_status_change = on_status_change
        self.on_telemetry = on_telemetry
        self.client: BleakClient | None = None
        self.write_char: str | None = None
        self.notify_char: str | None = None
        self.is_connected = False
        self.target_address: str | None = None
        self.target_name: str | None = None
        self.auto_reconnect = True
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None

        self._start_loop()

    def _start_loop(self):
        def runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def run_coro(self, coro):
        if self.loop and self.loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None

    def scan_devices(self, callback):
        async def _scan():
            discovered = await BleakScanner.discover(timeout=4.0, return_adv=True)
            devices = []
            for dev, adv in discovered.values():
                name = dev.name or adv.local_name or "Unknown Device"
                devices.append({"name": name, "address": dev.address, "rssi": adv.rssi if adv else "N/A"})
            callback(devices)

        self.run_coro(_scan())

    def connect(self, address: str, name: str):
        self.target_address = address
        self.target_name = name

        async def _connect():
            self.on_status_change("Connecting...", "#E67E22")
            try:
                if self.client and self.client.is_connected:
                    await self.client.disconnect()

                self.client = BleakClient(
                    self.target_address,
                    disconnected_callback=self._on_disconnect,
                    timeout=10.0
                )
                await self.client.connect()
                if self.client.is_connected:
                    self.is_connected = True
                    await self._discover_chars()
                    self.on_status_change(f"Connected: {self.target_name}", "#2ECC71")
                    save_device(self.target_address, self.target_name)
                else:
                    self.on_status_change("Connection Failed", "#E74C3C")
            except Exception as e:
                self.on_status_change(f"Failed: {e}", "#E74C3C")

        self.run_coro(_connect())

    def disconnect(self):
        async def _disconnect():
            self.auto_reconnect = False
            if self.client and self.client.is_connected:
                try:
                    await self.client.write_gatt_char(self.write_char, build_vibe_packet([0, 0, 0, 0]), response=False)
                    await self.client.disconnect()
                except Exception:
                    pass
            self.is_connected = False
            self.on_status_change("Disconnected", "#95A5A6")

        self.run_coro(_disconnect())

    def _on_disconnect(self, client):
        self.is_connected = False
        if self.auto_reconnect:
            self.on_status_change("Reconnecting...", "#E67E22")
            async def _reconn():
                while self.auto_reconnect and not self.is_connected:
                    try:
                        await self.client.connect()
                        if self.client.is_connected:
                            self.is_connected = True
                            await self._discover_chars()
                            self.on_status_change(f"Connected: {self.target_name}", "#2ECC71")
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(3.0)
            self.run_coro(_reconn())
        else:
            self.on_status_change("Disconnected", "#95A5A6")

    async def _discover_chars(self):
        self.write_char = None
        self.notify_char = None
        if not self.client:
            return
        for service in self.client.services:
            for char in service.characteristics:
                props = char.properties
                if "write" in props or "write-without-response" in props:
                    if self.write_char is None:
                        self.write_char = char.uuid
                if "notify" in props or "indicate" in props:
                    if self.notify_char is None:
                        self.notify_char = char.uuid

        if not self.write_char:
            all_chars = [c.uuid for s in self.client.services for c in s.characteristics]
            self.write_char = all_chars[0] if all_chars else None

        if self.notify_char:
            try:
                await self.client.start_notify(self.notify_char, self._notify_handler)
            except Exception:
                pass

    def _notify_handler(self, sender, data: bytearray):
        hex_str = data.hex()
        if hex_str.startswith("a021"):
            try:
                gyro = int(hex_str[-1], 16)
                self.on_telemetry(f"Gyro: {gyro}")
            except Exception:
                self.on_telemetry(hex_str)
        else:
            self.on_telemetry(hex_str)

    def write_vibe(self, speeds: list[int]):
        async def _write():
            if self.is_connected and self.client and self.write_char:
                try:
                    pkt = build_vibe_packet(speeds)
                    await self.client.write_gatt_char(self.write_char, pkt, response=False)
                except Exception:
                    pass
        self.run_coro(_write())

    def write_feature(self, op: str, state_on: bool, level: int = 1):
        async def _write():
            if self.is_connected and self.client and self.write_char:
                try:
                    pkt = build_feature_packet(op, state_on, level)
                    await self.client.write_gatt_char(self.write_char, pkt, response=False)
                except Exception:
                    pass
        self.run_coro(_write())

# ==================== GUI Application ====================
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class JoyhubApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Joyhub Bluetooth Controller")
        self.geometry("900x760")
        self.minsize(800, 700)

        self.ble = BleEngine(self._update_status, self._update_telemetry)
        self.scanned_devices = []

        # Pulse engine variables
        self.pulse_active = False
        self.pulse_min = 20
        self.pulse_max = 90
        self.pulse_freq = 1.0

        self._build_ui()
        self._check_saved_device()

        # Start pulse wave animator loop (40 ms = 25 FPS)
        self.after(40, self._pulse_tick)

    def _build_ui(self):
        # Top Header Bar
        header = ctk.CTkFrame(self, corner_radius=10, height=60)
        header.pack(fill="x", padx=16, pady=(16, 8))

        title_lbl = ctk.CTkLabel(header, text="✨ Joyhub Bluetooth Controller", font=ctk.CTkFont(size=20, weight="bold"))
        title_lbl.pack(side="left", padx=16, pady=12)

        self.status_badge = ctk.CTkLabel(
            header,
            text="Disconnected",
            text_color="#FFFFFF",
            fg_color="#95A5A6",
            corner_radius=12,
            font=ctk.CTkFont(size=12, weight="bold"),
            width=180,
            height=28
        )
        self.status_badge.pack(side="right", padx=16, pady=12)

        # Main Scrollable Grid Container
        main_scroll = ctk.CTkScrollableFrame(self, corner_radius=10)
        main_scroll.pack(fill="both", expand=True, padx=16, pady=8)

        # ---------------- Section 1: Device Connection ----------------
        conn_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        conn_frame.pack(fill="x", pady=6)

        ctk.CTkLabel(conn_frame, text="📡 Device Connection", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 4))

        self.scan_btn = ctk.CTkButton(conn_frame, text="🔍 Scan Devices", width=120, command=self._on_scan_clicked)
        self.scan_btn.grid(row=1, column=0, padx=12, pady=10)

        self.device_dropdown = ctk.CTkOptionMenu(conn_frame, values=["Click 'Scan Devices' first"], width=300)
        self.device_dropdown.grid(row=1, column=1, padx=8, pady=10)

        self.connect_btn = ctk.CTkButton(conn_frame, text="Connect", fg_color="#2ECC71", hover_color="#27AE60", width=110, command=self._on_connect_clicked)
        self.connect_btn.grid(row=1, column=2, padx=8, pady=10)

        self.disconnect_btn = ctk.CTkButton(conn_frame, text="Disconnect", fg_color="#E74C3C", hover_color="#C0392B", width=100, command=self._on_disconnect_clicked)
        self.disconnect_btn.grid(row=1, column=3, padx=12, pady=10)

        # ---------------- Section 2: Vibration & Motor Sliders ----------------
        vibe_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        vibe_frame.pack(fill="x", pady=6)

        ctk.CTkLabel(vibe_frame, text="⚡ Vibration & Multi-Motor Control", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=14, pady=(10, 4))

        # Master Slider
        m_row = ctk.CTkFrame(vibe_frame, fg_color="transparent")
        m_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(m_row, text="Master Speed:", width=120, anchor="w", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.master_slider = ctk.CTkSlider(m_row, from_=0, to=100, command=self._on_master_slider)
        self.master_slider.set(0)
        self.master_slider.pack(side="left", fill="x", expand=True, padx=10)
        self.master_lbl = ctk.CTkLabel(m_row, text="0%", width=45, font=ctk.CTkFont(weight="bold"))
        self.master_lbl.pack(side="right")

        # Quick Preset Buttons
        btn_row = ctk.CTkFrame(vibe_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=14, pady=6)
        ctk.CTkLabel(btn_row, text="Presets:", width=120, anchor="w").pack(side="left")
        for pct in [0, 25, 50, 75, 100]:
            btn = ctk.CTkButton(btn_row, text=f"{pct}%" if pct > 0 else "OFF", width=60, height=26, command=lambda p=pct: self._set_speed(p))
            btn.pack(side="left", padx=4)

        # Individual Channel Sliders
        self.ch_sliders = []
        self.ch_labels = []

        for i in range(1, 5):
            ch_row = ctk.CTkFrame(vibe_frame, fg_color="transparent")
            ch_row.pack(fill="x", padx=14, pady=2)
            ctk.CTkLabel(ch_row, text=f"Channel {i} (Motor {i}):", width=120, anchor="w").pack(side="left")
            slider = ctk.CTkSlider(ch_row, from_=0, to=100, command=lambda val, ch=i: self._on_ch_slider(ch, val))
            slider.set(0)
            slider.pack(side="left", fill="x", expand=True, padx=10)
            lbl = ctk.CTkLabel(ch_row, text="0%", width=45)
            lbl.pack(side="right")
            self.ch_sliders.append(slider)
            self.ch_labels.append(lbl)

        # ---------------- Section 3: Hardware Features (2-Column Subgrid) ----------------
        feat_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        feat_frame.pack(fill="x", pady=6)

        ctk.CTkLabel(feat_frame, text="🛠️ Hardware Features", font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 8))

        # Left Column Features
        self.heat_switch = ctk.CTkSwitch(feat_frame, text="🔥 Heating Element", font=ctk.CTkFont(size=13), command=self._on_heat_toggle)
        self.heat_switch.grid(row=1, column=0, sticky="w", padx=20, pady=8)

        self.light_switch = ctk.CTkSwitch(feat_frame, text="💡 LED Light Show", font=ctk.CTkFont(size=13), command=self._on_light_toggle)
        self.light_switch.grid(row=2, column=0, sticky="w", padx=20, pady=8)

        self.pump_switch = ctk.CTkSwitch(feat_frame, text="💦 Fluid Pump", font=ctk.CTkFont(size=13), command=self._on_pump_toggle)
        self.pump_switch.grid(row=3, column=0, sticky="w", padx=20, pady=8)

        # Right Column Features (Suction & Squeeze Sliders)
        suck_box = ctk.CTkFrame(feat_frame, fg_color="transparent")
        suck_box.grid(row=1, column=1, sticky="ew", padx=20, pady=4)
        ctk.CTkLabel(suck_box, text="🌀 Suction (0-5):", width=120, anchor="w").pack(side="left")
        self.suck_slider = ctk.CTkSlider(suck_box, from_=0, to=5, number_of_steps=5, width=150, command=self._on_suck_change)
        self.suck_slider.set(0)
        self.suck_slider.pack(side="left", padx=6)
        self.suck_lbl = ctk.CTkLabel(suck_box, text="Off", width=35)
        self.suck_lbl.pack(side="left")

        squeeze_box = ctk.CTkFrame(feat_frame, fg_color="transparent")
        squeeze_box.grid(row=2, column=1, sticky="ew", padx=20, pady=4)
        ctk.CTkLabel(squeeze_box, text="🤏 Squeezing (0-5):", width=120, anchor="w").pack(side="left")
        self.squeeze_slider = ctk.CTkSlider(squeeze_box, from_=0, to=5, number_of_steps=5, width=150, command=self._on_squeeze_change)
        self.squeeze_slider.set(0)
        self.squeeze_slider.pack(side="left", padx=6)
        self.squeeze_lbl = ctk.CTkLabel(squeeze_box, text="Off", width=35)
        self.squeeze_lbl.pack(side="left")

        feat_frame.columnconfigure(0, weight=1)
        feat_frame.columnconfigure(1, weight=1)

        # ---------------- Section 4: Automated Pulse & Waveform Mode ----------------
        pulse_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        pulse_frame.pack(fill="x", pady=6)

        ctk.CTkLabel(pulse_frame, text="🌊 Automated Pulse & Waveform Generator", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=14, pady=(10, 4))

        p_row1 = ctk.CTkFrame(pulse_frame, fg_color="transparent")
        p_row1.pack(fill="x", padx=14, pady=4)
        self.pulse_switch = ctk.CTkSwitch(p_row1, text="Enable Continuous Pulse Wave", font=ctk.CTkFont(weight="bold"), command=self._on_pulse_toggle)
        self.pulse_switch.pack(side="left")

        self.pulse_progress = ctk.CTkProgressBar(p_row1, width=250)
        self.pulse_progress.set(0)
        self.pulse_progress.pack(side="right", padx=10)

        # Pulse Controls
        p_row2 = ctk.CTkFrame(pulse_frame, fg_color="transparent")
        p_row2.pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(p_row2, text="Min %:", width=50).pack(side="left")
        self.p_min_slider = ctk.CTkSlider(p_row2, from_=0, to=100, width=100, command=lambda v: setattr(self, "pulse_min", int(v)))
        self.p_min_slider.set(20)
        self.p_min_slider.pack(side="left", padx=4)

        ctk.CTkLabel(p_row2, text="Max %:", width=50).pack(side="left", padx=(10, 0))
        self.p_max_slider = ctk.CTkSlider(p_row2, from_=0, to=100, width=100, command=lambda v: setattr(self, "pulse_max", int(v)))
        self.p_max_slider.set(90)
        self.p_max_slider.pack(side="left", padx=4)

        ctk.CTkLabel(p_row2, text="Speed (Hz):", width=70).pack(side="left", padx=(10, 0))
        self.p_freq_slider = ctk.CTkSlider(p_row2, from_=0.2, to=3.0, width=100, command=lambda v: setattr(self, "pulse_freq", float(v)))
        self.p_freq_slider.set(1.0)
        self.p_freq_slider.pack(side="left", padx=4)

        # ---------------- Section 5: Emergency Stop & Telemetry ----------------
        bottom_frame = ctk.CTkFrame(self, corner_radius=10, height=60)
        bottom_frame.pack(fill="x", padx=16, pady=(8, 16))

        self.stop_btn = ctk.CTkButton(
            bottom_frame,
            text="🛑 EMERGENCY STOP ALL",
            fg_color="#C0392B",
            hover_color="#962D22",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=38,
            command=self._emergency_stop
        )
        self.stop_btn.pack(side="left", padx=14, pady=10)

        self.telemetry_lbl = ctk.CTkLabel(bottom_frame, text="Telemetry: Standby", text_color="#BDC3C7", anchor="e")
        self.telemetry_lbl.pack(side="right", padx=16, pady=10)

    # ---------------- Event Handlers ----------------
    def _check_saved_device(self):
        saved = load_saved_device()
        if saved and "address" in saved:
            name = saved.get("name", "Saved Device")
            entry = f"{name} ({saved['address']})"
            self.device_dropdown.configure(values=[entry])
            self.device_dropdown.set(entry)
            self.scanned_devices = [{"name": name, "address": saved["address"]}]

    def _on_scan_clicked(self):
        self.status_badge.configure(text="Scanning...", fg_color="#F39C12")
        self.scan_btn.configure(state="disabled", text="Scanning...")

        def _on_done(devices):
            self.scanned_devices = devices
            self.scan_btn.configure(state="normal", text="🔍 Scan Devices")
            if devices:
                options = [f"{d['name']} ({d['address']}) [RSSI: {d['rssi']}]" for d in devices]
                self.device_dropdown.configure(values=options)
                self.device_dropdown.set(options[0])
                self.status_badge.configure(text=f"Found {len(devices)} Devices", fg_color="#3498DB")
            else:
                self.device_dropdown.configure(values=["No Devices Found"])
                self.status_badge.configure(text="No Devices", fg_color="#E74C3C")

        self.ble.scan_devices(lambda d: self.after(0, _on_done, d))

    def _on_connect_clicked(self):
        idx = self.device_dropdown.cget("values").index(self.device_dropdown.get()) if self.scanned_devices else -1
        if 0 <= idx < len(self.scanned_devices):
            dev = self.scanned_devices[idx]
            self.ble.connect(dev["address"], dev["name"])

    def _on_disconnect_clicked(self):
        self.ble.disconnect()

    def _update_status(self, text, color):
        self.after(0, lambda: self.status_badge.configure(text=text, fg_color=color))

    def _update_telemetry(self, text):
        self.after(0, lambda: self.telemetry_lbl.configure(text=f"Telemetry: {text}"))

    def _on_master_slider(self, val):
        pct = int(val)
        self.master_lbl.configure(text=f"{pct}%")
        # Sync individual channels
        for i in range(4):
            self.ch_sliders[i].set(pct)
            self.ch_labels[i].configure(text=f"{pct}%")
        self._dispatch_vibe([pct, pct, pct, pct])

    def _set_speed(self, pct):
        self.master_slider.set(pct)
        self._on_master_slider(pct)

    def _on_ch_slider(self, ch_num, val):
        pct = int(val)
        self.ch_labels[ch_num - 1].configure(text=f"{pct}%")
        speeds = [int(s.get()) for s in self.ch_sliders]
        self._dispatch_vibe(speeds)

    def _dispatch_vibe(self, pcts: list[int]):
        raw_speeds = [int(p * 255 / 100) for p in pcts]
        self.ble.write_vibe(raw_speeds)

    def _on_heat_toggle(self):
        on = self.heat_switch.get() == 1
        self.ble.write_feature(OP_HEATING, on)

    def _on_light_toggle(self):
        on = self.light_switch.get() == 1
        self.ble.write_feature(OP_LIGHTING, on)

    def _on_pump_toggle(self):
        on = self.pump_switch.get() == 1
        self.ble.write_feature(OP_SQUIRTING, on)

    def _on_suck_change(self, val):
        lvl = int(val)
        self.suck_lbl.configure(text=f"Lvl {lvl}" if lvl > 0 else "Off")
        self.ble.write_feature(OP_SUCKING, lvl > 0, lvl)

    def _on_squeeze_change(self, val):
        lvl = int(val)
        self.squeeze_lbl.configure(text=f"Lvl {lvl}" if lvl > 0 else "Off")
        self.ble.write_feature(OP_SQUEEZING, lvl > 0, lvl)

    def _on_pulse_toggle(self):
        self.pulse_active = self.pulse_switch.get() == 1
        if not self.pulse_active:
            self.pulse_progress.set(0)
            self._set_speed(0)

    def _pulse_tick(self):
        if self.pulse_active and self.ble.is_connected:
            min_v = self.pulse_min
            max_v = self.pulse_max
            freq = self.pulse_freq
            wave = (math.sin(time.time() * freq * math.pi * 2.0) + 1.0) * 0.5
            current_pct = int(min_v + (max_v - min_v) * wave)

            self.pulse_progress.set(wave)
            self._dispatch_vibe([current_pct, current_pct, 0, 0])

        self.after(40, self._pulse_tick)

    def _emergency_stop(self):
        self.pulse_switch.deselect()
        self.pulse_active = False
        self.pulse_progress.set(0)
        self.heat_switch.deselect()
        self.light_switch.deselect()
        self.pump_switch.deselect()
        self.suck_slider.set(0)
        self.suck_lbl.configure(text="Off")
        self.squeeze_slider.set(0)
        self.squeeze_lbl.configure(text="Off")

        self.master_slider.set(0)
        self.master_lbl.configure(text="0%")
        for i in range(4):
            self.ch_sliders[i].set(0)
            self.ch_labels[i].configure(text="0%")

        self.ble.write_vibe([0, 0, 0, 0])
        self.ble.write_feature(OP_HEATING, False)
        self.ble.write_feature(OP_LIGHTING, False)
        self.ble.write_feature(OP_SUCKING, False)
        self.ble.write_feature(OP_SQUEEZING, False)
        self.ble.write_feature(OP_SQUIRTING, False)

if __name__ == "__main__":
    app = JoyhubApp()
    app.mainloop()
