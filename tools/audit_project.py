"""Read/parse every Python source, including the vendored training/export code."""
import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    files = sorted(ROOT.rglob("*.py"))
    for path in files:
        if any(p in {".venv", "__pycache__"} for p in path.parts):
            continue
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        classes = sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
        functions = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree))
        print(f"{path.relative_to(ROOT)} | {len(source.splitlines())} lines | {classes} classes | {functions} functions | SHA256 {hashlib.sha256(path.read_bytes()).hexdigest()}")
    print(f"Parsed {len(files)} Python files successfully.")


if __name__ == "__main__":
    main()
