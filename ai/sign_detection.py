import os
import time
import threading
from collections import defaultdict
from difflib import SequenceMatcher

import cv2
import numpy as np
from ultralytics import YOLO


# Kelas hasil training yang secara langsung berkaitan dengan traffic light,
# supaya warna lampu (merah/kuning/hijau) yang menyala bisa ditambahkan lewat
# analisis warna pada kotak deteksinya (model hanya mengenali "ada lampu lalu
# lintas", bukan warnanya). Dicocokkan case-insensitive terhadap model.names.
_TRAFFIC_LIGHT_NAMES = {"lampu lalu lintas", "traffic light"}


class SignDetector:
    """Deteksi rambu & sinyal lalu lintas untuk keselamatan pengguna tunanetra.

    Memakai model YOLO custom (`model/rambu_yolo.pt`, YOLO26s, 40 kelas rambu
    lalu lintas Indonesia, mis. "Dilarang Parkir", "Lampu Lalu Lintas",
    "Penyebrangan Pejalan Kaki", "Polisi Tidur", dst). Path model bisa diganti
    lewat env `SIGN_MODEL_PATH`.

    Method utama: `analyze(frame, dedupe_key=None)` -> dict berisi `text`
    (ringkasan untuk diucapkan), `labels`, `confidence`, `reliable`,
    `duplicate`, `boxes` (tiap kotak: `box` [x, y, w, h], `label`,
    `confidence`), `processed_image`, `processing_ms`, `passes`, `warnings`.
    """

    def __init__(self, min_confidence=None):
        self.min_confidence = float(
            os.getenv("SIGN_MIN_CONFIDENCE", str(min_confidence if min_confidence is not None else 0.35))
        )
        self.reliable_confidence = float(os.getenv("SIGN_RELIABLE_CONFIDENCE", "0.50"))
        self.image_size = int(os.getenv("SIGN_IMAGE_SIZE", "640"))
        self.repeat_cooldown = float(os.getenv("SIGN_REPEAT_COOLDOWN", "5"))
        self.inference_lock = threading.Lock()

        ai_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(ai_dir)
        model_path = os.getenv("SIGN_MODEL_PATH", os.path.join(project_root, "model", "rambu_yolo.pt"))
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"Model deteksi rambu tidak ditemukan: {model_path}")

        self.model = YOLO(model_path)
        try:
            self.model.to(os.getenv("YOLO_DEVICE", "cpu"))
        except Exception:
            pass
        # dict {index: nama_kelas} bawaan model, sudah dalam Bahasa Indonesia
        # (mis. "Dilarang Parkir", "Lampu Lalu Lintas", "Jalur Penyebrangan").
        self.class_names = dict(self.model.names)

        self.last_results = []
        self.processing_fps = 0.0
        self._dedupe = defaultdict(lambda: {"text": "", "time": 0.0})
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Heuristik warna khusus untuk kelas "Lampu Lalu Lintas": model
    # mendeteksi keberadaan lampunya, warna yang menyala dianalisis di sini.
    # ------------------------------------------------------------------

    @staticmethod
    def _traffic_light_state(crop_bgr):
        if crop_bgr is None or not getattr(crop_bgr, "size", 0):
            return None
        hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
        h, w = hsv.shape[:2]
        if h < 6 or w < 3:
            return None
        thirds = [hsv[0:h // 3, :], hsv[h // 3:2 * h // 3, :], hsv[2 * h // 3:h, :]]
        scores = {
            "merah": float((cv2.inRange(thirds[0], (0, 90, 90), (10, 255, 255))
                            | cv2.inRange(thirds[0], (170, 90, 90), (180, 255, 255))).mean()),
            "kuning": float(cv2.inRange(thirds[1], (18, 90, 90), (35, 255, 255)).mean()),
            "hijau": float(cv2.inRange(thirds[2], (40, 60, 60), (90, 255, 255)).mean()),
        }
        best = max(scores, key=scores.get)
        return best if scores[best] > 12 else None

    def _detect_zebra_cross(self, frame):
        """Heuristik zebra cross (marka jalan): model belum dilatih untuk
        kelas ini, jadi tetap dideteksi terpisah lewat pola garis putih
        sejajar di bagian bawah frame."""
        h, w = frame.shape[:2]
        y0 = int(h * 0.55)
        band = frame[y0:h, :]
        if not band.size:
            return None
        gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        col_profile = binary.mean(axis=0) / 255.0
        stripe = col_profile > 0.5
        transitions = int(np.sum(stripe[1:] != stripe[:-1]))
        white_ratio = float(col_profile.mean())
        if transitions >= 6 and 0.25 <= white_ratio <= 0.75:
            ys, xs = np.where(binary > 0)
            if xs.size and ys.size:
                x1, x2 = int(xs.min()), int(xs.max())
                y1, y2 = y0 + int(ys.min()), y0 + int(ys.max())
                confidence = min(0.9, 0.35 + transitions * 0.03)
                return {"box": [x1, y1, max(1, x2 - x1), max(1, y2 - y1)],
                        "label": "Zebra Cross", "confidence": confidence}
        return None

    # ------------------------------------------------------------------
    # Entry point utama.
    # ------------------------------------------------------------------

    def analyze(self, frame, dedupe_key=None):
        """Deteksi rambu pada satu frame BGR; lihat docstring class untuk bentuk hasil."""
        start = time.time()
        try:
            if frame is None or not getattr(frame, "size", 0):
                raise ValueError("Frame deteksi rambu kosong")

            with self.inference_lock:
                results = self.model.predict(
                    frame, imgsz=self.image_size, conf=self.min_confidence, verbose=False
                )[0]

            detections = []
            for box in results.boxes:
                confidence = float(box.conf[0])
                if confidence < self.min_confidence:
                    continue
                cls_idx = int(box.cls[0])
                label = str(self.class_names.get(cls_idx, results.names.get(cls_idx, "Rambu")))
                x1, y1, x2, y2 = [max(0, int(round(v))) for v in box.xyxy[0].tolist()]
                x2, y2 = max(x1 + 1, x2), max(y1 + 1, y2)

                if label.strip().lower() in _TRAFFIC_LIGHT_NAMES:
                    crop = frame[y1:y2, x1:x2]
                    state = self._traffic_light_state(crop)
                    if state:
                        label = f"{label} {state}"

                detections.append({
                    "box": [x1, y1, x2 - x1, y2 - y1],
                    "label": label,
                    "confidence": confidence,
                })

            zebra = self._detect_zebra_cross(frame)
            if zebra:
                detections.append(zebra)

            labels = [d["label"] for d in detections]
            duplicate = self._duplicate_state(",".join(labels), dedupe_key)
            unique_labels = list(dict.fromkeys(labels))
            text = f"Terdeteksi: {', '.join(unique_labels)}." if unique_labels else ""
            confidence = (sum(d["confidence"] for d in detections) / len(detections)) if detections else 0.0
            reliable = bool(detections) and confidence >= self.reliable_confidence

            self.last_results = detections
            elapsed = max(1e-6, time.time() - start)
            self.processing_fps = 1.0 / elapsed
            annotated = self._draw_results(frame.copy(), detections)

            warnings = [] if detections else [
                "Rambu/sinyal lalu lintas belum terdeteksi jelas. Arahkan kamera ke rambu atau lampu lalu lintas."
            ]

            return {
                "text": text,
                "labels": unique_labels,
                "confidence": float(confidence),
                "reliable": reliable,
                "duplicate": duplicate,
                "boxes": detections,
                "processed_image": annotated,
                "processing_ms": int(elapsed * 1000),
                "passes": 1,
                "warnings": warnings,
            }
        except Exception as exc:
            print(f"Sign Detection Error: {exc}")
            return {
                "text": "",
                "confidence": 0.0,
                "reliable": False,
                "duplicate": False,
                "boxes": [],
                "processed_image": frame,
                "processing_ms": int(max(0.0, time.time() - start) * 1000),
                "error": str(exc),
            }

    def _duplicate_state(self, text, dedupe_key):
        if not dedupe_key or not text:
            return False
        now = time.time()
        with self._lock:
            if len(self._dedupe) > 256:
                self._dedupe.clear()
            prev = self._dedupe[str(dedupe_key)]
            similarity = SequenceMatcher(None, text.lower(), prev["text"].lower()).ratio() if prev["text"] else 0.0
            duplicate = similarity >= 0.90 and (now - prev["time"]) < self.repeat_cooldown
            if not duplicate:
                self._dedupe[str(dedupe_key)] = {"text": text, "time": now}
            return duplicate

    @staticmethod
    def _draw_results(frame, detections):
        for item in detections:
            x, y, w, h = item["box"]
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.putText(
                frame, f"{item['label']} ({item['confidence']:.2f})",
                (x, max(18, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2, cv2.LINE_AA,
            )
        return frame
