"""Print calibration JSON; do not infer real-world size from a class label."""
import argparse
import json


def main():
    p = argparse.ArgumentParser(description="Kalibrasi objek berukuran diketahui, posisi tegak/lurus ke kamera")
    p.add_argument("--camera-id", required=True, help="esp32cam (kamera utama). Kalibrasi lama untuk webcam TIDAK berlaku: ulangi dengan ESP32-CAM")
    p.add_argument("--label", required=True, help="Label model, misalnya bottle")
    p.add_argument("--width-cm", type=float, required=True)
    p.add_argument("--distance-cm", type=float, required=True)
    p.add_argument("--box-width-px", type=float, required=True)
    p.add_argument("--image-width", type=int, required=True)
    p.add_argument("--image-height", type=int, required=True)
    a = p.parse_args()
    if min(a.width_cm, a.distance_cm, a.box_width_px, a.image_width, a.image_height) <= 0:
        p.error("Semua ukuran harus positif")
    print(json.dumps({"camera_id": a.camera_id, "label": a.label, "real_width_cm": a.width_cm,
                      "focal_px": a.box_width_px*a.distance_cm/a.width_cm,
                      "image_width": a.image_width, "image_height": a.image_height}, indent=2))


if __name__ == "__main__":
    main()
