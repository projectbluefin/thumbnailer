#!/usr/bin/env python3
"""
fix-failed-thumbnails.py
Scans the Freedesktop / GNOME thumbnail fail cache (~/.cache/thumbnails/fail/),
regenerates thumbnails for previously failed media (videos AND pictures/images,
including recent:/// and file:/// URIs), embeds standard Freedesktop PNG metadata
(Thumb::URI, Thumb::MTime, Thumb::Size, Software), and purges failed thumbnail markers
so GNOME/Nautilus/file choosers display them immediately.
"""

import os
import sys
import hashlib
import subprocess
import urllib.parse
from PIL import Image, PngImagePlugin

FAIL_DIR = os.path.expanduser("~/.cache/thumbnails/fail")
LARGE_DIR = os.path.expanduser("~/.cache/thumbnails/large")
NORMAL_DIR = os.path.expanduser("~/.cache/thumbnails/normal")
XLARGE_DIR = os.path.expanduser("~/.cache/thumbnails/x-large")

# This runs inside the container (`apptainer run --app fix`), so ffmpeg is right here:
# no shim, no socket, no daemon round trip.
FFMPEG = os.environ.get("FFT_FFMPEG", "ffmpeg")

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff",
    ".tif", ".avif", ".heic", ".heif", ".jxl", ".svg", ".ico", ".qoi"
}

def ffmpeg_thumbnail(local_path, target_png, size):
    vf = (
        f"scale=min({size}\\,iw):min({size}\\,ih)"
        ":force_original_aspect_ratio=decrease"
    )
    for seek in ("00:00:03", "00:00:00"):
        res = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-ss", seek, "-i", local_path,
             "-vf", vf, "-vframes", "1", "-update", "1", target_png],
            capture_output=True,
        )
        if res.returncode == 0 and os.path.exists(target_png) \
           and os.path.getsize(target_png) > 0:
            return True
    return False

def resolve_target_uri(uri):
    """
    Resolve recent:/// or other virtual URIs to canonical file:// URI and local path.
    """
    if uri.startswith("file://"):
        local_path = urllib.parse.unquote(uri[7:])
        return uri, local_path

    if uri.startswith("recent://"):
        try:
            res = subprocess.run(["gio", "info", uri], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if "standard::target-uri:" in line:
                        target_uri = line.split("standard::target-uri:", 1)[1].strip()
                        if target_uri.startswith("file://"):
                            local_path = urllib.parse.unquote(target_uri[7:])
                            return target_uri, local_path
        except Exception:
            pass

    return uri, None

def generate_image_thumbnail(local_path, target_png, size=256):
    """
    Generate thumbnail for picture using Pillow, falling back to ffmpeg.
    """
    try:
        with Image.open(local_path) as im:
            # Handle orientation if present
            try:
                from PIL import ImageOps
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass

            # Convert palettes or unusual modes to RGBA/RGB
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")

            im.thumbnail((size, size), Image.Resampling.LANCZOS)
            im.save(target_png, "PNG")
            return True
    except Exception:
        pass
    return False

def generate_media_thumbnail(local_path, target_png, size=256):
    """
    Pillow for still images, ffmpeg for everything with a timeline.
    """
    ext = os.path.splitext(local_path)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        if generate_image_thumbnail(local_path, target_png, size=size):
            return True
    return ffmpeg_thumbnail(local_path, target_png, size)

def save_with_freedesktop_metadata(target_png, uri, local_path):
    stat = os.stat(local_path)
    with Image.open(target_png) as im_out:
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Thumb::URI", uri)
        meta.add_text("Thumb::MTime", str(int(stat.st_mtime)))
        meta.add_text("Thumb::Size", str(stat.st_size))
        meta.add_text("Software", "ffmpeg-thumbnailer")
        im_out.save(target_png, "PNG", pnginfo=meta)

def main():

    os.makedirs(LARGE_DIR, exist_ok=True)
    os.makedirs(NORMAL_DIR, exist_ok=True)
    os.makedirs(XLARGE_DIR, exist_ok=True)

    failed_files = []
    if os.path.exists(FAIL_DIR):
        for root, dirs, files in os.walk(FAIL_DIR):
            for f in files:
                if f.endswith(".png"):
                    failed_files.append(os.path.join(root, f))

    if not failed_files:
        print("No failed thumbnails found in ~/.cache/thumbnails/fail/")
        return

    print(f"Processing {len(failed_files)} failed thumbnail marker(s)...")
    recovered = 0
    cleaned = 0

    for fail_png in failed_files:
        try:
            with Image.open(fail_png) as im:
                orig_uri = im.info.get("Thumb::URI")

            if not orig_uri:
                os.unlink(fail_png)
                cleaned += 1
                continue

            target_uri, local_path = resolve_target_uri(orig_uri)

            if not local_path or not os.path.exists(local_path):
                # Target file deleted or inaccessible, purge stale fail marker
                os.unlink(fail_png)
                cleaned += 1
                continue

            # Compute MD5 for both original URI (e.g. recent://) and target file:// URI
            md5_orig = hashlib.md5(orig_uri.encode("utf-8")).hexdigest() + ".png"
            md5_target = hashlib.md5(target_uri.encode("utf-8")).hexdigest() + ".png"

            target_png = os.path.join(LARGE_DIR, md5_target)

            if generate_media_thumbnail(local_path, target_png, size=256):
                save_with_freedesktop_metadata(target_png, target_uri, local_path)

                # If original URI was recent://, also generate thumbnail under its key
                if md5_orig != md5_target:
                    orig_png = os.path.join(LARGE_DIR, md5_orig)
                    generate_media_thumbnail(local_path, orig_png, size=256)
                    save_with_freedesktop_metadata(orig_png, orig_uri, local_path)

                # Remove the failure marker!
                if os.path.exists(fail_png):
                    os.unlink(fail_png)

                print(f"✓ Recovered: {local_path} ({orig_uri})")
                recovered += 1
            else:
                print(f"✗ Failed to generate thumbnail for: {local_path}")
        except Exception as e:
            print(f"Error handling {fail_png}: {e}", file=sys.stderr)

    print(f"Finished: {recovered} thumbnail(s) generated, {cleaned} stale marker(s) purged.")

if __name__ == "__main__":
    main()
