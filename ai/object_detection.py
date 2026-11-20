from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import cv2
import numpy as np
import torch


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PassConfig:
    name: str
    confidence: float
    image_size: int
    small_object_confidence: float
    min_area_ratio: float
    min_box_px: int


class ObjectDetector:
    """YOLOv5 detector optimized for prompt-triggered camera snapshots.

    The browser sends OpenCV-decoded BGR frames. YOLOv5 AutoShape expects
    numpy/OpenCV arrays in RGB order, so inference explicitly converts BGR ->
    RGB before calling the model. The detector uses a light primary pass and
    higher-resolution recovery passes only when no object is found. Recovery
    never lowers the user's primary confidence threshold.

    This keeps normal CPU latency low while greatly improving recall for small,
    partially visible, or centered objects on phone/ESP32 camera snapshots.
    """

    def __init__(self):
        ai_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(ai_dir)
        model_path = os.getenv("YOLO_MODEL_PATH", os.path.join(project_root, "model", "yolov5n.pt"))
        yolov5_repo = os.getenv("YOLO_REPO_PATH", os.path.join(project_root, "model", "yolov5"))

        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"Model YOLOv5 tidak ditemukan: {model_path}")
        if not os.path.isfile(os.path.join(yolov5_repo, "hubconf.py")):
            raise FileNotFoundError(f"Source YOLOv5 lokal tidak ditemukan: {yolov5_repo}")

        # The bundled YOLOv5 revision imports the `ultralytics` package from
        # models/common.py. Fail fast with a useful message instead of letting
        # YOLOv5 silently run `pip install` during server startup.
        try:
            import ultralytics  # noqa: F401
        except Exception as exc:
            raise RuntimeError(
                "Dependency 'ultralytics' belum tersedia. Jalankan: pip install -r requirements.txt"
            ) from exc

        # Conservative primary floor; confidence remains the model's own score.
        self.confidence = float(os.getenv("YOLO_CONFIDENCE", "0.40"))
        self.high_confidence = float(os.getenv("YOLO_HIGH_CONFIDENCE", "0.65"))
        self.small_object_confidence = float(os.getenv("YOLO_SMALL_OBJECT_CONFIDENCE", "0.50"))
        self.iou_threshold = float(os.getenv("YOLO_IOU", "0.45"))
        self.image_size = int(os.getenv("YOLO_IMAGE_SIZE", "640"))
        self.max_detections = int(os.getenv("YOLO_MAX_DETECTIONS", "20"))
        self.min_area_ratio = float(os.getenv("YOLO_MIN_AREA_RATIO", "0.00003"))
        self.small_area_ratio = float(os.getenv("YOLO_SMALL_AREA_RATIO", "0.0010"))
        self.min_box_px = int(os.getenv("YOLO_MIN_BOX_PX", "4"))

        # Recovery increases input detail, never accepts lower-confidence boxes.
        self.adaptive_fallback = os.getenv("YOLO_ADAPTIVE_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
        self.fallback_confidence = max(self.confidence, float(os.getenv("YOLO_FALLBACK_CONFIDENCE", "0.40")))
        self.fallback_image_size = int(os.getenv("YOLO_FALLBACK_IMAGE_SIZE", "768"))
        self.fallback_small_object_confidence = max(self.small_object_confidence, float(os.getenv("YOLO_FALLBACK_SMALL_OBJECT_CONFIDENCE", "0.50")))
        self.fallback_min_area_ratio = float(os.getenv("YOLO_FALLBACK_MIN_AREA_RATIO", "0.00002"))
        self.fallback_min_box_px = int(os.getenv("YOLO_FALLBACK_MIN_BOX_PX", "3"))

        # Final recovery: zoom into the center only if both full-frame passes
        # are empty. This is useful when the user deliberately points the camera
        # at one object that occupies only a small part of a wide frame.
        self.center_crop_fallback = os.getenv("YOLO_CENTER_CROP_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
        self.center_crop_ratio = min(0.95, max(0.50, float(os.getenv("YOLO_CENTER_CROP_RATIO", "0.72"))))
        self.center_crop_image_size = int(os.getenv("YOLO_CENTER_CROP_IMAGE_SIZE", "768"))
        self.center_crop_confidence = max(self.confidence, float(os.getenv("YOLO_CENTER_CROP_CONFIDENCE", "0.40")))

        self.low_light_threshold = float(os.getenv("YOLO_LOW_LIGHT_THRESHOLD", "62"))
        self.low_light_enhance = os.getenv("YOLO_LOW_LIGHT_ENHANCE", "true").lower() in {"1", "true", "yes", "on"}
        self.warmup = os.getenv("YOLO_WARMUP", "false").lower() in {"1", "true", "yes", "on"}

        self.history_size = max(2, int(os.getenv("YOLO_HISTORY", "3")))
        self.stable_hits = max(1, int(os.getenv("YOLO_STABLE_HITS", "2")))
        self.track_iou = float(os.getenv("YOLO_TRACK_IOU", "0.25"))
        self._history = defaultdict(lambda: deque(maxlen=self.history_size))
        self._history_lock = threading.Lock()
        self._history_times = {}

        # model.conf/model.iou are mutable global attributes. The detector is
        # shared by all sessions, therefore each complete inference is locked.
        self._inference_lock = threading.Lock()
        self.last_diagnostics: dict = {}

        # Only the vendored YOLO checkpoint loader opts into trusted pickle.
        # Never monkey-patch global torch.load (it affects other threads/models).
        os.environ.setdefault("YOLOv5_AUTOINSTALL", "false")
        os.environ.setdefault("YOLO_AUTOINSTALL", "false")
        self.model = torch.hub.load(
            yolov5_repo,
            "custom",
            path=model_path,
            source="local",
            device=os.getenv("YOLO_DEVICE", "cpu"),
            _verbose=False,
        )

        self.model.conf = self.confidence
        self.model.iou = self.iou_threshold
        self.model.max_det = self.max_detections
        try:
            self.model.eval()
        except Exception:
            pass

        names = getattr(self.model, "names", None)
        class_count = len(names) if isinstance(names, (dict, list, tuple)) else "?"
        log.info(
            "Object detector aktif: model=%s classes=%s conf=%.2f fallback=%.2f imgsz=%s/%s RGB_input=true",
            os.path.basename(model_path),
            class_count,
            self.confidence,
            self.fallback_confidence,
            self.image_size,
            self.fallback_image_size,
        )

        if self.warmup:
            self._warmup_model()

    def _warmup_model(self):
        try:
            # Use RGB dummy just like real inference.
            dummy = np.zeros((self.image_size, self.image_size, 3), dtype=np.uint8)
            with self._inference_lock, torch.inference_mode():
                old_conf = float(getattr(self.model, "conf", self.confidence))
                self.model.conf = self.confidence
                try:
                    self.model(dummy, size=self.image_size)
                finally:
                    self.model.conf = old_conf
        except Exception as exc:
            log.debug("YOLO warmup dilewati: %s", exc)

    def _prepare_frame(self, img_data):
        if img_data is None or not getattr(img_data, "size", 0):
            raise ValueError("Frame object detection kosong")

        frame = img_data
        if self.low_light_enhance:
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if float(gray.mean()) < self.low_light_threshold:
                    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                    l, a, b = cv2.split(lab)
                    l = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(l)
                    frame = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
            except Exception:
                frame = img_data

        # Critical: local YOLOv5 AutoShape documentation explicitly expects
        # OpenCV input as cv2.imread(... )[:, :, ::-1] => RGB.
        try:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        except Exception:
            return frame[..., ::-1].copy()

    def _infer(self, img_data, *, confidence: float, image_size: int):
        rgb_frame = self._prepare_frame(img_data)
        with self._inference_lock, torch.inference_mode():
            old_conf = float(getattr(self.model, "conf", self.confidence))
            old_iou = float(getattr(self.model, "iou", self.iou_threshold))
            old_max_det = int(getattr(self.model, "max_det", self.max_detections))
            try:
                self.model.conf = float(confidence)
                self.model.iou = self.iou_threshold
                self.model.max_det = self.max_detections
                return self.model(rgb_frame, size=int(image_size))
            finally:
                self.model.conf = old_conf
                self.model.iou = old_iou
                self.model.max_det = old_max_det

    @staticmethod
    def _prediction_count(results) -> int:
        try:
            return int(len(results.pred[0]))
        except Exception:
            return 0

    def _to_boxes(
        self,
        results,
        *,
        confidence_floor: float,
        small_object_confidence: float,
        min_area_ratio: float,
        min_box_px: int,
        offset_x: int = 0,
        offset_y: int = 0,
        original_frame_area: float | None = None,
    ):
        boxes = []
        try:
            h, w = results.ims[0].shape[:2]
            result_frame_area = float(max(1, h * w))
        except Exception:
            result_frame_area = 1.0
        frame_area = float(original_frame_area or result_frame_area)

        predictions = getattr(results, "pred", None)
        if predictions is None or len(predictions) == 0:
            return boxes

        names = getattr(results, "names", getattr(self.model, "names", {}))
        for *xyxy, conf, cls in predictions[0]:
            confidence = float(conf)
            if confidence < confidence_floor:
                continue

            x1, y1, x2, y2 = [float(v) for v in xyxy]
            bw = max(0.0, x2 - x1)
            bh = max(0.0, y2 - y1)
            if min(bw, bh) < min_box_px:
                continue

            area_ratio = (bw * bh) / max(1.0, frame_area)
            if area_ratio < min_area_ratio:
                continue
            if area_ratio < self.small_area_ratio and confidence < small_object_confidence:
                continue

            try:
                label = str(names[int(cls)])
            except Exception:
                label = str(int(cls))

            boxes.append(
                {
                    "box": [
                        int(round(x1 + offset_x)),
                        int(round(y1 + offset_y)),
                        int(round(bw)),
                        int(round(bh)),
                    ],
                    "label": label,
                    "confidence": confidence,
                    "area_ratio": float(area_ratio),
                }
            )

        boxes.sort(key=lambda item: (item["confidence"], item["area_ratio"]), reverse=True)
        return boxes[: self.max_detections]

    def _run_pass(self, img_data, cfg: _PassConfig):
        results = self._infer(img_data, confidence=cfg.confidence, image_size=cfg.image_size)
        boxes = self._to_boxes(
            results,
            confidence_floor=cfg.confidence,
            small_object_confidence=cfg.small_object_confidence,
            min_area_ratio=cfg.min_area_ratio,
            min_box_px=cfg.min_box_px,
        )
        return boxes, results, self._prediction_count(results)

    def _center_crop(self, img_data):
        h, w = img_data.shape[:2]
        crop_w = max(32, int(round(w * self.center_crop_ratio)))
        crop_h = max(32, int(round(h * self.center_crop_ratio)))
        x1 = max(0, (w - crop_w) // 2)
        y1 = max(0, (h - crop_h) // 2)
        x2 = min(w, x1 + crop_w)
        y2 = min(h, y1 + crop_h)
        return img_data[y1:y2, x1:x2], x1, y1

    def _detect_adaptive(self, img_data, *, allow_fallback=True):
        h, w = img_data.shape[:2]
        original_area = float(max(1, h * w))
        diagnostics = {
            "frame_width": int(w),
            "frame_height": int(h),
            "primary_raw": 0,
            "fallback_raw": 0,
            "center_raw": 0,
            "selected_pass": "none",
        }

        primary_cfg = _PassConfig(
            "primary",
            self.confidence,
            self.image_size,
            self.small_object_confidence,
            self.min_area_ratio,
            self.min_box_px,
        )
        boxes, results, raw_count = self._run_pass(img_data, primary_cfg)
        diagnostics["primary_raw"] = raw_count
        if boxes:
            diagnostics["selected_pass"] = "primary"
            diagnostics["filtered_count"] = len(boxes)
            self.last_diagnostics = diagnostics
            return boxes, results, "primary", diagnostics

        if allow_fallback and self.adaptive_fallback:
            fallback_cfg = _PassConfig(
                "fallback",
                self.fallback_confidence,
                self.fallback_image_size,
                self.fallback_small_object_confidence,
                self.fallback_min_area_ratio,
                self.fallback_min_box_px,
            )
            boxes, results, raw_count = self._run_pass(img_data, fallback_cfg)
            diagnostics["fallback_raw"] = raw_count
            if boxes:
                diagnostics["selected_pass"] = "fallback"
                diagnostics["filtered_count"] = len(boxes)
                self.last_diagnostics = diagnostics
                return boxes, results, "fallback", diagnostics

        if allow_fallback and self.center_crop_fallback:
            crop, offset_x, offset_y = self._center_crop(img_data)
            results = self._infer(
                crop,
                confidence=self.center_crop_confidence,
                image_size=self.center_crop_image_size,
            )
            diagnostics["center_raw"] = self._prediction_count(results)
            boxes = self._to_boxes(
                results,
                confidence_floor=self.center_crop_confidence,
                small_object_confidence=max(self.center_crop_confidence, self.small_object_confidence),
                min_area_ratio=self.fallback_min_area_ratio,
                min_box_px=self.fallback_min_box_px,
                offset_x=offset_x,
                offset_y=offset_y,
                original_frame_area=original_area,
            )
            if boxes:
                diagnostics["selected_pass"] = "center_crop"
                diagnostics["filtered_count"] = len(boxes)
                self.last_diagnostics = diagnostics
                return boxes, results, "center_crop", diagnostics

        diagnostics["filtered_count"] = 0
        self.last_diagnostics = diagnostics
        return [], results, "none", diagnostics

    @staticmethod
    def _iou(box_a, box_b):
        ax, ay, aw, ah = box_a
        bx, by, bw, bh = box_b
        ax2, ay2 = ax + aw, ay + ah
        bx2, by2 = bx + bw, by + bh
        ix1, iy1 = max(ax, bx), max(ay, by)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
        inter = iw * ih
        union = aw * ah + bw * bh - inter
        return (inter / union) if union > 0 else 0.0

    def _stabilize(self, detections, stream_id):
        if not stream_id:
            return detections

        stream_id = str(stream_id)
        with self._history_lock:
            now = time.monotonic()
            for key in list(self._history_times):
                if now-self._history_times[key] > 2 or len(self._history_times) > 256:
                    self._history.pop(key, None)
                    self._history_times.pop(key, None)
            self._history_times[stream_id] = now
            previous_frames = list(self._history[stream_id])
            if not previous_frames:
                self._history[stream_id].append(detections or [])
                return [d for d in detections if d["confidence"] >= self.high_confidence or self.stable_hits == 1]

            stable = []
            for det in detections or []:
                if det["confidence"] >= self.high_confidence:
                    stable.append(det)
                    continue
                matched_frames = 0
                for frame_dets in previous_frames:
                    if any(
                        old["label"] == det["label"] and self._iou(old["box"], det["box"]) >= self.track_iou
                        for old in frame_dets
                    ):
                        matched_frames += 1
                if 1 + matched_frames >= self.stable_hits:
                    stable.append(det)

            self._history[stream_id].append(detections or [])
            return stable

    def detect_with_results(self, img_data, *, adaptive=True):
        boxes, results, pass_name, diagnostics = self._detect_adaptive(img_data, allow_fallback=adaptive)
        for item in boxes:
            item.setdefault("detection_pass", pass_name)
        return boxes, results, diagnostics

    def detect(self, img_data):
        """Compatibility API used by the original single-camera server."""
        boxes, results, _diagnostics = self.detect_with_results(img_data, adaptive=True)
        objects = [f"{item['label']} ({item['confidence']:.2f})" for item in boxes]
        return objects, results

    def detect_boxes(self, img_data, stream_id=None, stabilize=False, adaptive=True):
        boxes, _results, pass_name, _diagnostics = self._detect_adaptive(img_data, allow_fallback=adaptive)
        if stabilize and stream_id:
            boxes = self._stabilize(boxes, str(stream_id))
        for item in boxes:
            item.setdefault("detection_pass", pass_name)
        return boxes

    def diagnostics(self):
        return dict(self.last_diagnostics or {})

    @staticmethod
    def summarize(boxes, max_items=8):
        names = []
        for item in boxes or []:
            label = item.get("label")
            if label and label not in names:
                names.append(label)
            if len(names) >= max_items:
                break
        return names
