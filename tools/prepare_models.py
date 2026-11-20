"""Explicit one-time download; inference itself never downloads model files."""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", action="store_true", help="Download SegFormer B0 ADE20K")
    args = parser.parse_args()
    args.scene = True          # satu-satunya model unduhan; YOLO/CNN/rambu sudah ada di model/
    failed = False
    if args.scene:
        try:
            from huggingface_hub import snapshot_download
            folder = os.getenv("SCENE_MODEL_PATH", str(ROOT / "model" / "segformer-b0-ade"))
            snapshot_download("nvidia/segformer-b0-finetuned-ade-512-512", local_dir=folder,
                allow_patterns=["config.json", "preprocessor_config.json", "model.safetensors", "README.md"],
                revision=os.getenv("SCENE_MODEL_REVISION", "main"))
            if not (Path(folder)/"model.safetensors").is_file():
                raise RuntimeError("model.safetensors tidak tersedia pada revisi ini")
            print("SegFormer siap:", folder)
        except Exception as exc:
            print("Gagal menyiapkan segmentasi:", exc)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
