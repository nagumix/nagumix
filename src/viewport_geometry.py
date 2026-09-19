"""Viewport-only frame and content positioning in logical canvas pixels."""

from dataclasses import dataclass, replace
import math

from .image_geometry import scaled_size


HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


class GeometryError(ValueError):
    """The frame cannot be edited without exposing empty content."""


def pixel(value):
    value = float(value)
    if not math.isfinite(value):
        raise GeometryError("pointer coordinate must be finite")
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def clamp(value, low, high):
    return min(high, max(low, value))


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def contains(self, x, y):
        return self.x <= x < self.right and self.y <= y < self.bottom


@dataclass(frozen=True)
class ViewGeometry:
    frame: Rect
    content_size: tuple[int, int]
    offset: tuple[int, int]
    zoom: float

    @property
    def content_rect(self):
        return Rect(self.frame.x - self.offset[0],
                    self.frame.y - self.offset[1], *self.content_size)

    def validate(self):
        f = self.frame
        cw, ch = self.content_size
        vx, vy = self.offset
        numbers = (f.x, f.y, f.width, f.height, cw, ch, vx, vy)
        if (any(type(v) is not int for v in numbers)
                or not math.isfinite(float(self.zoom)) or self.zoom <= 0
                or f.width < 1 or f.height < 1 or cw < 1 or ch < 1
                or vx < 0 or vy < 0 or vx + f.width > cw or vy + f.height > ch):
            raise GeometryError("Frame is outside image content; use Reset Frame Size")
        return self


def from_object(obj):
    obj.load_image()
    if obj._original_image is None:
        raise GeometryError("Image content is unavailable")
    return ViewGeometry(
        Rect(obj.x, obj.y, obj.width, obj.height),
        scaled_size(obj._original_image.size, obj.zoom_factor),
        tuple(obj.viewport_offset), obj.zoom_factor)


def apply_to_object(obj, geometry):
    geometry.validate()
    f = geometry.frame
    pixels_changed = ((obj.width, obj.height) != (f.width, f.height)
                      or tuple(obj.viewport_offset) != geometry.offset)
    obj.x, obj.y, obj.width, obj.height = f.x, f.y, f.width, f.height
    obj.viewport_offset = geometry.offset
    if pixels_changed:
        obj._clear_image_caches()


def resize_frame(start, handle, pointer, minimum=(32, 32)):
    start.validate()
    if handle not in HANDLES:
        raise GeometryError(f"unknown handle: {handle}")
    c, f = start.content_rect, start.frame
    px, py = map(pixel, pointer)
    mw = min(c.width, max(1, pixel(minimum[0])))
    mh = min(c.height, max(1, pixel(minimum[1])))
    left, top, right, bottom = f.x, f.y, f.right, f.bottom
    if "w" in handle:
        left = clamp(px, c.x, right - mw)
    elif "e" in handle:
        right = clamp(px, left + mw, c.right)
    if "n" in handle:
        top = clamp(py, c.y, bottom - mh)
    elif "s" in handle:
        bottom = clamp(py, top + mh, c.bottom)
    frame = Rect(left, top, right - left, bottom - top)
    return replace(start, frame=frame,
                   offset=(frame.x - c.x, frame.y - c.y)).validate()


def reposition_content(start, delta):
    start.validate()
    dx, dy = map(pixel, delta)
    vx, vy = start.offset
    cw, ch = start.content_size
    f = start.frame
    return replace(start, offset=(clamp(vx - dx, 0, cw - f.width),
                                  clamp(vy - dy, 0, ch - f.height))).validate()


def reset_frame_size(start):
    # Deliberately accepts legacy crop outside the content bounds. The reset
    # is the explicit repair action and anchors the current content origin.
    f = start.frame
    cw, ch = start.content_size
    vx, vy = start.offset
    if (any(type(v) is not int for v in
            (f.x, f.y, f.width, f.height, cw, ch, vx, vy))
            or min(f.width, f.height, cw, ch) < 1
            or min(vx, vy) < 0 or not math.isfinite(start.zoom)
            or start.zoom <= 0):
        raise GeometryError("Frame cannot be reset safely")
    return replace(start, frame=start.content_rect, offset=(0, 0)).validate()


def reduced(start):
    start.validate()
    return (start.frame.width < start.content_size[0]
            or start.frame.height < start.content_size[1])


def handle_centers(frame):
    mx, my = frame.x + frame.width // 2, frame.y + frame.height // 2
    return (("nw", (frame.x, frame.y)), ("n", (mx, frame.y)),
            ("ne", (frame.right, frame.y)), ("e", (frame.right, my)),
            ("se", (frame.right, frame.bottom)), ("s", (mx, frame.bottom)),
            ("sw", (frame.x, frame.bottom)), ("w", (frame.x, my)))


def hit_handle(frame, point, radius):
    px, py = point
    choices = []
    for index, (name, (cx, cy)) in enumerate(handle_centers(frame)):
        dx, dy = px - cx, py - cy
        if abs(dx) <= radius and abs(dy) <= radius:
            choices.append((dx * dx + dy * dy, index, name))
    return min(choices)[2] if choices else None
