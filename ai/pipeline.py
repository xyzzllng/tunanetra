"""Exactly one requested task; shared by browser and local-webcam servers."""
import os
import time
import cv2

from datetime import datetime

from ai.spatial import SpatialAnalyzer, consensus


# Warna kotak per mode (BGR), supaya sekilas kelihatan ini hasil deteksi apa.
_BOX_COLORS = {
    "objek": (60, 200, 60),      # hijau
    "ekspresi": (60, 160, 255),  # oranye
    "rambu": (255, 120, 40),     # biru
}


class VisionPipeline:
    def __init__(self, services):
        self.services = services
        self.spatial = SpatialAnalyzer()
        self.scene_enabled = os.getenv("SCENE_SEGMENTATION", "true").lower() in {"1", "true", "yes", "on"}
        self.burst_count = min(3, max(1, int(os.getenv("QUICK_OBJECT_FRAMES", "3"))))
        self.expression_frames = min(3, max(1, int(os.getenv("QUICK_EXPRESSION_FRAMES", "3"))))

        # Simpan snapshot dengan kotak deteksi tergambar (untuk dilihat manusia,
        # tidak dipakai alur AI/TTS). Nonaktif secara default supaya tidak
        # memenuhi disk tanpa diminta.
        self.save_detection_images = os.getenv("SAVE_DETECTION_IMAGES", "false").lower() in {"1", "true", "yes", "on"}
        self.detection_image_dir = os.getenv(
            "DETECTION_IMAGE_DIR",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "captured_images", "detections"),
        )
        # Batas jumlah file yang disimpan per mode, biar disk tidak membengkak
        # tanpa batas selama testing panjang. 0 = tidak dibatasi.
        self.detection_image_keep = max(0, int(os.getenv("DETECTION_IMAGE_KEEP", "200")))

    def _save_annotated(self, mode, frame, items):
        """Gambar kotak+label di atas salinan frame lalu simpan ke disk.

        `items` adalah list dict dengan key "box" ([x, y, w, h]) dan "label"
        (opsional "confidence"). Dipakai untuk objek maupun wajah/ekspresi
        karena keduanya sudah punya bentuk yang sama. Gagal simpan tidak
        boleh menggagalkan alur deteksi utama, jadi semua error ditelan.
        """
        if not self.save_detection_images:
            return None
        try:
            annotated = frame.copy()
            color = _BOX_COLORS.get(mode, (60, 200, 60))
            for item in items or []:
                box = item.get("box")
                if not box or len(box) != 4:
                    continue
                x, y, w, h = [int(round(v)) for v in box]
                cv2.rectangle(annotated, (x, y), (x + w, y + h), color, 2)
                label = str(item.get("label", ""))
                confidence = item.get("confidence")
                text = f"{label} {confidence:.2f}" if isinstance(confidence, (int, float)) else label
                if text:
                    text_y = max(0, y - 8)
                    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                    cv2.rectangle(annotated, (x, text_y - th - 4), (x + tw + 4, text_y + 2), color, -1)
                    cv2.putText(annotated, text, (x + 2, text_y - 2),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)

            os.makedirs(self.detection_image_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"{mode}_{stamp}.jpg"
            path = os.path.join(self.detection_image_dir, filename)
            cv2.imwrite(path, annotated)

            if self.detection_image_keep:
                self._prune_old_detection_images(mode)
            return path
        except Exception:
            log_module = __import__("logging").getLogger(__name__)
            log_module.debug("Gagal menyimpan gambar deteksi (%s)", mode, exc_info=True)
            return None

    def _prune_old_detection_images(self, mode):
        """Hapus file terlama untuk `mode` ini bila melewati DETECTION_IMAGE_KEEP."""
        try:
            entries = [
                os.path.join(self.detection_image_dir, name)
                for name in os.listdir(self.detection_image_dir)
                if name.startswith(f"{mode}_") and name.endswith(".jpg")
            ]
            entries.sort(key=os.path.getmtime)
            excess = len(entries) - self.detection_image_keep
            for path in entries[:max(0, excess)]:
                try:
                    os.remove(path)
                except OSError:
                    pass
        except Exception:
            pass

    def frame_count(self, mode):
        return self.burst_count if mode == "objek" else self.expression_frames if mode == "ekspresi" else 1

    @staticmethod
    def _quality(frame):
        small = cv2.resize(frame, (320, max(1, round(frame.shape[0]*320/frame.shape[1]))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return {"brightness": round(float(gray.mean()), 1),
                "sharpness": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1)}

    def run(self, mode, frames, *, camera_id="", sensor=None):
        if mode not in {"objek", "ekspresi", "rambu"}:
            raise ValueError("Mode harus objek, ekspresi, atau rambu")
        if not frames or any(f is None or not f.size for f in frames):
            raise ValueError("Snapshot kamera kosong")
        if any(f.shape != frames[0].shape for f in frames):
            raise ValueError("Resolusi semua frame dalam burst harus sama")
        start = time.monotonic()
        frame = frames[-1]
        h, w = frame.shape[:2]
        warnings, errors = [], {}
        quality = self._quality(frame)
        if quality["brightness"] < 45:
            warnings.append("Gambar gelap; tambah pencahayaan.")
        if quality["sharpness"] < float(os.getenv("IMAGE_BLUR_WARNING", "25")):
            warnings.append("Detail gambar rendah atau kabur; stabilkan kamera dan periksa fokus.")
        result = {"mode": mode, "frame_width": w, "frame_height": h, "detected_objects": [],
                  "faces": [], "sign_boxes": [], "sign_text": "", "sign_confidence": 0.0,
                  "sign_reliable": False, "warnings": warnings, "errors": errors, "quality": quality}
        if mode == "objek":
            raw_frames = []
            try:
                detector = self.services["object"]
                # Recovery is allowed only on the final frame. Confirmation
                # requires another frame at the normal threshold.
                for index, f in enumerate(frames[-self.burst_count:]):
                    raw_frames.append(detector.detect_boxes(f, stabilize=False,
                                      adaptive=index == len(frames[-self.burst_count:])-1))
                boxes = consensus(raw_frames, min_hits=min(2, len(raw_frames)))
                result["detection_diagnostics"] = detector.diagnostics()
                result["object_detection_available"] = True
            except Exception as exc:
                errors["object"] = str(exc)
                warnings.append("Model objek tidak tersedia; periksa dependency/model melalui diagnostik.")
                boxes = []
                result["object_detection_available"] = False
            if len(frames) == 1:
                warnings.append("Hanya satu snapshot; hasil belum dikonfirmasi antargambar.")
            if self.scene_enabled:
                try:
                    boxes += self.services["scene"].detect_boxes(frame)
                    result["scene_available"] = True
                except Exception as exc:
                    errors["scene"] = str(exc)
                    result["scene_available"] = False
                    warnings.append("Deteksi tembok/pintu tidak aktif. Jalankan tools/prepare_models.py --scene.")
            else:
                result["scene_available"] = False
                warnings.append("Segmentasi dimatikan; model COCO bawaan tidak mengenali tembok/pintu.")
            if self.spatial.calibration_error:
                warnings.append(self.spatial.calibration_error)
            boxes = self.spatial.annotate(boxes, w, h, camera_id)
            result["detected_objects"] = boxes
            result["sensor_distance"] = sensor
            result["result"] = self.spatial.summarize(boxes, sensor)
            if not result["scene_available"]:
                result["result"] += " Pengenalan tembok dan pintu tidak aktif."
            result["detection_image_path"] = self._save_annotated("objek", frame, boxes)
        elif mode == "ekspresi":
            detector = self.services["expression"]
            faces = (detector.detect_burst(frames[-self.expression_frames:]) if len(frames) > 1
                     else detector.detect_faces(frame, primary_only=True))
            result["faces"] = faces
            result["result"] = detector.summarize(faces)
            result["detection_image_path"] = self._save_annotated("ekspresi", frame, faces)
        else:
            info = self.services["sign"].analyze(frame)
            if info.get("error"):
                raise RuntimeError(info["error"])
            result.update(sign_text=info["text"], sign_confidence=info["confidence"],
                          sign_reliable=info["reliable"], sign_boxes=info["boxes"],
                          sign_labels=info.get("labels", []))
            result["result"] = (info["text"] if info["reliable"] else
                                "Rambu lalu lintas belum terdeteksi jelas. Dekatkan dan stabilkan kamera.")
            warnings.extend(info.get("warnings", []))
            result["detection_image_path"] = self._save_annotated("rambu", frame, info["boxes"])
        result["processing_ms"] = round((time.monotonic()-start)*1000)
        return result