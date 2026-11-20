"""Standalone YOLOv5 diagnostic for Spider Sense.

Examples:
    python tools/check_object_detection.py
    python tools/check_object_detection.py --image C:\\path\\foto.jpg
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _print_boxes(path: Path, detector) -> bool:
    img = cv2.imread(str(path))
    if img is None:
        print(f"[ERROR] Gambar tidak dapat dibaca: {path}")
        return False

    boxes = detector.detect_boxes(img, stabilize=False, adaptive=True)
    diag = detector.diagnostics()
    print(f"\nImage : {path}")
    print(f"Shape : {img.shape[1]}x{img.shape[0]}")
    print(
        "YOLO  : selected_pass={pass_} primary_raw={p} fallback_raw={f} center_raw={c} filtered={n}".format(
            pass_=diag.get("selected_pass", "none"),
            p=diag.get("primary_raw", 0),
            f=diag.get("fallback_raw", 0),
            c=diag.get("center_raw", 0),
            n=len(boxes),
        )
    )
    if not boxes:
        print("Result: TIDAK ADA objek setelah semua recovery pass.")
        return False

    print("Result:")
    for item in boxes:
        print(
            "  - {label:<16} conf={conf:.3f} pass={pass_} box={box}".format(
                label=item.get("label", "?"),
                conf=float(item.get("confidence", 0.0)),
                pass_=item.get("detection_pass", "?"),
                box=item.get("box"),
            )
        )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="Path foto sendiri untuk dites")
    args = parser.parse_args()

    print("=== Spider Sense Object Detection Diagnostic ===")
    print("Python:", sys.version.split()[0])

    try:
        import torch
        print("Torch :", torch.__version__)
    except Exception as exc:
        print("[ERROR] torch tidak tersedia:", exc)
        return 2

    try:
        import ultralytics
        print("Ultralytics:", getattr(ultralytics, "__version__", "unknown"))
    except Exception as exc:
        print("[ERROR] package ultralytics tidak tersedia:", exc)
        print("Jalankan: pip install -r requirements.txt")
        return 2

    try:
        from ai.object_detection import ObjectDetector
        detector = ObjectDetector()
    except Exception as exc:
        print("[ERROR] ObjectDetector gagal diinisialisasi:")
        print(type(exc).__name__ + ":", exc)
        print("Cek model/yolov5n.pt, model/yolov5/, dependency, dan endpoint /api/health.")
        return 2

    images = []
    if args.image:
        images.append(Path(args.image).expanduser())
    else:
        images.extend(
            [
                ROOT / "model" / "yolov5" / "data" / "images" / "bus.jpg",
                ROOT / "model" / "yolov5" / "data" / "images" / "zidane.jpg",
            ]
        )

    tested = 0
    detected = 0
    for path in images:
        if not path.is_file():
            print(f"[SKIP] File tidak ditemukan: {path}")
            continue
        tested += 1
        detected += int(_print_boxes(path, detector))

    if not tested:
        print("[ERROR] Tidak ada gambar yang dapat dites.")
        return 2
    if detected:
        print("\nSUCCESS: model loading dan inference bekerja.")
        if detected < tested:
            print("Catatan: sebagian gambar tidak menghasilkan deteksi; uji foto kamera aktual dengan --image.")
        return 0

    print("\nFAIL: model berhasil dimuat tetapi semua gambar menghasilkan 0 deteksi.")
    print("Jika bus.jpg/zidane.jpg juga 0, masalah ada pada runtime/model, bukan kamera browser.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
