"""Build a clean source ZIP, preserving original bundled YOLO/emotion models."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--original", type=Path)
    args = parser.parse_args()
    excluded_dirs = {"__pycache__", ".pytest_cache", ".venv", ".git", ".cache", "node_modules"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(ROOT.rglob("*")):
            rel = file.relative_to(ROOT)
            if not file.is_file() or any(part in excluded_dirs for part in rel.parts):
                continue
            if rel.parts[:2] in {("model", "segformer-b0-ade")}:
                continue
            if rel.parts[0] in {"audio_storage", "captured_images"} and file.name != ".gitkeep":
                continue
            if file.name in {".env", "secrets.h"} or file.suffix in {".pyc", ".zip"}:
                continue
            archive.write(file, Path(ROOT.name)/rel)
    with zipfile.ZipFile(args.output) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        if args.original:
            with zipfile.ZipFile(args.original) as original:
                missing = [n for n in original.namelist() if not n.endswith("/") and n not in names]
                assert not missing, f"Original file paths missing: {missing}"
        print(f"Verified {len(names)} files, {args.output.stat().st_size} bytes: {args.output}")


if __name__ == "__main__":
    main()
