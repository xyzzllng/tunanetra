import threading
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

torch = pytest.importorskip("torch")
from ai.expression_detection import EmotionDetector
from ai.scene_segmentation import SceneDetector


def face_detector(rectangles):
    detector = EmotionDetector.__new__(EmotionDetector)
    detector.lock = threading.RLock()
    detector.center_x, detector.center_y = .4, .7
    detector.min_face_ratio, detector.min_neighbors, detector.face_max_width = .1, 6, 800
    detector.face_cascade = Mock(detectMultiScale=Mock(return_value=rectangles))
    detector._classify_crop = Mock(return_value={"emotion": "senang", "confidence": .9, "reliable": True})
    return detector


def test_face_only_central_primary():
    detector = face_detector([(10, 100, 80, 100), (270, 150, 100, 100), (490, 150, 100, 100)])
    result = detector.detect_faces(np.zeros((480, 640, 3), np.uint8))
    assert len(result) == 1 and result[0]["box"][0] == 270
    assert result[0]["face_confidence"] is None
    detector._classify_crop.assert_called_once()


def test_face_side_only_not_classified():
    detector = face_detector([(5, 120, 80, 80)])
    assert detector.detect_faces(np.zeros((480, 640, 3), np.uint8)) == []
    detector._classify_crop.assert_not_called()


def test_face_far_small_not_classified():
    detector = face_detector([(310, 240, 20, 20)])
    assert detector.detect_faces(np.zeros((480, 640, 3), np.uint8)) == []


def test_face_count_legacy_no_emotion_inference():
    detector = face_detector([(5, 100, 80, 100), (280, 200, 100, 100)])
    result = detector.detect_faces(np.zeros((480, 640, 3), np.uint8), primary_only=False, classify=False)
    assert len(result) == 2
    detector._classify_crop.assert_not_called()


def test_bundled_emotion_state_dict_and_blank_frame():
    torch.set_num_threads(2)
    detector = EmotionDetector()
    assert detector.detect_faces(np.zeros((480, 640, 3), np.uint8)) == []
    info = detector._classify_crop(np.zeros((96, 96, 3), np.uint8))
    assert 0 <= info["confidence"] <= 1 and "reliable" in info


def test_scene_uses_mask_not_bbox_for_corridor():
    detector = SceneDetector.__new__(SceneDetector)
    detector.model = SimpleNamespace(config=SimpleNamespace(id2label={0:"wall", 1:"floor"}))
    detector.confidence, detector.min_area, detector.labels = .65, .01, {"wall"}
    labels = np.zeros((100, 100), dtype=np.int32)
    labels[30:, 29:71] = 1  # a connected wall surrounds an unobstructed central region
    regions = detector.regions(labels, np.full((100, 100), .9), (480, 640))
    assert len(regions) == 1
    assert regions[0]["corridor_overlap"] == 0
    assert regions[0]["confidence_kind"] == "mean_pixel_probability"
