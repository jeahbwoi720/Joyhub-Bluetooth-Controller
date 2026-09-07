"""
Joyhub AI Video Vision & Audio Synchronization Engine
Author: jeahbwoi720 / Remus
Hardware: Windows DirectML / DirectX 12 GPU Acceleration
OS: Windows 10/11

Provides real-time Computer Vision Optical Flow motion detection + WASAPI loopback audio sync
for ANY video playing globally on the PC (Chrome, Edge, Firefox, VLC, Windows Media Player, VaM mirror, etc.).
"""

import time
import math
import threading
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional, Dict, List, Tuple
import numpy as np
import cv2
import mss

try:
    import onnxruntime as ort
    HAS_ONNX = True
except ImportError:
    HAS_ONNX = False

try:
    import soundcard as sc
    HAS_SOUNDCARD = True
except ImportError:
    HAS_SOUNDCARD = False

# ==================== Win32 Helpers ====================
u32 = ctypes.windll.user32

class RECT(ctypes.Structure):
    _fields_ = [
        ('left', ctypes.c_long),
        ('top', ctypes.c_long),
        ('right', ctypes.c_long),
        ('bottom', ctypes.c_long)
    ]

def _attach_thread_desktop():
    try:
        hdesk = u32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            u32.SetThreadDesktop(hdesk)
    except Exception:
        pass

def get_open_windows() -> List[Dict[str, any]]:
    """Returns a list of visible top-level windows with titles and coordinates."""
    _attach_thread_desktop()
    windows = []
    
    # Exclude system/hidden classes
    ignored_titles = {
        "", "Default IME", "MSCTFIME UI", "Program Manager", 
        "Windows Input Experience", "Settings"
    }

    def enum_cb(hwnd, lparam):
        if u32.IsWindowVisible(hwnd) and not u32.IsIconic(hwnd):
            length = u32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buff, length + 1)
                title = buff.value.strip()
                if title and title not in ignored_titles:
                    rect = RECT()
                    if u32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        if w > 250 and h > 200:
                            windows.append({
                                "hwnd": hwnd,
                                "title": title,
                                "rect": (max(0, rect.left), max(0, rect.top), w, h)
                            })
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumDesktopWindows(0, WNDENUMPROC(enum_cb), 0)
    return windows

def get_active_foreground_window() -> Tuple[int, str, Tuple[int, int, int, int]]:
    """Returns (hwnd, title, (left, top, width, height)) for the foreground window."""
    _attach_thread_desktop()
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return 0, "Unknown", (0, 0, 1920, 1080)
    
    length = u32.GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1) if length > 0 else None
    if buff:
        u32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()
    else:
        title = "Active Window"

    rect = RECT()
    if u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        w = max(100, rect.right - rect.left)
        h = max(100, rect.bottom - rect.top)
        return hwnd, title, (max(0, rect.left), max(0, rect.top), w, h)
    return hwnd, title, (0, 0, 1920, 1080)

# ==================== Neural Pose & Semantic Intimacy Classifier ====================
class NeuralPoseAnalyzer:
    """Real-time DirectML / GPU Neural Pose & Semantic Intimacy Classifier."""
    def __init__(self, model_path=None):
        self.session = None
        self.active_provider = "None"
        self.H_IN, self.W_IN = 320, 320
        self.inp_name = ""

        if not HAS_ONNX:
            return

        if model_path is None:
            candidates = [
                Path(__file__).parent / "yolov8n-pose.onnx",
                Path.cwd() / "yolov8n-pose.onnx",
            ]
            if getattr(sys, 'frozen', False):
                candidates.insert(0, Path(sys.executable).parent / "yolov8n-pose.onnx")
                if hasattr(sys, '_MEIPASS'):
                    candidates.insert(0, Path(sys._MEIPASS) / "yolov8n-pose.onnx")
            model_path = next((p for p in candidates if p.exists()), candidates[0])
        else:
            model_path = Path(model_path)

        if not model_path.exists():
            return

        try:
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            providers = ['DmlExecutionProvider', 'CPUExecutionProvider']
            self.session = ort.InferenceSession(str(model_path), opts, providers=providers)
            self.active_provider = self.session.get_providers()[0]
            self.inp_name = self.session.get_inputs()[0].name
        except Exception:
            self.session = None

    def is_available(self) -> bool:
        return self.session is not None

    def preprocess(self, frame):
        h, w = frame.shape[:2]
        scale = min(self.W_IN / w, self.H_IN / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((self.H_IN, self.W_IN, 3), dtype=np.uint8)
        dx = (self.W_IN - nw) // 2
        dy = (self.H_IN - nh) // 2
        canvas[dy:dy+nh, dx:dx+nw] = resized
        blob = canvas.astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))
        blob = np.expand_dims(blob, axis=0)
        return blob, scale, dx, dy

    def parse_detections(self, output, scale, dx, dy, conf_thresh=0.30):
        pred = output[0][0]
        scores = pred[4, :]
        mask = scores > conf_thresh
        if not np.any(mask):
            return []

        boxes = pred[:4, mask].T
        scs = scores[mask]
        kpts_raw = pred[5:, mask].T.reshape(-1, 17, 3)

        x1 = (boxes[:, 0] - boxes[:, 2] / 2 - dx) / scale
        y1 = (boxes[:, 1] - boxes[:, 3] / 2 - dy) / scale
        bw = boxes[:, 2] / scale
        bh = boxes[:, 3] / scale

        boxes_xywh = np.stack([x1, y1, bw, bh], axis=1)
        indices = cv2.dnn.NMSBoxes(boxes_xywh.tolist(), scs.tolist(), conf_thresh, 0.45)

        persons = []
        for idx in indices:
            i = idx if isinstance(idx, (int, np.integer)) else idx[0]
            kp = kpts_raw[i].copy()
            kp[:, 0] = (kp[:, 0] - dx) / scale
            kp[:, 1] = (kp[:, 1] - dy) / scale
            persons.append({
                "box": (int(x1[i]), int(y1[i]), int(bw[i]), int(bh[i])),
                "score": float(scs[i]),
                "kpts": kp
            })
        return persons

    def analyze(self, frame):
        h, w = frame.shape[:2]
        if not self.session:
            return {
                "detected": False,
                "act": "👀 Scene Motion",
                "roi": (0, 0, w, h),
                "is_oral": False,
                "is_thrusting": False,
                "is_stroking": False,
                "persons_count": 0,
                "inference_ms": 0.0
            }

        blob, scale, dx, dy = self.preprocess(frame)
        t0 = time.time()
        try:
            out = self.session.run(None, {self.inp_name: blob})
            inference_ms = (time.time() - t0) * 1000
            persons = self.parse_detections(out, scale, dx, dy)
        except Exception:
            return {
                "detected": False,
                "act": "👀 Scene Motion",
                "roi": (0, 0, w, h),
                "is_oral": False,
                "is_thrusting": False,
                "is_stroking": False,
                "persons_count": 0,
                "inference_ms": 0.0
            }

        if not persons:
            return {
                "detected": False,
                "act": "👀 Scene Motion",
                "roi": (0, 0, w, h),
                "is_oral": False,
                "is_thrusting": False,
                "is_stroking": False,
                "persons_count": 0,
                "inference_ms": inference_ms
            }

        # Keypoint analysis
        active_kpts = []
        is_oral = False
        is_thrusting = False
        is_stroking = False

        pelvises = []
        for p in persons:
            kp = p["kpts"]
            if kp[11, 2] > 0.25 and kp[12, 2] > 0.25:
                pelvis = (kp[11, :2] + kp[12, :2]) / 2.0
                pelvises.append((pelvis, p))
            elif kp[11, 2] > 0.25:
                pelvises.append((kp[11, :2], p))
            elif kp[12, 2] > 0.25:
                pelvises.append((kp[12, :2], p))

        # Pelvic thrusting (pelvis to pelvis)
        if len(pelvises) >= 2:
            p1, p2 = pelvises[0][0], pelvises[1][0]
            pelvis_dist = np.linalg.norm(p1 - p2) / max(1, h)
            if pelvis_dist < 0.38:
                is_thrusting = True
                active_kpts.extend([p1, p2])

        # Oral interaction (head to pelvis)
        for pel, p_owner in pelvises:
            for other in persons:
                if other is not p_owner:
                    head = other["kpts"][0, :2]
                    if other["kpts"][0, 2] > 0.25:
                        dist_head_pel = np.linalg.norm(head - pel) / max(1, h)
                        if dist_head_pel < 0.30:
                            is_oral = True
                            active_kpts.extend([head, pel])

        # Hand stroking interaction (wrist to pelvis)
        for pel, _ in pelvises:
            for p in persons:
                for wrist_idx in [9, 10]:
                    wrist = p["kpts"][wrist_idx, :2]
                    if p["kpts"][wrist_idx, 2] > 0.25:
                        dist_w_pel = np.linalg.norm(wrist - pel) / max(1, h)
                        if dist_w_pel < 0.30:
                            is_stroking = True
                            active_kpts.extend([wrist, pel])

        # Semantic Act determination
        if is_oral:
            act = "👅 Oral / Sucking"
        elif is_thrusting:
            act = "🔥 Deep Thrusting"
        elif is_stroking:
            act = "🖐️ Hand Stroking"
        elif len(persons) >= 2:
            act = "✨ Sensual Teasing"
        else:
            act = "👀 Solo Action"

        # Calculate Focused Intimacy ROI
        if active_kpts:
            pts = np.array(active_kpts)
            rx1 = max(0, int(np.min(pts[:, 0]) - w * 0.15))
            ry1 = max(0, int(np.min(pts[:, 1]) - h * 0.15))
            rx2 = min(w, int(np.max(pts[:, 0]) + w * 0.15))
            ry2 = min(h, int(np.max(pts[:, 1]) + h * 0.15))
            roi = (rx1, ry1, max(120, rx2 - rx1), max(120, ry2 - ry1))
        else:
            boxes = [p["box"] for p in persons]
            rx1 = max(0, min(b[0] for b in boxes))
            ry1 = max(0, min(b[1] for b in boxes))
            rx2 = min(w, max(b[0] + b[2] for b in boxes))
            ry2 = min(h, max(b[1] + b[3] for b in boxes))
            roi = (rx1, ry1, max(120, rx2 - rx1), max(120, ry2 - ry1))

        return {
            "detected": True,
            "act": act,
            "roi": roi,
            "is_oral": is_oral,
            "is_thrusting": is_thrusting,
            "is_stroking": is_stroking,
            "persons_count": len(persons),
            "inference_ms": inference_ms
        }

# ==================== Vision & Audio Engine ====================
class VisionAudioSyncEngine:
    def __init__(
        self,
        on_speed_dispatch: Callable[[List[int]], None],
        on_telemetry: Optional[Callable[[Dict[str, any]], None]] = None,
        on_feature_dispatch: Optional[Callable[[str, any], None]] = None
    ):
        self.on_speed_dispatch = on_speed_dispatch
        self.on_telemetry = on_telemetry
        self.on_feature_dispatch = on_feature_dispatch

        # Engine states
        self.is_running = False
        self._stop_event = threading.Event()

        # Threads
        self._vision_thread: Optional[threading.Thread] = None
        self._audio_thread: Optional[threading.Thread] = None
        self._dispatch_thread: Optional[threading.Thread] = None

        # Configuration parameters
        self.target_mode = "🌟 Auto Active Window" # "🌟 Auto Active Window", "🖥️ Full Screen", or window title
        self.selected_hwnd = 0
        self.selected_title = "Auto"
        self.fusion_mode = "👁️ + 🎵 Vision & Audio Blend" # "👁️ + 🎵 Vision & Audio Blend", "👁️ Vision Only", "🎵 Audio Only"
        
        self.sensitivity_vision = 1.0   # 0.2 to 3.0
        self.sensitivity_audio = 1.0    # 0.2 to 3.0
        self.min_cutoff = 5             # 0% to 40% noise floor
        self.max_speed_cap = 100        # 20% to 100%
        self.smoothing = 0.35           # 0.0 (raw snappy) to 0.85 (ultra smooth)
        
        self.target_channels = [True, True, True, True] # Channels 1, 2, 3, 4
        self.enable_rhythm_pulse = True # Modulate amplitude with detected stroke rhythm
        self.enable_feature_sync = False # Auto suction/squeeze on climax thrusting

        # Neural AI Pose & Act Sync
        self.ai_engine_mode = "🧠 Neural AI Pose & Act Sync"
        self.auto_suction_on_oral = True
        self.auto_thrust_apex_pulse = True
        self.neural_analyzer = NeuralPoseAnalyzer()

        # Live thread-safe telemetry & metrics
        self._lock = threading.Lock()
        self.live_vision_raw = 0.0
        self.live_vision_pct = 0
        self.live_audio_raw = 0.0
        self.live_audio_pct = 0
        self.live_combined_pct = 0
        self.live_stroke_hz = 0.0
        self.live_stroke_phase = 0.0
        self.live_fps = 0.0
        self.live_target_info = "Standby"
        self.live_beat_hit = False
        self.live_act_type = "👀 Scene Motion"
        self.live_is_thrusting = False
        self.live_is_oral = False

        # Internal state history
        self._smoothed_speed = 0.0
        self._prev_reversal_time = 0.0
        self._last_v_sign = 0
        self._stroke_periods = []
        self._intense_duration = 0.0
        self._feature_state_active = False
        self._oral_suction_active = False

    def start(self):
        """Starts the vision, audio, and dispatch threads."""
        if self.is_running:
            return
        self.is_running = True
        self._stop_event.clear()

        self._vision_thread = threading.Thread(target=self._vision_worker, daemon=True, name="Joyhub-VisionWorker")
        self._audio_thread = threading.Thread(target=self._audio_worker, daemon=True, name="Joyhub-AudioWorker")
        self._dispatch_thread = threading.Thread(target=self._dispatch_worker, daemon=True, name="Joyhub-DispatchWorker")

        self._vision_thread.start()
        if HAS_SOUNDCARD:
            self._audio_thread.start()
        self._dispatch_thread.start()

    def stop(self):
        """Stops all background sync threads and resets speeds."""
        if not self.is_running:
            return
        self.is_running = False
        self._stop_event.set()

        # Join threads briefly
        for th in [self._vision_thread, self._audio_thread, self._dispatch_thread]:
            if th and th.is_alive():
                th.join(timeout=0.6)

        # Reset speeds
        if self.on_speed_dispatch:
            self.on_speed_dispatch([0, 0, 0, 0])

    # ==================== Vision Worker ====================
    def _vision_worker(self):
        _attach_thread_desktop()

        sct = None
        try:
            sct = mss.MSS()
        except Exception:
            try:
                sct = mss.mss()
            except Exception:
                sct = None

        if not sct:
            return

        prev_gray = None
        peak_motion = 4.0
        frame_times = []
        last_target_query = 0.0
        cached_rect = (0, 0, 1920, 1080)
        target_name = "Desktop"

        # Analysis resolution (320x180 = lightning-fast 5-7ms per frame)
        ANALYSIS_W, ANALYSIS_H = 320, 180

        while not self._stop_event.is_set():
            t_start = time.time()

            # 1. Determine capture bounding box
            if t_start - last_target_query > 0.4:
                last_target_query = t_start
                if self.target_mode == "🌟 Auto Active Window":
                    _, title, rect = get_active_foreground_window()
                    target_name = f"Active: {title[:32]}"
                    cached_rect = rect
                elif self.target_mode == "🖥️ Full Screen":
                    mon = sct.monitors[2] if len(sct.monitors) > 2 else sct.monitors[1]
                    cached_rect = (mon["left"], mon["top"], mon["width"], mon["height"])
                    target_name = "Full Screen Monitor"
                else:
                    # Specific window title matching
                    windows = get_open_windows()
                    match = next((w for w in windows if self.target_mode in w["title"] or (self.selected_hwnd and w["hwnd"] == self.selected_hwnd)), None)
                    if match:
                        cached_rect = match["rect"]
                        target_name = f"Window: {match['title'][:32]}"
                    else:
                        target_name = f"Waiting for '{self.target_mode}'"

            # Clamp coordinates to monitor limits
            x, y, w, h = cached_rect
            if w < 100 or h < 100:
                w, h = 1280, 720

            # MSS region dict
            region = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}

            # 2. Grab screen frame
            try:
                sct_img = sct.grab(region)
                frame = np.array(sct_img)
            except Exception:
                time.sleep(0.04)
                continue

            # 3. Neural Pose & Semantic Act Recognition Pass
            act_type = "👀 Scene Motion"
            is_oral = False
            is_thrusting = False
            is_stroking = False
            flow_source = frame

            if ("Neural" in str(self.ai_engine_mode)) and self.neural_analyzer.is_available():
                ai_res = self.neural_analyzer.analyze(frame[:, :, :3])
                act_type = ai_res["act"]
                is_oral = ai_res["is_oral"]
                is_thrusting = ai_res["is_thrusting"]
                is_stroking = ai_res["is_stroking"]

                # Crop Optical Flow strictly to the Intimacy Interaction ROI!
                if ai_res["detected"]:
                    rx, ry, rw, rh = ai_res["roi"]
                    fh, fw = frame.shape[:2]
                    rx1 = max(0, min(fw - 50, rx))
                    ry1 = max(0, min(fh - 50, ry))
                    rx2 = max(rx1 + 50, min(fw, rx + rw))
                    ry2 = max(ry1 + 50, min(fh, ry + rh))
                    flow_source = frame[ry1:ry2, rx1:rx2]

                # Automatic Suction Trigger on Oral scene
                if self.auto_suction_on_oral and self.on_feature_dispatch:
                    if is_oral and not self._oral_suction_active:
                        self._oral_suction_active = True
                        self.on_feature_dispatch("suck", 2)
                    elif not is_oral and self._oral_suction_active:
                        self._oral_suction_active = False
                        self.on_feature_dispatch("suck", 0)
            else:
                act_type = "⚡ Optical Flow Only"

            # 4. Downscale & Grayscale
            try:
                small = cv2.resize(flow_source, (ANALYSIS_W, ANALYSIS_H), interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(small, cv2.COLOR_BGRA2GRAY if flow_source.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (5, 5), 0)
            except Exception:
                time.sleep(0.04)
                continue

            if prev_gray is None or prev_gray.shape != gray.shape:
                prev_gray = gray
                time.sleep(0.03)
                continue

            # 4. Dense Optical Flow (Farneback)
            try:
                flow = cv2.calcOpticalFlowFarneback(
                    prev_gray, gray, None,
                    pyr_scale=0.5, levels=2, winsize=13,
                    iterations=2, poly_n=5, poly_sigma=1.1, flags=0
                )
                prev_gray = gray
            except Exception:
                prev_gray = gray
                time.sleep(0.03)
                continue

            u = flow[..., 0]
            v = flow[..., 1]
            mag = np.sqrt(u**2 + v**2)

            # 5. Motion extraction & Filtering
            active = mag > 0.45 # Filter subpixel noise / compression artifacts
            if np.any(active):
                mean_mag = float(np.mean(mag[active]))
                coverage = float(np.count_nonzero(active) / mag.size)
                raw_score = mean_mag * (0.2 + 0.8 * min(1.0, coverage * 5.0))
                mean_v = float(np.mean(v[active])) # Dominant vertical thrusting velocity
            else:
                raw_score = 0.0
                mean_v = 0.0

            # Adaptive dynamic range normalization
            peak_motion = max(peak_motion * 0.992, raw_score, 1.2)
            normalized_pct = min(100, int((raw_score / peak_motion) * 100 * self.sensitivity_vision))

            # 6. Stroke Rhythm & Reversal Detection
            now = time.time()
            v_sign = 1 if mean_v > 0.3 else (-1 if mean_v < -0.3 else 0)
            
            if v_sign != 0 and v_sign != self._last_v_sign:
                # Reversal detected! (Inflection point of stroke)
                if self._prev_reversal_time > 0:
                    half_cycle_dt = now - self._prev_reversal_time
                    if 0.15 <= half_cycle_dt <= 1.5: # 0.3 Hz to 3.3 Hz valid stroke range
                        full_period = half_cycle_dt * 2.0
                        self._stroke_periods.append(full_period)
                        if len(self._stroke_periods) > 6:
                            self._stroke_periods.pop(0)

                self._prev_reversal_time = now
                self._last_v_sign = v_sign

            # Estimate stroke frequency
            stroke_hz = 0.0
            if self._stroke_periods and (now - self._prev_reversal_time < 2.0):
                avg_period = float(np.median(self._stroke_periods))
                if avg_period > 0.1:
                    stroke_hz = round(1.0 / avg_period, 1)

            # Stroke phase
            stroke_phase = 0.0
            if stroke_hz > 0 and self._prev_reversal_time > 0:
                elapsed = now - self._prev_reversal_time
                stroke_phase = (elapsed * stroke_hz) % 1.0

            # Update FPS
            dt = time.time() - t_start
            frame_times.append(dt)
            if len(frame_times) > 30:
                frame_times.pop(0)
            avg_dt = np.mean(frame_times) if frame_times else 0.033
            fps = round(1.0 / max(0.001, avg_dt), 1)

            with self._lock:
                self.live_vision_raw = raw_score
                self.live_vision_pct = normalized_pct
                self.live_stroke_hz = stroke_hz
                self.live_stroke_phase = stroke_phase
                self.live_fps = fps
                self.live_target_info = target_name
                self.live_act_type = act_type
                self.live_is_thrusting = is_thrusting
                self.live_is_oral = is_oral

            # Sleep to maintain smooth 30-35 FPS target
            sleep_time = max(0.002, 0.030 - dt)
            time.sleep(sleep_time)

        if sct:
            try:
                sct.close()
            except Exception:
                pass

    # ==================== Audio Worker (WASAPI Loopback) ====================
    def _audio_worker(self):
        if not HAS_SOUNDCARD:
            return

        while not self._stop_event.is_set():
            try:
                speaker = sc.default_speaker()
                if not speaker:
                    time.sleep(1.0)
                    continue

                mic = sc.get_microphone(id=str(speaker.id), include_loopback=True)
                if not mic:
                    time.sleep(1.0)
                    continue

                with mic.recorder(samplerate=44100, blocksize=1024) as rec:
                    running_avg_energy = 0.01

                    while not self._stop_event.is_set():
                        # Read 1024 frames (~23.2 ms of audio)
                        data = rec.record(numframes=1024)
                        if data is None or len(data) == 0:
                            time.sleep(0.02)
                            continue

                        # Convert stereo to mono
                        mono = np.mean(data, axis=1) if data.ndim > 1 else data
                        
                        # RMS amplitude
                        rms = float(np.sqrt(np.mean(mono**2)))

                        # Fast Fourier Transform (FFT) for bass / rhythm isolation
                        fft_vals = np.abs(np.fft.rfft(mono))
                        freqs = np.fft.rfftfreq(len(mono), 1.0 / 44100)

                        # Bass band: 20 Hz to 250 Hz (kicks, moans, rhythmic impact)
                        bass_mask = (freqs >= 20) & (freqs <= 250)
                        bass_energy = float(np.mean(fft_vals[bass_mask])) if np.any(bass_mask) else 0.0

                        # Beat transient detection
                        is_beat = False
                        if bass_energy > running_avg_energy * 1.6 and bass_energy > 0.04:
                            is_beat = True
                        running_avg_energy = running_avg_energy * 0.95 + bass_energy * 0.05

                        # Normalize audio percent (0 - 100%)
                        audio_val = (rms * 0.4 + bass_energy * 0.6) * self.sensitivity_audio * 280.0
                        audio_pct = max(0, min(100, int(audio_val)))

                        with self._lock:
                            self.live_audio_raw = bass_energy
                            self.live_audio_pct = audio_pct
                            self.live_beat_hit = is_beat

            except Exception:
                time.sleep(0.5)

    # ==================== Dispatch & Fusion Worker ====================
    def _dispatch_worker(self):
        last_dispatch_time = time.time()
        last_sent_speeds = [-1, -1, -1, -1]

        while not self._stop_event.is_set():
            t_now = time.time()
            dt = t_now - last_dispatch_time
            last_dispatch_time = t_now

            with self._lock:
                v_pct = self.live_vision_pct
                a_pct = self.live_audio_pct
                stroke_hz = self.live_stroke_hz
                stroke_phase = self.live_stroke_phase
                fps = self.live_fps
                target_info = self.live_target_info
                beat_hit = self.live_beat_hit
                act_type = self.live_act_type
                is_thrusting = self.live_is_thrusting

            # 1. Multi-Modal Fusion
            if self.fusion_mode == "👁️ Vision Only (Motion Flow)":
                raw_combined = v_pct
            elif self.fusion_mode == "🎵 Audio Only (WASAPI Beat)":
                raw_combined = a_pct
            else: # "👁️ + 🎵 Vision & Audio Blend"
                # Blend 65% visual motion + 35% audio energy, with beat accent
                raw_combined = int(v_pct * 0.65 + a_pct * 0.35)
                if beat_hit:
                    raw_combined = min(100, raw_combined + 15)

            # 2. Apply Noise Gate Cutoff
            if raw_combined < self.min_cutoff:
                target_pct = 0
            else:
                # Rescale from [min_cutoff, 100] -> [15, max_speed_cap]
                scaled = 15 + (raw_combined - self.min_cutoff) / (100.0 - self.min_cutoff) * (self.max_speed_cap - 15)
                target_pct = max(0, min(self.max_speed_cap, int(scaled)))

            # 3. Exponential Smoothing
            alpha = max(0.05, min(0.95, 1.0 - self.smoothing))
            self._smoothed_speed = self._smoothed_speed * (1.0 - alpha) + target_pct * alpha
            final_base_pct = int(round(self._smoothed_speed))

            # 4. Optional Rhythm Stroke Pulse Modulation & Apex Impact
            final_output_pct = final_base_pct
            if self.enable_rhythm_pulse and stroke_hz >= 0.5 and final_base_pct > 15:
                cos_phase = math.cos(2.0 * math.pi * stroke_phase)
                if is_thrusting and self.auto_thrust_apex_pulse:
                    # Thrusting mode: heavy contrast + sharp apex penetration impact
                    pulse_mod = 0.40 + 0.60 * (cos_phase + 1.0) * 0.5
                    apex_bonus = 12 if cos_phase > 0.82 else 0
                    final_output_pct = int(max(15, min(self.max_speed_cap, (final_base_pct + apex_bonus) * pulse_mod)))
                else:
                    pulse_mod = 0.55 + 0.45 * (cos_phase + 1.0) * 0.5
                    final_output_pct = int(max(15, min(self.max_speed_cap, final_base_pct * pulse_mod)))

            with self._lock:
                self.live_combined_pct = final_output_pct

            # 5. Build Channel Output List
            channel_speeds = []
            for ch_active in self.target_channels:
                channel_speeds.append(final_output_pct if ch_active else 0)

            # 6. Dispatch to Toy if speeds changed or periodic refresh
            if channel_speeds != last_sent_speeds:
                last_sent_speeds = list(channel_speeds)
                if self.on_speed_dispatch:
                    self.on_speed_dispatch(channel_speeds)

            # 7. Optional Feature Sync (Suction / Squeeze on Intense Thrusting)
            if self.enable_feature_sync and self.on_feature_dispatch:
                if final_output_pct > 75:
                    self._intense_duration += dt
                    if self._intense_duration > 1.2 and not self._feature_state_active:
                        self._feature_state_active = True
                        self.on_feature_dispatch("suck", 2)
                else:
                    self._intense_duration = max(0.0, self._intense_duration - dt * 2.0)
                    if self._intense_duration == 0.0 and self._feature_state_active:
                        self._feature_state_active = False
                        self.on_feature_dispatch("suck", 0)

            # 8. Send UI Telemetry
            if self.on_telemetry:
                self.on_telemetry({
                    "fps": fps,
                    "vision_pct": v_pct,
                    "audio_pct": a_pct,
                    "combined_pct": final_output_pct,
                    "stroke_hz": stroke_hz,
                    "target_info": target_info,
                    "beat_hit": beat_hit,
                    "act_type": act_type
                })

            time.sleep(0.033) # 30 FPS dispatch loop
