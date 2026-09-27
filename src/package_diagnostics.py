"""Narrow opt-in diagnostics for inspecting a frozen image-codec payload."""

from hashlib import sha256
import json
from pathlib import Path

from .brand_resources import resource_path
from .image_pixels import (
    get_animation_metadata,
    load_animation_frame,
    load_source_pixels,
)


def write_package_diagnostics(report_path, image_paths):
    """Decode caller-supplied images and write non-executable JSON evidence."""
    inputs = []
    for supplied in image_paths:
        path = Path(supplied).resolve(strict=True)
        pixels = load_source_pixels(path)
        try:
            rgba = pixels.convert("RGBA")
            try:
                record = {
                    "path": str(path),
                    "size": list(pixels.size),
                    "mode": pixels.mode,
                    "rgba_sha256": sha256(rgba.tobytes()).hexdigest(),
                }
            finally:
                rgba.close()
            animation = get_animation_metadata(pixels)
            if animation is not None:
                record["frame_count"] = animation.frame_count
                if animation.frame_count > 1:
                    frame = load_animation_frame(path, 1)
                    try:
                        rgba_frame = frame.convert("RGBA")
                        try:
                            record["frame_2_rgba_sha256"] = sha256(
                                rgba_frame.tobytes()).hexdigest()
                        finally:
                            rgba_frame.close()
                    finally:
                        frame.close()
            inputs.append(record)
        finally:
            pixels.close()

    required = (
        "branding/nagumix-logo-dark-80.png",
        "branding/nagumix-logo-light-80.png",
        "icons/nagumix-app.ico",
        "legal/RELEASE-SOURCE.txt",
        "legal/AGPL-3.0.txt",
    )
    resources = {name: str(resource_path(name)) if resource_path(name) else None
                 for name in required}
    report = Path(report_path)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"images": inputs, "resources": resources}, indent=2)
                      + "\n", encoding="utf-8")
    return 0
