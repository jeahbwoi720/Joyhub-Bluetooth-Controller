import asyncio
import math
import sys
import time
import json
import random
import threading
from pathlib import Path
import customtkinter as ctk
from bleak import BleakScanner, BleakClient

from joyhub_vision_audio_sync import (
    VisionAudioSyncEngine,
    get_open_windows,
    get_active_foreground_window,
    HAS_SOUNDCARD
)

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

WAVEFORM_CHOICES = ["Sine 🌊", "Triangle 🔺", "Square ⏹️", "Sawtooth 📈", "Heartbeat 💓", "Smooth Noise 🎲", "Chaos Burst ⚡"]
CHANNEL_MODES = ["🌊 Wave", "⚓ Constant", "⭘ Off"]

def build_vibe_packet(speeds: list[int]) -> bytes:
    padded = (speeds + [0, 0, 0, 0])[:4]
    hex_payload = "".join(f"{max(0, min(255, s)):02x}" for s in padded)
    return bytes.fromhex(f"{CMD_HEADER}{OP_MOTOR_WAVE}{hex_payload}{CMD_TRAILER_AA}")

def build_feature_packet(op: str, state_on: bool, level: int = 1) -> bytes:
    if state_on and level > 0:
        return bytes.fromhex(f"{CMD_HEADER}{op}0100{level:02x}{CMD_TRAILER_FF}")
    return bytes.fromhex(f"{CMD_HEADER}{op}00000000")

# ==================== VaM UDP Bridge Protocol ====================
class VamUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet

    def datagram_received(self, data: bytes, addr):
        try:
            text = data.decode("utf-8", errors="ignore").strip()
            if self.on_packet:
                self.on_packet(text, addr)
        except Exception:
            pass

# ==================== BLE Background Engine ====================
class BleEngine:
    def __init__(self, on_status_change, on_telemetry, on_battery=None):
        self.on_status_change = on_status_change
        self.on_telemetry = on_telemetry
        self.on_battery = on_battery
        self.battery_char: str | None = None
        self.battery_level: int | None = None
        self._battery_task = None
        self.client: BleakClient | None = None
        self.write_char: str | None = None
        self.notify_char: str | None = None
        self.is_connected = False
        self.target_address: str | None = None
        self.target_name: str | None = None
        self.auto_reconnect = True
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.udp_transport = None

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
            if self._battery_task:
                self._battery_task.cancel()
                self._battery_task = None
            if self.client and self.client.is_connected:
                try:
                    await self.client.write_gatt_char(self.write_char, build_vibe_packet([0, 0, 0, 0]), response=False)
                    await self.client.disconnect()
                except Exception:
                    pass
            self.is_connected = False
            self.battery_level = None
            if self.on_battery:
                self.on_battery(-1)
            self.on_status_change("Disconnected", "#95A5A6")

        self.run_coro(_disconnect())

    def _on_disconnect(self, client):
        self.is_connected = False
        self.battery_level = None
        if self._battery_task:
            self._battery_task.cancel()
            self._battery_task = None
        if self.on_battery:
            self.on_battery(-1)
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
        self.battery_char = None
        if not self.client:
            return
        for service in self.client.services:
            for char in service.characteristics:
                props = char.properties
                uuid_lower = char.uuid.lower()
                if "2a19" in uuid_lower or "180f" in service.uuid.lower():
                    self.battery_char = char.uuid
                if "write" in props or "write-without-response" in props:
                    if self.write_char is None:
                        self.write_char = char.uuid
                if "notify" in props or "indicate" in props:
                    if self.notify_char is None and "2a19" not in uuid_lower:
                        self.notify_char = char.uuid

        if not self.write_char:
            all_chars = [c.uuid for s in self.client.services for c in s.characteristics]
            self.write_char = all_chars[0] if all_chars else None

        if self.notify_char:
            try:
                await self.client.start_notify(self.notify_char, self._notify_handler)
            except Exception:
                pass

        # Discover & read battery level (standard BLE 0x2A19)
        if self.battery_char:
            try:
                bat_bytes = await self.client.read_gatt_char(self.battery_char)
                if bat_bytes and len(bat_bytes) > 0:
                    pct = int(bat_bytes[0])
                    self.battery_level = pct
                    if self.on_battery:
                        self.on_battery(pct)
            except Exception:
                pass

            for s in self.client.services:
                for c in s.characteristics:
                    if c.uuid == self.battery_char and ("notify" in c.properties or "indicate" in c.properties):
                        try:
                            await self.client.start_notify(self.battery_char, self._battery_notify_handler)
                        except Exception:
                            pass

        # Start periodic battery poll loop
        if self._battery_task:
            self._battery_task.cancel()
        self._battery_task = asyncio.create_task(self._battery_poll_loop())

    def _battery_notify_handler(self, sender, data: bytearray):
        if data and len(data) > 0:
            pct = int(data[0])
            self.battery_level = pct
            if self.on_battery:
                self.on_battery(pct)

    async def _battery_poll_loop(self):
        while self.is_connected and self.client and self.client.is_connected:
            await asyncio.sleep(25.0)
            if self.battery_char and self.is_connected:
                try:
                    bat_bytes = await self.client.read_gatt_char(self.battery_char)
                    if bat_bytes and len(bat_bytes) > 0:
                        pct = int(bat_bytes[0])
                        self.battery_level = pct
                        if self.on_battery:
                            self.on_battery(pct)
                except Exception:
                    pass

    def _notify_handler(self, sender, data: bytearray):
        hex_str = data.hex()
        # Check for proprietary Joyhub / Svakom battery telemetry packets (e.g. a008... or a020...)
        if hex_str.startswith("a0") and len(data) >= 3:
            op = hex_str[2:4]
            if op in ["08", "20"]:
                try:
                    val = int(data[2])
                    if 0 <= val <= 100:
                        self.battery_level = val
                        if self.on_battery:
                            self.on_battery(val)
                except Exception:
                    pass

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

    def start_udp_bridge(self, port: int, on_packet_callback, on_bridge_status):
        async def _start_udp():
            try:
                if self.udp_transport:
                    try:
                        self.udp_transport.close()
                    except Exception:
                        pass
                    self.udp_transport = None

                loop = asyncio.get_running_loop()
                self.udp_transport, _ = await loop.create_datagram_endpoint(
                    lambda: VamUdpProtocol(on_packet_callback),
                    local_addr=("127.0.0.1", port)
                )
                on_bridge_status(True, f"Listening on 127.0.0.1:{port}")
            except Exception as ex:
                on_bridge_status(False, f"UDP Error: {ex}")

        self.run_coro(_start_udp())

    def stop_udp_bridge(self):
        async def _stop_udp():
            if self.udp_transport:
                try:
                    self.udp_transport.close()
                except Exception:
                    pass
                self.udp_transport = None
        self.run_coro(_stop_udp())

# ==================== GUI Application ====================
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class JoyhubApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Joyhub Bluetooth Controller - Pro Edition")
        self.geometry("1020x940")
        self.minsize(900, 800)

        self.ble = BleEngine(self._update_status, self._update_telemetry, self._update_battery)
        self.scanned_devices = []

        # Master Pulse engine variables
        self.pulse_active = False
        self.auto_morph_active = False
        self.last_morph_time = time.time()

        # Per-Channel Multi-Mode & Randomness Configs (Channels 1 to 4)
        self.pulse_channels = []
        for i in range(4):
            self.pulse_channels.append({
                "mode": ctk.StringVar(value="🌊 Wave" if i < 2 else "⭘ Off"),
                "min_pct": ctk.IntVar(value=20),
                "max_pct": ctk.IntVar(value=90),
                "const_pct": ctk.IntVar(value=50),
                "freq": ctk.DoubleVar(value=1.0),
                "phase": ctk.IntVar(value=0 if i % 2 == 0 else 180),
                "jitter": ctk.IntVar(value=0),
                "waveform": ctk.StringVar(value="Sine 🌊"),
                # UI references
                "mode_seg": None,
                "wave_frame": None,
                "const_frame": None,
                "wave_menu": None,
                "min_slider": None,
                "max_slider": None,
                "const_slider": None,
                "freq_slider": None,
                "phase_slider": None,
                "jitter_slider": None,
                "min_lbl": None,
                "max_lbl": None,
                "const_lbl": None,
                "freq_lbl": None,
                "phase_lbl": None,
                "jitter_lbl": None,
                "progress_bar": None,
                "speed_badge": None
            })

        # VaM UDP Bridge state
        self.vam_bridge_enabled = ctk.BooleanVar(value=True)
        self.vam_bridge_mode = ctk.StringVar(value="🎮 VaM Priority")
        self.vam_mirror_gui = ctk.BooleanVar(value=True)
        self.vam_port = 8888
        self.vam_packet_count = 0
        self.vam_last_packet_time = 0.0
        self.vam_last_vibe = [0, 0, 0, 0]
        self.current_feature_states = {"heat": False, "light": False, "suck": 0, "squeeze": 0, "pump": False}
        self._updating_from_vam = False
        self.last_dispatched_pcts = [-1, -1, -1, -1]
        self.last_dispatch_time = 0.0
        self.vam_is_active = False

        # AI Vision & Audio Video Sync Engine
        self.ai_sync_engine: VisionAudioSyncEngine | None = None
        self.ai_sync_enabled = ctk.BooleanVar(value=False)
        self.ai_fusion_mode = ctk.StringVar(value="👁️ + 🎵 Blend")
        self.ai_model_engine = ctk.StringVar(value="🧠 Neural AI Pose & Act Sync")
        self.ai_target_mode = ctk.StringVar(value="🌟 Auto: Active Foreground Window")
        self.ai_rhythm_pulse = ctk.BooleanVar(value=True)
        self.ai_feature_sync = ctk.BooleanVar(value=False)
        self.ai_auto_oral_suction = ctk.BooleanVar(value=True)
        self.ai_apex_thrust_pulse = ctk.BooleanVar(value=True)
        self.ai_audio_boost = ctk.BooleanVar(value=True)
        self.ai_channel_mode = ctk.StringVar(value="All Channels (1-4)")
        self.ai_sensitivity_vision = ctk.DoubleVar(value=1.0)
        self.ai_sensitivity_audio = ctk.DoubleVar(value=1.0)
        self.ai_min_cutoff = ctk.IntVar(value=5)
        self.ai_max_cap = ctk.IntVar(value=50)
        self.ai_smoothing = ctk.DoubleVar(value=0.35)
        self.ai_windows_cache = []

        # Thread-safe mirrored flags for background callbacks
        self._ai_sync_active = False
        self._vam_bridge_mode_val = "🎮 VaM Priority"
        self._vam_mirror_gui_val = True
        self._vam_bridge_enabled_val = True
        self._ai_latest_speeds = None
        self._ai_latest_telemetry = None
        self._ai_latest_feature = None

        self._build_ui()
        self._check_saved_device()
        self._start_vam_bridge()
        self.protocol("WM_DELETE_WINDOW", self._on_window_closing)

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
            width=140,
            height=30
        )
        self.status_badge.pack(side="right", padx=16, pady=12)

        self.battery_badge = ctk.CTkLabel(
            header,
            text="🔋 --%",
            text_color="#FFFFFF",
            fg_color="#34495E",
            corner_radius=12,
            font=ctk.CTkFont(size=12, weight="bold"),
            width=85,
            height=30
        )
        self.battery_badge.pack(side="right", padx=(0, 10), pady=12)

        # Main Scrollable Body
        main_scroll = ctk.CTkScrollableFrame(self, corner_radius=10)
        main_scroll.pack(fill="both", expand=True, padx=16, pady=6)

        # ---------------- Section 1: Device Connection ----------------
        conn_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        conn_frame.pack(fill="x", pady=6)

        ctk.CTkLabel(conn_frame, text="📡 Bluetooth Connection", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=14, pady=(10, 4))

        conn_row = ctk.CTkFrame(conn_frame, fg_color="transparent")
        conn_row.pack(fill="x", padx=14, pady=(0, 10))

        self.scan_btn = ctk.CTkButton(conn_row, text="🔍 Scan Devices", width=120, command=self._on_scan_clicked)
        self.scan_btn.pack(side="left", padx=(0, 10))

        self.device_dropdown = ctk.CTkOptionMenu(conn_row, values=["No Devices Scanned"], width=300)
        self.device_dropdown.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.connect_btn = ctk.CTkButton(conn_row, text="⚡ Connect", width=100, fg_color="#2ECC71", hover_color="#27AE60", command=self._on_connect_clicked)
        self.connect_btn.pack(side="left", padx=(0, 6))

        self.disconnect_btn = ctk.CTkButton(conn_row, text="Disconnect", width=90, fg_color="#E74C3C", hover_color="#C0392B", command=self._on_disconnect_clicked)
        self.disconnect_btn.pack(side="left")

        # ---------------- Section 2: Virt-a-Mate (VaM) UDP Bridge ----------------
        vam_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        vam_frame.pack(fill="x", pady=6)

        v_head = ctk.CTkFrame(vam_frame, fg_color="transparent")
        v_head.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(v_head, text="🎮 Virt-a-Mate (VaM) UDP Bridge", font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")

        self.vam_status_badge = ctk.CTkLabel(
            v_head,
            text="📡 Listening (Port 8888)",
            text_color="#FFFFFF",
            fg_color="#3498DB",
            corner_radius=10,
            font=ctk.CTkFont(size=11, weight="bold"),
            width=180,
            height=26
        )
        self.vam_status_badge.pack(side="right")

        # Row 1: Toggles & Controls
        v_row1 = ctk.CTkFrame(vam_frame, fg_color="transparent")
        v_row1.pack(fill="x", padx=14, pady=4)

        self.vam_enable_switch = ctk.CTkSwitch(
            v_row1,
            text="⚡ Enable VaM Bridge",
            font=ctk.CTkFont(weight="bold"),
            variable=self.vam_bridge_enabled,
            command=self._on_vam_enable_toggle
        )
        self.vam_enable_switch.pack(side="left", padx=(0, 20))

        self.vam_mirror_switch = ctk.CTkSwitch(
            v_row1,
            text="🪞 Live Mirror to Sliders",
            font=ctk.CTkFont(size=12),
            variable=self.vam_mirror_gui
        )
        self.vam_mirror_switch.pack(side="left", padx=(0, 20))

        # Row 2: Mode Selector & Stats
        v_row2 = ctk.CTkFrame(vam_frame, fg_color="transparent")
        v_row2.pack(fill="x", padx=14, pady=(4, 10))

        ctk.CTkLabel(v_row2, text="Control Mode:", width=100, anchor="w", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.vam_mode_seg = ctk.CTkSegmentedButton(
            v_row2,
            values=["🎮 VaM Priority", "🔀 Mix / Max Blend", "🖐️ Manual Only"],
            variable=self.vam_bridge_mode,
            command=self._on_vam_mode_changed
        )
        self.vam_mode_seg.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.vam_stats_lbl = ctk.CTkLabel(v_row2, text="Pkts: 0", font=ctk.CTkFont(size=11), text_color="#BDC3C7", width=120)
        self.vam_stats_lbl.pack(side="right")

        # ---------------- Section 3: AI Video Vision & Audio Sync ----------------
        ai_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        ai_frame.pack(fill="x", pady=6)

        ai_head = ctk.CTkFrame(ai_frame, fg_color="transparent")
        ai_head.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(ai_head, text="👁️ AI Video Vision & Audio Sync (Global Browser / Video Player)", font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")

        self.ai_status_badge = ctk.CTkLabel(
            ai_head,
            text="Offline",
            text_color="#FFFFFF",
            fg_color="#7F8C8D",
            corner_radius=10,
            font=ctk.CTkFont(size=11, weight="bold"),
            width=150,
            height=26
        )
        self.ai_status_badge.pack(side="right")

        # Row 1: Enable switch, Engine Selector & Fusion Mode
        ai_r1 = ctk.CTkFrame(ai_frame, fg_color="transparent")
        ai_r1.pack(fill="x", padx=14, pady=4)

        self.ai_enable_switch = ctk.CTkSwitch(
            ai_r1,
            text="⚡ Enable AI Video Sync",
            font=ctk.CTkFont(weight="bold"),
            variable=self.ai_sync_enabled,
            command=self._on_ai_sync_toggle
        )
        self.ai_enable_switch.pack(side="left", padx=(0, 12))

        self.ai_engine_seg = ctk.CTkSegmentedButton(
            ai_r1,
            values=["🧠 Neural AI Pose & Act Sync", "⚡ Fast Optical Flow"],
            variable=self.ai_model_engine,
            command=self._on_ai_engine_mode_changed
        )
        self.ai_engine_seg.pack(side="left", padx=(0, 10))

        self.ai_mode_seg = ctk.CTkSegmentedButton(
            ai_r1,
            values=["👁️ + 🎵 Blend", "👁️ Vision Only", "🎵 Audio Only"],
            variable=self.ai_fusion_mode,
            command=self._on_ai_fusion_mode_changed
        )
        self.ai_mode_seg.pack(side="left", fill="x", expand=True)

        # Row 2: Target Window Selection
        ai_r2 = ctk.CTkFrame(ai_frame, fg_color="transparent")
        ai_r2.pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(ai_r2, text="Capture Target:", width=110, anchor="w", font=ctk.CTkFont(weight="bold")).pack(side="left")

        self.ai_target_dropdown = ctk.CTkOptionMenu(
            ai_r2,
            values=["🌟 Auto: Active Foreground Window", "🖥️ Full Screen (Primary)", "🖥️ Full Screen (Display 2)"],
            variable=self.ai_target_mode,
            width=360,
            command=self._on_ai_target_changed
        )
        self.ai_target_dropdown.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.ai_refresh_btn = ctk.CTkButton(
            ai_r2,
            text="🔄 Refresh Windows",
            width=140,
            command=self._refresh_ai_window_list
        )
        self.ai_refresh_btn.pack(side="left")

        # Row 3: Live Visualizer & Meters
        ai_meters = ctk.CTkFrame(ai_frame, corner_radius=8, fg_color="#242424")
        ai_meters.pack(fill="x", padx=14, pady=6)

        # Subrow 3a: Individual Meters & Act Badges
        m_r1 = ctk.CTkFrame(ai_meters, fg_color="transparent")
        m_r1.pack(fill="x", padx=10, pady=(6, 2))

        # Vision Meter
        self.ai_vision_lbl = ctk.CTkLabel(m_r1, text="👁️ Motion: 0%", width=95, anchor="w", font=ctk.CTkFont(size=12, weight="bold"))
        self.ai_vision_lbl.pack(side="left")
        self.ai_vision_meter = ctk.CTkProgressBar(m_r1, width=110, height=12)
        self.ai_vision_meter.set(0)
        self.ai_vision_meter.pack(side="left", padx=(0, 14))

        # Audio Meter
        self.ai_audio_lbl = ctk.CTkLabel(m_r1, text="🎵 Audio: 0%", width=85, anchor="w", font=ctk.CTkFont(size=12, weight="bold"))
        self.ai_audio_lbl.pack(side="left")
        self.ai_audio_meter = ctk.CTkProgressBar(m_r1, width=110, height=12)
        self.ai_audio_meter.set(0)
        self.ai_audio_meter.pack(side="left", padx=(0, 14))

        # Stroke Rhythm Badge
        self.ai_stroke_badge = ctk.CTkLabel(
            m_r1,
            text="⚡ Rhythm: Idle",
            text_color="#FFFFFF",
            fg_color="#7F8C8D",
            corner_radius=8,
            font=ctk.CTkFont(size=11, weight="bold"),
            width=145,
            height=24
        )
        self.ai_stroke_badge.pack(side="right")

        # Semantic Act Recognition Badge
        self.ai_act_badge = ctk.CTkLabel(
            m_r1,
            text="👀 Act: Offline",
            text_color="#FFFFFF",
            fg_color="#7F8C8D",
            corner_radius=8,
            font=ctk.CTkFont(size=11, weight="bold"),
            width=180,
            height=24
        )
        self.ai_act_badge.pack(side="right", padx=(0, 8))

        # Subrow 3b: Master Composite Output & Target Info
        m_r2 = ctk.CTkFrame(ai_meters, fg_color="transparent")
        m_r2.pack(fill="x", padx=10, pady=(2, 6))

        self.ai_combined_lbl = ctk.CTkLabel(m_r2, text="⚡ Toy Output: 0%", width=130, anchor="w", font=ctk.CTkFont(size=12, weight="bold"), text_color="#2ECC71")
        self.ai_combined_lbl.pack(side="left")

        self.ai_combined_meter = ctk.CTkProgressBar(m_r2, height=14)
        self.ai_combined_meter.set(0)
        self.ai_combined_meter.pack(side="left", fill="x", expand=True, padx=(0, 14))

        self.ai_target_lbl = ctk.CTkLabel(m_r2, text="Target: Standby | 0 FPS", font=ctk.CTkFont(size=11), text_color="#BDC3C7")
        self.ai_target_lbl.pack(side="right")

        # Row 4: Tuning Sliders
        ai_r4 = ctk.CTkFrame(ai_frame, fg_color="transparent")
        ai_r4.pack(fill="x", padx=14, pady=4)

        # Vision Sensitivity
        ctk.CTkLabel(ai_r4, text="Vision Sens:", width=75, anchor="w").pack(side="left")
        self.ai_vis_sens_slider = ctk.CTkSlider(ai_r4, from_=0.2, to=3.0, width=70, command=self._on_ai_sens_vision_change)
        self.ai_vis_sens_slider.set(1.0)
        self.ai_vis_sens_slider.pack(side="left", padx=2)
        self.ai_vis_sens_lbl = ctk.CTkLabel(ai_r4, text="1.0x", width=35)
        self.ai_vis_sens_lbl.pack(side="left", padx=(0, 8))

        # Audio Sensitivity
        ctk.CTkLabel(ai_r4, text="Audio Sens:", width=75, anchor="w").pack(side="left")
        self.ai_aud_sens_slider = ctk.CTkSlider(ai_r4, from_=0.2, to=3.0, width=70, command=self._on_ai_sens_audio_change)
        self.ai_aud_sens_slider.set(1.0)
        self.ai_aud_sens_slider.pack(side="left", padx=2)
        self.ai_aud_sens_lbl = ctk.CTkLabel(ai_r4, text="1.0x", width=35)
        self.ai_aud_sens_lbl.pack(side="left", padx=(0, 8))

        # Cutoff Floor
        ctk.CTkLabel(ai_r4, text="Min Floor:", width=65, anchor="w").pack(side="left")
        self.ai_cutoff_slider = ctk.CTkSlider(ai_r4, from_=0, to=30, width=65, command=self._on_ai_min_cutoff_change)
        self.ai_cutoff_slider.set(5)
        self.ai_cutoff_slider.pack(side="left", padx=2)
        self.ai_cutoff_lbl = ctk.CTkLabel(ai_r4, text="5%", width=30)
        self.ai_cutoff_lbl.pack(side="left", padx=(0, 8))

        # Max Speed Cap
        ctk.CTkLabel(ai_r4, text="Max Cap:", width=60, anchor="w").pack(side="left")
        self.ai_max_slider = ctk.CTkSlider(ai_r4, from_=10, to=100, width=65, command=self._on_ai_max_cap_change)
        self.ai_max_slider.set(50)
        self.ai_max_slider.pack(side="left", padx=2)
        self.ai_max_lbl = ctk.CTkLabel(ai_r4, text="50%", width=35)
        self.ai_max_lbl.pack(side="left", padx=(0, 8))

        # Smoothing
        ctk.CTkLabel(ai_r4, text="Smooth:", width=55, anchor="w").pack(side="left")
        self.ai_smooth_slider = ctk.CTkSlider(ai_r4, from_=0.05, to=0.85, width=65, command=self._on_ai_smoothing_change)
        self.ai_smooth_slider.set(0.35)
        self.ai_smooth_slider.pack(side="left", padx=2)
        self.ai_smooth_lbl = ctk.CTkLabel(ai_r4, text="0.35", width=35)
        self.ai_smooth_lbl.pack(side="left")

        # Row 5: Routing & Smart Features
        ai_r5 = ctk.CTkFrame(ai_frame, fg_color="transparent")
        ai_r5.pack(fill="x", padx=14, pady=(4, 10))

        ctk.CTkLabel(ai_r5, text="Channels:", width=65, anchor="w", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.ai_ch_seg = ctk.CTkSegmentedButton(
            ai_r5,
            values=["All Channels (1-4)", "Ch 1 & 2 Only", "Ch 1 Only", "Ch 2 Only"],
            variable=self.ai_channel_mode,
            command=self._on_ai_channel_mode_changed
        )
        self.ai_ch_seg.pack(side="left", padx=(0, 12))

        self.ai_rhythm_switch = ctk.CTkSwitch(
            ai_r5,
            text="⚡ Rhythm Stroke",
            font=ctk.CTkFont(size=12, weight="bold"),
            variable=self.ai_rhythm_pulse,
            command=self._on_ai_rhythm_pulse_toggle
        )
        self.ai_rhythm_switch.pack(side="left", padx=(0, 12))

        self.ai_oral_switch = ctk.CTkSwitch(
            ai_r5,
            text="👅 Auto Oral Suction",
            font=ctk.CTkFont(size=12),
            variable=self.ai_auto_oral_suction,
            command=self._on_ai_oral_toggle
        )
        self.ai_oral_switch.pack(side="left", padx=(0, 12))

        self.ai_apex_switch = ctk.CTkSwitch(
            ai_r5,
            text="🔥 Thrust Apex Strike",
            font=ctk.CTkFont(size=12),
            variable=self.ai_apex_thrust_pulse,
            command=self._on_ai_apex_toggle
        )
        self.ai_apex_switch.pack(side="left", padx=(0, 12))

        self.ai_audio_boost_switch = ctk.CTkSwitch(
            ai_r5,
            text="💋 Moan & Impact Surge",
            font=ctk.CTkFont(size=12),
            variable=self.ai_audio_boost,
            command=self._on_ai_audio_boost_toggle
        )
        self.ai_audio_boost_switch.pack(side="left")

        # ---------------- Section 4: Vibration & Motor Controls ----------------
        vibe_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        vibe_frame.pack(fill="x", pady=6)

        ctk.CTkLabel(vibe_frame, text="🎛️ Manual Motor Controls", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w", padx=14, pady=(10, 4))

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

        # ---------------- Section 3: Hardware Features ----------------
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

        # Right Column Features
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

        # ---------------- Section 4: Automated Pulse & Random Waveform Mode ----------------
        pulse_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        pulse_frame.pack(fill="x", pady=6)

        header_p = ctk.CTkFrame(pulse_frame, fg_color="transparent")
        header_p.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(header_p, text="🌊 Automated Pulse & Random Wave Engine", font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")

        # Top Master Control Row
        p_master_row = ctk.CTkFrame(pulse_frame, fg_color="transparent")
        p_master_row.pack(fill="x", padx=14, pady=4)

        self.pulse_switch = ctk.CTkSwitch(p_master_row, text="⚡ Enable Multi-Channel Engine", font=ctk.CTkFont(weight="bold"), command=self._on_pulse_toggle)
        self.pulse_switch.pack(side="left")

        self.auto_morph_switch = ctk.CTkSwitch(p_master_row, text="🔄 Chaos Auto-Morph", font=ctk.CTkFont(weight="bold"), command=self._on_automorph_toggle)
        self.auto_morph_switch.pack(side="left", padx=20)

        self.pulse_master_progress = ctk.CTkProgressBar(p_master_row, width=200)
        self.pulse_master_progress.set(0)
        self.pulse_master_progress.pack(side="right", padx=10)

        # Quick Preset Buttons Bar
        preset_bar = ctk.CTkFrame(pulse_frame, fg_color="transparent")
        preset_bar.pack(fill="x", padx=14, pady=4)

        ctk.CTkLabel(preset_bar, text="Presets:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 6))
        ctk.CTkButton(preset_bar, text="🎲 Roll Random Setup", width=135, height=24, fg_color="#E67E22", hover_color="#D35400", command=self._random_roll_setup).pack(side="left", padx=3)
        ctk.CTkButton(preset_bar, text="All Wave 🌊", width=85, height=24, command=self._preset_all_wave).pack(side="left", padx=3)
        ctk.CTkButton(preset_bar, text="Ch 1 Wave + Ch 2 Const ⚓", width=165, height=24, fg_color="#2980B9", hover_color="#3498DB", command=self._preset_ch1wave_ch2const).pack(side="left", padx=3)
        ctk.CTkButton(preset_bar, text="All Constant ⚓", width=95, height=24, fg_color="#27AE60", hover_color="#2ECC71", command=self._preset_all_const).pack(side="left", padx=3)
        ctk.CTkButton(preset_bar, text="Alt Phase (180°)", width=110, height=24, fg_color="#8E44AD", hover_color="#9B59B6", command=self._alternating_phases).pack(side="left", padx=3)
        ctk.CTkButton(preset_bar, text="Rolling (90°)", width=95, height=24, fg_color="#16A085", hover_color="#1ABC9C", command=self._stagger_phases).pack(side="left", padx=3)
        ctk.CTkButton(preset_bar, text="All Off", width=65, height=24, fg_color="#7F8C8D", hover_color="#95A5A6", command=self._clear_pulse_channels).pack(side="left", padx=3)

        # 4 Individual Channel Control Cards
        cards_container = ctk.CTkFrame(pulse_frame, fg_color="transparent")
        cards_container.pack(fill="x", padx=10, pady=6)

        for ch_idx in range(4):
            cfg = self.pulse_channels[ch_idx]
            ch_num = ch_idx + 1

            card = ctk.CTkFrame(cards_container, corner_radius=8, fg_color="#242424")
            card.pack(fill="x", pady=5)

            # Top Card Line: Channel Title, Mode Selector (Wave / Constant / Off), Live Badge & Progress
            c_top = ctk.CTkFrame(card, fg_color="transparent")
            c_top.pack(fill="x", padx=12, pady=(6, 2))

            ctk.CTkLabel(c_top, text=f"Channel {ch_num} (Motor {ch_num})", font=ctk.CTkFont(size=13, weight="bold"), width=150, anchor="w").pack(side="left")

            cfg["mode_seg"] = ctk.CTkSegmentedButton(
                c_top,
                values=CHANNEL_MODES,
                variable=cfg["mode"],
                command=lambda val, idx=ch_idx: self._on_ch_mode_changed(idx, val)
            )
            cfg["mode_seg"].pack(side="left", padx=10)

            cfg["speed_badge"] = ctk.CTkLabel(c_top, text="OFF", font=ctk.CTkFont(weight="bold"), text_color="#BDC3C7", width=60)
            cfg["speed_badge"].pack(side="right", padx=4)

            cfg["progress_bar"] = ctk.CTkProgressBar(c_top, width=130, height=12)
            cfg["progress_bar"].set(0)
            cfg["progress_bar"].pack(side="right", padx=6)

            # ---------------- Subframe 1: Wave Mode Controls ----------------
            cfg["wave_frame"] = ctk.CTkFrame(card, fg_color="transparent")
            
            # Row 1: Shape Selector + Jitter Randomness Slider
            w_r1 = ctk.CTkFrame(cfg["wave_frame"], fg_color="transparent")
            w_r1.pack(fill="x", padx=12, pady=(2, 2))
            ctk.CTkLabel(w_r1, text="Shape:", width=45, anchor="w").pack(side="left")
            cfg["wave_menu"] = ctk.CTkOptionMenu(
                w_r1,
                values=WAVEFORM_CHOICES,
                variable=cfg["waveform"],
                width=150,
                height=24
            )
            cfg["wave_menu"].pack(side="left", padx=2)

            ctk.CTkLabel(w_r1, text="🎲 Jitter:", width=55, anchor="e").pack(side="left", padx=(15, 2))
            cfg["jitter_slider"] = ctk.CTkSlider(
                w_r1, from_=0, to=100, width=90,
                command=lambda v, idx=ch_idx: self._on_ch_jitter_change(idx, v)
            )
            cfg["jitter_slider"].set(cfg["jitter"].get())
            cfg["jitter_slider"].pack(side="left", padx=2)
            cfg["jitter_lbl"] = ctk.CTkLabel(w_r1, text=f"{cfg['jitter'].get()}%", width=36)
            cfg["jitter_lbl"].pack(side="left")

            # Row 2: Wave Sliders (Min, Max, Speed, Phase)
            w_r2 = ctk.CTkFrame(cfg["wave_frame"], fg_color="transparent")
            w_r2.pack(fill="x", padx=12, pady=(2, 6))

            # 1. Min %
            ctk.CTkLabel(w_r2, text="Min:", width=30).pack(side="left")
            cfg["min_slider"] = ctk.CTkSlider(
                w_r2, from_=0, to=100, width=75,
                command=lambda v, idx=ch_idx: self._on_ch_min_change(idx, v)
            )
            cfg["min_slider"].set(cfg["min_pct"].get())
            cfg["min_slider"].pack(side="left", padx=2)
            cfg["min_lbl"] = ctk.CTkLabel(w_r2, text=f"{cfg['min_pct'].get()}%", width=36)
            cfg["min_lbl"].pack(side="left", padx=(0, 4))

            # 2. Max %
            ctk.CTkLabel(w_r2, text="Max:", width=32).pack(side="left")
            cfg["max_slider"] = ctk.CTkSlider(
                w_r2, from_=0, to=100, width=75,
                command=lambda v, idx=ch_idx: self._on_ch_max_change(idx, v)
            )
            cfg["max_slider"].set(cfg["max_pct"].get())
            cfg["max_slider"].pack(side="left", padx=2)
            cfg["max_lbl"] = ctk.CTkLabel(w_r2, text=f"{cfg['max_pct'].get()}%", width=36)
            cfg["max_lbl"].pack(side="left", padx=(0, 4))

            # 3. Speed (Hz)
            ctk.CTkLabel(w_r2, text="Speed:", width=42).pack(side="left")
            cfg["freq_slider"] = ctk.CTkSlider(
                w_r2, from_=0.2, to=4.0, width=75,
                command=lambda v, idx=ch_idx: self._on_ch_freq_change(idx, v)
            )
            cfg["freq_slider"].set(cfg["freq"].get())
            cfg["freq_slider"].pack(side="left", padx=2)
            cfg["freq_lbl"] = ctk.CTkLabel(w_r2, text=f"{cfg['freq'].get():.1f}Hz", width=42)
            cfg["freq_lbl"].pack(side="left", padx=(0, 4))

            # 4. Phase Offset (deg)
            ctk.CTkLabel(w_r2, text="Phase:", width=40).pack(side="left")
            cfg["phase_slider"] = ctk.CTkSlider(
                w_r2, from_=0, to=360, width=75,
                command=lambda v, idx=ch_idx: self._on_ch_phase_change(idx, v)
            )
            cfg["phase_slider"].set(cfg["phase"].get())
            cfg["phase_slider"].pack(side="left", padx=2)
            cfg["phase_lbl"] = ctk.CTkLabel(w_r2, text=f"{cfg['phase'].get()}°", width=36)
            cfg["phase_lbl"].pack(side="left")

            # ---------------- Subframe 2: Constant Mode Controls ----------------
            cfg["const_frame"] = ctk.CTkFrame(card, fg_color="transparent")
            
            c_r1 = ctk.CTkFrame(cfg["const_frame"], fg_color="transparent")
            c_r1.pack(fill="x", padx=12, pady=(2, 6))

            ctk.CTkLabel(c_r1, text="Constant Speed:", width=110, anchor="w", font=ctk.CTkFont(weight="bold")).pack(side="left")
            cfg["const_slider"] = ctk.CTkSlider(
                c_r1, from_=0, to=100, width=160,
                command=lambda v, idx=ch_idx: self._on_ch_const_change(idx, v)
            )
            cfg["const_slider"].set(cfg["const_pct"].get())
            cfg["const_slider"].pack(side="left", padx=4)
            cfg["const_lbl"] = ctk.CTkLabel(c_r1, text=f"{cfg['const_pct'].get()}%", width=36, font=ctk.CTkFont(weight="bold"))
            cfg["const_lbl"].pack(side="left", padx=(0, 6))

            # Quick presets for constant mode
            for cpct in [25, 50, 75, 100]:
                btn = ctk.CTkButton(
                    c_r1, text=f"{cpct}%", width=42, height=22,
                    command=lambda p=cpct, idx=ch_idx: self._set_ch_const(idx, p)
                )
                btn.pack(side="left", padx=1)

            # Jitter on Constant Mode
            ctk.CTkLabel(c_r1, text="🎲 Jitter:", width=55, anchor="e").pack(side="left", padx=(10, 2))
            c_jit_slider = ctk.CTkSlider(
                c_r1, from_=0, to=100, width=70,
                command=lambda v, idx=ch_idx: self._on_ch_jitter_change(idx, v)
            )
            c_jit_slider.set(cfg["jitter"].get())
            c_jit_slider.pack(side="left", padx=2)

            # Initialize initial frame visibility
            self._update_ch_ui_visibility(ch_idx)

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

    # ---------------- Channel Mode Switching ----------------
    def _on_ch_mode_changed(self, idx, val):
        self._update_ch_ui_visibility(idx)

    def _update_ch_ui_visibility(self, idx):
        cfg = self.pulse_channels[idx]
        mode = cfg["mode"].get()

        if mode == "🌊 Wave":
            if cfg["const_frame"]:
                cfg["const_frame"].pack_forget()
            if cfg["wave_frame"]:
                cfg["wave_frame"].pack(fill="x", pady=(2, 4))
        elif mode == "⚓ Constant":
            if cfg["wave_frame"]:
                cfg["wave_frame"].pack_forget()
            if cfg["const_frame"]:
                cfg["const_frame"].pack(fill="x", pady=(2, 4))
        else: # "⭘ Off"
            if cfg["wave_frame"]:
                cfg["wave_frame"].pack_forget()
            if cfg["const_frame"]:
                cfg["const_frame"].pack_forget()
            if cfg["progress_bar"]:
                cfg["progress_bar"].set(0)
            if cfg["speed_badge"]:
                cfg["speed_badge"].configure(text="OFF", text_color="#7F8C8D")

    # ---------------- Slider Event Callbacks ----------------
    def _on_ch_min_change(self, idx, val):
        v = int(val)
        self.pulse_channels[idx]["min_pct"].set(v)
        self.pulse_channels[idx]["min_lbl"].configure(text=f"{v}%")

    def _on_ch_max_change(self, idx, val):
        v = int(val)
        self.pulse_channels[idx]["max_pct"].set(v)
        self.pulse_channels[idx]["max_lbl"].configure(text=f"{v}%")

    def _on_ch_jitter_change(self, idx, val):
        v = int(val)
        self.pulse_channels[idx]["jitter"].set(v)
        if self.pulse_channels[idx]["jitter_lbl"]:
            self.pulse_channels[idx]["jitter_lbl"].configure(text=f"{v}%")

    def _on_ch_const_change(self, idx, val):
        v = int(val)
        self.pulse_channels[idx]["const_pct"].set(v)
        self.pulse_channels[idx]["const_lbl"].configure(text=f"{v}%")

    def _set_ch_const(self, idx, val):
        self.pulse_channels[idx]["const_pct"].set(val)
        self.pulse_channels[idx]["const_slider"].set(val)
        self.pulse_channels[idx]["const_lbl"].configure(text=f"{val}%")

    def _on_ch_freq_change(self, idx, val):
        v = round(float(val), 1)
        self.pulse_channels[idx]["freq"].set(v)
        self.pulse_channels[idx]["freq_lbl"].configure(text=f"{v:.1f}Hz")

    def _on_ch_phase_change(self, idx, val):
        v = int(val)
        self.pulse_channels[idx]["phase"].set(v)
        self.pulse_channels[idx]["phase_lbl"].configure(text=f"{v}°")

    def _on_automorph_toggle(self):
        self.auto_morph_active = self.auto_morph_switch.get() == 1
        self.last_morph_time = time.time()

    # ---------------- Randomness & Presets ----------------
    def _random_roll_setup(self):
        """Rolls a completely unique, randomized profile across all channels."""
        for idx in range(4):
            cfg = self.pulse_channels[idx]
            mode = random.choice(["🌊 Wave", "🌊 Wave", "⚓ Constant"])
            cfg["mode"].set(mode)
            
            min_v = random.randint(0, 35)
            max_v = random.randint(max(min_v + 20, 60), 100)
            const_v = random.randint(30, 90)
            freq_v = round(random.uniform(0.5, 2.5), 1)
            phase_v = random.choice([0, 45, 90, 135, 180, 225, 270])
            jitter_v = random.choice([0, 10, 20, 35])
            wave_v = random.choice(WAVEFORM_CHOICES)

            cfg["min_pct"].set(min_v)
            cfg["min_slider"].set(min_v)
            cfg["min_lbl"].configure(text=f"{min_v}%")

            cfg["max_pct"].set(max_v)
            cfg["max_slider"].set(max_v)
            cfg["max_lbl"].configure(text=f"{max_v}%")

            cfg["const_pct"].set(const_v)
            cfg["const_slider"].set(const_v)
            cfg["const_lbl"].configure(text=f"{const_v}%")

            cfg["freq"].set(freq_v)
            cfg["freq_slider"].set(freq_v)
            cfg["freq_lbl"].configure(text=f"{freq_v:.1f}Hz")

            cfg["phase"].set(phase_v)
            cfg["phase_slider"].set(phase_v)
            cfg["phase_lbl"].configure(text=f"{phase_v}°")

            cfg["jitter"].set(jitter_v)
            cfg["jitter_slider"].set(jitter_v)
            if cfg["jitter_lbl"]:
                cfg["jitter_lbl"].configure(text=f"{jitter_v}%")

            cfg["waveform"].set(wave_v)
            cfg["wave_menu"].set(wave_v)
            self._update_ch_ui_visibility(idx)

    def _preset_all_wave(self):
        for idx in range(4):
            self.pulse_channels[idx]["mode"].set("🌊 Wave")
            self._update_ch_ui_visibility(idx)

    def _preset_ch1wave_ch2const(self):
        self.pulse_channels[0]["mode"].set("🌊 Wave")
        self.pulse_channels[1]["mode"].set("⚓ Constant")
        self.pulse_channels[2]["mode"].set("⭘ Off")
        self.pulse_channels[3]["mode"].set("⭘ Off")
        for idx in range(4):
            self._update_ch_ui_visibility(idx)

    def _preset_all_const(self):
        for idx in range(4):
            self.pulse_channels[idx]["mode"].set("⚓ Constant")
            self._update_ch_ui_visibility(idx)

    def _clear_pulse_channels(self):
        for idx in range(4):
            self.pulse_channels[idx]["mode"].set("⭘ Off")
            self._update_ch_ui_visibility(idx)

    def _alternating_phases(self):
        for idx in range(4):
            phase_v = 0 if idx % 2 == 0 else 180
            cfg = self.pulse_channels[idx]
            cfg["phase"].set(phase_v)
            cfg["phase_slider"].set(phase_v)
            cfg["phase_lbl"].configure(text=f"{phase_v}°")

    def _stagger_phases(self):
        phases = [0, 90, 180, 270]
        for idx in range(4):
            phase_v = phases[idx]
            cfg = self.pulse_channels[idx]
            cfg["phase"].set(phase_v)
            cfg["phase_slider"].set(phase_v)
            cfg["phase_lbl"].configure(text=f"{phase_v}°")

    # ---------------- Waveform Math & Engine Loop ----------------
    def _calculate_channel_wave(self, wtype: str, t: float, freq: float, phase_deg: float) -> float:
        phase = (t * freq + (phase_deg / 360.0)) % 1.0

        if "Triangle" in wtype:
            return phase * 2.0 if phase < 0.5 else (1.0 - phase) * 2.0
        elif "Square" in wtype:
            return 1.0 if phase < 0.5 else 0.0
        elif "Sawtooth" in wtype:
            return phase
        elif "Heartbeat" in wtype:
            if phase < 0.15:
                return math.sin(phase / 0.15 * math.pi)
            elif 0.20 <= phase < 0.35:
                return 0.7 * math.sin((phase - 0.20) / 0.15 * math.pi)
            else:
                return 0.0
        elif "Smooth Noise" in wtype:
            phi = phase_deg / 360.0
            w = (math.sin(2 * math.pi * (freq * t + phi)) +
                 0.5 * math.sin(2 * math.pi * (1.618033 * freq * t + 1.3 * phi)) +
                 0.25 * math.sin(2 * math.pi * (2.718281 * freq * t + 0.7 * phi))) / 1.75
            return max(0.0, min(1.0, (w + 1.0) * 0.5))
        elif "Chaos Burst" in wtype:
            phi = phase_deg / 360.0
            fast = math.sin(2 * math.pi * (freq * 1.8 * t + phi))
            slow = math.sin(2 * math.pi * (freq * 0.35 * t + 0.3 * phi))
            burst_env = max(0.0, slow + 0.35)
            wave = ((fast + 1.0) * 0.5) * min(1.0, burst_env * 1.8)
            return max(0.0, min(1.0, wave))
        else: # Sine
            return (math.sin(phase * 2.0 * math.pi) + 1.0) * 0.5

    def _on_pulse_toggle(self):
        self.pulse_active = self.pulse_switch.get() == 1
        if not self.pulse_active:
            self.pulse_master_progress.set(0)
            for cfg in self.pulse_channels:
                if cfg["progress_bar"]:
                    cfg["progress_bar"].set(0)
                if cfg["speed_badge"]:
                    cfg["speed_badge"].configure(text="OFF", text_color="#7F8C8D")
            self._set_speed(0)

    def _pulse_tick(self):
        now = time.time()

        # Process pending AI Video Sync telemetry & UI updates on the main GUI thread
        if hasattr(self, "_ai_latest_telemetry") and self._ai_latest_telemetry:
            try:
                telem = self._ai_latest_telemetry
                self._ai_latest_telemetry = None
                v_pct = telem.get("vision_pct", 0)
                a_pct = telem.get("audio_pct", 0)
                c_pct = telem.get("combined_pct", 0)
                hz = telem.get("stroke_hz", 0.0)
                fps = telem.get("fps", 0.0)
                target = telem.get("target_info", "Active")

                self.ai_vision_meter.set(v_pct / 100.0)
                self.ai_vision_lbl.configure(text=f"👁️ Motion: {v_pct}%")

                self.ai_audio_meter.set(a_pct / 100.0)
                self.ai_audio_lbl.configure(text=f"🎵 Audio: {a_pct}%")

                self.ai_combined_meter.set(c_pct / 100.0)
                self.ai_combined_lbl.configure(text=f"⚡ Toy Output: {c_pct}%")

                rhythm_src = telem.get("rhythm_source", "motion")
                audio_bpm = telem.get("audio_bpm", 0)

                if hz >= 0.5:
                    if rhythm_src == "audio":
                        display_bpm = audio_bpm if audio_bpm > 0 else int(round(hz * 60))
                        self.ai_stroke_badge.configure(text=f"🎵 {display_bpm} BPM ({hz:.1f}Hz)", fg_color="#2980B9")
                    else:
                        bpm = int(round(hz * 60))
                        self.ai_stroke_badge.configure(text=f"⚡ {hz:.1f} Hz ({bpm} BPM)", fg_color="#9B59B6")
                else:
                    self.ai_stroke_badge.configure(text="⚡ Rhythm: Idle", fg_color="#7F8C8D")

                # Update Semantic Act Badge with Rich Affect Color Palette
                act = telem.get("act_type", "👀 Scene Motion")
                act_color = "#16A085"
                if "💋" in act or "Moan" in act:
                    act_color = "#E91E63" # Hot Pink
                elif "💥" in act or "Impact" in act or "Spank" in act:
                    act_color = "#C0392B" # Crimson Red
                elif "😮‍💨" in act or "Panting" in act or "Breath" in act:
                    act_color = "#E67E22" # Warm Amber/Orange
                elif "🗣️" in act or "Dialogue" in act:
                    act_color = "#16A085" # Soft Teal
                elif "Listening" in act or "Offline" in act:
                    act_color = "#7F8C8D" # Slate Gray
                elif "🎵" in act or "Music" in act or "Beat" in act or "Audio" in act:
                    act_color = "#2980B9" # Deep Blue
                elif "Oral" in act:
                    act_color = "#E91E63"
                elif "Thrust" in act:
                    act_color = "#E67E22"
                elif "Stroke" in act:
                    act_color = "#3498DB"
                elif "Teasing" in act:
                    act_color = "#9B59B6"
                elif "Optical Flow" in act:
                    act_color = "#7F8C8D"
                self.ai_act_badge.configure(text=f"{act}", fg_color=act_color)

                self.ai_target_lbl.configure(text=f"{target} | {fps} FPS")
            except Exception:
                pass

        if hasattr(self, "_ai_latest_speeds") and self._ai_latest_speeds:
            sp = self._ai_latest_speeds
            self._ai_latest_speeds = None
            if self._vam_mirror_gui_val:
                self._mirror_vibe_to_sliders(sp)

        if hasattr(self, "_ai_latest_feature") and self._ai_latest_feature:
            feat, val = self._ai_latest_feature
            self._ai_latest_feature = None
            if feat == "suck":
                lvl = int(val)
                self.suck_slider.set(lvl)
                self.suck_lbl.configure(text=f"Lvl {lvl}" if lvl > 0 else "Off")
                self.current_feature_states["suck"] = lvl
                self.ble.write_feature(OP_SUCKING, lvl > 0, lvl)

        is_vam_active = (now - self.vam_last_packet_time < 1.5) and self.vam_bridge_enabled.get()

        # Update VaM UI Status Badge & packet stats
        if hasattr(self, "vam_status_badge") and self.vam_bridge_enabled.get():
            if is_vam_active:
                self.vam_status_badge.configure(text=f"🎮 Active ({self.vam_packet_count} pkts)", fg_color="#2ECC71")
            else:
                self.vam_status_badge.configure(text=f"📡 Listening (Port {self.vam_port})", fg_color="#3498DB")
            if hasattr(self, "vam_stats_lbl"):
                ago = f"({now - self.vam_last_packet_time:.1f}s ago)" if self.vam_last_packet_time > 0 else "(Idle)"
                self.vam_stats_lbl.configure(text=f"Pkts: {self.vam_packet_count} {ago}")

        # Safety: if VaM was active and just timed out (>1.5s), reset speeds if in VaM Priority and pulse not running
        if hasattr(self, "vam_is_active"):
            if self.vam_is_active and not is_vam_active:
                if self.vam_bridge_mode.get() == "🎮 VaM Priority" and not self.pulse_active:
                    self._dispatch_vibe([0, 0, 0, 0], force=True)
                    if self.vam_mirror_gui.get():
                        self._mirror_vibe_to_sliders([0, 0, 0, 0])
        self.vam_is_active = is_vam_active

        # Handle Chaos Auto-Morph every 10 seconds if enabled
        if self.pulse_active and self.auto_morph_active and (now - self.last_morph_time > 10.0):
            self.last_morph_time = now
            for idx in range(4):
                cfg = self.pulse_channels[idx]
                if cfg["mode"].get() == "🌊 Wave":
                    # Gently mutate frequency and wave
                    new_freq = round(max(0.5, min(3.0, cfg["freq"].get() + random.uniform(-0.3, 0.3))), 1)
                    cfg["freq"].set(new_freq)
                    cfg["freq_slider"].set(new_freq)
                    cfg["freq_lbl"].configure(text=f"{new_freq:.1f}Hz")
                    if random.random() < 0.25:
                        cfg["waveform"].set(random.choice(WAVEFORM_CHOICES))
                        cfg["wave_menu"].set(cfg["waveform"].get())

        mode = self.vam_bridge_mode.get()

        if self.pulse_active and self.ble.is_connected:
            # If in VaM Priority mode and VaM is active, let VaM drive the hardware
            if mode == "🎮 VaM Priority" and is_vam_active:
                pass
            else:
                t = now
                speeds = []
                max_wave = 0.0

                for i in range(4):
                    cfg = self.pulse_channels[i]
                    mode_ch = cfg["mode"].get()
                    jitter_pct = cfg["jitter"].get()

                    # Calculate smooth jitter noise
                    jitter_noise = 0.0
                    if jitter_pct > 0:
                        jitter_noise = (math.sin(2 * math.pi * 6.31 * t + i) * math.cos(2 * math.pi * 11.17 * t + i * 1.5)) * (jitter_pct / 100.0)

                    if mode_ch == "🌊 Wave":
                        min_v = cfg["min_pct"].get()
                        max_v = cfg["max_pct"].get()
                        freq = cfg["freq"].get()
                        phase_deg = cfg["phase"].get()
                        wtype = cfg["waveform"].get()

                        wave = self._calculate_channel_wave(wtype, t, freq, phase_deg)
                        
                        # Apply jitter to wave
                        if jitter_pct > 0:
                            wave = max(0.0, min(1.0, wave + jitter_noise * 0.25))

                        current_pct = int(min_v + (max_v - min_v) * wave)
                        current_pct = max(0, min(100, current_pct))

                        if wave > max_wave:
                            max_wave = wave

                        cfg["progress_bar"].set(wave)
                        cfg["speed_badge"].configure(text=f"{current_pct}%", text_color="#3498DB")

                        if not (is_vam_active and self.vam_mirror_gui.get()):
                            self.ch_sliders[i].set(current_pct)
                            self.ch_labels[i].configure(text=f"{current_pct}%")
                        speeds.append(current_pct)

                    elif mode_ch == "⚓ Constant":
                        const_v = cfg["const_pct"].get()
                        if jitter_pct > 0:
                            const_v = int(max(0, min(100, const_v + jitter_noise * 20.0)))

                        cfg["progress_bar"].set(const_v / 100.0)
                        cfg["speed_badge"].configure(text=f"{const_v}%", text_color="#E67E22")

                        if not (is_vam_active and self.vam_mirror_gui.get()):
                            self.ch_sliders[i].set(const_v)
                            self.ch_labels[i].configure(text=f"{const_v}%")
                        speeds.append(const_v)

                    else: # "⭘ Off"
                        cfg["progress_bar"].set(0)
                        cfg["speed_badge"].configure(text="OFF", text_color="#7F8C8D")
                        if not (is_vam_active and self.vam_mirror_gui.get()):
                            self.ch_sliders[i].set(0)
                            self.ch_labels[i].configure(text="0%")
                        speeds.append(0)

                if mode == "🔀 Mix / Max Blend" and is_vam_active:
                    speeds = [max(speeds[j], self.vam_last_vibe[j]) for j in range(4)]

                self.pulse_master_progress.set(max_wave)
                self._dispatch_vibe(speeds)

        self.after(40, self._pulse_tick)

    # ---------------- VaM UDP Bridge Integration ----------------
    def _start_vam_bridge(self):
        self.ble.start_udp_bridge(
            self.vam_port,
            self._on_vam_raw_packet,
            self._on_bridge_status
        )

    def _on_bridge_status(self, success: bool, message: str):
        def _update():
            if not self.vam_bridge_enabled.get():
                self.vam_status_badge.configure(text="Bridge Disabled", fg_color="#7F8C8D")
            elif success:
                self.vam_status_badge.configure(text=f"📡 Listening (Port {self.vam_port})", fg_color="#3498DB")
            else:
                self.vam_status_badge.configure(text=message, fg_color="#E74C3C")
        self.after(0, _update)

    def _on_vam_enable_toggle(self):
        if self.vam_bridge_enabled.get():
            self._start_vam_bridge()
            self.vam_status_badge.configure(text=f"📡 Listening (Port {self.vam_port})", fg_color="#3498DB")
        else:
            self.ble.stop_udp_bridge()
            self.vam_status_badge.configure(text="Bridge Disabled", fg_color="#7F8C8D")

    def _on_vam_mode_changed(self, mode: str):
        pass

    def _parse_vam_packet(self, text: str):
        parsed = {
            "vibe": None,
            "heat": None,
            "light": None,
            "suck": None,
            "squeeze": None,
            "pump": None,
            "is_stop": False,
        }
        if text.startswith("{"):
            try:
                d = json.loads(text)
                if "vibe" in d:
                    v = d["vibe"]
                    if isinstance(v, list):
                        parsed["vibe"] = [max(0, min(100, int(x))) for x in (v + [0, 0, 0, 0])[:4]]
                    else:
                        val = max(0, min(100, int(v)))
                        parsed["vibe"] = [val, val, 0, 0]

                if "heat" in d:
                    parsed["heat"] = bool(d["heat"])
                if "light" in d:
                    parsed["light"] = bool(d["light"])
                if "suck" in d:
                    parsed["suck"] = max(0, min(5, int(d["suck"])))
                if "squeeze" in d:
                    parsed["squeeze"] = max(0, min(5, int(d["squeeze"])))
                if "pump" in d:
                    parsed["pump"] = bool(d["pump"])
            except Exception:
                pass
        elif text.startswith("VIBE:"):
            payload = text[5:].strip()
            if "," in payload:
                parts = payload.split(",")
                parsed["vibe"] = [max(0, min(100, int(p.strip()))) for p in parts if p.strip().isdigit()][:4]
                while len(parsed["vibe"]) < 4:
                    parsed["vibe"].append(0)
            elif payload.isdigit():
                val = max(0, min(100, int(payload)))
                parsed["vibe"] = [val, val, 0, 0]
        elif text.startswith("HEAT:"):
            parsed["heat"] = text[5:].strip().lower() in ["1", "true", "on"]
        elif text.startswith("LIGHT:"):
            parsed["light"] = text[6:].strip().lower() in ["1", "true", "on"]
        elif text.startswith("SUCK:"):
            val = text[5:].strip()
            parsed["suck"] = max(0, min(5, int(val))) if val.isdigit() else 0
        elif text.startswith("SQUEEZE:"):
            val = text[8:].strip()
            parsed["squeeze"] = max(0, min(5, int(val))) if val.isdigit() else 0
        elif text.startswith("PUMP:"):
            parsed["pump"] = text[5:].strip().lower() in ["1", "true", "on"]
        elif text.upper() == "STOP":
            parsed["is_stop"] = True
            parsed["vibe"] = [0, 0, 0, 0]
            parsed["heat"] = False
            parsed["light"] = False
            parsed["suck"] = 0
            parsed["squeeze"] = 0
            parsed["pump"] = False
        return parsed

    def _on_vam_raw_packet(self, text: str, addr):
        now = time.time()
        self.vam_packet_count += 1
        self.vam_last_packet_time = now

        if not self.vam_bridge_enabled.get():
            return

        parsed = self._parse_vam_packet(text)
        mode = self.vam_bridge_mode.get()

        if parsed["is_stop"]:
            self.after(0, self._emergency_stop)
            return

        if mode == "🖐️ Manual Only":
            return

        # Vibe speed handling
        if parsed["vibe"] is not None:
            vibe_pcts = parsed["vibe"]
            self.vam_last_vibe = list(vibe_pcts)

            if mode == "🔀 Mix / Max Blend":
                manual_pcts = [int(s.get()) for s in self.ch_sliders]
                final_pcts = [max(vibe_pcts[i], manual_pcts[i]) for i in range(4)]
            else: # "🎮 VaM Priority"
                final_pcts = list(vibe_pcts)

            self._dispatch_vibe(final_pcts)

            if self.vam_mirror_gui.get():
                self.after(0, lambda p=final_pcts: self._mirror_vibe_to_sliders(p))

        # Hardware features handling
        if parsed["heat"] is not None and parsed["heat"] != self.current_feature_states["heat"]:
            self.current_feature_states["heat"] = parsed["heat"]
            self.ble.write_feature(OP_HEATING, parsed["heat"])
            if self.vam_mirror_gui.get():
                self.after(0, lambda h=parsed["heat"]: self._set_heat_ui(h))

        if parsed["light"] is not None and parsed["light"] != self.current_feature_states["light"]:
            self.current_feature_states["light"] = parsed["light"]
            self.ble.write_feature(OP_LIGHTING, parsed["light"])
            if self.vam_mirror_gui.get():
                self.after(0, lambda l=parsed["light"]: self._set_light_ui(l))

        if parsed["suck"] is not None and parsed["suck"] != self.current_feature_states["suck"]:
            self.current_feature_states["suck"] = parsed["suck"]
            self.ble.write_feature(OP_SUCKING, parsed["suck"] > 0, parsed["suck"])
            if self.vam_mirror_gui.get():
                self.after(0, lambda s=parsed["suck"]: self._set_suck_ui(s))

        if parsed["squeeze"] is not None and parsed["squeeze"] != self.current_feature_states["squeeze"]:
            self.current_feature_states["squeeze"] = parsed["squeeze"]
            self.ble.write_feature(OP_SQUEEZING, parsed["squeeze"] > 0, parsed["squeeze"])
            if self.vam_mirror_gui.get():
                self.after(0, lambda sq=parsed["squeeze"]: self._set_squeeze_ui(sq))

        if parsed["pump"] is not None and parsed["pump"] != self.current_feature_states["pump"]:
            self.current_feature_states["pump"] = parsed["pump"]
            self.ble.write_feature(OP_SQUIRTING, parsed["pump"])
            if self.vam_mirror_gui.get():
                self.after(0, lambda p=parsed["pump"]: self._set_pump_ui(p))

    def _mirror_vibe_to_sliders(self, pcts):
        self._updating_from_vam = True
        try:
            max_p = max(pcts) if pcts else 0
            self.master_slider.set(max_p)
            self.master_lbl.configure(text=f"{max_p}%")
            for i in range(4):
                val = pcts[i]
                self.ch_sliders[i].set(val)
                self.ch_labels[i].configure(text=f"{val}%")
        finally:
            self._updating_from_vam = False

    def _set_heat_ui(self, on: bool):
        self._updating_from_vam = True
        try:
            if on: self.heat_switch.select()
            else: self.heat_switch.deselect()
        finally:
            self._updating_from_vam = False

    def _set_light_ui(self, on: bool):
        self._updating_from_vam = True
        try:
            if on: self.light_switch.select()
            else: self.light_switch.deselect()
        finally:
            self._updating_from_vam = False

    def _set_pump_ui(self, on: bool):
        self._updating_from_vam = True
        try:
            if on: self.pump_switch.select()
            else: self.pump_switch.deselect()
        finally:
            self._updating_from_vam = False

    def _set_suck_ui(self, lvl: int):
        self._updating_from_vam = True
        try:
            self.suck_slider.set(lvl)
            self.suck_lbl.configure(text=f"Lvl {lvl}" if lvl > 0 else "Off")
        finally:
            self._updating_from_vam = False

    def _set_squeeze_ui(self, lvl: int):
        self._updating_from_vam = True
        try:
            self.squeeze_slider.set(lvl)
            self.squeeze_lbl.configure(text=f"Lvl {lvl}" if lvl > 0 else "Off")
        finally:
            self._updating_from_vam = False

    # ---------------- AI Video Vision & Audio Sync Callbacks ----------------
    def _on_ai_sync_toggle(self):
        if self.ai_sync_enabled.get():
            self._start_ai_sync()
        else:
            self._stop_ai_sync()

    def _start_ai_sync(self):
        if not self.ai_sync_engine:
            self.ai_sync_engine = VisionAudioSyncEngine(
                on_speed_dispatch=self._on_ai_speed_dispatched,
                on_telemetry=self._on_ai_telemetry,
                on_feature_dispatch=self._on_ai_feature_dispatched
            )

        # Apply current configs
        self.ai_sync_engine.fusion_mode = self.ai_fusion_mode.get()
        self.ai_sync_engine.ai_engine_mode = self.ai_model_engine.get()
        self.ai_sync_engine.auto_suction_on_oral = self.ai_auto_oral_suction.get()
        self.ai_sync_engine.auto_thrust_apex_pulse = self.ai_apex_thrust_pulse.get()
        self._apply_ai_target()
        self.ai_sync_engine.sensitivity_vision = float(self.ai_vis_sens_slider.get())
        self.ai_sync_engine.sensitivity_audio = float(self.ai_aud_sens_slider.get())
        self.ai_sync_engine.min_cutoff = int(self.ai_cutoff_slider.get())
        self.ai_sync_engine.max_speed_cap = int(self.ai_max_slider.get())
        self.ai_sync_engine.smoothing = float(self.ai_smooth_slider.get())
        self.ai_sync_engine.enable_rhythm_pulse = self.ai_rhythm_pulse.get()
        self.ai_sync_engine.enable_feature_sync = self.ai_feature_sync.get()
        self.ai_sync_engine.enable_audio_boost = self.ai_audio_boost.get()
        self._on_ai_channel_mode_changed(self.ai_channel_mode.get())

        self._ai_sync_active = True
        self.ai_sync_engine.start()
        self.ai_status_badge.configure(text="⚡ Active (Running)", fg_color="#2ECC71")

    def _stop_ai_sync(self):
        self._ai_sync_active = False
        if self.ai_sync_engine:
            self.ai_sync_engine.stop()
        self.ai_status_badge.configure(text="Offline", fg_color="#7F8C8D")
        self.ai_act_badge.configure(text="👀 Act: Offline", fg_color="#7F8C8D")
        self.ai_vision_meter.set(0)
        self.ai_vision_lbl.configure(text="👁️ Motion: 0%")
        self.ai_audio_meter.set(0)
        self.ai_audio_lbl.configure(text="🎵 Audio: 0%")
        self.ai_combined_meter.set(0)
        self.ai_combined_lbl.configure(text="⚡ Toy Output: 0%")
        self.ai_stroke_badge.configure(text="⚡ Rhythm: Idle", fg_color="#7F8C8D")
        self.ai_target_lbl.configure(text="Target: Standby | 0 FPS")
        if not self.pulse_active and not (self.vam_is_active and self.vam_bridge_mode.get() == "🎮 VaM Priority"):
            self._dispatch_vibe([0, 0, 0, 0], force=True)
            if self.vam_mirror_gui.get():
                self._mirror_vibe_to_sliders([0, 0, 0, 0])

    def _apply_ai_target(self):
        if not self.ai_sync_engine:
            return
        target_val = self.ai_target_mode.get()
        if "🌟 Auto" in target_val:
            self.ai_sync_engine.target_mode = "🌟 Auto Active Window"
            self.ai_sync_engine.selected_hwnd = 0
        elif "🖥️ Full Screen (Primary)" in target_val or "🖥️ Full Screen" in target_val:
            self.ai_sync_engine.target_mode = "🖥️ Full Screen"
            self.ai_sync_engine.selected_hwnd = 0
        elif "🖥️ Full Screen (Display 2)" in target_val:
            self.ai_sync_engine.target_mode = "🖥️ Full Screen"
            self.ai_sync_engine.selected_hwnd = 0
        else:
            # Specific window title
            raw_title = target_val.replace("🪟 ", "").strip()
            match = next((w for w in self.ai_windows_cache if raw_title in w["title"]), None)
            if match:
                self.ai_sync_engine.target_mode = match["title"]
                self.ai_sync_engine.selected_hwnd = match["hwnd"]
            else:
                self.ai_sync_engine.target_mode = raw_title

    def _on_ai_target_changed(self, val):
        self._apply_ai_target()

    def _on_ai_fusion_mode_changed(self, val):
        if self.ai_sync_engine:
            self.ai_sync_engine.fusion_mode = val

    def _refresh_ai_window_list(self):
        try:
            windows = get_open_windows()
            self.ai_windows_cache = windows
            items = ["🌟 Auto: Active Foreground Window", "🖥️ Full Screen (Primary)", "🖥️ Full Screen (Display 2)"]
            for w in windows:
                items.append(f"🪟 {w['title'][:55]}")
            self.ai_target_dropdown.configure(values=items)
        except Exception:
            pass

    def _on_ai_sens_vision_change(self, val):
        v = round(float(val), 2)
        self.ai_vis_sens_lbl.configure(text=f"{v:.1f}x")
        if self.ai_sync_engine:
            self.ai_sync_engine.sensitivity_vision = v

    def _on_ai_sens_audio_change(self, val):
        v = round(float(val), 2)
        self.ai_aud_sens_lbl.configure(text=f"{v:.1f}x")
        if self.ai_sync_engine:
            self.ai_sync_engine.sensitivity_audio = v

    def _on_ai_min_cutoff_change(self, val):
        v = int(val)
        self.ai_cutoff_lbl.configure(text=f"{v}%")
        if self.ai_sync_engine:
            self.ai_sync_engine.min_cutoff = v

    def _on_ai_max_cap_change(self, val):
        v = int(val)
        self.ai_max_lbl.configure(text=f"{v}%")
        if self.ai_sync_engine:
            self.ai_sync_engine.max_speed_cap = v

    def _on_ai_smoothing_change(self, val):
        v = round(float(val), 2)
        self.ai_smooth_lbl.configure(text=f"{v:.2f}")
        if self.ai_sync_engine:
            self.ai_sync_engine.smoothing = v

    def _on_ai_rhythm_pulse_toggle(self):
        if self.ai_sync_engine:
            self.ai_sync_engine.enable_rhythm_pulse = self.ai_rhythm_pulse.get()

    def _on_ai_engine_mode_changed(self, val):
        if self.ai_sync_engine:
            self.ai_sync_engine.ai_engine_mode = val

    def _on_ai_oral_toggle(self):
        if self.ai_sync_engine:
            self.ai_sync_engine.auto_suction_on_oral = self.ai_auto_oral_suction.get()

    def _on_ai_apex_toggle(self):
        if self.ai_sync_engine:
            self.ai_sync_engine.auto_thrust_apex_pulse = self.ai_apex_thrust_pulse.get()

    def _on_ai_audio_boost_toggle(self):
        if self.ai_sync_engine:
            self.ai_sync_engine.enable_audio_boost = self.ai_audio_boost.get()

    def _on_ai_feature_sync_toggle(self):
        if self.ai_sync_engine:
            self.ai_sync_engine.enable_feature_sync = self.ai_feature_sync.get()

    def _on_ai_channel_mode_changed(self, val):
        if not self.ai_sync_engine:
            return
        if val == "All Channels (1-4)":
            self.ai_sync_engine.target_channels = [True, True, True, True]
        elif val == "Ch 1 & 2 Only":
            self.ai_sync_engine.target_channels = [True, True, False, False]
        elif val == "Ch 1 Only":
            self.ai_sync_engine.target_channels = [True, False, False, False]
        elif val == "Ch 2 Only":
            self.ai_sync_engine.target_channels = [False, True, False, False]

    def _on_ai_speed_dispatched(self, speeds: list[int]):
        # Check priority against VaM bridge and Pulse Engine
        if not self._ai_sync_active:
            return

        now = time.time()
        is_vam_active = (now - self.vam_last_packet_time < 1.5) and self._vam_bridge_enabled_val
        vam_mode = self._vam_bridge_mode_val

        if is_vam_active and vam_mode == "🎮 VaM Priority":
            # VaM has higher priority while active
            return

        final_speeds = list(speeds)
        if is_vam_active and vam_mode == "🔀 Mix / Max Blend":
            final_speeds = [max(final_speeds[i], self.vam_last_vibe[i]) for i in range(4)]

        self._dispatch_vibe(final_speeds)
        self._ai_latest_speeds = final_speeds

    def _on_ai_telemetry(self, telem: dict):
        self._ai_latest_telemetry = telem

    def _on_ai_feature_dispatched(self, feat: str, val: any):
        self._ai_latest_feature = (feat, val)

    # ---------------- General Event Handlers ----------------
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

    def _update_battery(self, pct: int):
        self.after(0, lambda: self._apply_battery_ui(pct))

    def _apply_battery_ui(self, pct: int):
        if pct < 0:
            self.battery_badge.configure(text="🔋 --%", fg_color="#34495E")
        else:
            pct = max(0, min(100, pct))
            if pct > 50:
                color = "#27AE60" # Emerald Green
            elif pct >= 20:
                color = "#E67E22" # Warm Amber
            else:
                color = "#C0392B" # Alert Red
            self.battery_badge.configure(text=f"🔋 {pct}%", fg_color=color)

    def _on_master_slider(self, val):
        if self._updating_from_vam:
            return
        pct = int(val)
        self.master_lbl.configure(text=f"{pct}%")
        for i in range(4):
            self.ch_sliders[i].set(pct)
            self.ch_labels[i].configure(text=f"{pct}%")
        self._dispatch_vibe([pct, pct, pct, pct])

    def _set_speed(self, pct):
        self.master_slider.set(pct)
        self._on_master_slider(pct)

    def _on_ch_slider(self, ch_num, val):
        if self._updating_from_vam:
            return
        pct = int(val)
        self.ch_labels[ch_num - 1].configure(text=f"{pct}%")
        speeds = [int(s.get()) for s in self.ch_sliders]
        self._dispatch_vibe(speeds)

    def _dispatch_vibe(self, pcts: list[int], force: bool = False):
        now = time.time()
        if not force and pcts == self.last_dispatched_pcts and (now - self.last_dispatch_time < 0.8):
            return
        self.last_dispatched_pcts = list(pcts)
        self.last_dispatch_time = now
        raw_speeds = [int(p * 255 / 100) for p in pcts]
        self.ble.write_vibe(raw_speeds)

    def _on_heat_toggle(self):
        if self._updating_from_vam:
            return
        on = self.heat_switch.get() == 1
        self.current_feature_states["heat"] = on
        self.ble.write_feature(OP_HEATING, on)

    def _on_light_toggle(self):
        if self._updating_from_vam:
            return
        on = self.light_switch.get() == 1
        self.current_feature_states["light"] = on
        self.ble.write_feature(OP_LIGHTING, on)

    def _on_pump_toggle(self):
        if self._updating_from_vam:
            return
        on = self.pump_switch.get() == 1
        self.current_feature_states["pump"] = on
        self.ble.write_feature(OP_SQUIRTING, on)

    def _on_suck_change(self, val):
        if self._updating_from_vam:
            return
        lvl = int(val)
        self.suck_lbl.configure(text=f"Lvl {lvl}" if lvl > 0 else "Off")
        self.current_feature_states["suck"] = lvl
        self.ble.write_feature(OP_SUCKING, lvl > 0, lvl)

    def _on_squeeze_change(self, val):
        if self._updating_from_vam:
            return
        lvl = int(val)
        self.squeeze_lbl.configure(text=f"Lvl {lvl}" if lvl > 0 else "Off")
        self.current_feature_states["squeeze"] = lvl
        self.ble.write_feature(OP_SQUEEZING, lvl > 0, lvl)

    def _emergency_stop(self):
        # Stop AI Video Sync Engine if running
        if hasattr(self, "ai_sync_engine") and self.ai_sync_engine and self.ai_sync_engine.is_running:
            self.ai_enable_switch.deselect()
            self._stop_ai_sync()

        self.pulse_switch.deselect()
        self.pulse_active = False
        self.auto_morph_switch.deselect()
        self.auto_morph_active = False
        self.pulse_master_progress.set(0)
        for cfg in self.pulse_channels:
            if cfg["progress_bar"]:
                cfg["progress_bar"].set(0)
            if cfg["speed_badge"]:
                cfg["speed_badge"].configure(text="OFF", text_color="#7F8C8D")

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

        self.vam_last_vibe = [0, 0, 0, 0]
        self.last_dispatched_pcts = [-1, -1, -1, -1]
        self.current_feature_states = {"heat": False, "light": False, "suck": 0, "squeeze": 0, "pump": False}

        self.ble.write_vibe([0, 0, 0, 0])
        self.ble.write_feature(OP_HEATING, False)
        self.ble.write_feature(OP_LIGHTING, False)
        self.ble.write_feature(OP_SUCKING, False)
        self.ble.write_feature(OP_SQUEEZING, False)
        self.ble.write_feature(OP_SQUIRTING, False)

    def _on_window_closing(self):
        if hasattr(self, "ai_sync_engine") and self.ai_sync_engine:
            self.ai_sync_engine.stop()
        self.ble.stop_udp_bridge()
        self.ble.disconnect()
        self.destroy()

if __name__ == "__main__":
    app = JoyhubApp()
    app.mainloop()
