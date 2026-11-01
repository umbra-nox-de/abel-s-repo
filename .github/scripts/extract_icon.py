#!/usr/bin/env python3
"""Extract a usable iOS app icon, preferring Info.plist PNGs then Assets.car."""
import json
import os
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
    try:
        subprocess.run(
            ["convert", str(source), "-auto-orient", "-resize", "1024x1024",
             "-gravity", "center", "-background", "none", "-extent", "1024x1024",
             "-strip", "PNG32:" + str(destination)],
            check=True, capture_output=True, timeout=30
        )
        return image_size(destination) == (1024, 1024)
    except Exception:
        destination.unlink(missing_ok=True)
        return False

def best(candidates):
    ranked = [(rank_image(p), p) for p in candidates]
    ranked = [(r, p) for r, p in ranked if r]
    return max(ranked, key=lambda item: item[0])[1] if ranked else None

def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    names = []
    try:
        names = [str(x) for x in json.loads(ICON_NAMES_FILE.read_text()) if x]
    except Exception:
        pass

    diagnostics = {"declared_icon_names": names, "standalone_candidates": [], "asset_catalogs": [], "compiled_png_candidates": [], "selected_source": None}

    # 1. Prefer a valid standalone PNG named by Info.plist.
    standalone = []
    for name in names:
        base = Path(name).name
        standalone.extend(p for p in APP_DIR.rglob("*.png")
                          if p.stem.lower().startswith(base.lower()))
    diagnostics["standalone_candidates"] = [{"path": str(p.relative_to(APP_DIR)), "dimensions": image_size(p)} for p in sorted(set(standalone))]
    source = best(standalone)
    if source is not None and normalize(source, OUTPUT):
        diagnostics["selected_source"] = f"Info.plist:{source.name}"
        if REPORT:
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(diagnostics, indent=2))
        print(f"ICON_SOURCE=Info.plist:{source.name}")
        return 0

    # 2. If standalone files are absent, corrupt, or unsupported (for example CgBI),
    # unpack compiled asset catalogs and choose the largest square app-icon rendition.
    with tempfile.TemporaryDirectory(prefix="app-icons-") as tmp:
        tmpdir = Path(tmp)
        extracted = tmpdir / "assets"
        extracted.mkdir()
        for car in APP_DIR.rglob("Assets.car"):
            diagnostics["asset_catalogs"].append(str(car.relative_to(APP_DIR)))
            try:
                subprocess.run(["python3", str(CAR_UNPACKER), str(car), str(extracted)], check=True, capture_output=True, text=True, timeout=180)
            except Exception as exc:
                diagnostics.setdefault("parser_errors", []).append(str(exc))
                print(f"Assets.car extraction warning ({car}): {exc}", file=sys.stderr)
        pngs = list(extracted.rglob("*.png"))
        diagnostics["compiled_png_candidates"] = [{"path": str(p.relative_to(extracted)), "dimensions": image_size(p)} for p in sorted(pngs)]
        # Only accept renditions whose names match Apple's declared icon asset
        # or clearly identify themselves as an app icon. Never choose an arbitrary
        # largest image (which can be an ad, splash screen, or tracking asset).
        normalized_names = {
            "".join(ch for ch in Path(name).stem.lower() if ch.isalnum())
            for name in names
        }
        preferred = []
        for path in pngs:
            stem = "".join(ch for ch in path.stem.lower() if ch.isalnum())
            if any(n and (stem == n or stem.startswith(n)) for n in normalized_names):
                preferred.append(path)
            elif "appicon" in stem or "app-icon" in path.name.lower():
                preferred.append(path)
        source = best(preferred)
        if source is not None and normalize(source, OUTPUT):
            diagnostics["selected_source"] = f"Assets.car:{source.name}"
            if REPORT:
                REPORT.parent.mkdir(parents=True, exist_ok=True)
                REPORT.write_text(json.dumps(diagnostics, indent=2))
            print(f"ICON_SOURCE=Assets.car:{source.name}")
            return 0

    if REPORT:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(diagnostics, indent=2))
    OUTPUT.unlink(missing_ok=True)
    print("ICON_SOURCE=default-fallback")
    return 1

if __name__ == "__main__":
    sys.exit(main())
