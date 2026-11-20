"""Real local inference smoke test. Does NOT validate distance or field accuracy."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.settings import settings
import cv2
import numpy as np
from ai.runtime import vision_services
from ai.pipeline import VisionPipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="Optional own image to test object/scene inference")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    image = cv2.imread(args.image or str(ROOT/"model/yolov5/data/images/bus.jpg"))
    if image is None:
        raise SystemExit("Tidak dapat membaca gambar")
    blank = np.full((480, 640, 3), 128, np.uint8)
    pipeline = VisionPipeline(vision_services())
    failures = []
    for iteration in range(max(1, args.repeat)):
        for mode, frame in (("objek", image), ("ekspresi", blank), ("rambu", image)):
            try:
                info = pipeline.run(mode, [frame])
                print(json.dumps({"iteration": iteration+1, "mode": mode, "ms": info["processing_ms"],
                      "result": info["result"], "objects": info["detected_objects"], "errors": info["errors"]}, ensure_ascii=False))
                if info["errors"]:
                    failures.append(mode)
                if mode == "objek" and not args.image and not info["detected_objects"]:
                    failures.append("bundled objects empty")
                if mode == "ekspresi" and info["faces"]:
                    failures.append("blank frame produced face")
            except Exception as exc:
                print(f"ERROR {mode}: {exc}")
                failures.append(mode)
    print("FAIL" if failures else "PASS", failures)
    print("Ini smoke test, bukan pengukuran akurasi, jangkauan 50 cm, atau kestabilan video nyata.")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
