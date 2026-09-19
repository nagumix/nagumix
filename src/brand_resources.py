"""Cached native branding resources, independent of the process working directory."""

from functools import lru_cache
import logging
from pathlib import Path
import sys

import wx


def resource_path(relative):
    """Find a public asset in a source checkout or a collected frozen bundle."""
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Resource paths must be relative to the asset root")
    roots = []
    if getattr(sys, "_MEIPASS", None):
        roots.append(Path(sys._MEIPASS))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    else:
        roots.append(Path(__file__).resolve().parents[1])
    for root in roots:
        path = root / "assets" / relative
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=16)
def header_bitmap(variant, pixels):
    if variant not in ("light", "dark") or pixels < 1:
        raise ValueError("Invalid brand bitmap request")
    path = resource_path(f"branding/nagumix-logo-{variant}-80.png")
    if path is None:
        logging.warning("Missing NaguMIX %s header logo", variant)
        return None
    image = wx.Image(str(path), wx.BITMAP_TYPE_PNG)
    if not image.IsOk():
        logging.warning("Could not decode NaguMIX header logo: %s", path)
        return None
    if image.GetWidth() != pixels or image.GetHeight() != pixels:
        image = image.Scale(pixels, pixels, wx.IMAGE_QUALITY_HIGH)
    return wx.Bitmap(image)


@lru_cache(maxsize=1)
def app_icon_bundle():
    """The stable dark launcher mark for native top-level window icons."""
    path = resource_path("icons/nagumix-app-256.png")
    if path is None:
        logging.warning("Missing NaguMIX window icon")
        return None
    source = wx.Image(str(path), wx.BITMAP_TYPE_PNG)
    if not source.IsOk():
        logging.warning("Could not decode NaguMIX window icon: %s", path)
        return None
    bundle = wx.IconBundle()
    for pixels in (16, 24, 32, 48, 64, 128, 256):
        image = (source.Copy() if pixels == 256 else
                 source.Scale(pixels, pixels, wx.IMAGE_QUALITY_HIGH))
        icon = wx.Icon()
        icon.CopyFromBitmap(wx.Bitmap(image))
        bundle.AddIcon(icon)
    return bundle


def set_window_icon(window):
    bundle = app_icon_bundle()
    if bundle is not None:
        window.SetIcons(bundle)
