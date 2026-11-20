"""ADE20K semantic regions supplement COCO (which has no wall/door class).

Scores are mean winning pixel probabilities, NOT object detector confidences.
Masks remain low resolution to avoid allocating H*W*150 full-frame tensors.
"""
import os
import threading

import cv2
import numpy as np
import torch


class SceneDetector:
    def __init__(self):
        from transformers import SegformerImageProcessor, SegformerForSemanticSegmentation
        from config.settings import BASE_DIR

        path = os.getenv("SCENE_MODEL_PATH", str(BASE_DIR / "model" / "segformer-b0-ade"))
        self.processor = SegformerImageProcessor.from_pretrained(path, local_files_only=True)
        self.device = torch.device(os.getenv("SCENE_DEVICE", "cpu"))
        self.model = SegformerForSemanticSegmentation.from_pretrained(
            path, local_files_only=True, use_safetensors=True
        ).to(self.device).eval()
        self.confidence = float(os.getenv("SCENE_CONFIDENCE", "0.65"))
        self.min_area = float(os.getenv("SCENE_MIN_AREA_RATIO", "0.015"))
        self.labels = {"wall", "door", "screen door", "cabinet", "column", "fence",
                       "railing", "stairs", "stairway", "shelf", "box", "wardrobe"}
        self.lock = threading.Lock()

    def detect_boxes(self, frame):
        if frame is None or not frame.size:
            raise ValueError("Frame segmentasi kosong")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        with self.lock, torch.inference_mode():
            inputs = self.processor(images=rgb, return_tensors="pt")
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            probs = self.model(**inputs).logits.softmax(dim=1)[0]
            scores, classes = probs.max(dim=0)
            scores, classes = scores.cpu().numpy(), classes.cpu().numpy()
        return self.regions(classes, scores, frame.shape[:2])

    def regions(self, classes, scores, shape):
        h, w = shape
        mh, mw = classes.shape
        output = []
        for class_id in np.unique(classes):
            label = str(self.model.config.id2label[int(class_id)]).strip()
            if label not in self.labels:
                continue
            mask = ((classes == class_id) & (scores >= self.confidence)).astype(np.uint8)
            count, components, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
            for index in range(1, count):
                x, y, bw, bh, area = map(int, stats[index])
                ratio = area / float(mw * mh)
                if ratio < self.min_area:
                    continue
                pixels = components == index
                # Corridor occupancy is measured on the actual mask, not its
                # bounding rectangle (a wall can surround a clear doorway).
                corridor = pixels[int(mh * .35):, int(mw * .3):int(mw * .7)]
                overlap = float(corridor.sum()) / max(1, int(pixels.sum()))
                output.append({"label": label, "box": [round(x*w/mw), round(y*h/mh),
                               round(bw*w/mw), round(bh*h/mh)], "area_ratio": ratio,
                               "confidence": float(scores[pixels].mean()),
                               "confidence_kind": "mean_pixel_probability", "source": "segformer",
                               "corridor_overlap": overlap, "detection_pass": "semantic"})
        return sorted(output, key=lambda b: b["area_ratio"], reverse=True)[:12]
