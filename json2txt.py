from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


HEADER_COLS = [
    "layer_type","name","left","top","width","height","type","opacity","visible",
    "layer_id","group_layer_id","base","images",""
]


def load_json_any(path: Path):
    """Try common encodings for JSON files."""
    b = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "utf-16le", "utf-8"):
        try:
            return json.loads(b.decode(enc))
        except Exception:
            pass
    return json.loads(b.decode("latin1"))


def extract(doc):
    """
    Supports:
      - dict with keys: width, height, layers (list of dicts)
      - list where first item has width/height and remaining items are layers
    """
    if isinstance(doc, dict) and "layers" in doc:
        width = doc.get("width", "")
        height = doc.get("height", "")
        layers = doc.get("layers", [])
        return width, height, layers

    if isinstance(doc, list) and doc:
        first = doc[0] if isinstance(doc[0], dict) else {}
        width = first.get("width", "")
        height = first.get("height", "")
        layers = [x for x in doc[1:] if isinstance(x, dict)]
        return width, height, layers

    raise ValueError("Unsupported JSON structure: expected dict-with-layers or list-with-first-size.")


def row_to_line(values):
    values = list(values) + [""] * (len(HEADER_COLS) - len(values))
    values = values[:len(HEADER_COLS)]
    values = ["" if v is None else str(v) for v in values]
    return "\t".join(values)


def write_txt(width, height, layers, out_path: Path):
    lines = []
    lines.append("#" + "\t".join(HEADER_COLS))

    # size row: width/height in the 5th/6th columns
    size_row = ["", "", "", "", str(width), str(height), "", "", "", "", "", "", "", ""]
    lines.append(row_to_line(size_row))

    for ly in layers:
        lines.append(row_to_line([
            ly.get("layer_type",""),
            ly.get("name",""),
            ly.get("left",""),
            ly.get("top",""),
            ly.get("width",""),
            ly.get("height",""),
            ly.get("type",""),
            ly.get("opacity",""),
            ly.get("visible",""),
            ly.get("layer_id",""),
            ly.get("group_layer_id",""),
            "",  # base
            "",  # images
            ""   # trailing empty col
        ]))

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-16")


def iter_json_files(inputs: list[Path], folder: Path | None, recursive: bool, pattern: str):
    seen: set[Path] = set()

    # Explicit file inputs (supports drag&drop multiple files on Windows)
    for p in inputs:
        p = p.expanduser().resolve()
        if p.is_file() and p.suffix.lower() == ".json" and p not in seen:
            seen.add(p)
            yield p

    # Folder scan
    if folder:
        folder = folder.expanduser().resolve()
        if folder.is_dir():
            globber = folder.rglob if recursive else folder.glob
            for p in globber(pattern):
                if p.is_file() and p.suffix.lower() == ".json" and p not in seen:
                    seen.add(p)
                    yield p


def main():
    ap = argparse.ArgumentParser(
        description="Batch convert your layer JSON(s) to the sample TXT format (UTF-16, tab-separated)."
    )
    ap.add_argument(
        "json_paths",
        nargs="*",
        type=Path,
        help="One or more .json files (you can drag & drop multiple files onto this script)"
    )
    ap.add_argument(
        "-d", "--dir",
        type=Path,
        default=None,
        help="Scan a folder for JSON files (default pattern: *.json)"
    )
    ap.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="Scan folder recursively (works with --dir)"
    )
    ap.add_argument(
        "-p", "--pattern",
        default="*.json",
        help="Glob pattern when scanning a folder (default: *.json)"
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing .txt (default: skip if exists)"
    )
    args = ap.parse_args()

    # If user provided nothing, default to current folder *.json
    if not args.json_paths and not args.dir:
        args.dir = Path.cwd()

    ok = 0
    skipped = 0
    failed = 0

    for jp in iter_json_files(args.json_paths, args.dir, args.recursive, args.pattern):
        out_path = jp.with_suffix(".txt")
        if out_path.exists() and not args.overwrite:
            skipped += 1
            print(f"SKIP (exists): {out_path}")
            continue

        try:
            doc = load_json_any(jp)
            w, h, layers = extract(doc)
            write_txt(w, h, layers, out_path)
            ok += 1
            print(f"OK: {jp} -> {out_path}")
        except Exception as e:
            failed += 1
            print(f"FAIL: {jp} ({e})", file=sys.stderr)

    print("\nDone.")
    print(f"OK: {ok}, Skipped: {skipped}, Failed: {failed}")


if __name__ == "__main__":
    main()
