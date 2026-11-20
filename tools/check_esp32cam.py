#!/usr/bin/env python3
"""Uji ESP32-CAM tanpa menjalankan backend.

    python tools/check_esp32cam.py http://192.168.1.60
    python tools/check_esp32cam.py http://192.168.1.60 --token RAHASIA --frames 3 --save

Memeriksa /status, mengambil beberapa frame lewat adapter yang sama dengan
backend, lalu melaporkan resolusi, latensi, dan ketajaman. Dengan --save, frame
terakhir ditulis ke captured_images/esp32cam_check.jpg agar sudut pandang dapat
dicek dengan mata.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2

from camera import CameraError
from camera.esp32cam import Esp32Cam


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="mis. http://192.168.1.60")
    parser.add_argument("--token", default="")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    cam = Esp32Cam(args.url, token=args.token)
    info = cam.probe(timeout=3)
    print("/status :", info if info else "TIDAK terjangkau (periksa IP, Wi-Fi, token)")
    try:
        started = time.monotonic()
        frames = cam.capture_frames(args.frames)
    except CameraError as exc:
        print("GAGAL   :", exc)
        return 1
    elapsed = time.monotonic() - started
    height, width = frames[-1].shape[:2]
    gray = cv2.cvtColor(frames[-1], cv2.COLOR_BGR2GRAY)
    print(f"OK      : {len(frames)} frame {width}x{height} dalam {elapsed:.2f} dtk "
          f"(latensi terakhir {cam.get_status()['latency_ms']} ms)")
    print(f"          kecerahan {gray.mean():.0f}/255, ketajaman {cv2.Laplacian(gray, cv2.CV_64F).var():.0f}")
    if gray.mean() < 45:
        print("          PERINGATAN: gambar gelap; tambah cahaya atau set ESP32CAM_FLASH=true.")
    if args.save:
        target = ROOT / "captured_images" / "esp32cam_check.jpg"
        target.parent.mkdir(exist_ok=True)
        cv2.imwrite(str(target), frames[-1])
        print("          disimpan:", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
