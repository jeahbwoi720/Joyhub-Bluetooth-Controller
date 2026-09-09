"""
Joyhub AI Video Vision & Audio Synchronization Engine
Author: jeahbwoi720 / Remus
Hardware: Windows DirectML / DirectX 12 GPU Acceleration
OS: Windows 10/11

Provides real-time Computer Vision Optical Flow motion detection + WASAPI loopback audio sync
for ANY video playing globally on the PC (Chrome, Edge, Firefox, VLC, Windows Media Player, VaM mirror, etc.).
"""

import sys
import time
import math
import csv
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
except Exception:
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

# ==================== Neural Deep Learning Audio Classifier (YAMNet DirectML) ====================
class NeuralYamnetAudioClassifier:
    """
    True Deep Learning Audio Event Classifier running Google's YAMNet (AudioSet ONNX)
    accelerated with DirectML over DirectX 12.
    Classifies 521 audio classes from 16 kHz resampled waveforms in sub-1ms, recognizing:
      - Moan / Groan (AudioSet IDs: 22, 33)
      - Pant / Gasp / Breathing / Sigh (AudioSet IDs: 40, 39, 36, 23)
      - Slap / Smack (AudioSet ID: 461)
      - Speech / Whispering / Laughter (AudioSet IDs: 0, 1, 12, 13)
      - Music / Beat (AudioSet ID: 132)
    """
    def __init__(self, model_path: Optional[Path | str] = None, class_map_path: Optional[Path | str] = None):
        self.session = None
        self.active_provider = "None"
        self.inp_name = ""
        self.class_names: List[str] = []

        if not HAS_ONNX:
            return

        if model_path is None:
            candidates = [
                Path(__file__).parent / "yamnet.onnx",
                Path.cwd() / "yamnet.onnx",
            ]
            if getattr(sys, 'frozen', False):
                candidates.insert(0, Path(sys.executable).parent / "yamnet.onnx")
                if hasattr(sys, '_MEIPASS'):
                    candidates.insert(0, Path(sys._MEIPASS) / "yamnet.onnx")
            model_path = next((p for p in candidates if p.exists()), candidates[0])
        else:
            model_path = Path(model_path)

        if not model_path.exists():
            return

        if class_map_path is None:
            map_candidates = [
                Path(__file__).parent / "yamnet_class_map.csv",
                Path.cwd() / "yamnet_class_map.csv",
            ]
            if getattr(sys, 'frozen', False):
                map_candidates.insert(0, Path(sys.executable).parent / "yamnet_class_map.csv")
                if hasattr(sys, '_MEIPASS'):
                    map_candidates.insert(0, Path(sys._MEIPASS) / "yamnet_class_map.csv")
            class_map_path = next((p for p in map_candidates if p.exists()), map_candidates[0])
        else:
            class_map_path = Path(class_map_path)

        try:
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            providers = ['DmlExecutionProvider', 'CPUExecutionProvider']
            self.session = ort.InferenceSession(str(model_path), opts, providers=providers)
            self.active_provider = self.session.get_providers()[0]
            self.inp_name = self.session.get_inputs()[0].name
        except Exception:
            self.session = None

        try:
            if class_map_path.exists():
                with open(class_map_path, 'r', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    next(reader)
                    self.class_names = [r[2] for r in reader if len(r) > 2]
        except Exception:
            pass

        # AudioSet semantic indices
        self.idx_moan = [22, 33]           # Wail, moan; Groan
        self.idx_breath = [23, 36, 39, 40] # Sigh, Breathing, Gasp, Pant
        self.idx_slap = [461]              # Slap, smack
        self.idx_speech = [0, 1, 5, 12, 65]# Speech, Whispering
        self.idx_music = [132]             # Music
        self.idx_laughter = [13, 14]       # Laughter

    def is_available(self) -> bool:
        return self.session is not None

    def analyze(self, audio_44k: np.ndarray) -> Optional[Dict[str, any]]:
        if not self.session or len(audio_44k) < 16000:
            return None

        try:
            # Resample to 16 kHz using vectorized interpolation (< 0.5ms)
            num_samples = 15600 # standard 0.975s patch
            indices = np.linspace(0, len(audio_44k) - 1, num_samples)
            audio_16k = np.interp(indices, np.arange(len(audio_44k)), audio_44k).astype(np.float32)

            t0 = time.time()
            out = self.session.run(None, {self.inp_name: audio_16k})
            inference_ms = (time.time() - t0) * 1000

            scores = out[0]
            mean_scores = np.mean(scores, axis=0) if scores.ndim > 1 else scores

            p_moan = float(np.max(mean_scores[self.idx_moan])) if self.idx_moan else 0.0
            p_breath = float(np.max(mean_scores[self.idx_breath])) if self.idx_breath else 0.0
            p_slap = float(np.max(mean_scores[self.idx_slap])) if self.idx_slap else 0.0
            p_speech = float(np.max(mean_scores[self.idx_speech])) if self.idx_speech else 0.0
            p_music = float(np.max(mean_scores[self.idx_music])) if self.idx_music else 0.0

            top_idx = int(np.argmax(mean_scores))
            top_name = self.class_names[top_idx] if top_idx < len(self.class_names) else "Unknown"
            top_prob = float(mean_scores[top_idx])

            return {
                "top_class": top_name,
                "top_prob": top_prob,
                "p_moan": p_moan,
                "p_breath": p_breath,
                "p_slap": p_slap,
                "p_speech": p_speech,
                "p_music": p_music,
                "inference_ms": inference_ms
            }
        except Exception:
            return None

# ==================== Audio Semantic Classifier ====================
class AudioSemanticClassifier:
    """
    Real-Time Acoustic Semantic Classifier & Affect Recognition Engine.
    Extracts pitch (F0 via autocorrelation), Harmonics-to-Noise Ratio (HNR),
    Zero-Crossing Rate (ZCR), crest factor, and dual-band spectral energy.
    Cross-validates with YAMNet Deep Learning inferences.
    """
    def __init__(self, sr: int = 44100):
        self.sr = sr
        self._last_impact_time = 0.0
        self._moan_duration = 0.0
        self._breath_duration = 0.0

    def classify(self, samples: np.ndarray, dt: float = 0.023, yamnet_data: Optional[Dict[str, any]] = None) -> Dict[str, any]:
        rms = float(np.sqrt(np.mean(samples**2)))
        if rms < 0.003:
            self._moan_duration = max(0.0, self._moan_duration - dt * 2.0)
            self._breath_duration = max(0.0, self._breath_duration - dt * 2.0)
            return {
                "event": "silence",
                "label": "🤫 Silence",
                "f0": 0.0,
                "hnr": 0.0,
                "zcr": 0.0,
                "surge": 0,
                "is_moan": False,
                "is_impact": False,
                "is_breath": False
            }

        zcr = float(np.mean(np.abs(np.diff(np.signbit(samples)))))

        N = len(samples)
        fft_vals = np.abs(np.fft.rfft(samples)) / N
        freqs = np.fft.rfftfreq(N, 1.0 / self.sr)

        bass = float(np.mean(fft_vals[(freqs >= 25) & (freqs <= 220)]))
        vocal = float(np.mean(fft_vals[(freqs >= 250) & (freqs <= 1800)]))
        high = float(np.mean(fft_vals[(freqs >= 2000) & (freqs <= 8000)]))

        # Pitch tracking via autocorrelation
        min_lag = int(self.sr / 1100)
        max_lag = int(self.sr / 75)
        corr = np.correlate(samples, samples, mode='full')
        corr = corr[len(corr)//2:]
        peak_lag = min_lag + np.argmax(corr[min_lag:max_lag])
        corr_peak = float(corr[peak_lag] / max(1e-9, corr[0]))
        f0 = float(self.sr / peak_lag) if corr_peak > 0.32 else 0.0
        hnr = float(corr_peak / max(0.01, (1.0 - corr_peak)))

        peak_val = float(np.max(np.abs(samples)))
        crest_factor = peak_val / max(1e-5, rms)
        now = time.time()

        is_impact = False
        is_moan = False
        is_breath = False
        surge = 0

        # Baseline DSP event classification
        if crest_factor > 4.5 and high > vocal * 0.7 and rms > 0.030 and (now - self._last_impact_time > 0.18):
            is_impact = True
            self._last_impact_time = now
            event = "impact"
            label = "💥 Spank / Impact"
            surge = 35
        elif f0 >= 280 and hnr > 1.40 and vocal > 0.002:
            is_moan = True
            self._moan_duration += dt
            event = "moan"
            label = f"💋 Moan ({int(round(f0))} Hz)"
            pitch_boost = min(20, int((f0 - 280) / 16.0))
            dur_boost = min(15, int(self._moan_duration * 12))
            surge = pitch_boost + dur_boost
        elif f0 >= 80 and f0 < 280 and hnr > 1.25 and vocal > 0.002:
            self._moan_duration = max(0.0, self._moan_duration - dt)
            event = "dialogue"
            label = "🗣️ Voice / Dialogue"
        elif zcr > 0.15 and hnr < 1.15 and rms > 0.007:
            is_breath = True
            self._breath_duration += dt
            self._moan_duration = max(0.0, self._moan_duration - dt)
            event = "panting"
            label = "😮‍💨 Heavy Panting"
            surge = 8
        elif bass > vocal * 1.4 and bass > 0.004:
            self._moan_duration = max(0.0, self._moan_duration - dt)
            event = "music"
            label = "🎵 Music / Beat"
        else:
            self._moan_duration = max(0.0, self._moan_duration - dt)
            event = "ambient"
            label = "✨ Whispers / Tease"

        # Cross-validate with Neural Deep Learning (YAMNet DirectML)
        if yamnet_data:
            p_moan = yamnet_data.get("p_moan", 0.0)
            p_slap = yamnet_data.get("p_slap", 0.0)
            p_breath = yamnet_data.get("p_breath", 0.0)
            p_speech = yamnet_data.get("p_speech", 0.0)
            p_music = yamnet_data.get("p_music", 0.0)

            if p_moan > 0.18:
                is_moan = True
                self._moan_duration += dt
                event = "moan"
                if f0 > 0:
                    label = f"💋 Moan ({int(round(f0))} Hz)"
                else:
                    label = f"💋 Moan (AI: {int(p_moan*100)}%)"
                surge = max(surge, int(p_moan * 28))
            elif p_slap > 0.18 and (now - self._last_impact_time > 0.18):
                is_impact = True
                self._last_impact_time = now
                event = "impact"
                label = "💥 Spank / Impact (AI)"
                surge = 35
            elif p_breath > 0.22 and event not in ["moan", "impact"]:
                is_breath = True
                event = "panting"
                label = f"😮‍💨 Panting (AI: {int(p_breath*100)}%)"
                surge = max(surge, 8)
            elif p_speech > 0.35 and event not in ["moan", "impact", "panting"]:
                event = "dialogue"
                label = "🗣️ Voice / Dialogue (AI)"
            elif p_music > 0.38 and event not in ["moan", "impact", "panting"]:
                event = "music"
                label = "🎵 Music / Beat (AI)"

        return {
            "event": event,
            "label": label,
            "f0": f0,
            "hnr": hnr,
            "zcr": zcr,
            "surge": surge,
            "is_moan": is_moan,
            "is_impact": is_impact,
            "is_breath": is_breath
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
        self.max_speed_cap = 25         # 10% to 100%
        self.smoothing = 0.35           # 0.0 (raw snappy) to 0.85 (ultra smooth)
        
        self.target_channels = [True, True, True, True] # Channels 1, 2, 3, 4
        self.enable_rhythm_pulse = True # Modulate amplitude with detected stroke rhythm
        self.enable_feature_sync = False # Auto suction/squeeze on climax thrusting
        self.enable_audio_boost = True  # Dynamic moan intensity surge & spank kick hits

        # Neural AI Pose & Act Sync
        self.ai_engine_mode = "🧠 Neural AI Pose & Act Sync"
        self.auto_suction_on_oral = True
        self.auto_thrust_apex_pulse = True
        self.neural_analyzer = NeuralPoseAnalyzer()

        # Semantic Audio Context Analyzer (DSP + Deep Neural AI YAMNet)
        self.audio_classifier = AudioSemanticClassifier(sr=44100)
        self.neural_audio_classifier = NeuralYamnetAudioClassifier()
        self._rolling_audio_buf = np.zeros(0, dtype=np.float32)
        self._last_yamnet_time = 0.0
        self._latest_yamnet_res: Optional[Dict[str, any]] = None

        # Live thread-safe telemetry & metrics
        self._lock = threading.Lock()
        self.live_vision_raw = 0.0
        self.live_vision_pct = 0
        self.live_audio_raw = 0.0
        self.live_audio_pct = 0
        self.live_combined_pct = 0
        self.live_stroke_hz = 0.0
        self.live_stroke_phase = 0.0
        self.live_audio_hz = 0.0
        self.live_audio_bpm = 0
        self.live_audio_phase = 0.0
        self.live_rhythm_source = "idle"
        self.live_fps = 0.0
        self.live_target_info = "Standby"
        self.live_beat_hit = False
        self.live_act_type = "👀 Scene Motion"
        self.live_is_thrusting = False
        self.live_is_oral = False
        self.live_audio_context = "🤫 Silence"
        self.live_audio_event = "silence"
        self.live_audio_surge = 0
        self.live_audio_is_moan = False
        self.live_audio_is_impact = False
        self.live_audio_is_breath = False

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

            # If user selected Audio Only mode, bypass screen capture & inference to save CPU/GPU
            if "Audio Only" in self.fusion_mode:
                prev_gray = None
                with self._lock:
                    self.live_vision_raw = 0.0
                    self.live_vision_pct = 0
                    self.live_target_info = "Audio Only Active"
                    self.live_fps = 0.0
                time.sleep(0.05)
                continue

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

                mic = None
                try:
                    mic = sc.get_microphone(id=str(speaker.id), include_loopback=True)
                except Exception:
                    mic = None

                if not mic:
                    # Fallback: search all loopback microphones
                    try:
                        for m in sc.all_microphones(include_loopback=True):
                            if getattr(m, 'isloopback', False) and (speaker.name in m.name or m.name in speaker.name):
                                mic = m
                                break
                    except Exception:
                        mic = None

                if not mic:
                    time.sleep(1.0)
                    continue

                with mic.recorder(samplerate=44100, blocksize=1024) as rec:
                    prev_bass = 0.0
                    prev_mid = 0.0
                    flux_hist = []
                    last_beat_time = 0.0
                    recent_intervals = []

                    while not self._stop_event.is_set():
                        # Read 1024 frames (~23.2 ms of audio)
                        data = rec.record(numframes=1024)
                        if data is None or len(data) == 0:
                            time.sleep(0.02)
                            continue

                        now = time.time()

                        # Convert stereo to mono
                        mono = np.mean(data, axis=1) if data.ndim > 1 else data
                        
                        # RMS amplitude
                        rms = float(np.sqrt(np.mean(mono**2)))

                        # Fast Fourier Transform (FFT) for rhythm & frequency band isolation (normalized)
                        fft_vals = np.abs(np.fft.rfft(mono)) / len(mono)
                        freqs = np.fft.rfftfreq(len(mono), 1.0 / 44100)

                        # Dual-band frequency analysis:
                        # Bass band: 25 Hz to 220 Hz (kicks, low-end transients, basslines)
                        # Mid band: 220 Hz to 1600 Hz (percussive claps/snares, moans, vocal rhythm)
                        bass_mask = (freqs >= 25) & (freqs <= 220)
                        mid_mask = (freqs >= 220) & (freqs <= 1600)
                        bass_energy = float(np.mean(fft_vals[bass_mask])) if np.any(bass_mask) else 0.0
                        mid_energy = float(np.mean(fft_vals[mid_mask])) if np.any(mid_mask) else 0.0

                        # Spectral Flux (Onset Detection Function)
                        flux = max(0.0, bass_energy - prev_bass) * 0.75 + max(0.0, mid_energy - prev_mid) * 0.25
                        prev_bass = bass_energy
                        prev_mid = mid_energy

                        flux_hist.append(flux)
                        if len(flux_hist) > 40: # ~900 ms rolling statistical window
                            flux_hist.pop(0)

                        mu_flux = float(np.mean(flux_hist))
                        std_flux = float(np.std(flux_hist))
                        onset_threshold = mu_flux + 1.25 * std_flux + 0.0015

                        # Beat transient detection
                        is_beat = False
                        if flux > onset_threshold and (now - last_beat_time) > 0.22:
                            is_beat = True
                            if last_beat_time > 0:
                                dt = now - last_beat_time
                                if 0.26 <= dt <= 1.65: # Valid BPM range: 36 BPM to 230 BPM
                                    recent_intervals.append(dt)
                                    if len(recent_intervals) > 10:
                                        recent_intervals.pop(0)
                            last_beat_time = now

                        # Estimate Audio BPM & Hz from interval clusters
                        audio_bpm = 0
                        audio_hz = 0.0
                        audio_phase = 0.0

                        if (now - last_beat_time) > 2.2:
                            # Silence or no rhythmic beat for 2.2s -> idle
                            recent_intervals.clear()
                        elif len(recent_intervals) >= 3:
                            # Harmonic folding: fold fast eighth-notes into fundamental quarter-note beat
                            normalized_dts = []
                            for dt_val in recent_intervals:
                                if dt_val < 0.32 and (dt_val * 2.0) <= 1.6:
                                    normalized_dts.append(dt_val * 2.0)
                                else:
                                    normalized_dts.append(dt_val)

                            med_dt = float(np.median(normalized_dts))
                            inliers = [x for x in normalized_dts if abs(x - med_dt) <= 0.28 * med_dt]
                            best_dt = float(np.mean(inliers)) if inliers else med_dt
                            if best_dt > 0.1:
                                audio_bpm = int(round(60.0 / best_dt))
                                audio_hz = round(1.0 / best_dt, 1)
                                audio_phase = ((now - last_beat_time) * audio_hz) % 1.0

                        # Normalize audio percent (0 - 100%) with dynamic response
                        composite_energy = (rms * 0.45 + bass_energy * 3.5 + mid_energy * 1.5)
                        audio_val = composite_energy * self.sensitivity_audio * 350.0
                        audio_pct = max(0, min(100, int(audio_val)))

                        # Maintain rolling buffer for Deep Learning YAMNet inference (~1 sec window = 44100 samples)
                        self._rolling_audio_buf = np.append(self._rolling_audio_buf, mono)
                        if len(self._rolling_audio_buf) > 44100:
                            self._rolling_audio_buf = self._rolling_audio_buf[-44100:]

                        # Run YAMNet Deep Neural Network every ~220ms
                        if self.neural_audio_classifier.is_available() and (now - self._last_yamnet_time > 0.22) and len(self._rolling_audio_buf) >= 22050:
                            self._last_yamnet_time = now
                            self._latest_yamnet_res = self.neural_audio_classifier.analyze(self._rolling_audio_buf)

                        # Real-Time Semantic Audio Context & Affect Classification (fused with Deep Learning)
                        audio_sem = self.audio_classifier.classify(mono, dt=0.023, yamnet_data=self._latest_yamnet_res)

                        with self._lock:
                            self.live_audio_raw = bass_energy
                            self.live_audio_pct = audio_pct
                            self.live_beat_hit = is_beat
                            self.live_audio_bpm = audio_bpm
                            self.live_audio_hz = audio_hz
                            self.live_audio_phase = audio_phase
                            self.live_audio_context = audio_sem["label"]
                            self.live_audio_event = audio_sem["event"]
                            self.live_audio_surge = audio_sem["surge"]
                            self.live_audio_is_moan = audio_sem["is_moan"]
                            self.live_audio_is_impact = audio_sem["is_impact"]
                            self.live_audio_is_breath = audio_sem["is_breath"]

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
                motion_hz = self.live_stroke_hz
                motion_phase = self.live_stroke_phase
                audio_hz = self.live_audio_hz
                audio_bpm = self.live_audio_bpm
                audio_phase = self.live_audio_phase
                fps = self.live_fps
                target_info = self.live_target_info
                beat_hit = self.live_beat_hit
                act_type = self.live_act_type
                is_thrusting = self.live_is_thrusting
                is_oral = self.live_is_oral
                audio_context = self.live_audio_context
                audio_event = self.live_audio_event
                audio_surge = self.live_audio_surge
                is_audio_moan = self.live_audio_is_moan
                is_audio_impact = self.live_audio_is_impact
                is_audio_breath = self.live_audio_is_breath

            # If Moan & Impact Surge is deactivated, suppress moan & impact events completely
            if not self.enable_audio_boost:
                if is_audio_moan or is_audio_impact or audio_event in ["moan", "impact"]:
                    is_audio_moan = False
                    is_audio_impact = False
                    audio_surge = 0
                    audio_event = "ambient" if a_pct > 3 else "silence"
                    audio_context = "🎵 Audio: Active" if a_pct > 3 else "🎵 Audio: Listening..."

            # Determine Active Rhythm & Driver (Motion vs. Audio)
            active_hz = 0.0
            active_phase = 0.0
            rhythm_source = "idle"

            if "Audio Only" in self.fusion_mode:
                if audio_hz >= 0.5:
                    active_hz = audio_hz
                    active_phase = audio_phase
                    rhythm_source = "audio"
            elif "Vision Only" in self.fusion_mode:
                if motion_hz >= 0.5:
                    active_hz = motion_hz
                    active_phase = motion_phase
                    rhythm_source = "motion"
            else: # "👁️ + 🎵 Vision & Audio Blend"
                if motion_hz >= 0.5:
                    active_hz = motion_hz
                    active_phase = motion_phase
                    rhythm_source = "motion"
                elif audio_hz >= 0.5:
                    active_hz = audio_hz
                    active_phase = audio_phase
                    rhythm_source = "audio"

            # 1. Multi-Modal Fusion
            effective_audio_surge = audio_surge if self.enable_audio_boost else 0
            if "Vision Only" in self.fusion_mode:
                raw_combined = v_pct
            elif "Audio Only" in self.fusion_mode:
                raw_combined = min(100, a_pct + effective_audio_surge)
                if beat_hit:
                    raw_combined = min(100, raw_combined + 20)
                if is_audio_impact and self.enable_audio_boost:
                    raw_combined = 100
            else: # "👁️ + 🎵 Vision & Audio Blend" or "👁️ + 🎵 Blend"
                # Blend 60% visual motion + 40% audio energy, with beat accent
                raw_combined = int(v_pct * 0.60 + a_pct * 0.40)
                if beat_hit:
                    raw_combined = min(100, raw_combined + 15)
                if effective_audio_surge > 0:
                    raw_combined = min(100, raw_combined + int(effective_audio_surge * 0.75))
                if is_audio_impact and self.enable_audio_boost:
                    raw_combined = min(100, max(raw_combined + 35, 90))

            # 2. Apply Noise Gate Cutoff
            if raw_combined < self.min_cutoff:
                target_pct = 0
            else:
                # Rescale from [min_cutoff, 100] -> [min_floor, max_speed_cap]
                min_floor = min(15, self.max_speed_cap)
                if self.max_speed_cap > min_floor:
                    scaled = min_floor + (raw_combined - self.min_cutoff) / (100.0 - self.min_cutoff) * (self.max_speed_cap - min_floor)
                else:
                    scaled = self.max_speed_cap
                target_pct = max(0, min(self.max_speed_cap, int(scaled)))

            # 3. Exponential Smoothing
            alpha = max(0.05, min(0.95, 1.0 - self.smoothing))
            self._smoothed_speed = self._smoothed_speed * (1.0 - alpha) + target_pct * alpha
            final_base_pct = int(round(self._smoothed_speed))

            # 4. Optional Rhythm Stroke Pulse Modulation & Apex Impact
            final_output_pct = final_base_pct
            min_floor = min(15, self.max_speed_cap)
            if self.enable_rhythm_pulse and active_hz >= 0.5 and final_base_pct >= min_floor:
                cos_phase = math.cos(2.0 * math.pi * active_phase)
                if is_thrusting and self.auto_thrust_apex_pulse and rhythm_source == "motion":
                    # Thrusting mode: heavy contrast + sharp apex penetration impact
                    pulse_mod = 0.40 + 0.60 * (cos_phase + 1.0) * 0.5
                    apex_bonus = max(1, int(self.max_speed_cap * 0.12)) if cos_phase > 0.82 else 0
                    final_output_pct = int(max(min_floor, min(self.max_speed_cap, (final_base_pct + apex_bonus) * pulse_mod)))
                elif rhythm_source == "audio":
                    # Audio rhythm mode: punchy beat pulse aligned with audio BPM
                    pulse_mod = 0.50 + 0.50 * max(0.0, (cos_phase + 1.0) * 0.5) ** 1.3
                    final_output_pct = int(max(min_floor, min(self.max_speed_cap, final_base_pct * pulse_mod)))
                else:
                    pulse_mod = 0.55 + 0.45 * (cos_phase + 1.0) * 0.5
                    final_output_pct = int(max(min_floor, min(self.max_speed_cap, final_base_pct * pulse_mod)))

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

            # 7. Optional Feature Sync (Suction / Squeeze on Intense Thrusting or Climax Moans)
            if self.enable_feature_sync and self.on_feature_dispatch:
                if final_output_pct > 75 or (self.enable_audio_boost and is_audio_moan and effective_audio_surge > 20):
                    self._intense_duration += dt
                    if self._intense_duration > 1.2 and not self._feature_state_active:
                        self._feature_state_active = True
                        self.on_feature_dispatch("suck", 2)
                else:
                    self._intense_duration = max(0.0, self._intense_duration - dt * 2.0)
                    if self._intense_duration == 0.0 and self._feature_state_active:
                        self._feature_state_active = False
                        self.on_feature_dispatch("suck", 0)

            # 8. Send UI Telemetry with Multi-Modal Semantic Act Fusion
            if "Audio Only" in self.fusion_mode:
                if audio_bpm > 0 and audio_event == "music":
                    display_act = f"🎵 {audio_bpm} BPM Beat"
                elif is_audio_moan or is_audio_impact:
                    display_act = audio_context
                elif audio_event in ["panting", "dialogue"]:
                    display_act = audio_context
                elif a_pct > 3:
                    display_act = "🎵 Audio: Active"
                else:
                    display_act = "🎵 Audio: Listening..."
            elif "Vision Only" in self.fusion_mode:
                display_act = act_type
            else: # "👁️ + 🎵 Blend"
                # Multi-modal fusion combinations
                if is_thrusting and is_audio_moan:
                    display_act = f"🔥 Climax Thrusting & {audio_context}"
                elif is_oral and is_audio_breath:
                    display_act = "👅 Oral Sucking & Panting"
                elif ("Stroke" in act_type or is_thrusting) and is_audio_impact:
                    display_act = "💥 Spanking & Impact"
                elif is_audio_moan:
                    display_act = audio_context
                elif is_audio_impact:
                    display_act = "💥 Spank / Impact"
                elif is_audio_breath and "Scene" in act_type:
                    display_act = "😮‍💨 Heavy Panting"
                elif act_type != "👀 Scene Motion" and act_type != "👀 Solo Action":
                    display_act = act_type
                elif audio_event in ["moan", "panting", "impact", "dialogue"]:
                    display_act = audio_context
                elif audio_bpm > 0 and audio_event == "music":
                    display_act = f"🎵 {audio_bpm} BPM Beat"
                else:
                    display_act = act_type

            if self.on_telemetry:
                self.on_telemetry({
                    "fps": fps,
                    "vision_pct": v_pct,
                    "audio_pct": a_pct,
                    "combined_pct": final_output_pct,
                    "stroke_hz": active_hz,
                    "motion_hz": motion_hz,
                    "audio_hz": audio_hz,
                    "audio_bpm": audio_bpm,
                    "rhythm_source": rhythm_source,
                    "target_info": target_info,
                    "beat_hit": beat_hit,
                    "act_type": display_act,
                    "audio_context": audio_context
                })

            time.sleep(0.033) # 30 FPS dispatch loop
