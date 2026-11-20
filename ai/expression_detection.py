import os
import threading

import cv2
import torch
import numpy as np
from PIL import Image
from torchvision import transforms


class EmotionDetector:
    def __init__(self):
        self.device = torch.device(os.getenv("EMOTION_DEVICE", "cpu"))
        self.labels = ['marah', 'jijik', 'takut', 'senang', 'sedih', 'kaget', 'netral']
        self.transform = transforms.Compose([
            transforms.Grayscale(),
            transforms.Resize((48, 48)),
            transforms.ToTensor()
        ])
        self.model = self._load_model()
        self.face_cascade = self._load_face_cascade()
        self.lock = threading.RLock()
        self.center_x = float(os.getenv("FACE_CENTER_WIDTH", "0.40"))
        self.center_y = float(os.getenv("FACE_CENTER_HEIGHT", "0.70"))
        self.min_face_ratio = float(os.getenv("FACE_MIN_WIDTH_RATIO", "0.10"))
        self.min_neighbors = int(os.getenv("FACE_MIN_NEIGHBORS", "6"))
        self.face_max_width = int(os.getenv("FACE_MAX_WIDTH", "800"))
        self.confidence_threshold = float(os.getenv("EMOTION_MIN_CONFIDENCE", "0.55"))
        self.margin_threshold = float(os.getenv("EMOTION_MIN_MARGIN", "0.15"))
        self.expressive_confidence = max(self.confidence_threshold, float(os.getenv("EMOTION_EXPRESSIVE_CONFIDENCE", "0.70")))
        self.expressive_margin = max(self.margin_threshold, float(os.getenv("EMOTION_EXPRESSIVE_MARGIN", "0.20")))
        self.min_contrast = float(os.getenv("FACE_MIN_CONTRAST", "10"))
        self.min_sharpness = float(os.getenv("FACE_MIN_SHARPNESS", "12"))

    def _load_face_cascade(self):
        """Muat Haar Cascade yang sudah ada di folder model/ (file ini
        sebelumnya tidak pernah dipakai - itu sebab deteksi wajah tidak
        pernah menghasilkan bounding box)."""
        ai_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(ai_dir)
        cascade_path = os.path.join(project_root, "model", "haarcascade_frontalface_default.xml")
        if not os.path.isfile(cascade_path):
            raise FileNotFoundError(f"Haar cascade tidak ditemukan: {cascade_path}")
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            raise RuntimeError(f"Gagal memuat Haar cascade dari: {cascade_path}")
        return cascade

    def _load_model(self):
        class SmallEmotionCNN(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Sequential(
                    torch.nn.Conv2d(1, 32, 3, padding=1),
                    torch.nn.ReLU(),
                    torch.nn.MaxPool2d(2),
                    torch.nn.Conv2d(32, 64, 3, padding=1),
                    torch.nn.ReLU(),
                    torch.nn.MaxPool2d(2),
                )
                self.fc = torch.nn.Sequential(
                    torch.nn.Linear(64 * 12 * 12, 128),
                    torch.nn.ReLU(),
                    torch.nn.Linear(128, 7)
                )

            def forward(self, x):
                x = self.conv(x)
                x = x.view(x.size(0), -1)
                return self.fc(x)

        model = SmallEmotionCNN().to(self.device)

        ai_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(ai_dir)
        model_path = os.path.join(project_root, "model", "emotion_model.pth")
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"Model emosi tidak ditemukan: {model_path}")

        state_dict = torch.load(model_path, map_location=self.device, weights_only=True)
        model.load_state_dict(state_dict)
        model.eval()
        return model

    def _classify_crop(self, img_data):
        if isinstance(img_data, Image.Image):
            img = img_data
        else:
            img = Image.fromarray(cv2.cvtColor(img_data, cv2.COLOR_BGR2RGB))

        img = img.convert('L')
        gray = np.asarray(img)
        small = cv2.resize(gray, (96, 96), interpolation=cv2.INTER_AREA)
        quality_ok = (min(gray.shape) >= 40 and 25 <= float(gray.mean()) <= 230
                      and float(gray.std()) >= self.min_contrast
                      and float(cv2.Laplacian(small, cv2.CV_64F).var()) >= self.min_sharpness)
        # Keep the original input distribution. Mild exposure correction is an
        # extra vote only in poor light, never a replacement for the raw crop.
        w, h = img.size
        dx, dy = max(1, round(w*.025)), max(1, round(h*.025))
        variants = [img, img.crop((dx, dy, w-dx, h-dy)),
                    img.crop((0, dy, w, h))]
        if float(gray.mean()) < 65 or float(gray.std()) < 25:
            enhanced = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(4, 4)).apply(gray)
            variants[-1] = Image.fromarray(enhanced)
        tensor = torch.stack([self.transform(v) for v in variants]).to(self.device)
        with self.lock, torch.inference_mode():
            votes = torch.softmax(self.model(tensor), dim=1).cpu().numpy()
        info = self._decision(votes.mean(axis=0))
        winner = int(np.argmax(votes.mean(axis=0)))
        agreement = int(np.sum(votes.argmax(axis=1) == winner))
        # Require raw-image agreement; enhancement must not create an emotion.
        reliable = info['reliable'] and quality_ok and agreement >= 2 and int(votes[0].argmax()) == winner
        info.update(reliable=bool(reliable), emotion=info['candidate_emotion'] if reliable else 'belum yakin',
                    quality_ok=bool(quality_ok), crop_agreement=agreement)
        return info

    def _decision(self, probabilities):
        probabilities = np.asarray(probabilities, dtype=float)
        indices = np.argsort(probabilities)[::-1]
        confidence = float(probabilities[indices[0]])
        margin = confidence - float(probabilities[indices[1]])
        label = self.labels[int(indices[0])]
        supported = label in {'netral', 'senang', 'sedih'}
        floor = self.confidence_threshold if label == 'netral' else self.expressive_confidence
        gap = self.margin_threshold if label == 'netral' else self.expressive_margin
        reliable = supported and confidence >= floor and margin >= gap
        # Seven logits remain necessary for the bundled weights. Never
        # renormalize three classes or relabel unsupported emotions as neutral.
        return {"emotion": label if reliable else "belum yakin", "candidate_emotion": label,
                "confidence": confidence, "margin": margin, "reliable": bool(reliable),
                "probabilities": probabilities.tolist(),
                "confidence_kind": "softmax_uncalibrated"}

    def detect_burst(self, frames):
        """Confirm the current face within this request only; no shared history."""
        from ai.spatial import iou
        observations = [self.detect_faces(frame, primary_only=True) for frame in frames[-3:]]
        if not observations or not observations[-1]:
            return []
        current = dict(observations[-1][0])
        matched = []
        # Only a contiguous track ending in the current frame can contribute.
        last_box = current['box']
        for faces in reversed(observations):
            if not faces or iou(last_box, faces[0]['box']) < .40:
                break
            matched.append(faces[0])
            last_box = faces[0]['box']
        if len(observations) > 1:
            vectors = [face['probabilities'] for face in matched if face.get('quality_ok')]
            if vectors:
                decision = self._decision(np.mean(vectors, axis=0))
                hits = sum(f.get('reliable') and f['emotion'] == decision['candidate_emotion'] for f in matched)
                reliable = (decision['reliable'] and hits >= 2 and current.get('reliable')
                            and current['emotion'] == decision['candidate_emotion'])
                current.update(decision, reliable=bool(reliable),
                               emotion=decision['candidate_emotion'] if reliable else 'belum yakin')
            else:
                current.update(reliable=False, emotion='belum yakin')
        current.update(temporal_frames=len(observations), temporal_hits=len(matched),
                       temporal_verified=bool(len(matched) >= 2 and current['reliable']))
        return [current]

    def detect(self, img_data):
        """Legacy API now locates a face first; never classifies the whole scene."""
        if isinstance(img_data, Image.Image):
            import numpy as np
            img_data = cv2.cvtColor(np.array(img_data.convert("RGB")), cv2.COLOR_RGB2BGR)
        faces = self.detect_faces(img_data)
        return faces[0]["emotion"] if faces else "wajah utama tidak ditemukan"

    def detect_faces(self, img_data, *, primary_only=True, classify=True):
        """Locate frontal faces and classify the central primary face by default."""
        frame = img_data
        if frame is None or not getattr(frame, "size", 0):
            raise ValueError("Frame wajah kosong")
        fh, fw = frame.shape[:2]
        scale = min(1.0, self.face_max_width / fw)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if scale < 1:
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        minimum = max(32, int(fw * self.min_face_ratio * scale)) if primary_only else 32
        with self.lock:
            faces = self.face_cascade.detectMultiScale(
                cv2.equalizeHist(gray), scaleFactor=1.1, minNeighbors=self.min_neighbors,
                minSize=(minimum, minimum))
        candidates = []
        for x, y, w, h in faces:
            x, y, w, h = [int(round(float(v) / scale)) for v in (x, y, w, h)]
            cx, cy = (x+w/2)/fw, (y+h/2)/fh
            if primary_only and (abs(cx-.5) > self.center_x/2 or abs(cy-.5) > self.center_y/2
                                 or w/fw < self.min_face_ratio):
                continue
            # Reject truncated faces; they destabilize the tiny expression CNN.
            if primary_only and (x <= 1 or y <= 1 or x+w >= fw-1 or y+h >= fh-1):
                continue
            candidates.append((x, y, w, h))
        candidates.sort(key=lambda b: (abs((b[0]+b[2]/2)/fw-.5), -b[2]*b[3]))
        if primary_only:
            candidates = candidates[:1]

        results = []
        for (x, y, w, h) in candidates:
            crop = frame[y:y + h, x:x + w]
            expression = self._classify_crop(crop) if classify and crop.size else {}
            results.append({
                "box": [int(x), int(y), int(w), int(h)],
                "label": "Face",
                "face_confidence": None,  # Haar minNeighbors is NOT a probability.
                "primary": primary_only,
                **expression,
            })
        return results

    @staticmethod
    def summarize(faces):
        if not faces:
            return "Wajah belum terlihat jelas. Hadapkan wajah ke tengah kamera."
        face = faces[0]
        if not face.get("reliable"):
            return "Ekspresi belum terlihat jelas."
        return f"Wajah terlihat {face['emotion']}."
