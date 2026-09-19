# src/image_object.py
import wx
import logging
import uuid
from dataclasses import dataclass, field
from PIL import Image
from .utils import bytes_to_human_readable
from .image_geometry import fit_geometry, render_pil_crop, scaled_size
from .image_pixels import (
    SharedSourcePixels,
    get_animation_metadata,
    get_source_identity,
    load_source_pixels,
    normalize_source_pixels,
)
from .gif_playback import effective_frame_duration_ms


def layout_status_overlay(frame_rect, box_size, canvas_size, position="center", inset=8):
    """Return a clamped overlay rectangle for one object frame."""
    frame_x, frame_y, frame_w, frame_h = (int(value) for value in frame_rect)
    canvas_w, canvas_h = (max(0, int(value)) for value in canvas_size)
    box_w = max(0, min(int(box_size[0]), canvas_w))
    box_h = max(0, min(int(box_size[1]), canvas_h))
    if canvas_w <= 0 or canvas_h <= 0 or box_w <= 0 or box_h <= 0:
        return None
    vertical, horizontal = ((position.rsplit("_", 1)
                             if "_" in position else ("middle", position))
                            if position != "center" else ("middle", "center"))
    x_anchors = {
        "left": frame_x + inset,
        "center": frame_x + (frame_w - box_w) // 2,
        "right": frame_x + frame_w - box_w - inset,
    }
    y_anchors = {
        "top": frame_y + inset,
        "middle": frame_y + (frame_h - box_h) // 2,
        "bottom": frame_y + frame_h - box_h - inset,
    }
    x = max(0, min(x_anchors.get(horizontal, x_anchors["center"]), canvas_w - box_w))
    y = max(0, min(y_anchors.get(vertical, y_anchors["middle"]), canvas_h - box_h))
    return (int(x), int(y), box_w, box_h)


@dataclass
class AnimationDescriptor:
    """Object-owned, runtime-only intent for one animated GIF."""

    frame_count: int
    displayed_index: int
    requested_index: int
    frame_duration_ms: int
    logical_size: tuple
    source_identity: tuple
    request_generation: int = 0
    loop_count: object = None
    playing: bool = False
    playback_generation: int = 0
    loop_repeats_completed: int = 0
    completed: bool = False
    playback_buffer: list = field(default_factory=list, repr=False)
    presented_frames: int = 0
    dropped_frames: int = 0
    late_frames: int = 0


class ImageObject:
    def __init__(self, source_path, canvas_width=None, canvas_height=None, object_id=None,
                 normalize_orientation=True):
        # Object identity is independent from the source path. Multiple canvas
        # objects may intentionally display the same file with different
        # transforms.
        self._object_id = object_id or uuid.uuid4().hex
        self.source_path = source_path
        self.normalize_orientation = normalize_orientation
        self.x = 0
        self.y = 0
        self.width = 200  # default
        self.height = 150  # default
        self.zoom_factor = 1.0
        self._minimum_zoom = 0.25
        self.viewport_offset = (0, 0)  # top-left corner within the image

        # Cache decoded source pixels and exactly one prepared display bitmap.
        # _source_revision changes whenever this object accepts different
        # source pixels; transform values are checked in _display_cache_key so
        # direct assignments used by arrangement/state/swap paths are safe.
        self._source_revision = 0
        self.source_identity = None
        self.__source_pixels = None
        self._aspect_ratio = None
        self._prepared_bitmap = None
        self._prepared_bitmap_key = None
        # Kept as an empty compatibility sentinel for older diagnostics/tests.
        # Drawing does not retain a duplicate PIL crop.
        self._visible_image = None

        # Status overlay system for user feedback
        self.status_message = None  # Text to show in overlay
        self.status_type = None  # 'info', 'warning', 'processing'
        self.show_status_overlay = False
        self._status_revision = 0
        self._status_deadline = None
        self.status_operation = None

        # Generation token and logical intent for object-scoped background work.
        self._work_generation = 0
        self._navigation_base_path = None
        self._navigation_steps = 0
        self._navigation_request_id = 0
        self.animation = None

        if canvas_width and canvas_height:
            self.canvas_w = canvas_width
            self.canvas_h = canvas_height
        else:
            self.canvas_w = None
            self.canvas_h = None

    @property
    def object_id(self):
        """Stable runtime identity for this canvas object."""
        return self._object_id

    @property
    def _original_image(self):
        if self.__source_pixels is None:
            return None
        return self.__source_pixels.pixels

    @_original_image.setter
    def _original_image(self, pixels):
        """Centralize even legacy direct decoded-pixel replacement."""
        previous = self.__source_pixels
        if previous is not None and previous.pixels is pixels:
            return
        self.__source_pixels = SharedSourcePixels(pixels) if pixels is not None else None
        self.source_identity = get_source_identity(pixels)
        self._source_revision += 1
        if pixels is None:
            self._aspect_ratio = 1.0
        else:
            w, h = pixels.size
            self._aspect_ratio = w / float(h) if h != 0 else 1.0
        self._adopt_animation_metadata(pixels)
        self._clear_image_caches()
        if previous is not None:
            previous.release_owner()

    def _adopt_animation_metadata(self, pixels):
        metadata = get_animation_metadata(pixels)
        if metadata is None or metadata.frame_count <= 1:
            self._close_animation_buffer()
            self.animation = None
            return
        previous = self.animation
        descriptor = AnimationDescriptor(
            frame_count=metadata.frame_count,
            displayed_index=metadata.frame_index,
            requested_index=metadata.frame_index,
            frame_duration_ms=effective_frame_duration_ms(metadata.duration_ms),
            logical_size=tuple(metadata.logical_size),
            source_identity=tuple(metadata.source_identity),
            loop_count=metadata.loop_count,
        )
        if (previous is not None
                and previous.frame_count == descriptor.frame_count
                and previous.source_identity == descriptor.source_identity):
            descriptor.request_generation = previous.request_generation
            descriptor.playing = previous.playing
            descriptor.playback_generation = previous.playback_generation
            descriptor.loop_repeats_completed = previous.loop_repeats_completed
            descriptor.completed = previous.completed
            descriptor.playback_buffer = previous.playback_buffer
            descriptor.presented_frames = previous.presented_frames
            descriptor.dropped_frames = previous.dropped_frames
            descriptor.late_frames = previous.late_frames
        elif previous is not None:
            self._close_animation_buffer(previous)
        self.animation = descriptor

    def _close_animation_buffer(self, descriptor=None):
        """Release all not-yet-adopted playback transfers exactly once."""
        descriptor = descriptor if descriptor is not None else self.animation
        if descriptor is None:
            return 0
        packets = list(getattr(descriptor, "playback_buffer", ()))
        descriptor.playback_buffer.clear()
        for packet in packets:
            packet.close()
        return len(packets)

    @property
    def is_animated(self):
        return self.animation is not None and self.animation.frame_count > 1

    def lease_source_pixels(self):
        """Lease the exact currently displayed source-pixel revision."""
        if self.__source_pixels is None:
            return None
        return self.__source_pixels.lease()

    def load_image(self):
        if self._original_image is None:
            # Always load fresh to ensure complete isolation between objects
            try:
                pixels = load_source_pixels(
                    self.source_path,
                    apply_orientation=self.normalize_orientation,
                    capture_animation=False)
                self._replace_source_pixels(pixels)
                self._minimum_zoom = min(0.25, self.zoom_factor)
            except Exception as e:
                logging.error(f"Failed to load image {self.source_path}: {e}")
                self._replace_source_pixels(None)

    def _replace_source_pixels(self, pixels):
        """Commit independent source pixels and invalidate this object's bitmap."""
        self._original_image = pixels

    def change_source_path(self, new_path, preloaded_image=None):
        """Change the source path and optionally use a preloaded image."""
        if new_path == self.source_path:
            return  # No change needed

        # A directory-discovery result for the prior source must not be applied
        # if the object is changed elsewhere and later returns to that path.
        self._work_generation += 1
        self.reset_navigation_intent()
        if self.show_status_overlay:
            self.clear_status_overlay()
        self.source_path = new_path

        # Use preloaded image if available, otherwise clear cache
        if preloaded_image and self.normalize_orientation:
            # Make sure we have a completely independent copy
            try:
                pixels = normalize_source_pixels(
                    preloaded_image, apply_orientation=self.normalize_orientation)
                self._replace_source_pixels(pixels)
            except Exception as e:
                logging.error(f"Failed to copy preloaded image for {new_path}: {e}")
                self._replace_source_pixels(None)
        else:
            # Legacy saved objects intentionally retain the former raw EXIF
            # interpretation, so they cannot consume an already-oriented
            # preload. Their next geometry/render request loads raw pixels.
            self._replace_source_pixels(None)

    def draw(self, dc, canvas_size=None, info_position="center", scale=1.0):
        """Draw prepared pixels and then transient object decorations."""
        self.draw_bitmap(dc)
        self.draw_decorations(dc, canvas_size, info_position, scale)

    def draw_bitmap(self, dc):
        """Draw cached image pixels, preparing them on a cache miss."""
        # The canvas owns the paint DC. Retaining it here would postpone
        # BufferedPaintDC's final blit until a later paint releases it.
        self.load_image()
        bitmap = self._get_prepared_bitmap()
        if bitmap is None:
            return False
        dc.DrawBitmap(bitmap, self.x, self.y, True)
        return True

    def draw_decorations(self, dc, canvas_size=None, info_position="center", scale=1.0):
        """Draw transient status UI; decorations are never cached as pixels."""
        if self.show_status_overlay and self.status_message:
            self._draw_status_overlay(dc, canvas_size, info_position, scale)

    def _display_cache_key(self):
        """Return all state that can change the prepared display pixels.

        Position, selection, z-order, canvas size, and status text are omitted:
        they affect placement/decorations, not raster preparation. Display
        scale is also omitted because the current renderer prepares in logical
        pixels and does not create scale-specific previews.
        """
        if self._original_image is None:
            return None
        return (
            self._source_revision,
            id(self._original_image),
            self._original_image.size,
            self._original_image.mode,
            self.source_path,
            self.normalize_orientation,
            self.zoom_factor,
            tuple(self.viewport_offset),
            self.width,
            self.height,
        )

    def _get_prepared_bitmap(self):
        key = self._display_cache_key()
        if key is None:
            return None
        if self._prepared_bitmap is not None and self._prepared_bitmap_key == key:
            return self._prepared_bitmap

        cropped = self._render_pil_crop()
        if cropped is None:
            self._clear_image_caches()
            return None
        try:
            bitmap = self._bitmap_from_pil(cropped)
        except Exception as e:
            logging.error(f"Error preparing bitmap for {self.source_path}: {e}")
            self._clear_image_caches()
            return None

        # Replace the old representation. No zoom/source history is retained.
        self._prepared_bitmap = bitmap
        self._prepared_bitmap_key = key
        return bitmap

    def _resize_source(self, size):
        """Resize source pixels; split out for deterministic instrumentation."""
        return self._original_image.resize(size, Image.Resampling.LANCZOS)

    @staticmethod
    def _bitmap_from_pil(image):
        """Create a GUI-thread-owned wx bitmap from normalized PIL pixels."""
        if "A" in image.getbands():
            rgba = image if image.mode == "RGBA" else image.convert("RGBA")
            rgb_bytes = rgba.convert("RGB").tobytes()
            alpha_bytes = rgba.getchannel("A").tobytes()
        else:
            rgb = image if image.mode == "RGB" else image.convert("RGB")
            rgb_bytes = rgb.tobytes()
            alpha_bytes = None

        wx_image = wx.Image(image.size[0], image.size[1])
        wx_image.SetData(rgb_bytes)
        if alpha_bytes is not None:
            wx_image.SetAlpha(alpha_bytes)
        return wx.Bitmap(wx_image)

    def contains(self, mx, my):
        """Check if the mouse point (mx,my) is inside this object's bounding box."""
        return (self.x <= mx <= self.x + self.width) and (self.y <= my <= self.y + self.height)

    def zoom_in(self):
        """Zoom in by 25% (up to 500%)."""
        self.load_image()
        new_zoom = self.zoom_factor * 1.25
        if new_zoom <= 5.0:
            old_zoom = self.zoom_factor
            self.zoom_factor = new_zoom
            self._update_dimensions_for_zoom(old_zoom, new_zoom)
            self._clear_image_caches()

            # Show zoom level feedback
            zoom_percent = int(self.zoom_factor * 100)
            self.set_status_overlay(f"Zoom: {zoom_percent}%", 'info')
            # Auto-clear after a short delay would be handled by the canvas
        else:
            # Show limit reached message
            self.set_status_overlay("Max zoom reached (500%)", 'warning')

    def zoom_out(self):
        """Zoom out toward 25%, or the smaller scale established by fitting."""
        self.load_image()
        new_zoom = self.zoom_factor * 0.8
        if new_zoom >= self._minimum_zoom or self.zoom_factor > self._minimum_zoom:
            new_zoom = max(new_zoom, self._minimum_zoom)
            old_zoom = self.zoom_factor
            self.zoom_factor = new_zoom
            self._update_dimensions_for_zoom(old_zoom, new_zoom)
            self._clear_image_caches()

            # Show zoom level feedback
            zoom_percent = int(self.zoom_factor * 100)
            self.set_status_overlay(f"Zoom: {zoom_percent}%", 'info')
            # Auto-clear after a short delay would be handled by the canvas
        else:
            # Show limit reached message
            self.set_status_overlay(
                f"Min zoom reached ({self._minimum_zoom * 100:g}%)", 'warning')

    def _update_dimensions_for_zoom(self, old_zoom, new_zoom):
        """Update object dimensions when zoom changes."""
        if not self._original_image:
            self.load_image()

        if self._original_image:
            # Calculate the new display size based on zoom
            base_width = self._original_image.width
            base_height = self._original_image.height

            # Update width and height to reflect the zoomed size
            self.width, self.height = scaled_size((base_width, base_height), new_zoom)

            # Adjust viewport offset to try to keep the same center point visible
            if old_zoom != 0:
                zoom_ratio = new_zoom / old_zoom
                center_x = self.viewport_offset[0] + (self.width / zoom_ratio) // 2
                center_y = self.viewport_offset[1] + (self.height / zoom_ratio) // 2

                new_vx = max(0, int(center_x - self.width // 2))
                new_vy = max(0, int(center_y - self.height // 2))

                # Ensure viewport doesn't exceed scaled image bounds
                max_vx = max(0, int(base_width * new_zoom) - self.width)
                max_vy = max(0, int(base_height * new_zoom) - self.height)

                self.viewport_offset = (min(new_vx, max_vx), min(new_vy, max_vy))

    def _clear_image_caches(self):
        """Discard this object's one prepared display representation."""
        self._prepared_bitmap = None
        self._prepared_bitmap_key = None
        self._visible_image = None

    def set_status_overlay(self, message, status_type='info', operation=None):
        """Set a status overlay message to display on the image object.

        Args:
            message: Text to display (None to hide overlay)
            status_type: 'info', 'warning', 'processing'
        """
        self._status_revision += 1
        self._status_deadline = None
        self.status_message = message
        self.status_type = status_type if message is not None else None
        self.status_operation = operation if message is not None else None
        self.show_status_overlay = message is not None
        return self._status_revision

    def clear_status_overlay(self):
        """Clear the status overlay."""
        return self.set_status_overlay(None)

    @property
    def status_revision(self):
        """Return the token for the currently displayed status."""
        return self._status_revision

    @property
    def status_deadline(self):
        """Return the absolute monotonic expiry, or ``None`` for persistent status."""
        return self._status_deadline

    def set_status_deadline(self, deadline, revision=None):
        """Attach an absolute monotonic deadline to the current status."""
        if (revision is not None and revision != self._status_revision) or not self.show_status_overlay:
            return False
        if self.status_type == 'processing':
            return False
        self._status_deadline = deadline
        return True

    def expire_status_if_due(self, now, revision=None):
        """Clear this status only when its current deadline has elapsed."""
        if (revision is not None and revision != self._status_revision) or not self.show_status_overlay:
            return False
        if self.status_type == 'processing' or self._status_deadline is None:
            return False
        if self._status_deadline > now:
            return False
        self.clear_status_overlay()
        return True

    def _draw_status_overlay(self, dc, canvas_size=None, info_position="center", scale=1.0):
        """Draw a status overlay on the image object."""
        if not self.status_message:
            return

        # Set up drawing parameters based on status type
        if self.status_type == 'processing':
            bg_color = wx.Colour(255, 165, 0, 180)  # Orange with transparency
            text_color = wx.Colour(0, 0, 0)
        elif self.status_type == 'warning':
            bg_color = wx.Colour(255, 69, 0, 180)  # Red-orange with transparency
            text_color = wx.Colour(255, 255, 255)
        else:  # 'info' or default
            bg_color = wx.Colour(70, 130, 180, 180)  # Steel blue with transparency
            text_color = wx.Colour(255, 255, 255)

        # Calculate overlay position and size
        overlay_padding = max(4, int(round(8 * max(0.5, float(scale)))))
        canvas_w, canvas_h = canvas_size or (self.x + self.width, self.y + self.height)
        available_width = max(1, int(canvas_w))
        max_text_width = max(1, available_width - 2 * overlay_padding)
        words = str(self.status_message).split()
        lines, current = [], ""
        for word in words or [""]:
            while dc.GetTextExtent(word).width > max_text_width and len(word) > 1:
                cut = len(word) - 1
                while cut > 1 and dc.GetTextExtent(word[:cut]).width > max_text_width:
                    cut -= 1
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:cut])
                word = word[cut:]
            candidate = word if not current else current + " " + word
            if current and dc.GetTextExtent(candidate).width > max_text_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current or not lines:
            lines.append(current)
        text_height = dc.GetTextExtent("Ag").height
        max_lines = max(1, (int(canvas_h) - 2 * overlay_padding) // max(1, text_height))
        lines = lines[:max_lines]
        if len(lines) < len(words) and lines:
            last = lines[-1]
            while last and dc.GetTextExtent(last + "…").width > max_text_width:
                last = last[:-1]
            lines[-1] = (last.rstrip() + "…") if last else "…"
        text_width = max((dc.GetTextExtent(line).width for line in lines), default=0)
        overlay_width = min(int(canvas_w), text_width + 2 * overlay_padding)
        overlay_height = min(int(canvas_h), len(lines) * text_height + 2 * overlay_padding)
        layout = layout_status_overlay(
            (self.x, self.y, self.width, self.height),
            (overlay_width, overlay_height), (canvas_w, canvas_h),
            info_position, overlay_padding)
        if layout is None:
            return
        overlay_x, overlay_y, overlay_width, overlay_height = layout

        # Draw semi-transparent background
        dc.SetBrush(wx.Brush(bg_color))
        dc.SetPen(wx.Pen(wx.Colour(0, 0, 0, 100)))
        dc.DrawRoundedRectangle(overlay_x, overlay_y, overlay_width, overlay_height, 4)

        # Draw text
        dc.SetTextForeground(text_color)
        text_x = overlay_x + overlay_padding
        text_y = overlay_y + overlay_padding
        for index, line in enumerate(lines):
            dc.DrawText(line, text_x, text_y + index * text_height)

    def get_pil_cropped(self):
        """Return independent RGBA pixels for export, never the display cache."""
        self.load_image()
        cropped = self._render_pil_crop()
        if cropped is None:
            return None
        if cropped.mode != "RGBA":
            cropped = cropped.convert("RGBA")
        return cropped

    def _render_pil_crop(self):
        """Render current geometry with Pillow for display preparation/export."""
        if self._original_image is None:
            return None
        try:
            return render_pil_crop(
                self._original_image,
                self.zoom_factor,
                (self.width, self.height),
                self.viewport_offset,
                resize_source=self._resize_source,
            )
        except ValueError:
            return None

    def set_canvas_size(self, canvas_width, canvas_height):
        self.canvas_w = canvas_width
        self.canvas_h = canvas_height

    def fit_to_bounds(self, width, height):
        """Show the whole image within bounds, preserving position if possible.

        This geometry operation leaves status messages alone. Invalid bounds
        leave the transform untouched until the caller has a valid layout.
        """
        self.load_image()
        if self._original_image is None:
            return False
        geometry = fit_geometry(self._original_image.size, (width, height))
        if geometry is None:
            return False
        self.zoom_factor, self.width, self.height = geometry
        self._minimum_zoom = min(0.25, self.zoom_factor)
        self.viewport_offset = (0, 0)
        self.x = min(max(0, self.x), width - self.width)
        self.y = min(max(0, self.y), height - self.height)
        self._clear_image_caches()
        return True

    def reset_size(self, fit_to_canvas=True):
        """Reset the image size to its original dimensions."""
        self.load_image()
        if not self._original_image:
            return False

        # Reset width and height to original dimensions
        self.width = max(1, self._original_image.width)
        self.height = max(1, self._original_image.height)

        if fit_to_canvas:
            # Missing or non-positive bounds can occur before initial layout or
            # while the window is minimized. In that case, restoring the
            # positive source dimensions is the safest useful reset.
            try:
                has_valid_bounds = self.canvas_w > 0 and self.canvas_h > 0
            except TypeError:
                has_valid_bounds = False

            if not has_valid_bounds:
                self._clear_image_caches()
                return True

            # Fit to canvas and adjust placement on that axis
            if self.width > self.canvas_w:
                self.width = self.canvas_w
                self.x = 0
            if self.height > self.canvas_h:
                self.height = self.canvas_h
                self.y = 0

        self._clear_image_caches()
        return True

    def reset_zoom(self):
        """Reset the zoom factor to 1.0."""
        old_zoom = self.zoom_factor
        self.zoom_factor = 1.0
        self._update_dimensions_for_zoom(old_zoom, 1.0)
        self._clear_image_caches()

        # Show feedback
        self.set_status_overlay("Zoom reset to 100%", 'info')
        return True

    def reset_viewport_offset(self):
        """Reset the viewport offset to (0, 0)."""
        self.viewport_offset = (0, 0)
        self._clear_image_caches()
        return True

    def force_refresh(self):
        """Explicitly discard this object's prepared display bitmap."""
        self._clear_image_caches()

    def advance_navigation_intent(self, step):
        """Accumulate wheel intent relative to the last displayed source."""
        if self._navigation_base_path is None:
            self._navigation_base_path = self.source_path
            self._navigation_steps = 0
        self._navigation_steps += step
        self._navigation_request_id += 1
        return (
            self._navigation_base_path,
            self._navigation_steps,
            self._navigation_request_id,
        )

    def reset_navigation_intent(self):
        """Forget attempted targets so the next request starts from displayed state."""
        self._navigation_base_path = None
        self._navigation_steps = 0

    def advance_animation_intent(self, step):
        """Clamp and record logical frame intent independently of display."""
        descriptor = self.animation
        if descriptor is None:
            return None
        return self.set_animation_intent(descriptor.requested_index + int(step))

    def set_animation_intent(self, target_index):
        """Clamp and record one absolute exact-frame intent."""
        descriptor = self.animation
        if (descriptor is None or isinstance(target_index, bool)
                or not isinstance(target_index, int)):
            return None
        target = min(descriptor.frame_count - 1, max(0, target_index))
        if target == descriptor.requested_index:
            return descriptor, False
        descriptor.request_generation += 1
        descriptor.requested_index = target
        return descriptor, True

    def start_animation_playback(self, now):
        """Start/resume playback and return immutable scheduler arguments."""
        descriptor = self.animation
        if descriptor is None or descriptor.playing:
            return None
        self._close_animation_buffer(descriptor)
        descriptor.playback_generation += 1
        descriptor.playing = True
        restart = descriptor.completed
        if restart:
            descriptor.loop_repeats_completed = 0
            descriptor.completed = False
        return {
            "object_id": self.object_id,
            "path": self.source_path,
            "source_identity": descriptor.source_identity,
            "playback_generation": descriptor.playback_generation,
            "frame_count": descriptor.frame_count,
            "logical_size": descriptor.logical_size,
            "loop_count": descriptor.loop_count,
            "displayed_index": descriptor.displayed_index,
            "displayed_duration_ms": effective_frame_duration_ms(
                descriptor.frame_duration_ms),
            "repeats_completed": descriptor.loop_repeats_completed,
            "started_at": float(now),
            "restart": restart,
        }

    def pause_animation_playback(self):
        """Freeze committed pixels and invalidate buffered playback results."""
        descriptor = self.animation
        if descriptor is None:
            return False
        was_playing = descriptor.playing
        descriptor.playing = False
        descriptor.playback_generation += 1
        self._close_animation_buffer(descriptor)
        return was_playing

    def fail_animation_playback(self):
        descriptor = self.animation
        if descriptor is None:
            return False
        descriptor.playing = False
        descriptor.playback_generation += 1
        self._close_animation_buffer(descriptor)
        return True

    def commit_playback_candidate(self, pixels, packet):
        """Publish one current-generation playback frame only."""
        descriptor = self.animation
        metadata = get_animation_metadata(pixels)
        if descriptor is None or metadata is None:
            raise ValueError("playback frame candidate is incomplete")
        if (not descriptor.playing
                or packet.object_id != self.object_id
                or packet.source_path != self.source_path
                or packet.source_identity != descriptor.source_identity
                or packet.playback_generation != descriptor.playback_generation
                or metadata.source_identity != descriptor.source_identity
                or metadata.frame_index != packet.frame_index
                or metadata.frame_count != descriptor.frame_count
                or tuple(metadata.logical_size) != descriptor.logical_size):
            raise ValueError("playback frame candidate is stale or inconsistent")
        self._original_image = pixels
        committed = self.animation
        committed.displayed_index = packet.frame_index
        committed.requested_index = packet.frame_index
        committed.frame_duration_ms = effective_frame_duration_ms(
            packet.duration_ms)
        committed.loop_repeats_completed = packet.repeat_index
        committed.presented_frames += 1
        if packet.terminal:
            committed.playing = False
            committed.completed = True
        return True

    def cancel_animation_intent(self):
        """Reject late frame work and restore intent to committed pixels."""
        descriptor = self.animation
        if descriptor is None:
            return False
        changed = descriptor.requested_index != descriptor.displayed_index
        descriptor.request_generation += 1
        descriptor.requested_index = descriptor.displayed_index
        if self.status_operation == "animation" and self.status_type == "processing":
            self.clear_status_overlay()
        return changed

    def commit_animation_candidate(self, pixels, frame_index, source_identity,
                                   request_generation, *,
                                   allow_source_identity_refresh=False):
        """Publish a validated frame without changing object geometry."""
        descriptor = self.animation
        metadata = get_animation_metadata(pixels)
        if descriptor is None or metadata is None:
            raise ValueError("animation frame candidate is incomplete")
        requested_identity = tuple(source_identity)
        candidate_identity = tuple(metadata.source_identity)
        if requested_identity != descriptor.source_identity:
            raise ValueError("animation source identity changed")
        if (candidate_identity != descriptor.source_identity
                and not allow_source_identity_refresh):
            raise ValueError("animation source identity changed")
        if (int(request_generation) != descriptor.request_generation
                or int(frame_index) != descriptor.requested_index
                or metadata.frame_index != int(frame_index)
                or metadata.frame_count != descriptor.frame_count
                or tuple(metadata.logical_size) != descriptor.logical_size):
            raise ValueError("animation frame candidate is stale or inconsistent")

        self._original_image = pixels
        committed = self.animation
        committed.request_generation = int(request_generation)
        committed.displayed_index = int(frame_index)
        committed.requested_index = int(frame_index)
        return True

    def commit_navigation_candidate(self, new_path, pixels, geometry, bounds):
        """Publish validated path, independent pixels, and fitted geometry together."""
        if pixels is None or not isinstance(new_path, str) or not new_path:
            raise ValueError("navigation candidate is incomplete")
        try:
            scale, width, height = geometry
            canvas_w, canvas_h = bounds
        except (TypeError, ValueError) as exc:
            raise ValueError("navigation candidate geometry is incomplete") from exc
        if (scale <= 0 or width <= 0 or height <= 0
                or canvas_w <= 0 or canvas_h <= 0
                or width > canvas_w or height > canvas_h):
            raise ValueError("navigation candidate does not fit the canvas")

        self.source_path = new_path
        self._original_image = pixels
        self.zoom_factor = scale
        self.width = width
        self.height = height
        self._minimum_zoom = min(0.25, scale)
        self.viewport_offset = (0, 0)
        self.canvas_w, self.canvas_h = bounds
        self.x = min(max(0, self.x), canvas_w - width)
        self.y = min(max(0, self.y), canvas_h - height)
        self._work_generation += 1
        self.reset_navigation_intent()
        self.clear_status_overlay()
        return True

    def commit_drop_candidate(self, pixels, geometry, bounds, position):
        """Publish already-decoded initial pixels and whole-image fit geometry."""
        if pixels is None:
            raise ValueError("drop candidate has no pixels")
        try:
            scale, width, height = geometry
            canvas_w, canvas_h = bounds
            intended_x, intended_y = position
        except (TypeError, ValueError) as exc:
            raise ValueError("drop candidate geometry is incomplete") from exc
        if (scale <= 0 or width <= 0 or height <= 0
                or canvas_w <= 0 or canvas_h <= 0
                or width > canvas_w or height > canvas_h):
            raise ValueError("drop candidate does not fit the canvas")

        self._original_image = pixels
        self.zoom_factor = scale
        self.width = width
        self.height = height
        self._minimum_zoom = min(0.25, scale)
        self.viewport_offset = (0, 0)
        self.canvas_w, self.canvas_h = bounds
        self.x = min(max(0, intended_x), canvas_w - width)
        self.y = min(max(0, intended_y), canvas_h - height)
        return True

    def commit_scene_candidate(self, pixels, record, bounds=None):
        """Attach decoded pixels while preserving the validated saved transform."""
        if pixels is None or not isinstance(record, dict):
            raise ValueError("scene candidate is incomplete")
        required = (
            "source_path", "x", "y", "width", "height",
            "zoom_factor", "viewport_offset",
        )
        if any(name not in record for name in required):
            raise ValueError("scene candidate record is incomplete")
        animation = record.get("animation")
        if animation is not None:
            metadata = get_animation_metadata(pixels)
            if (metadata is None
                    or metadata.frame_index != animation["frame_index"]
                    or metadata.frame_count <= 1):
                raise ValueError(
                    "scene animation candidate does not match its saved frame")

        self.source_path = record["source_path"]
        self._original_image = pixels
        self.x = record["x"]
        self.y = record["y"]
        self.width = record["width"]
        self.height = record["height"]
        self.zoom_factor = record["zoom_factor"]
        self._minimum_zoom = min(0.25, self.zoom_factor)
        self.viewport_offset = tuple(record["viewport_offset"])
        if bounds is not None:
            canvas_w, canvas_h = bounds
            if canvas_w > 0 and canvas_h > 0:
                self.canvas_w, self.canvas_h = canvas_w, canvas_h
        return True

    def commit_duplicate_candidate(self, pixels, snapshot):
        """Attach copied pixels and the source object's exact transform."""
        if pixels is None or snapshot is None:
            raise ValueError("duplicate candidate is incomplete")
        self.source_path = snapshot.source_path
        self.normalize_orientation = snapshot.normalize_orientation
        self._original_image = pixels
        self.x = snapshot.x
        self.y = snapshot.y
        self.width = snapshot.width
        self.height = snapshot.height
        self.zoom_factor = snapshot.zoom_factor
        self._minimum_zoom = snapshot.minimum_zoom
        self.viewport_offset = tuple(snapshot.viewport_offset)
        if snapshot.canvas_size is not None:
            self.canvas_w, self.canvas_h = snapshot.canvas_size
        for name, value in snapshot.frame_metadata:
            setattr(self, name, value)
        return True

    def dispose_source_pixels(self):
        """Release pixels owned solely by a retired or rejected object."""
        pixels = self._original_image
        self._original_image = None
        return pixels is not None

    def cancel_pending_work(self):
        """Invalidate any pending work associated with this canvas object."""
        self._work_generation += 1
        self.reset_navigation_intent()
        self.cancel_animation_intent()
        self.pause_animation_playback()
        if self.status_type == 'processing':
            self.clear_status_overlay()

    def __repr__(self):
        # Calculate memory usage for PIL Images
        if self._original_image:
            # PIL Images: width * height * number of channels * bytes per channel
            orig_mem = self._original_image.width * self._original_image.height * len(self._original_image.getbands())
            mem_orig = bytes_to_human_readable(orig_mem)
        else:
            mem_orig = "0 B"

        if self._prepared_bitmap:
            bitmap_mem = (self._prepared_bitmap.GetWidth() *
                          self._prepared_bitmap.GetHeight() * 4)
            mem_bitmap = bytes_to_human_readable(bitmap_mem)
        else:
            mem_bitmap = "0 B"

        return (
            f"ImageObject(id={self.object_id}, {self.source_path}, x={self.x}, y={self.y}, "
            f"w={self.width}, h={self.height}, "
            f"mem_orig={mem_orig}, "
            f"mem_bitmap={mem_bitmap})"
        )
