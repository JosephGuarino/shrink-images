#!/usr/bin/env python3

import argparse
import io
import math
import os
import re
import shutil
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("Pillow is required. Install it with:  pip install Pillow")

Image.MAX_IMAGE_PIXELS = None

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
UNITS = {"B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2, "G": 1024**3, "GB": 1024**3}


def parse_size(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"Invalid size '{text}'. Try e.g. 500KB, 1MB, 2.5MB.")
    number, unit = float(m.group(1)), (m.group(2) or "B").upper()
    if unit not in UNITS:
        raise argparse.ArgumentTypeError(f"Unknown unit '{m.group(2)}'. Use B, KB, MB or GB.")
    size = int(number * UNITS[unit])
    if size <= 0:
        raise argparse.ArgumentTypeError("Size must be greater than zero.")
    return size


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def encode(img: Image.Image, fmt: str, exif: bytes | None) -> bytes:
    buf = io.BytesIO()
    kwargs = {}
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        kwargs = {"quality": 90, "optimize": True}
        if exif:
            kwargs["exif"] = exif
    elif fmt == "WEBP":
        kwargs = {"quality": 90}
        if exif:
            kwargs["exif"] = exif
    elif fmt == "PNG":
        kwargs = {"compress_level": 6}
    elif fmt == "TIFF":
        kwargs = {"compression": "tiff_lzw"}
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def shrink_to_target(img, fmt, exif, target, orig_size, min_scale):
    w0, h0 = img.size

    def render(scale, resample, gap):
        w, h = max(1, round(w0 * scale)), max(1, round(h0 * scale))
        out = img.resize((w, h), resample, reducing_gap=gap)
        return encode(out, fmt, exif), (w, h)

    s = min(1.0, max(min_scale, math.sqrt(target / orig_size) * 0.97))
    best = None  # (scale, size) largest scale known to fit
    bad = None   # smallest scale known to be too big

    for _ in range(10):
        data, _dims = render(s, Image.BILINEAR, 2.0)
        n = len(data)
        if n <= target:
            if best is None or s > best[0]:
                best = (s, n)
            if n >= target * 0.95:
                break
        else:
            if s <= min_scale:
                return None
            if bad is None or s < bad:
                bad = s

        lo = best[0] if best else min_scale
        hi = bad if bad else 1.0
        if hi - lo < 0.005:
            break
        nxt = min(max(s * math.sqrt(target * 0.975 / n), min_scale), 1.0)
        if nxt <= lo or nxt >= hi:
            nxt = (lo + hi) / 2
        s = nxt

    if best is None:
        # Last resort: try the floor directly
        data, _dims = render(min_scale, Image.BILINEAR, 2.0)
        if len(data) > target:
            return None
        best = (min_scale, len(data))

    # Final high-quality render, verified against the target
    scale = best[0]
    for _ in range(8):
        data, dims = render(scale, Image.LANCZOS, None)
        if len(data) <= target:
            return data, dims
        scale *= 0.97
        if scale < min_scale:
            break
    return None


def process(path_str, root_str, target, min_scale, dry_run, backup_dir):
    path, root = Path(path_str), Path(root_str)
    size = path.stat().st_size

    try:
        with Image.open(path) as im:
            fmt = im.format
            if getattr(im, "is_animated", False):
                return "skipped", f"  SKIP  {path}  (animated image)"
            im.load()
            img = ImageOps.exif_transpose(im)
            exif = img.getexif().tobytes() if fmt in ("JPEG", "WEBP") and img.getexif() else None
    except Exception as e:
        return "error", f"  ERROR {path}  ({e})"

    try:
        result = shrink_to_target(img, fmt, exif, target, size, min_scale)
    except Exception as e:
        return "error", f"  ERROR {path}  ({e})"

    if result is None:
        return "failed", (f"  FAIL  {path}  (can't reach {human(target)} without going below "
                          f"{min_scale:.0%} scale; left untouched)")

    data, (w, h) = result
    line = (f"{path}  {img.size[0]}x{img.size[1]} -> {w}x{h}  "
            f"{human(size)} -> {human(len(data))}")

    if dry_run:
        return "resized", f"  DRY   {line}"

    if backup_dir:
        dest = Path(backup_dir) / path.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copystat(path, tmp)
        os.replace(tmp, path)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return "error", f"  ERROR {path}  ({e})"

    return "resized", f"  OK    {line}"


def find_images(root: Path, recursive: bool):
    walker = os.walk(root) if recursive else [(str(root), [], os.listdir(root))]
    for dirpath, _dirs, files in walker:
        for name in sorted(files):
            p = Path(dirpath) / name
            if p.suffix.lower() in SUPPORTED and p.is_file():
                yield p


def main():
    p = argparse.ArgumentParser(
        description="Shrink images over a given file size by reducing their resolution "
                    "(aspect ratio preserved). Images at or under the size are untouched.")
    p.add_argument("directory", type=Path, help="Directory to scan (subdirectories included)")
    p.add_argument("-s", "--size", type=parse_size, required=True,
                   help="Target max file size, e.g. 500KB, 1MB, 2.5MB")
    p.add_argument("--no-recursive", action="store_true",
                   help="Only process the top-level directory")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="Show what would change without modifying any files")
    p.add_argument("-b", "--backup-dir", type=Path,
                   help="Copy originals here (folder structure preserved) before overwriting")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                   help="Number of images to process in parallel (default: CPU count)")
    p.add_argument("--min-scale", type=float, default=0.05,
                   help="Smallest allowed scale factor (default 0.05 = 5%% of original dimensions)")
    args = p.parse_args()

    root = args.directory.resolve()
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")
    backup = args.backup_dir.resolve() if args.backup_dir else None

    print(f"Scanning {root} for images over {human(args.size)}"
          f"{' (dry run)' if args.dry_run else ''}...\n")

    counts = {"resized": 0, "skipped": 0, "failed": 0, "error": 0}
    todo = []
    for path in find_images(root, not args.no_recursive):
        if backup and backup in path.resolve().parents:
            continue
        if path.stat().st_size <= args.size:
            counts["skipped"] += 1
        else:
            todo.append(path)

    print(f"{len(todo)} image(s) to shrink, {counts['skipped']} already small enough.\n")

    def record(status, msg):
        counts[status] += 1
        print(msg, flush=True)

    jobs = max(1, args.jobs)
    job_args = (str(root), args.size, args.min_scale, args.dry_run, str(backup) if backup else None)

    if jobs == 1 or len(todo) <= 1:
        for path in todo:
            record(*process(str(path), *job_args))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(process, str(path), *job_args) for path in todo]
            for fut in as_completed(futures):
                try:
                    record(*fut.result())
                except Exception as e:
                    record("error", f"  ERROR  ({e})")

    verb = "Would resize" if args.dry_run else "Resized"
    print(f"\nDone. {verb}: {counts['resized']} | Already small enough: {counts['skipped']} "
          f"| Couldn't reach target: {counts['failed']} | Errors: {counts['error']}")


if __name__ == "__main__":
    main()
