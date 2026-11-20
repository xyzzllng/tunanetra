"""Conservative 2D path priority and optional calibrated pinhole estimates."""
import json
import math
import os
import statistics
from pathlib import Path


LABELS_ID = {"person": "manusia", "chair": "kursi", "dining table": "meja", "table": "meja",
            "wall": "tembok", "door": "pintu", "screen door": "pintu kasa", "couch": "sofa",
            "bed": "tempat tidur", "car": "mobil", "motorcycle": "sepeda motor", "bicycle": "sepeda",
            "truck": "truk", "bus": "bus", "bench": "bangku", "cabinet": "lemari",
            "wardrobe": "lemari", "box": "kotak", "stairs": "tangga", "stairway": "tangga",
            "column": "tiang", "fence": "pagar", "railing": "pagar pembatas", "shelf": "rak",
            "refrigerator": "kulkas", "suitcase": "koper", "dog": "anjing", "cat": "kucing"}
OBSTACLES = set(LABELS_ID)


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    inter = max(0, min(ax+aw, bx+bw)-max(ax, bx)) * max(0, min(ay+ah, by+bh)-max(ay, by))
    return inter / max(1, aw*ah + bw*bh - inter)


def consensus(frames, min_hits=2, threshold=.25):
    """Keep CURRENT detections only; one-to-one matches across a short burst.

    History never spans requests, sources, sessions or resolution changes.
    Confidence is the median model score, never multiplied by hit count.
    """
    if not frames:
        return []
    output = []
    used = [set() for _ in frames[:-1]]
    for current in sorted(frames[-1], key=lambda x: x["confidence"], reverse=True):
        matches = [current]
        for fi, previous in enumerate(frames[:-1]):
            candidates = [(iou(current["box"], old["box"]), oi, old) for oi, old in enumerate(previous)
                          if oi not in used[fi] and old["label"] == current["label"]]
            if candidates:
                score, oi, old = max(candidates, key=lambda x: x[0])
                if score >= threshold:
                    matches.append(old)
                    used[fi].add(oi)
        if len(matches) < min(min_hits, len(frames)):
            continue
        item = dict(current)
        item["confidence"] = statistics.median(x["confidence"] for x in matches)
        item["temporal_hits"] = len(matches)
        item["temporal_frames"] = len(frames)
        item["temporal_verified"] = len(matches) >= 2
        item["distance_box_width_px"] = statistics.median(x["box"][2] for x in matches)
        output.append(item)
    return output


class SpatialAnalyzer:
    def __init__(self):
        self.near_cm = float(os.getenv("OBSTACLE_WARNING_CM", "50"))
        self.large_area = float(os.getenv("OBSTACLE_LARGE_AREA", "0.08"))
        self.calibration = None
        self.calibration_error = None
        path = os.getenv("DISTANCE_CALIBRATION_FILE", "").strip()
        if path:
            try:
                from config.settings import BASE_DIR
                target = Path(path)
                self.calibration = json.loads((target if target.is_absolute() else BASE_DIR/target).read_text())
                for key in ("focal_px", "image_width", "image_height", "real_width_cm"):
                    value = float(self.calibration[key])
                    if not math.isfinite(value) or value <= 0:
                        raise ValueError(f"Kalibrasi {key} harus positif")
                    self.calibration[key] = value
                if not self.calibration.get("label") or not self.calibration.get("camera_id"):
                    raise ValueError("Kalibrasi membutuhkan label dan camera_id")
            except Exception as exc:
                self.calibration_error = f"Kalibrasi tidak berlaku: {exc}"
                self.calibration = None

    def distance(self, item, width, height, camera_id):
        c = self.calibration
        if not c or c["camera_id"] != camera_id or c["label"] != item["label"]:
            return None
        if item.get("source") == "segformer":
            return None  # A semantic region does not measure a complete object.
        x, y, bw, bh = item["box"]
        if min(x, y) <= 2 or x+bw >= width-2 or y+bh >= height-2 or bw < 12:
            return None
        if abs(width/height - c["image_width"]/c["image_height"]) > .02:
            return None
        focal = c["focal_px"] * width/c["image_width"]
        estimate = c["real_width_cm"] * focal / item.get("distance_box_width_px", bw)
        return round(estimate, 1) if math.isfinite(estimate) and estimate > 0 else None

    def annotate(self, boxes, width, height, camera_id=""):
        result = []
        for det in boxes:
            item = dict(det)
            x, y, bw, bh = item["box"]
            center = (x+bw/2) / width
            overlap = item.get("corridor_overlap")
            if overlap is None:
                intersection = max(0, min(x+bw, width*.7)-max(x, width*.3))
                intersection *= max(0, min(y+bh, height)-max(y, height*.35))
                overlap = intersection/max(1, bw*bh)
            area = item.get("area_ratio", bw*bh/(width*height))
            potential = overlap >= .15 and (item["label"] in OBSTACLES or area >= self.large_area)
            distance = self.distance(item, width, height, camera_id)
            item.update(label_id=LABELS_ID.get(item["label"], item["label"]),
                        position="kiri" if center < .3 else "kanan" if center > .7 else "tengah",
                        possible_obstacle=potential, distance_cm=distance,
                        distance_source="calibrated_known_width" if distance is not None else None,
                        priority="perhatian" if potential else "informasi")
            result.append(item)
        return sorted(result, key=lambda x: (x["possible_obstacle"], x.get("area_ratio", 0), x["confidence"]), reverse=True)

    def summarize(self, boxes, sensor=None):
        sentences = []
        if sensor:
            d = sensor["distance_cm"]
            prefix = "Perhatian, " if d <= self.near_cm else ""
            sentences.append(f"{prefix}sensor membaca halangan sekitar {d:.0f} cm; jenisnya belum diketahui.")
        seen = set()
        for item in boxes:
            key = (item['label_id'], item['position'])
            if key in seen:
                continue
            seen.add(key)
            qualifier = "Hati-hati, ada " if item["possible_obstacle"] else "Ada "
            position = 'depan' if item['position'] == 'tengah' else item['position']
            text = f"{qualifier}{item['label_id']} di {position}"
            if item["distance_cm"] is not None:
                text += f", perkiraan jarak {item['distance_cm']:.0f} cm berdasarkan kalibrasi"
            sentences.append(text + ".")
            if len(seen) >= 3:
                break
        if not boxes:
            sentences.append("Objek belum terlihat jelas. Hasil ini tidak memastikan jalur aman.")
        return " ".join(sentences)
