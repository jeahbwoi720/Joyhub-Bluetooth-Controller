import asyncio
import os
import sys
import time
import json
from pathlib import Path
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
    except Exception as e:
        print(f"[Warning] Could not save device config: {e}")

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
OP_RIPPLING = "25"
OP_RANGE = "26"

def build_vibe_packet(speeds: list[int]) -> bytes:
    padded = (speeds + [0, 0, 0, 0])[:4]
    hex_payload = "".join(f"{max(0, min(255, s)):02x}" for s in padded)
    return bytes.fromhex(f"{CMD_HEADER}{OP_MOTOR_WAVE}{hex_payload}{CMD_TRAILER_AA}")

def build_feature_packet(op: str, state_on: bool, level: int = 1) -> bytes:
    if state_on:
        return bytes.fromhex(f"{CMD_HEADER}{op}0100{level:02x}{CMD_TRAILER_FF}")
    return bytes.fromhex(f"{CMD_HEADER}{op}00000000")

def notification_handler(sender, data: bytearray):
    hex_str = data.hex()
    if hex_str.startswith("a021"):
        try:
            gyro = int(hex_str[-1], 16)
            print(f"\n[Telemetry] Gyro/Sensor: {gyro}\nJoyHub > ", end="", flush=True)
        except Exception:
            print(f"\n[Telemetry] Raw: {hex_str}\nJoyHub > ", end="", flush=True)
    else:
        print(f"\n[Incoming BLE] {hex_str}\nJoyHub > ", end="", flush=True)

# ==================== Connection Manager ====================
class JoyhubDeviceManager:
    def __init__(self):
        self.client: BleakClient | None = None
        self.target_address: str | None = None
        self.target_name: str | None = None
        self.write_char: str | None = None
        self.notify_char: str | None = None
        self.is_reconnecting = False
        self.should_run = True
        self.disconnect_event = asyncio.Event()

    def _on_disconnected(self, client):
        if self.should_run:
            print(f"\n[!] Connection lost to {self.target_name or self.target_address}!")
            self.disconnect_event.set()

    async def find_characteristics(self):
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
            if all_chars:
                self.write_char = all_chars[0]

        if self.notify_char:
            try:
                await self.client.start_notify(self.notify_char, notification_handler)
            except Exception:
                pass

    async def connect_with_retries(self, max_attempts=None):
        attempt = 0
        while self.should_run and (max_attempts is None or attempt < max_attempts):
            attempt += 1
            print(f"[Connecting] Attempting to connect to {self.target_name or self.target_address} (Attempt {attempt})...")
            try:
                self.disconnect_event.clear()
                self.client = BleakClient(
                    self.target_address,
                    disconnected_callback=self._on_disconnected,
                    timeout=10.0
                )
                await self.client.connect()
                if self.client.is_connected:
                    print(f"[✓] Connected successfully to {self.target_name or self.target_address}!")
                    await self.find_characteristics()
                    return True
            except Exception as e:
                print(f"[x] Connection failed ({e}). Retrying in 3 seconds...")
            await asyncio.sleep(3.0)
        return False

    async def send_bytes(self, payload: bytes, desc: str = ""):
        if not self.client or not self.client.is_connected:
            print("[!] Not connected. Waiting for reconnection...")
            return False
        try:
            await self.client.write_gatt_char(self.write_char, payload, response=False)
            if desc:
                print(f"-> Sent: {desc} (Hex: {payload.hex()})")
            return True
        except Exception as ex:
            print(f"[Error writing BLE]: {ex}")
            self.disconnect_event.set()
            return False

    async def auto_reconnect_loop(self):
        while self.should_run:
            await self.disconnect_event.wait()
            self.disconnect_event.clear()
            if not self.should_run:
                break
            print("\n[Auto-Reconnect] Device disconnected. Reconnecting in background...")
            await self.connect_with_retries()
            print("\n[Auto-Reconnect] Connection restored! Resume commands below:\nJoyHub > ", end="", flush=True)

# ==================== Main CLI ====================
async def main():
    print("=" * 60)
    print("  Joyhub BLE Device Controller CLI (Auto-Reconnect Enabled)")
    print("=" * 60)

    mgr = JoyhubDeviceManager()
    saved = load_saved_device()

    target_address = None
    target_name = None

    if saved and "address" in saved:
        print(f"\n[Saved Device Found]: {saved.get('name', 'Unknown')} ({saved['address']})")
        choice = await asyncio.to_thread(input, "Connect to saved device? [Y/n/scan]: ")
        choice = choice.strip().lower()

        if choice in ["", "y", "yes"]:
            target_address = saved["address"]
            target_name = saved.get("name", "Saved Device")

    if not target_address:
        print("\nScanning for Bluetooth Low Energy (BLE) devices (5 seconds)...")
        discovered_dict = await BleakScanner.discover(timeout=5.0, return_adv=True)
        if not discovered_dict:
            print("No BLE devices found. Ensure device is ON and Bluetooth is enabled.")
            return

        device_list = list(discovered_dict.values())
        print("\nFound Devices:")
        for idx, (dev, adv) in enumerate(device_list):
            name = dev.name or adv.local_name or "Unknown / Unnamed"
            rssi = adv.rssi if adv else "N/A"
            print(f"  [{idx + 1}] {name} (Address: {dev.address}, RSSI: {rssi} dBm)")

        selection = await asyncio.to_thread(input, "\nEnter device number to connect (or 'q' to quit): ")
        selection = selection.strip()
        if selection.lower() == 'q' or not selection.isdigit():
            return

        target_idx = int(selection) - 1
        if target_idx < 0 or target_idx >= len(device_list):
            print("Invalid selection.")
            return

        selected_dev, _ = device_list[target_idx]
        target_address = selected_dev.address
        target_name = selected_dev.name or target_address

        # Save to memory for future launches
        save_device(target_address, target_name)
        print(f"[✓] Device memorized in {CONFIG_FILE.name}")

    mgr.target_address = target_address
    mgr.target_name = target_name

    # Initial Connection
    connected = await mgr.connect_with_retries(max_attempts=3)
    if not connected:
        print("Could not connect to device. Exiting.")
        return

    # Start Auto-Reconnect background worker
    reconnect_task = asyncio.create_task(mgr.auto_reconnect_loop())

    print("\n" + "=" * 60)
    print("Available Commands:")
    print("  vibe <0-100> [ch2] [ch3] [ch4]  - Set motor speed (e.g. 'vibe 50' or 'vibe 100 50')")
    print("  pulse <min> <max> [step_time]   - Loop pulsing waveform (Ctrl+C to stop)")
    print("  heat <on|off>                   - Toggle heating")
    print("  light <on|off>                  - Toggle LED lights")
    print("  suck <1-5|off>                  - Set suction level or off")
    print("  squeeze <1-5|off>               - Set squeezing level or off")
    print("  pump <on|off>                   - Toggle fluid pump")
    print("  stop                            - Stop all motors and features")
    print("  forget                          - Clear saved device memory")
    print("  raw <hex>                       - Send custom hex string (e.g. 'raw a003ff00aa')")
    print("  help                            - Show this menu")
    print("  exit / quit                     - Disconnect and exit")
    print("=" * 60 + "\n")

    # Interactive Command Loop
    while True:
        try:
            cmd_line = await asyncio.to_thread(input, "JoyHub > ")
            cmd_line = cmd_line.strip()
            if not cmd_line:
                continue

            parts = cmd_line.split()
            cmd = parts[0].lower()
            args = parts[1:]

            if cmd in ["exit", "quit", "q"]:
                print("Stopping all motors and disconnecting...")
                mgr.should_run = False
                reconnect_task.cancel()
                await mgr.send_bytes(build_vibe_packet([0, 0, 0, 0]), "Emergency Stop")
                if mgr.client and mgr.client.is_connected:
                    await mgr.client.disconnect()
                break

            elif cmd == "forget":
                if CONFIG_FILE.exists():
                    CONFIG_FILE.unlink()
                    print("[✓] Saved device memory cleared.")

            elif cmd == "stop":
                await mgr.send_bytes(build_vibe_packet([0, 0, 0, 0]), "Stop All")
                await mgr.send_bytes(build_feature_packet(OP_HEATING, False), "Heat Off")
                await mgr.send_bytes(build_feature_packet(OP_LIGHTING, False), "Light Off")
                await mgr.send_bytes(build_feature_packet(OP_SUCKING, False), "Suck Off")

            elif cmd == "vibe":
                if not args:
                    print("Usage: vibe <0-100> [ch2] [ch3] [ch4]")
                    continue
                speeds = []
                for a in args:
                    pct = max(0, min(100, int(a)))
                    speeds.append(int(pct * 255 / 100))
                packet = build_vibe_packet(speeds)
                await mgr.send_bytes(packet, f"Vibration ({', '.join(args)}%)")

            elif cmd == "heat":
                if args and args[0].lower() == "on":
                    await mgr.send_bytes(build_feature_packet(OP_HEATING, True), "Heating ON")
                else:
                    await mgr.send_bytes(build_feature_packet(OP_HEATING, False), "Heating OFF")

            elif cmd == "light":
                if args and args[0].lower() == "on":
                    await mgr.send_bytes(build_feature_packet(OP_LIGHTING, True), "Lighting ON")
                else:
                    await mgr.send_bytes(build_feature_packet(OP_LIGHTING, False), "Lighting OFF")

            elif cmd == "suck":
                if args and args[0].isdigit():
                    lvl = max(1, min(5, int(args[0])))
                    await mgr.send_bytes(build_feature_packet(OP_SUCKING, True, lvl), f"Suction Level {lvl}")
                else:
                    await mgr.send_bytes(build_feature_packet(OP_SUCKING, False), "Suction OFF")

            elif cmd == "squeeze":
                if args and args[0].isdigit():
                    lvl = max(1, min(5, int(args[0])))
                    await mgr.send_bytes(build_feature_packet(OP_SQUEEZING, True, lvl), f"Squeeze Level {lvl}")
                else:
                    await mgr.send_bytes(build_feature_packet(OP_SQUEEZING, False), "Squeeze OFF")

            elif cmd == "pump":
                if args and args[0].lower() == "on":
                    await mgr.send_bytes(build_feature_packet(OP_SQUIRTING, True), "Pump ON")
                else:
                    await mgr.send_bytes(build_feature_packet(OP_SQUIRTING, False), "Pump OFF")

            elif cmd == "pulse":
                min_v = int(args[0]) if len(args) > 0 else 10
                max_v = int(args[1]) if len(args) > 1 else 90
                delay = float(args[2]) if len(args) > 2 else 0.05
                print(f"Running pulse wave ({min_v}% -> {max_v}%). Press Ctrl+C in console to stop pulse...")
                try:
                    while True:
                        for sp in range(min_v, max_v + 1, 5):
                            await mgr.send_bytes(build_vibe_packet([int(sp * 255 / 100)]))
                            await asyncio.sleep(delay)
                        for sp in range(max_v, min_v - 1, -5):
                            await mgr.send_bytes(build_vibe_packet([int(sp * 255 / 100)]))
                            await asyncio.sleep(delay)
                except KeyboardInterrupt:
                    print("\nPulse stopped.")
                    await mgr.send_bytes(build_vibe_packet([0]), "Stop")

            elif cmd == "raw":
                if not args:
                    print("Usage: raw <hexstring> (e.g. 'raw a003ff000000aa')")
                    continue
                try:
                    raw_bytes = bytes.fromhex(args[0])
                    await mgr.send_bytes(raw_bytes, f"Raw Hex {args[0]}")
                except ValueError:
                    print("Invalid hex string.")

            elif cmd == "help":
                print("Commands: vibe <0-100>, pulse <min> <max>, heat <on|off>, light <on|off>, suck <1-5|off>, stop, forget, raw <hex>, exit")

            else:
                print(f"Unknown command: '{cmd}'. Type 'help' for options.")

        except Exception as err:
            print(f"Error: {err}")

    print("Disconnected.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
