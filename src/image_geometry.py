"""Shared geometry for whole-image fitting and scaled pixel dimensions."""

import math

from PIL import Image


def scaled_size(source_size, scale):
    """Use the same pixel rounding for geometry, preview, and export."""
    return tuple(max(1, int(length * scale)) for length in source_size)


def fit_geometry(source_size, bounds):
    """Return (scale, width, height), or None while bounds are unavailable.

    Fit without enlargement. Subpixel dimensions round to one pixel so very
    thin images remain drawable; the source is always resized in its entirety.
    """
    if any(type(value) is not int or value <= 0 for value in (*source_size, *bounds)):
        return None
    scale = min(1.0, bounds[0] / source_size[0], bounds[1] / source_size[1])
    width, height = scaled_size(source_size, scale)
    return scale, width, height


def render_pil_crop(source_pixels, zoom_factor, frame_size, viewport_offset,
                    *, resample=Image.Resampling.LANCZOS, resize_source=None):
    """Render the shared display/export crop from detached source pixels."""
    if source_pixels is None:
        raise ValueError("source pixels are unavailable")
    try:
        zoom = float(zoom_factor)
        frame_w, frame_h = frame_size
        vx, vy = viewport_offset
    except (TypeError, ValueError) as exc:
        raise ValueError("image transform is incomplete") from exc
    numeric = (zoom, frame_w, frame_h, vx, vy)
    if (not all(isinstance(value, (int, float)) and math.isfinite(value)
                for value in numeric)
            or zoom <= 0 or frame_w <= 0 or frame_h <= 0
            or vx < 0 or vy < 0):
        raise ValueError("image transform is invalid")

    scaled_w, scaled_h = scaled_size(source_pixels.size, zoom)
    right = min(int(vx) + int(frame_w), scaled_w)
    bottom = min(int(vy) + int(frame_h), scaled_h)
    if right <= int(vx) or bottom <= int(vy):
        raise ValueError("image crop is outside its scaled source")
    resize = resize_source or (
        lambda size: source_pixels.resize(size, resample))
    scaled = resize((scaled_w, scaled_h))
    try:
        return scaled.crop((int(vx), int(vy), right, bottom))
    finally:
        scaled.close()
