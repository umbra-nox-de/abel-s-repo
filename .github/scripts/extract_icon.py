#!/usr/bin/env python3
"""Extract the primary iOS app icon from Info.plist PNGs or Assets.car."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(sys.argv[1])
ICON_NAMES_FILE = Path(sys.argv[2])
OUTPUT = Path(sys.argv[3])
CAR_UNPACKER = Path(sys.argv[4])
LZFSE = Path(sys.argv[5])
REPORT = Path(sys.argv[6]) if len(sys.argv) > 6 else None


def image_size(path):
    try:
        result = subprocess.run(
            ["identify", "-ping", "-format", "%w %h", str(path)],
            check=True, capture_output=True, text=True, timeout=20
        )
        width, height = map(int, result.stdout.split()[:2])
        if width < 64 or height < 64:
            return None
        return width, height
    except Exception:
        return None


def rank_image(path):
    size = image_size(path)
    if not size:
        return None
    width, height = size
    if width != height:
        return None
    return (width * height, width, str(path))


def normalize(source, destination):
    """Normalize ordinary PNGs; revert Apple's CgBI optimization when needed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["convert", str(source), "-auto-orient", "-resize", "1024x1024",
             "-gravity", "center", "-background", "none", "-extent", "1024x1024",
             "-strip", "PNG32:" + str(destination)],
            check=True, capture_output=True, timeout=30
        )
        if image_size(destination) == (1024, 1024):
            return True
    except Exception:
        pass
    destination.unlink(missing_ok=True)

    pngcrush = shutil.which("pngcrush")
    if pngcrush:
        with tempfile.TemporaryDirectory(prefix="cgbi-") as tmp:
            reverted = Path(tmp) / "reverted.png"
            try:
                subprocess.run(
                    [pngcrush, "-q", "-revert-iphone-optimizations",
                     str(source), str(reverted)],
                    check=True, capture_output=True, timeout=45
                )
                subprocess.run(
                    ["convert", str(reverted), "-auto-orient", "-resize", "1024x1024",
                     "-gravity", "center", "-background", "none", "-extent", "1024x1024",
                     "-strip", "PNG32:" + str(destination)],
                    check=True, capture_output=True, timeout=30
                )
                if image_size(destination) == (1024, 1024):
                    return True
            except Exception:
                pass
    destination.unlink(missing_ok=True)
    return False


def best(candidates):
    ranked = [(rank_image(p), p) for p in candidates]
    ranked = [(r, p) for r, p in ranked if r]
    return max(ranked, key=lambda item: item[0])[1] if ranked else None


def write_report(data):
    if REPORT:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(data, indent=2))


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    try:
        names = [str(x) for x in json.loads(ICON_NAMES_FILE.read_text()) if x]
    except Exception:
        names = []

    diagnostics = {
        "declared_icon_names": names,
        "standalone_candidates": [],
        "asset_catalogs": [],
        "compiled_png_candidates": [],
        "parser_runs": [],
        "selected_source": None
    }

    # Prefer the exact icon files declared by the app's Info.plist.
    standalone = []
    for name in names:
        base = Path(name).name.lower()
        for path in APP_DIR.rglob("*.png"):
            filename = path.name.lower()
            if filename == base or filename.startswith(base.rsplit(".", 1)[0] + "@"):
                standalone.append(path)
            elif path.stem.lower().startswith(Path(base).stem + "@"):
                standalone.append(path)

    standalone = sorted(set(standalone))
    diagnostics["standalone_candidates"] = [
        {"path": str(p.relative_to(APP_DIR)), "dimensions": image_size(p)}
        for p in standalone
    ]
    source = best(standalone)
    if source is not None and normalize(source, OUTPUT):
        diagnostics["selected_source"] = f"Info.plist:{source.name}"
        write_report(diagnostics)
        print(f"ICON_SOURCE=Info.plist:{source.name}")
        return 0

    # Extract each catalog separately so framework resources cannot overwrite or
    # masquerade as the app's primary icon. Keep parser output for diagnosis.
    with tempfile.TemporaryDirectory(prefix="app-icons-") as tmp:
        tmpdir = Path(tmp)
        all_candidates = []
        catalogs = sorted(APP_DIR.rglob("Assets.car"),
                          key=lambda p: (len(p.relative_to(APP_DIR).parts), str(p)))
        for index, car in enumerate(catalogs):
            relative_car = str(car.relative_to(APP_DIR))
            diagnostics["asset_catalogs"].append(relative_car)
            extracted = tmpdir / f"catalog-{index}"
            extracted.mkdir()
            try:
                result = subprocess.run(
                    ["python3", str(CAR_UNPACKER), str(car), str(extracted)],
                    check=True, capture_output=True, text=True, timeout=240
                )
                diagnostics["parser_runs"].append({
                    "catalog": relative_car,
                    "returncode": result.returncode,
                    "stdout": result.stdout[-12000:],
                    "stderr": result.stderr[-12000:]
                })
            except Exception as exc:
                diagnostics["parser_runs"].append({
                    "catalog": relative_car,
                    "error": str(exc),
                    "stdout": getattr(exc, "stdout", "")[-12000:] if getattr(exc, "stdout", None) else "",
                    "stderr": getattr(exc, "stderr", "")[-12000:] if getattr(exc, "stderr", None) else ""
                })
                continue

            for png in extracted.rglob("*.png"):
                all_candidates.append((relative_car, png))

        diagnostics["compiled_png_candidates"] = [
            {
                "catalog": catalog,
                "path": str(path.name),
                "dimensions": image_size(path)
            }
            for catalog, path in all_candidates
        ]

        # Match declared asset names even when the unpacker prefixes rendition
        # indexes (e.g. 005_icon.png.png). Only use square renditions.
        normalized_names = set()
        for name in names:
            raw = Path(name).name.lower()
            normalized_names.add("".join(ch for ch in raw if ch.isalnum()))
            normalized_names.add("".join(ch for ch in Path(raw).stem.lower() if ch.isalnum()))

        preferred = []
        for catalog, path in all_candidates:
            stem = "".join(ch for ch in path.stem.lower() if ch.isalnum())
            if any(n and (stem.endswith(n) or stem == n or stem.startswith(n))
                   for n in normalized_names):
                preferred.append((catalog, path))
            elif "appicon" in stem or "app-icon" in path.name.lower():
                preferred.append((catalog, path))

        # Prefer the app's root Assets.car over embedded framework catalogs.
        preferred.sort(key=lambda item: (item[0] != "Assets.car", item[0], item[1].name))
        source = None
        for catalog, candidate in preferred:
            if rank_image(candidate) and normalize(candidate, OUTPUT):
                source = (catalog, candidate)
                break

        if source:
            diagnostics["selected_source"] = f"Assets.car:{source[0]}:{source[1].name}"
            write_report(diagnostics)
            print(f"ICON_SOURCE=Assets.car:{source[0]}:{source[1].name}")
            return 0

    write_report(diagnostics)
    OUTPUT.unlink(missing_ok=True)
    print("ICON_SOURCE=default-fallback")
    return 1


if __name__ == "__main__":
    sys.exit(main())
