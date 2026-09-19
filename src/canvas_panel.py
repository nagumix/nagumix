# src/canvas_panel.py
import wx
import os
import math
import threading
import time
from dataclasses import dataclass, field, replace
# from PIL import ImageDraw
from .image_geometry import fit_geometry
from .viewport_geometry import (
    GeometryError, from_object as viewport_from_object,
    apply_to_object as apply_viewport, resize_frame, reposition_content,
    reset_frame_size, reduced as viewport_reduced, handle_centers, hit_handle,
    Rect as ViewRect,
)
from .image_object import ImageObject
from .image_pixels import (
    SOURCE_PIXEL_NORMALIZATION_VERSION,
    load_animation_frame,
)
from .exporting import (
    EXPORT_FORMATS,
    ExportCancellation,
    ExportObjectSnapshot,
    ExportSnapshot,
    ExportTask,
    resolve_export_path,
)
from .utils import snap_to_nearby_edges
from .file_navigator import (
    DuplicationDecodeResult,
    FileNavigator,
    path_comparison_key,
)
from .canvas_state import read_state, write_state
from .canvas_saving import (
    CanvasSaveCancellation,
    CanvasSaveObjectSnapshot,
    CanvasSaveSnapshot,
    CanvasSaveTask,
)
from .filename_suggestions import filename_base, suggested_filename, timestamp_base
from .animation_controls import (
    CONTROL_LABELS,
    FADE_TICK_MS,
    FOCUS_NAMES,
    TIMELINE_PREVIEW_DELAY_MS,
    AnimationControlState,
    animation_control_presentation,
    layout_animation_controls,
    timeline_frame_at,
    timeline_thumb_x,
)
from .settings_manager import (
    OVERLAY_TIMEOUT_DEFAULT_MS,
    OVERLAY_TIMEOUT_MAX_MS,
    OVERLAY_TIMEOUT_MIN_MS,
)
import logging


SUCCESS_CARD_DISMISS_MS = 2500
_FRAME_METADATA_NAMES = (
    "frame_index", "current_frame", "frame_count", "frame_duration",
    "is_playing",
)


def same_canvas_object(left, right):
    """Return whether two references identify the same canvas object."""
    if left is None or right is None:
        return False
    return left is right or left.object_id == right.object_id


def _reschedule_status_timer(canvas, **kwargs):
    """Invoke the timer scheduler when a headless compatibility surface has it."""
    reschedule = getattr(canvas, "_reschedule_overlay_timer", None)
    if reschedule is not None:
        return reschedule(**kwargs)
    return None


def _cancel_navigation_decode(canvas, image_object):
    """Cancel navigator decode ownership without changing object generation."""
    navigator = getattr(canvas, "file_navigator", None)
    cancel = getattr(navigator, "cancel_navigation_decode", None)
    if cancel is not None:
        cancel(image_object.object_id)


def _cancel_animation_decode(canvas, image_object):
    """Cancel owned GIF reconstruction without changing displayed pixels."""
    navigator = getattr(canvas, "file_navigator", None)
    cancel = getattr(navigator, "cancel_animation_frame", None)
    if cancel is not None:
        cancel(image_object.object_id)


def _cancel_animation_playback(canvas, image_object):
    """Cancel decoder ownership and freeze one object's committed GIF frame."""
    navigator = getattr(canvas, "file_navigator", None)
    cancel = getattr(navigator, "cancel_gif_playback", None)
    if cancel is not None:
        cancel(image_object.object_id)
    return image_object.pause_animation_playback()


def _cancel_object_work(canvas, image_object):
    """Cancel navigator ownership and invalidate callbacks for one object."""
    _cancel_navigation_decode(canvas, image_object)
    _cancel_animation_decode(canvas, image_object)
    _cancel_animation_playback(canvas, image_object)
    image_object.cancel_pending_work()


def _cancel_duplication(canvas, source_id, *, reason):
    cancel = getattr(canvas, "cancel_duplication_operation", None)
    if cancel is not None:
        return cancel(source_id, reason=reason)
    return False


def _cancel_all_duplication(canvas, *, reason):
    cancel = getattr(canvas, "cancel_all_duplication_operations", None)
    if cancel is not None:
        return cancel(reason=reason)
    return False


def _navigation_context_parts(context):
    """Read legacy and revisioned navigation callback contexts."""
    image_object, generation, base_path = context[:3]
    request_id = context[3] if len(context) >= 4 else None
    return image_object, generation, base_path, request_id


@dataclass
class DropEntry:
    """One path retained by a canvas drop operation."""

    request_id: int
    path: str
    position: tuple
    state: str = "accepted"
    error: str = None
    image_object: object = None

    @property
    def is_terminal(self):
        return self.state in ("succeeded", "failed", "canceled")


@dataclass
class DropOperation:
    """Small runtime-only aggregate for one appendable drop operation."""

    generation: int
    anchor: tuple
    interaction_revision: int
    entries: list = field(default_factory=list)
    active_requests: set = field(default_factory=set)
    revision: int = 1
    canceled: bool = False
    terminal: bool = False
    cancel_reason: str = None
    card_deadline: float = None
    card_presentation_revision: int = 0

    def touch(self):
        self.revision += 1

    @property
    def total(self):
        return len(self.entries)

    @property
    def completed(self):
        # Canceled entries are reported separately rather than counted as
        # completed file outcomes.
        return self.succeeded + self.failed

    @property
    def resolved(self):
        return self.completed + self.canceled_count

    @property
    def succeeded(self):
        return sum(entry.state == "succeeded" for entry in self.entries)

    @property
    def failed(self):
        return sum(entry.state == "failed" for entry in self.entries)

    @property
    def canceled_count(self):
        return sum(entry.state == "canceled" for entry in self.entries)


@dataclass
class SceneEntry:
    """One validated document record in a staged scene operation."""

    request_id: int
    record: dict
    state: str = "accepted"
    error: str = None
    image_object: object = None

    @property
    def is_terminal(self):
        return self.state in ("succeeded", "failed", "canceled")


@dataclass
class SceneLoadOperation:
    """Runtime-only state for one transactional saved-scene request."""

    generation: int
    path: str
    stage: str = "reading"
    entries: list = field(default_factory=list)
    active_requests: set = field(default_factory=set)
    revision: int = 1
    terminal: bool = False
    canceled: bool = False
    committed: bool = False
    error: str = None
    card_deadline: float = None
    card_presentation_revision: int = 0

    def touch(self):
        self.revision += 1

    @property
    def total(self):
        return len(self.entries)

    @property
    def decoded(self):
        return self.succeeded + self.failed

    @property
    def resolved(self):
        return self.decoded + self.canceled_count

    @property
    def succeeded(self):
        return sum(entry.state == "succeeded" for entry in self.entries)

    @property
    def failed(self):
        return sum(entry.state == "failed" for entry in self.entries)

    @property
    def canceled_count(self):
        return sum(entry.state == "canceled" for entry in self.entries)


@dataclass
class ExportOperation:
    """Runtime-only state for one asynchronous atomic export."""

    generation: int
    destination: str
    format_name: str
    width: int
    height: int
    total: int
    cancellation: object
    request_key: object
    document_identity: int = 0
    naming_request: int = 0
    stage: str = "preparing"
    rendered: int = 0
    visible: int = 0
    clipped: int = 0
    outside: int = 0
    failures: tuple = ()
    error: str = None
    terminal: bool = False
    committed: bool = False
    revision: int = 1
    card_deadline: float = None
    card_presentation_revision: int = 0

    def touch(self):
        self.revision += 1


@dataclass
class SaveOperation:
    """Runtime-only state for one asynchronous atomic canvas save."""

    generation: int
    destination: str
    total: int
    cancellation: object
    request_key: object
    document_identity: int
    naming_request: int
    include_file_identification: bool
    stage: str = "hashing"
    completed: int = 0
    warnings: tuple = ()
    error: str = None
    terminal: bool = False
    committed: bool = False
    revision: int = 1
    card_deadline: float = None
    card_presentation_revision: int = 0

    def touch(self):
        self.revision += 1


@dataclass(frozen=True)
class DuplicationSnapshot:
    """Invocation-time primitive state plus a lease on committed source pixels."""

    document_identity: int
    source_object_id: str
    source_path: str
    normalize_orientation: bool
    source_revision: int
    x: object
    y: object
    width: object
    height: object
    zoom_factor: object
    viewport_offset: tuple
    minimum_zoom: object
    canvas_size: tuple
    frame_metadata: tuple
    pixel_lease: object = field(repr=False, compare=False)

    @property
    def pixels(self):
        return self.pixel_lease.pixels if self.pixel_lease is not None else None

    def release(self):
        if self.pixel_lease is None:
            return False
        return self.pixel_lease.release()


class DuplicationCancellation:
    """Small cancellation boundary for one bounded pixel-copy task."""

    def __init__(self):
        self._lock = threading.Lock()
        self._canceled = False

    @property
    def canceled(self):
        with self._lock:
            return self._canceled

    def cancel(self):
        with self._lock:
            self._canceled = True
            return True


class DuplicationTask:
    """Copy leased Pillow pixels off the GUI thread and release the lease."""

    def __init__(self, snapshot, *, copy_pixels=None, cancellation=None):
        self.snapshot = snapshot
        self.cancellation = cancellation or DuplicationCancellation()
        self._copy_pixels = copy_pixels or (lambda pixels: pixels.copy())
        self._released = False
        self._release_lock = threading.Lock()

    def _release_snapshot(self):
        with self._release_lock:
            if self._released:
                return False
            self._released = True
        return self.snapshot.release()

    def cancel(self):
        self.cancellation.cancel()
        return True

    def cancel_before_start(self):
        self.cancel()
        self._release_snapshot()

    def run(self):
        try:
            if self.cancellation.canceled:
                return None
            pixels = self.snapshot.pixels
            if pixels is None:
                raise ValueError("committed source pixels are no longer available")
            copied = self._copy_pixels(pixels)
            if copied is pixels:
                raise ValueError("duplicate worker returned shared source pixels")
            copied.load()
            return copied
        finally:
            self._release_snapshot()


@dataclass
class DuplicationOperation:
    """Runtime-only state for one source object's pending duplicate."""

    generation: int
    request_key: object
    snapshot: DuplicationSnapshot
    cancellation: DuplicationCancellation
    interaction_revision: int
    terminal: bool = False
    stage: str = "copying"
    error: str = None


class ImageObjectList(list):
    """Ordered canvas objects with ID-based membership and uniqueness."""

    def __init__(self, objects=()):
        super().__init__()
        self.extend(objects)

    @staticmethod
    def _validate_unique(objects):
        object_ids = [obj.object_id for obj in objects]
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("duplicate canvas object ID")

    def index_for(self, target):
        for index, obj in enumerate(self):
            if same_canvas_object(obj, target):
                return index
        raise ValueError("image object is not on this canvas")

    def contains_object(self, target):
        try:
            self.index_for(target)
            return True
        except ValueError:
            return False

    def append(self, obj):
        if self.contains_object(obj):
            raise ValueError(f"duplicate canvas object ID: {obj.object_id}")
        super().append(obj)

    def extend(self, objects):
        for obj in objects:
            self.append(obj)

    def insert(self, index, obj):
        if self.contains_object(obj):
            raise ValueError(f"duplicate canvas object ID: {obj.object_id}")
        super().insert(index, obj)

    def __setitem__(self, index, value):
        candidate = list(self)
        candidate[index] = value
        self._validate_unique(candidate)
        super().__setitem__(index, value)

    def __iadd__(self, objects):
        self.extend(objects)
        return self

    def remove(self, target):
        return self.pop(self.index_for(target))

    def move_to_front(self, target):
        obj = self.pop(self.index_for(target))
        super().append(obj)


class CanvasPanel(wx.Panel):
    MAX_DROP_IN_FLIGHT = 3
    MAX_SCENE_IN_FLIGHT = 3

    def __init__(self, parent, settings_manager, monotonic_clock=None,
                 wall_clock=None):
        super().__init__(parent, style=wx.WANTS_CHARS)
        self.settings_manager = settings_manager
        self._monotonic = monotonic_clock or time.monotonic
        self._wall_clock = wall_clock or __import__("datetime").datetime.now
        self._document_identity = 0
        self._naming_request = 0
        self._last_naming_success = 0
        self._suggested_base = None

        self.image_objects = ImageObjectList()
        self.selected_object = None
        # Context menus retain only an immutable identity token; deleted
        # objects are resolved again by MainFrame when the command dispatches.
        self._context_object_id = None
        self.marked_object = None
        self._zoom_wheel_remainders = {}
        self._interaction_revision = 0
        self._drop_generation = 0
        self.drop_operation = None
        self._drop_card_rect = None
        self._drop_cancel_rect = None
        self._scene_generation = 0
        self.scene_operation = None
        self._scene_card_rect = None
        self._scene_cancel_rect = None
        self._export_generation = 0
        self.export_operation = None
        self._export_card_rect = None
        self._export_cancel_rect = None
        self._save_generation = 0
        self.save_operation = None
        self._save_card_rect = None
        self._save_cancel_rect = None
        self._duplication_generation = 0
        self._duplication_operations = {}
        self._animation_controls = AnimationControlState(self._monotonic)
        self._animation_control_pointer = None

        # Initialize file navigator
        self.file_navigator = FileNavigator(
            settings_manager,
            monotonic_clock=self._monotonic,
            result_dispatch=lambda callback, result: wx.CallAfter(callback, result),
        )

        # Try to avoid flickering due to redraws on-screen
        self.SetDoubleBuffered(True)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)

        # Canvas background color (could come from settings)
        self.canvas_bg = self.settings_manager.get_setting("Canvas", "background_color", fallback="#FFFFFF")

        # Enable drag-and-drop
        self.SetDropTarget(FileDropTarget(self))

        # For drag
        self.drag_offset = None
        self._viewport_gesture = None
        self._viewport_pointer = None
        self._viewport_hover_handle = None

        # Bind events
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_left_up)
        self.Bind(wx.EVT_MOTION, self.on_mouse_move)
        self.Bind(wx.EVT_LEAVE_WINDOW, self.on_mouse_leave)
        self.Bind(wx.EVT_KILL_FOCUS, self.on_kill_focus)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self.on_mouse_capture_lost)
        self.Bind(wx.EVT_RIGHT_DOWN, self.on_right_down)
        self.Bind(wx.EVT_KEY_DOWN, self.on_key_down)
        self.Bind(wx.EVT_SIZE, self.on_size)
        self.Bind(wx.EVT_MOUSEWHEEL, self.on_mouse_wheel)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)

        # Timer for overlay management
        self.overlay_clear_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_overlay_timer, self.overlay_clear_timer)
        self.playback_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_playback_timer, self.playback_timer)
        self.animation_control_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_animation_control_timer,
                  self.animation_control_timer)
        self.timeline_preview_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timeline_preview_timer,
                  self.timeline_preview_timer)

        # For resizing or panning
        self.resizing = False
        self.resizing_edge = None  # 'corner' or 'side'
        self.original_rect = None
        self.original_mouse_pos = None

        # Set focus so keys work immediately without needing to click first
        wx.CallAfter(self.SetFocus)

    def on_size(self, event):
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        if canvas_w > 0 and canvas_h > 0:
            for obj in self.image_objects:
                obj.set_canvas_size(canvas_w, canvas_h)
            CanvasPanel._pump_drop_operation(self)
        self.Refresh()
        event.Skip()

    def on_destroy(self, event):
        """Start terminal preload cleanup when this canvas is destroyed."""
        if event.GetEventObject() is self:
            CanvasPanel._cancel_viewport_gesture(self, restore=False)
            self._stop_overlay_timer()
            CanvasPanel._stop_playback_timer(self)
            CanvasPanel._clear_animation_controls(self)
            _cancel_all_duplication(self, reason="window closed")
            CanvasPanel.cancel_drop_operation(
                self, clear=True, reason="window closed")
            CanvasPanel.cancel_scene_operation(
                self, clear=True, reason="window closed")
            CanvasPanel.cancel_export_operation(
                self, clear=True, reason="window closed")
            CanvasPanel.cancel_save_operation(
                self, clear=True, reason="window closed")
            for obj in tuple(self.image_objects):
                _cancel_object_work(self, obj)
                obj.dispose_source_pixels()
            self.file_navigator.shutdown()
        event.Skip()

    def get_client_dimensions(self):
        """Return the drawable canvas dimensions, excluding decorations."""
        get_client_size = getattr(self, "GetClientSize", None)
        size = get_client_size() if get_client_size else self.GetSize()
        try:
            return size.width, size.height
        except AttributeError:
            return size[0], size[1]

    def get_selected_object(self):
        if self.image_objects.contains_object(self.selected_object):
            return self.selected_object
        return None

    def set_selected_object(self, obj):
        if obj is not None and not self.image_objects.contains_object(obj):
            raise ValueError("cannot select an object that is not on the canvas")
        previous = self.get_selected_object()
        gesture = getattr(self, "_viewport_gesture", None)
        if gesture is not None and (obj is None or obj.object_id != gesture["object_id"]):
            CanvasPanel._cancel_viewport_gesture(self)
        self._interaction_revision = getattr(self, "_interaction_revision", 0) + 1
        if previous is not None and not same_canvas_object(previous, obj):
            # Selection redirects exact stepping/navigation intent, but does
            # not stop an independently started animation.
            _cancel_navigation_decode(self, previous)
            _cancel_animation_decode(self, previous)
            previous.cancel_animation_intent()
        self._zoom_wheel_remainders.clear()
        self.selected_object = obj
        # Start preloading when an object is selected
        if obj:
            self.start_preloading_for_object(obj)
        reschedule = getattr(self, "_reschedule_overlay_timer", None)
        if reschedule is not None:
            reschedule()
        self.Refresh()

    def add_image_object(self, obj):
        """Add one unique object to the back-to-front canvas order."""
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        if canvas_w > 0 and canvas_h > 0:
            obj.set_canvas_size(canvas_w, canvas_h)
        self.image_objects.append(obj)

    def prepare_canvas_edit(self, reason="canvas edited", *, clear_scene=True):
        """Invalidate a pending scene before a deliberate content edit."""
        CanvasPanel.cancel_scene_operation(
            self, clear=clear_scene, reason=reason)
        self._interaction_revision = getattr(self, "_interaction_revision", 0) + 1

    def _note_user_interaction(self):
        CanvasPanel.prepare_canvas_edit(self, "canvas edited")

    @staticmethod
    def _drop_request_key(operation, entry):
        return "drop", operation.generation, entry.request_id

    def accept_drop(self, x, y, filenames):
        """Capture a drop synchronously and schedule only bounded worker work."""
        paths = list(filenames)
        if not paths or self.file_navigator.is_shutdown:
            return False
        CanvasPanel.prepare_canvas_edit(self, "new images were dropped")

        operation = getattr(self, "drop_operation", None)
        if operation is None or operation.terminal:
            if operation is not None:
                CanvasPanel._retire_drop_card(self)
            self._drop_generation = getattr(self, "_drop_generation", 0) + 1
            operation = DropOperation(
                self._drop_generation, (x, y),
                getattr(self, "_interaction_revision", 0))
            self.drop_operation = operation
            _reschedule_status_timer(self)

        first_index = operation.total
        supported = self.file_navigator.SUPPORTED_EXTENSIONS
        for offset, raw_path in enumerate(paths):
            try:
                path = os.fspath(raw_path)
            except TypeError:
                path = str(raw_path)
            entry = DropEntry(
                first_index + offset + 1,
                path,
                (x + offset * 20, y + offset * 20),
            )
            if os.path.splitext(path)[1].lower() not in supported:
                entry.state = "failed"
                entry.error = "unsupported file type"
            operation.entries.append(entry)

        operation.touch()
        self.Refresh()
        self._pump_drop_operation()
        self._finalize_drop_operation()
        return True

    def _pump_drop_operation(self):
        """Feed at most three drop entries into the shared decode scheduler."""
        operation = getattr(self, "drop_operation", None)
        if operation is None or operation.terminal or operation.canceled:
            return False
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        if canvas_w <= 0 or canvas_h <= 0:
            return False

        callback = getattr(self, "_on_drop_decoded", None)
        if callback is None:
            callback = CanvasPanel._on_drop_decoded.__get__(self)
        changed = False
        while len(operation.active_requests) < self.MAX_DROP_IN_FLIGHT:
            entry = next((candidate for candidate in operation.entries
                          if candidate.state in ("accepted", "waiting_bounds")), None)
            if entry is None:
                break
            request_key = self._drop_request_key(operation, entry)
            entry.state = "submitted"
            operation.active_requests.add(request_key)
            accepted = self.file_navigator.request_drop_decode(
                entry.path, request_key,
                (operation.generation, entry.request_id), callback)
            if not accepted:
                operation.active_requests.discard(request_key)
                entry.state = "accepted"
                if self.file_navigator.is_shutdown:
                    self.cancel_drop_operation(reason="image service stopped")
                break
            changed = True
        if changed:
            operation.touch()
            self.Refresh()
        return changed

    def _find_drop_entry(self, operation, request_id):
        return next((entry for entry in operation.entries
                     if entry.request_id == request_id), None)

    def _on_drop_decoded(self, result):
        """Validate and publish one current drop result on the GUI thread."""
        try:
            generation, request_id = result.context
        except (TypeError, ValueError):
            result.close()
            return
        operation = getattr(self, "drop_operation", None)
        entry = (self._find_drop_entry(operation, request_id)
                 if operation is not None else None)
        request_key = ("drop", generation, request_id)
        if (operation is None or operation.generation != generation
                or operation.terminal or entry is None
                or entry.state != "submitted"
                or request_key not in operation.active_requests):
            result.close()
            logging.debug("Released stale drop decode result")
            return

        operation.active_requests.discard(request_key)
        if result.error is not None or result.pixels is None:
            result.close()
            entry.state = "failed"
            entry.error = result.error or "decoder returned no pixels"
        else:
            canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
            if canvas_w <= 0 or canvas_h <= 0:
                result.close()
                entry.state = "waiting_bounds"
            else:
                geometry = fit_geometry(result.pixels.size, (canvas_w, canvas_h))
                if geometry is None:
                    result.close()
                    entry.state = "failed"
                    entry.error = "image cannot be fitted to the canvas"
                else:
                    pixels = result.take_pixels()
                    image_object = ImageObject(
                        entry.path, canvas_width=canvas_w, canvas_height=canvas_h)
                    try:
                        image_object.commit_drop_candidate(
                            pixels, geometry, (canvas_w, canvas_h), entry.position)
                    except Exception as exc:
                        pixels.close()
                        logging.exception("Failed to publish dropped image")
                        entry.state = "failed"
                        entry.error = str(exc)
                    else:
                        entry.state = "succeeded"
                        entry.image_object = image_object
                        self._insert_drop_object(operation, entry)

        operation.touch()
        self._pump_drop_operation()
        self._finalize_drop_operation()
        self.Refresh()

    def _insert_drop_object(self, operation, entry):
        """Insert a success in input order without retaining decoded waiters."""
        earlier = [candidate.image_object for candidate in operation.entries
                   if candidate.request_id < entry.request_id
                   and candidate.state == "succeeded"]
        later = [candidate.image_object for candidate in operation.entries
                 if candidate.request_id > entry.request_id
                 and candidate.state == "succeeded"]
        if later:
            insertion = min(self.image_objects.index_for(obj) for obj in later)
            self.image_objects.insert(insertion, entry.image_object)
        elif earlier:
            insertion = max(self.image_objects.index_for(obj) for obj in earlier) + 1
            self.image_objects.insert(insertion, entry.image_object)
        else:
            self.add_image_object(entry.image_object)

    def _finalize_drop_operation(self):
        operation = getattr(self, "drop_operation", None)
        if (operation is None or operation.terminal
                or operation.resolved != operation.total):
            return False
        operation.terminal = True
        operation.touch()
        successes = [entry for entry in operation.entries
                     if entry.state == "succeeded"]
        if (successes and getattr(self, "_interaction_revision", 0)
                == operation.interaction_revision):
            last_object = successes[-1].image_object
            previous = self.get_selected_object()
            if previous is not None and not same_canvas_object(previous, last_object):
                _cancel_object_work(self, previous)
            self._zoom_wheel_remainders.clear()
            self.selected_object = last_object
            self.start_preloading_for_object(last_object)
            _reschedule_status_timer(self)
        self.Refresh()
        return True

    def cancel_drop_operation(self, *, clear=False, reason="canceled by user"):
        """Cancel only drop-owned work; already committed images remain."""
        operation = getattr(self, "drop_operation", None)
        if operation is None:
            return False
        if not operation.terminal:
            cancel = getattr(self.file_navigator, "cancel_drop_decode", None)
            for request_key in tuple(operation.active_requests):
                if cancel is not None:
                    cancel(request_key)
            operation.active_requests.clear()
            for entry in operation.entries:
                if not entry.is_terminal:
                    entry.state = "canceled"
                    entry.error = reason
            operation.canceled = True
            operation.cancel_reason = reason
            operation.terminal = True
            operation.touch()
        if clear:
            self.drop_operation = None
            self._drop_card_rect = None
            self._drop_cancel_rect = None
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def _retire_drop_requests_for_clear(self):
        """Retire navigator ownership so active drop entries can be resubmitted."""
        operation = getattr(self, "drop_operation", None)
        if operation is None or operation.terminal:
            return
        cancel = getattr(self.file_navigator, "cancel_drop_decode", None)
        for request_key in tuple(operation.active_requests):
            if cancel is not None:
                cancel(request_key)
            entry = self._find_drop_entry(operation, request_key[2])
            if entry is not None and entry.state == "submitted":
                entry.state = "accepted"
        operation.active_requests.clear()
        operation.touch()

    @staticmethod
    def _point_in_rect(x, y, rect):
        if rect is None:
            return False
        left, top, width, height = rect
        return left <= x < left + width and top <= y < top + height

    def _retire_drop_card(self):
        """Remove the current drop card and all of its hit regions."""
        self.drop_operation = None
        self._drop_card_rect = None
        self._drop_cancel_rect = None

    def _retire_scene_card(self):
        """Remove the current scene card and all of its hit regions."""
        self.scene_operation = None
        self._scene_card_rect = None
        self._scene_cancel_rect = None

    def _retire_export_card(self):
        """Remove the current export card and all of its hit regions."""
        self.export_operation = None
        self._export_card_rect = None
        self._export_cancel_rect = None

    def _retire_save_card(self):
        """Remove the current save card and all of its hit regions."""
        self.save_operation = None
        self._save_card_rect = None
        self._save_cancel_rect = None

    @staticmethod
    def _is_routine_success_card(kind, operation):
        """Classify only complete, warning-free outcomes as transient cards."""
        if kind == "drop":
            return (operation.terminal and not operation.canceled
                    and operation.failed == 0
                    and operation.succeeded == operation.total)
        if kind == "scene":
            return (operation.terminal and operation.stage == "complete"
                    and operation.failed == 0
                    and operation.decoded == operation.total)
        if kind == "export":
            return (operation.terminal and operation.stage == "success"
                    and operation.committed and not operation.failures
                    and operation.clipped == 0 and operation.outside == 0)
        if kind == "save":
            return (operation.terminal and operation.stage == "success"
                    and operation.committed and not operation.warnings)
        return False

    def _present_success_card(self, kind, operation):
        """Start a success deadline exactly once, when its card is painted."""
        if (CanvasPanel._is_routine_success_card(kind, operation)
                and operation.card_deadline is None):
            operation.card_presentation_revision += 1
            operation.card_deadline = (
                self._monotonic() + SUCCESS_CARD_DISMISS_MS / 1000.0)
            _reschedule_status_timer(self)

    def _expire_operation_card(self, kind, operation, now):
        """Expire one current card only when its presented revision is due."""
        if (operation is None
                or not CanvasPanel._is_routine_success_card(kind, operation)
                or operation.card_deadline is None
                or operation.card_deadline > now):
            return False
        current = getattr(self, f"{kind}_operation", None)
        if (current is not operation
                or current.generation != operation.generation
                or current.card_presentation_revision !=
                operation.card_presentation_revision):
            return False
        if kind == "drop":
            CanvasPanel._retire_drop_card(self)
        elif kind == "scene":
            CanvasPanel._retire_scene_card(self)
        elif kind == "save":
            CanvasPanel._retire_save_card(self)
        else:
            CanvasPanel._retire_export_card(self)
        return True

    def _handle_save_card_click(self, x, y):
        operation = getattr(self, "save_operation", None)
        if operation is None:
            return False
        if (not operation.terminal
                and self._point_in_rect(x, y, self._save_cancel_rect)):
            self.cancel_save_operation()
            return True
        if (operation.terminal
                and self._point_in_rect(x, y, self._save_card_rect)):
            CanvasPanel._retire_save_card(self)
            _reschedule_status_timer(self)
            self.Refresh()
            return True
        return False

    def _handle_drop_card_click(self, x, y):
        operation = getattr(self, "drop_operation", None)
        if operation is None:
            return False
        if (not operation.terminal
                and self._point_in_rect(x, y, self._drop_cancel_rect)):
            self.cancel_drop_operation()
            return True
        if (operation.terminal
                and self._point_in_rect(x, y, self._drop_card_rect)):
            CanvasPanel._retire_drop_card(self)
            _reschedule_status_timer(self)
            self.Refresh()
            return True
        return False

    def _drop_status_lines(self, operation):
        queued = operation.total - operation.resolved - len(operation.active_requests)
        if operation.terminal:
            if operation.canceled:
                heading = "Drop canceled"
            elif operation.failed == 0:
                heading = "Drop completed"
            elif operation.succeeded:
                heading = "Drop completed with failures"
            else:
                heading = "Drop failed"
        elif operation.completed == 0 and not operation.active_requests:
            heading = "Drop accepted"
        else:
            heading = "Loading dropped images"
        lines = [heading,
                 (f"{operation.completed}/{operation.total} completed  |  "
                  f"{operation.succeeded} succeeded  |  {operation.failed} failed")]
        if operation.canceled_count:
            lines.append(f"{operation.canceled_count} canceled")
        elif not operation.terminal:
            lines.append(
                f"{len(operation.active_requests)} queued/loading  |  {max(0, queued)} waiting")
        failures = [entry for entry in operation.entries if entry.state == "failed"]
        for entry in failures[:3]:
            detail = (entry.error or "unknown error").replace("\n", " ")
            if len(detail) > 70:
                detail = detail[:67] + "..."
            lines.append(f"{os.path.basename(entry.path) or entry.path}: {detail}")
        if len(failures) > 3:
            lines.append(f"...and {len(failures) - 3} more failure(s)")
        lines.append("Click to dismiss" if operation.terminal else "Cancel")
        return lines

    @staticmethod
    def _duplicate_request_key(operation):
        return "duplicate", operation.generation, operation.snapshot.source_object_id

    @staticmethod
    def _capture_frame_metadata(image_object):
        """Copy only future-compatible scalar frame fields, never object state."""
        metadata = []
        for name in _FRAME_METADATA_NAMES:
            if not hasattr(image_object, name):
                continue
            value = getattr(image_object, name)
            if isinstance(value, (str, int, float, bool, type(None))):
                metadata.append((name, value))
            elif isinstance(value, tuple) and all(
                    isinstance(item, (str, int, float, bool, type(None)))
                    for item in value):
                metadata.append((name, tuple(value)))
        return tuple(metadata)

    def _capture_duplication_snapshot(self, image_object):
        """Capture committed pixels and primitive metadata without loading."""
        CanvasPanel._cancel_viewport_gesture(self)
        lease = image_object.lease_source_pixels()
        if lease is None or lease.pixels is None:
            if lease is not None:
                lease.release()
            raise ValueError("the object has no committed image pixels")
        try:
            return DuplicationSnapshot(
                getattr(self, "_document_identity", 0),
                str(image_object.object_id),
                os.fspath(image_object.source_path),
                bool(image_object.normalize_orientation),
                int(image_object._source_revision),
                image_object.x, image_object.y,
                image_object.width, image_object.height,
                image_object.zoom_factor,
                tuple(image_object.viewport_offset),
                image_object._minimum_zoom,
                (getattr(image_object, "canvas_w", None),
                 getattr(image_object, "canvas_h", None)),
                CanvasPanel._capture_frame_metadata(image_object),
                lease,
            )
        except Exception:
            lease.release()
            raise

    def begin_duplicate(self, image_object):
        """Accept one invocation-time duplicate request for a live object."""
        if (image_object is None
                or not self.image_objects.contains_object(image_object)):
            return False
        source_id = image_object.object_id
        existing = self._duplication_operations.get(source_id)
        if existing is not None and not existing.terminal:
            image_object.set_status_overlay(
                "Duplicate already in progress", "processing",
                operation="duplication")
            _reschedule_status_timer(self)
            self.Refresh()
            return True
        if self.file_navigator.is_shutdown:
            return False

        try:
            snapshot = self._capture_duplication_snapshot(image_object)
        except Exception as exc:
            image_object.set_status_overlay(
                f"Couldn't duplicate: {exc}", "warning",
                operation="duplication")
            _reschedule_status_timer(self)
            self.Refresh()
            return False

        self._duplication_generation = getattr(self, "_duplication_generation", 0) + 1
        generation = self._duplication_generation
        request_key = ("duplicate", generation, source_id)
        cancellation = DuplicationCancellation()
        task_factory = getattr(self, "_duplication_task_factory", DuplicationTask)
        try:
            task = task_factory(snapshot, cancellation=cancellation)
        except Exception:
            snapshot.release()
            raise
        operation = DuplicationOperation(
            generation, request_key, snapshot, cancellation,
            getattr(self, "_interaction_revision", 0))
        self._duplication_operations[source_id] = operation
        image_object.set_status_overlay(
            "Duplicating image...", "processing", operation="duplication")
        _reschedule_status_timer(self)
        self.Refresh()

        callback = getattr(self, "_on_duplication_finished", None)
        if callback is None:
            callback = CanvasPanel._on_duplication_finished.__get__(self)
        accepted = self.file_navigator.request_duplication(
            snapshot.source_path, request_key,
            (snapshot.document_identity, generation, source_id),
            callback, task)
        if accepted:
            return True

        task.cancel_before_start()
        self._duplication_operations.pop(source_id, None)
        operation.terminal = True
        operation.stage = "failed"
        operation.error = "background image service rejected the request"
        image_object.set_status_overlay(
            "Duplicate request was rejected. Try again.", "warning",
            operation="duplication")
        _reschedule_status_timer(self)
        self.Refresh()
        return False

    duplicate_object = begin_duplicate

    def _on_duplication_finished(self, result):
        """Validate a copied candidate and publish it on the GUI thread."""
        try:
            document_identity, generation, source_id = result.context
        except (TypeError, ValueError):
            result.close()
            return False
        operation = self._duplication_operations.get(source_id)
        if (operation is None
                or operation.generation != generation
                or operation.terminal
                or operation.snapshot.document_identity != document_identity):
            result.close()
            return False

        self._duplication_operations.pop(source_id, None)
        operation.terminal = True
        source = next((obj for obj in self.image_objects
                       if obj.object_id == source_id), None)
        if (self.file_navigator.is_shutdown
                or document_identity != getattr(self, "_document_identity", 0)
                or source is None):
            result.close()
            operation.stage = "canceled"
            return False
        if result.error is not None or result.pixels is None:
            result.close()
            operation.stage = "failed"
            operation.error = result.error or "worker returned no copied pixels"
            if source.status_operation == "duplication":
                source.set_status_overlay(
                    f"Couldn't duplicate: {operation.error}", "warning",
                    operation="duplication")
                _reschedule_status_timer(self)
                self.Refresh()
            return False

        pixels = result.take_pixels()
        candidate = ImageObject(
            operation.snapshot.source_path,
            normalize_orientation=operation.snapshot.normalize_orientation)
        try:
            candidate.commit_duplicate_candidate(pixels, operation.snapshot)
            index = self.image_objects.index_for(source)
            self.image_objects.insert(index + 1, candidate)
        except Exception as exc:
            if candidate._original_image is not None:
                candidate.dispose_source_pixels()
            elif pixels is not None:
                pixels.close()
            operation.stage = "failed"
            operation.error = str(exc)
            if source.status_operation == "duplication":
                source.set_status_overlay(
                    f"Couldn't duplicate: {exc}", "warning",
                    operation="duplication")
                _reschedule_status_timer(self)
                self.Refresh()
            return False

        operation.stage = "complete"
        if source.status_operation == "duplication":
            source.clear_status_overlay()
        newer_intent = (
            getattr(self, "_interaction_revision", 0)
            != operation.interaction_revision)
        if (not newer_intent and same_canvas_object(self.selected_object, source)):
            self.selected_object = candidate
            self._zoom_wheel_remainders.clear()
            candidate.set_status_overlay(
                "Duplicate ready", "info", operation="duplication")
            self._schedule_overlay_clear(image_object=candidate)
        else:
            candidate.set_status_overlay(
                "Duplicate ready", "info", operation="duplication")
            self._schedule_overlay_clear(image_object=candidate)
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def cancel_duplication_operation(self, source_id, *, reason="canceled"):
        """Retire one source-owned duplicate without waiting for its worker."""
        operation = self._duplication_operations.pop(source_id, None)
        if operation is None:
            return False
        operation.cancellation.cancel()
        cancel = getattr(self.file_navigator, "cancel_duplication_work", None)
        if cancel is not None:
            cancel(operation.request_key)
        operation.terminal = True
        operation.stage = "canceled"
        operation.error = reason
        source = next((obj for obj in self.image_objects
                       if obj.object_id == source_id), None)
        if source is not None and source.status_operation == "duplication":
            source.clear_status_overlay()
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def cancel_all_duplication_operations(self, *, reason="canceled"):
        changed = False
        for source_id in tuple(self._duplication_operations):
            changed = (self.cancel_duplication_operation(
                source_id, reason=reason) or changed)
        return changed

    @staticmethod
    def _scene_document_request_key(operation):
        return "scene-document", operation.generation

    @staticmethod
    def _scene_entry_request_key(operation, entry):
        return "scene", operation.generation, entry.request_id

    def begin_load_canvas_state(self, path):
        """Acknowledge and schedule transactional scene reading immediately."""
        if self.file_navigator.is_shutdown:
            return False
        CanvasPanel.cancel_save_operation(
            self, clear=True, reason="document replacement started")
        CanvasPanel.cancel_scene_operation(
            self, clear=True, reason="superseded by a newer scene request")
        _cancel_all_duplication(self, reason="scene loading started")
        CanvasPanel.cancel_drop_operation(
            self, clear=True, reason="scene loading started")
        for obj in tuple(self.image_objects):
            _cancel_object_work(self, obj)

        self._scene_generation = getattr(self, "_scene_generation", 0) + 1
        operation = SceneLoadOperation(self._scene_generation, os.fspath(path))
        request_key = self._scene_document_request_key(operation)
        operation.active_requests.add(request_key)
        self.scene_operation = operation
        self.Refresh()
        callback = getattr(self, "_on_scene_document_read", None)
        if callback is None:
            callback = CanvasPanel._on_scene_document_read.__get__(self)
        accepted = self.file_navigator.request_scene_document(
            operation.path, request_key, operation.generation, callback)
        if not accepted:
            operation.active_requests.discard(request_key)
            operation.stage = "failed"
            operation.error = "background image service rejected the request"
            operation.terminal = True
            operation.touch()
            self.Refresh()
            return False
        return True

    def _capture_naming_request(self):
        request = getattr(self, "_naming_request", 0) + 1
        self._naming_request = request
        return getattr(self, "_document_identity", 0), request

    def _suggested_name(self, suffix):
        base = getattr(self, "_suggested_base", None)
        if base is None:
            clock = getattr(self, "_wall_clock", None)
            self._suggested_base = timestamp_base(clock) if clock else timestamp_base()
            base = self._suggested_base
        return suggested_filename(base, suffix)

    def _record_naming_success(self, document_identity, request, base):
        if (document_identity == getattr(self, "_document_identity", 0)
                and request > getattr(self, "_last_naming_success", 0)):
            self._suggested_base = base
            self._last_naming_success = request

    def get_scene_save_suggestion(self):
        return self._suggested_name(".json")

    def get_export_suggestion(self, format_name):
        suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp",
                  "BMP": ".bmp"}[str(format_name).upper()]
        return self._suggested_name(suffix)

    def _on_scene_document_read(self, result):
        generation = result.context
        operation = getattr(self, "scene_operation", None)
        request_key = ("scene-document", generation)
        if (operation is None or operation.generation != generation
                or operation.terminal or request_key not in operation.active_requests):
            result.close()
            return
        operation.active_requests.discard(request_key)
        if result.error is not None or result.records is None:
            operation.stage = "failed"
            operation.error = result.error or "reader returned no validated document"
            operation.terminal = True
            operation.touch()
            result.close()
            self.Refresh()
            return

        records = result.records
        result.records = None
        operation.entries = [
            SceneEntry(index, record)
            for index, record in enumerate(records, 1)
        ]
        operation.stage = "decoding"
        operation.touch()
        if not operation.entries:
            self._commit_scene_operation(operation)
        else:
            self._pump_scene_operation()
        self.Refresh()

    def _pump_scene_operation(self):
        """Incrementally feed scene entries through the shared scheduler."""
        operation = getattr(self, "scene_operation", None)
        if (operation is None or operation.terminal
                or operation.stage != "decoding"):
            return False
        callback = getattr(self, "_on_scene_entry_decoded", None)
        if callback is None:
            callback = CanvasPanel._on_scene_entry_decoded.__get__(self)
        changed = False
        while len(operation.active_requests) < self.MAX_SCENE_IN_FLIGHT:
            entry = next((candidate for candidate in operation.entries
                          if candidate.state == "accepted"), None)
            if entry is None:
                break
            request_key = self._scene_entry_request_key(operation, entry)
            entry.state = "submitted"
            operation.active_requests.add(request_key)
            normalized = (
                entry.record.get("source_pixel_normalization") ==
                SOURCE_PIXEL_NORMALIZATION_VERSION)
            accepted = self.file_navigator.request_scene_decode(
                entry.record["source_path"], request_key,
                (operation.generation, entry.request_id), callback,
                apply_orientation=normalized,
                animation=entry.record.get("animation"))
            if not accepted:
                operation.active_requests.discard(request_key)
                entry.state = "failed"
                entry.error = "background image service rejected the request"
            changed = True
        if changed:
            operation.touch()
        self._finalize_scene_operation()
        return changed

    def _find_scene_entry(self, operation, request_id):
        return next((entry for entry in operation.entries
                     if entry.request_id == request_id), None)

    def _on_scene_entry_decoded(self, result):
        try:
            generation, request_id = result.context
        except (TypeError, ValueError):
            result.close()
            return
        operation = getattr(self, "scene_operation", None)
        entry = (self._find_scene_entry(operation, request_id)
                 if operation is not None else None)
        request_key = ("scene", generation, request_id)
        if (operation is None or operation.generation != generation
                or operation.terminal or entry is None
                or entry.state != "submitted"
                or request_key not in operation.active_requests):
            result.close()
            return
        operation.active_requests.discard(request_key)

        if result.error is not None or result.pixels is None:
            result.close()
            entry.state = "failed"
            entry.error = result.error or "decoder returned no pixels"
        else:
            pixels = result.take_pixels()
            record = entry.record
            normalized = (
                record.get("source_pixel_normalization") ==
                SOURCE_PIXEL_NORMALIZATION_VERSION)
            canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
            bounds = (canvas_w, canvas_h) if canvas_w > 0 and canvas_h > 0 else None
            image_object = ImageObject(
                record["source_path"], normalize_orientation=normalized)
            try:
                image_object.commit_scene_candidate(pixels, record, bounds)
            except Exception as exc:
                pixels.close()
                entry.state = "failed"
                entry.error = str(exc)
            else:
                entry.state = "succeeded"
                entry.image_object = image_object

        operation.touch()
        self._pump_scene_operation()
        self._finalize_scene_operation()
        self.Refresh()

    def _finalize_scene_operation(self):
        operation = getattr(self, "scene_operation", None)
        if (operation is None or operation.terminal
                or operation.stage != "decoding"
                or operation.resolved != operation.total):
            return False
        if operation.failed:
            self._release_scene_candidates(operation)
            operation.stage = "failed"
            operation.error = "one or more image sources could not be decoded"
            operation.terminal = True
            operation.touch()
            self.Refresh()
            return True
        return self._commit_scene_operation(operation)

    def _commit_scene_operation(self, operation):
        if (operation is not getattr(self, "scene_operation", None)
                or operation.terminal):
            return False
        CanvasPanel._cancel_viewport_gesture(self, restore=False)
        candidates = ImageObjectList(
            entry.image_object for entry in operation.entries)
        if len(candidates) != operation.total:
            raise ValueError("scene operation has incomplete candidates")

        CanvasPanel.cancel_save_operation(
            self, clear=True, reason="document replacement committed")
        old_objects = self.image_objects
        CanvasPanel._clear_animation_controls(self)
        self.image_objects = candidates
        self.selected_object = None
        self.marked_object = None
        self.drag_offset = None
        self._zoom_wheel_remainders.clear()
        operation.committed = True
        # A committed scene is a new runtime document.  Its filename is the
        # only naming state adopted from the loaded file.
        self._document_identity = getattr(self, "_document_identity", 0) + 1
        self._naming_request = 0
        self._last_naming_success = 0
        self._suggested_base = filename_base(operation.path)
        operation.terminal = True
        operation.stage = "complete"
        operation.touch()
        for entry in operation.entries:
            entry.image_object = None
        for obj in old_objects:
            _cancel_object_work(self, obj)
            obj.clear_status_overlay()
            obj.dispose_source_pixels()
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def _release_scene_candidates(self, operation):
        for entry in operation.entries:
            image_object = entry.image_object
            entry.image_object = None
            if image_object is not None:
                image_object.dispose_source_pixels()

    def cancel_scene_operation(self, *, clear=False, reason="canceled by user"):
        """Cancel only scene-owned work and deterministically release staging."""
        operation = getattr(self, "scene_operation", None)
        if operation is None:
            return False
        if not operation.terminal:
            cancel = getattr(self.file_navigator, "cancel_scene_work", None)
            for request_key in tuple(operation.active_requests):
                if cancel is not None:
                    cancel(request_key)
            operation.active_requests.clear()
            for entry in operation.entries:
                if not entry.is_terminal:
                    entry.state = "canceled"
                    entry.error = reason
            self._release_scene_candidates(operation)
            operation.canceled = True
            operation.error = reason
            operation.stage = "canceled"
            operation.terminal = True
            operation.touch()
        if clear:
            self.scene_operation = None
            self._scene_card_rect = None
            self._scene_cancel_rect = None
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def _handle_scene_card_click(self, x, y):
        operation = getattr(self, "scene_operation", None)
        if operation is None:
            return False
        if (not operation.terminal
                and self._point_in_rect(x, y, self._scene_cancel_rect)):
            self.cancel_scene_operation()
            return True
        if (operation.terminal
                and self._point_in_rect(x, y, self._scene_card_rect)):
            CanvasPanel._retire_scene_card(self)
            _reschedule_status_timer(self)
            self.Refresh()
            return True
        return False

    def _scene_status_lines(self, operation):
        if operation.stage == "reading":
            return ["Loading scene", "Reading and validating document...", "Cancel"]
        if operation.stage == "complete":
            return ["Scene loaded", f"{operation.total}/{operation.total} decoded",
                    "Click to dismiss"]
        if operation.canceled:
            return ["Scene load canceled", operation.error or "canceled",
                    "Click to dismiss"]
        if operation.stage == "failed" and not operation.entries:
            detail = (operation.error or "unknown error").replace("\n", " ")
            return ["Scene load failed", detail[:100], "Click to dismiss"]

        lines = [
            "Scene load failed" if operation.terminal else "Loading scene",
            f"{operation.decoded}/{operation.total} decoded  |  {operation.failed} failed",
        ]
        if not operation.terminal:
            waiting = operation.total - operation.resolved - len(operation.active_requests)
            lines.append(
                f"{len(operation.active_requests)} queued/loading  |  {max(0, waiting)} waiting")
        for entry in (entry for entry in operation.entries if entry.state == "failed"):
            detail = (entry.error or "unknown error").replace("\n", " ")
            name = os.path.basename(entry.record["source_path"]) or entry.record["source_path"]
            lines.append(f"{name}: {detail}"[:110])
            if len(lines) >= 6:
                break
        lines.append("Click to dismiss" if operation.terminal else "Cancel")
        return lines

    def bring_image_object_to_front(self, obj):
        """Move the identified object to the front without duplicating it."""
        if self.image_objects.index_for(obj) != len(self.image_objects) - 1:
            CanvasPanel.prepare_canvas_edit(self, "image order changed")
        self.image_objects.move_to_front(obj)

    def remove_image_object(self, obj):
        """Remove one object by identity and clean object-scoped references."""
        if not self.image_objects.contains_object(obj):
            return False
        gesture = getattr(self, "_viewport_gesture", None)
        if gesture is not None and gesture["object_id"] == obj.object_id:
            CanvasPanel._cancel_viewport_gesture(self, restore=False)
        CanvasPanel.prepare_canvas_edit(self, "image deleted")

        _cancel_duplication(
            self, obj.object_id, reason="source object was deleted")
        removed = self.image_objects.remove(obj)
        controls = getattr(self, "_animation_controls", None)
        if (controls is not None
                and controls.target_id == str(removed.object_id)):
            CanvasPanel._clear_animation_controls(self)
        _cancel_object_work(self, removed)
        removed.clear_status_overlay()
        removed.dispose_source_pixels()

        if same_canvas_object(self.selected_object, removed):
            self.selected_object = None
            self.drag_offset = None
        if same_canvas_object(self.marked_object, removed):
            self.marked_object = None

        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def swap_image_objects(self, selected, marked):
        """Swap transforms when both distinct objects still belong to the canvas."""
        if (selected is None or marked is None or
                same_canvas_object(selected, marked) or
                not self.image_objects.contains_object(selected) or
                not self.image_objects.contains_object(marked)):
            return False
        CanvasPanel.prepare_canvas_edit(self, "images swapped")

        selected.x, marked.x = marked.x, selected.x
        selected.y, marked.y = marked.y, selected.y
        selected.width, marked.width = marked.width, selected.width
        selected.height, marked.height = marked.height, selected.height
        selected.viewport_offset, marked.viewport_offset = marked.viewport_offset, selected.viewport_offset
        selected.zoom_factor, marked.zoom_factor = marked.zoom_factor, selected.zoom_factor
        self.Refresh()
        return True

    def on_paint(self, event):
        dc = wx.BufferedPaintDC(self)
        dc.Clear()

        # Fill background
        bg_color = wx.Colour(self.canvas_bg)
        dc.SetBrush(wx.Brush(bg_color))
        dc.SetPen(wx.Pen(bg_color))
        w, h = self.GetSize()
        dc.DrawRectangle(0, 0, w, h)

        # Draw each image object
        for i, img_obj in enumerate(self.image_objects):
            try:
                img_obj.draw(
                    dc, canvas_size=(w, h),
                    info_position=self.settings_manager.get_object_info_position()
                    if hasattr(self.settings_manager, "get_object_info_position") else "center",
                    scale=dc.GetContentScaleFactor()
                    if hasattr(dc, "GetContentScaleFactor") else 1.0)
                logging.debug(f"Drew image object {i}: {os.path.basename(img_obj.source_path)}")
            except Exception as e:
                logging.error(f"Failed to draw image object {i}: {e}")

        # Draw selection border if any
        if self.selected_object:
            x, y = self.selected_object.x, self.selected_object.y
            w, h = self.selected_object.width, self.selected_object.height
            dc.SetPen(wx.Pen(wx.RED, 2, style=wx.PENSTYLE_SOLID))
            dc.SetBrush(wx.TRANSPARENT_BRUSH)
            dc.DrawRectangle(x, y, w, h)

        CanvasPanel._draw_viewport_controls(self, dc)
        self._draw_canvas_navigation_status(dc)
        self._draw_drop_status(dc)
        self._draw_scene_status(dc)
        self._draw_export_status(dc)
        self._draw_save_status(dc)
        self._draw_animation_controls(dc)

    def _draw_canvas_navigation_status(self, dc):
        """Repeat the selected navigation status at a stable canvas position."""
        image_object = self.get_selected_object()
        if (image_object is None or not image_object.show_status_overlay
                or image_object.status_operation != "navigation"
                or not image_object.status_message):
            return False

        message = image_object.status_message
        text_size = dc.GetTextExtent(message)
        try:
            text_w, text_h = text_size.width, text_size.height
        except AttributeError:
            text_w, text_h = text_size
        padding = 8
        bg_color = (wx.Colour(255, 165, 0, 230)
                    if image_object.status_type == "processing"
                    else wx.Colour(180, 55, 30, 230)
                    if image_object.status_type == "warning"
                    else wx.Colour(70, 130, 180, 230))
        dc.SetPen(wx.Pen(bg_color))
        dc.SetBrush(wx.Brush(bg_color))
        dc.DrawRectangle(10, 10, text_w + 2 * padding, text_h + 2 * padding)
        dc.SetTextForeground(wx.BLACK if image_object.status_type == "processing"
                             else wx.WHITE)
        dc.DrawText(message, 10 + padding, 10 + padding)
        return True

    def _draw_drop_status(self, dc):
        """Draw the aggregate drop card near its clamped acceptance point."""
        operation = getattr(self, "drop_operation", None)
        if operation is None:
            self._drop_card_rect = None
            self._drop_cancel_rect = None
            return False
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        if canvas_w <= 0 or canvas_h <= 0:
            return False

        lines = self._drop_status_lines(operation)
        padding = 10
        line_gap = 4
        extents = []
        for line in lines:
            extent = dc.GetTextExtent(line)
            try:
                extents.append((extent.width, extent.height))
            except AttributeError:
                extents.append(tuple(extent))
        content_w = max((width for width, _ in extents), default=1)
        content_h = sum(height for _, height in extents) + line_gap * (len(lines) - 1)
        card_w = min(canvas_w, content_w + 2 * padding)
        card_h = min(canvas_h, content_h + 2 * padding)
        anchor_x, anchor_y = operation.anchor
        card_x = min(max(0, int(anchor_x)), max(0, canvas_w - card_w))
        card_y = min(max(0, int(anchor_y)), max(0, canvas_h - card_h))
        self._drop_card_rect = (card_x, card_y, card_w, card_h)

        if operation.terminal:
            bg_color = (wx.Colour(55, 125, 80, 235)
                        if operation.failed == 0 and not operation.canceled
                        else wx.Colour(70, 130, 180, 235)
                        if operation.canceled
                        else wx.Colour(180, 55, 30, 235))
        else:
            bg_color = wx.Colour(255, 165, 0, 235)
        dc.SetPen(wx.Pen(bg_color))
        dc.SetBrush(wx.Brush(bg_color))
        dc.DrawRoundedRectangle(card_x, card_y, card_w, card_h, 5)
        dc.SetTextForeground(wx.BLACK if not operation.terminal else wx.WHITE)

        text_y = card_y + padding
        for index, line in enumerate(lines):
            line_w, line_h = extents[index]
            if index == len(lines) - 1 and not operation.terminal:
                button_w = min(card_w - 2 * padding, line_w + 2 * padding)
                button_h = line_h + padding
                button_x = card_x + padding
                button_y = min(text_y - padding // 2, card_y + card_h - button_h - padding)
                self._drop_cancel_rect = (button_x, button_y, button_w, button_h)
                dc.SetPen(wx.Pen(wx.Colour(80, 55, 0)))
                dc.SetBrush(wx.Brush(wx.Colour(255, 220, 120)))
                dc.DrawRoundedRectangle(button_x, button_y, button_w, button_h, 3)
                dc.SetTextForeground(wx.BLACK)
                dc.DrawText(line, button_x + padding, button_y + padding // 2)
            else:
                dc.DrawText(line, card_x + padding, text_y)
            text_y += line_h + line_gap
        if operation.terminal:
            self._drop_cancel_rect = None
            CanvasPanel._present_success_card(self, "drop", operation)
        return True

    def _draw_scene_status(self, dc):
        """Draw one stable runtime-only scene-loading card."""
        operation = getattr(self, "scene_operation", None)
        if operation is None:
            self._scene_card_rect = None
            self._scene_cancel_rect = None
            return False
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        if canvas_w <= 0 or canvas_h <= 0:
            return False

        lines = self._scene_status_lines(operation)
        padding = 10
        line_gap = 4
        extents = []
        for line in lines:
            extent = dc.GetTextExtent(line)
            try:
                extents.append((extent.width, extent.height))
            except AttributeError:
                extents.append(tuple(extent))
        content_w = max((width for width, _ in extents), default=1)
        content_h = sum(height for _, height in extents) + line_gap * (len(lines) - 1)
        card_w = min(canvas_w, content_w + 2 * padding)
        card_h = min(canvas_h, content_h + 2 * padding)
        card_x = min(10, max(0, canvas_w - card_w))
        card_y = min(10, max(0, canvas_h - card_h))
        self._scene_card_rect = (card_x, card_y, card_w, card_h)

        if operation.stage == "complete":
            bg_color = wx.Colour(55, 125, 80, 235)
        elif operation.terminal:
            bg_color = (wx.Colour(70, 130, 180, 235)
                        if operation.canceled
                        else wx.Colour(180, 55, 30, 235))
        else:
            bg_color = wx.Colour(255, 165, 0, 235)
        dc.SetPen(wx.Pen(bg_color))
        dc.SetBrush(wx.Brush(bg_color))
        dc.DrawRoundedRectangle(card_x, card_y, card_w, card_h, 5)
        dc.SetTextForeground(wx.BLACK if not operation.terminal else wx.WHITE)

        text_y = card_y + padding
        for index, line in enumerate(lines):
            line_w, line_h = extents[index]
            if index == len(lines) - 1 and not operation.terminal:
                button_w = min(card_w - 2 * padding, line_w + 2 * padding)
                button_h = line_h + padding
                button_x = card_x + padding
                button_y = min(text_y - padding // 2,
                               card_y + card_h - button_h - padding)
                self._scene_cancel_rect = (
                    button_x, button_y, button_w, button_h)
                dc.SetPen(wx.Pen(wx.Colour(80, 55, 0)))
                dc.SetBrush(wx.Brush(wx.Colour(255, 220, 120)))
                dc.DrawRoundedRectangle(
                    button_x, button_y, button_w, button_h, 3)
                dc.SetTextForeground(wx.BLACK)
                dc.DrawText(line, button_x + padding, button_y + padding // 2)
            else:
                dc.DrawText(line, card_x + padding, text_y)
            text_y += line_h + line_gap
        if operation.terminal:
            self._scene_cancel_rect = None
            CanvasPanel._present_success_card(self, "scene", operation)
        return True

    def _draw_export_status(self, dc):
        """Draw export last so its Cancel action remains accessible."""
        operation = getattr(self, "export_operation", None)
        if operation is None:
            self._export_card_rect = None
            self._export_cancel_rect = None
            return False
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        if canvas_w <= 0 or canvas_h <= 0:
            return False
        lines = CanvasPanel._export_status_lines(self, operation)
        padding = 10
        line_gap = 4
        extents = []
        for line in lines:
            extent = dc.GetTextExtent(line)
            try:
                extents.append((extent.width, extent.height))
            except AttributeError:
                extents.append(tuple(extent))
        content_w = max((width for width, _ in extents), default=1)
        content_h = sum(height for _, height in extents) + line_gap * (len(lines) - 1)
        card_w = min(canvas_w, content_w + 2 * padding)
        card_h = min(canvas_h, content_h + 2 * padding)
        card_x = max(0, canvas_w - card_w - 10)
        card_y = min(10, max(0, canvas_h - card_h))
        self._export_card_rect = (card_x, card_y, card_w, card_h)

        if operation.stage == "success":
            bg_color = wx.Colour(55, 125, 80, 235)
        elif operation.terminal:
            bg_color = (wx.Colour(70, 130, 180, 235)
                        if operation.stage == "canceled"
                        else wx.Colour(180, 55, 30, 235))
        else:
            bg_color = wx.Colour(255, 165, 0, 235)
        dc.SetPen(wx.Pen(bg_color))
        dc.SetBrush(wx.Brush(bg_color))
        dc.DrawRoundedRectangle(card_x, card_y, card_w, card_h, 5)
        dc.SetTextForeground(wx.BLACK if not operation.terminal else wx.WHITE)
        text_y = card_y + padding
        for index, line in enumerate(lines):
            line_w, line_h = extents[index]
            if index == len(lines) - 1 and not operation.terminal:
                button_w = min(card_w - 2 * padding, line_w + 2 * padding)
                button_h = line_h + padding
                button_x = card_x + padding
                button_y = min(text_y - padding // 2,
                               card_y + card_h - button_h - padding)
                self._export_cancel_rect = (
                    button_x, button_y, button_w, button_h)
                dc.SetPen(wx.Pen(wx.Colour(80, 55, 0)))
                dc.SetBrush(wx.Brush(wx.Colour(255, 220, 120)))
                dc.DrawRoundedRectangle(
                    button_x, button_y, button_w, button_h, 3)
                dc.SetTextForeground(wx.BLACK)
                dc.DrawText(line, button_x + padding, button_y + padding // 2)
            else:
                dc.DrawText(line, card_x + padding, text_y)
            text_y += line_h + line_gap
        if operation.terminal:
            self._export_cancel_rect = None
            CanvasPanel._present_success_card(self, "export", operation)
        return True

    def _draw_save_status(self, dc):
        """Draw the current save card with one stable Cancel target."""
        operation = getattr(self, "save_operation", None)
        if operation is None:
            self._save_card_rect = None
            self._save_cancel_rect = None
            return False
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        if canvas_w <= 0 or canvas_h <= 0:
            return False
        lines = CanvasPanel._save_status_lines(self, operation)
        padding = 10
        line_gap = 4
        extents = []
        for line in lines:
            extent = dc.GetTextExtent(line)
            try:
                extents.append((extent.width, extent.height))
            except AttributeError:
                extents.append(tuple(extent))
        content_w = max((width for width, _ in extents), default=1)
        content_h = sum(height for _, height in extents) + line_gap * (len(lines) - 1)
        card_w = min(canvas_w, content_w + 2 * padding)
        card_h = min(canvas_h, content_h + 2 * padding)
        card_x = max(0, canvas_w - card_w - 10)
        card_y = max(0, canvas_h - card_h - 10)
        self._save_card_rect = (card_x, card_y, card_w, card_h)

        if operation.stage == "success" and not operation.warnings:
            bg_color = wx.Colour(55, 125, 80, 235)
        elif operation.stage == "success":
            bg_color = wx.Colour(180, 85, 30, 235)
        elif operation.terminal:
            bg_color = (wx.Colour(70, 130, 180, 235)
                        if operation.stage == "canceled"
                        else wx.Colour(180, 55, 30, 235))
        else:
            bg_color = wx.Colour(255, 165, 0, 235)
        dc.SetPen(wx.Pen(bg_color))
        dc.SetBrush(wx.Brush(bg_color))
        dc.DrawRoundedRectangle(card_x, card_y, card_w, card_h, 5)
        dc.SetTextForeground(wx.BLACK if not operation.terminal else wx.WHITE)
        text_y = card_y + padding
        for index, line in enumerate(lines):
            line_w, line_h = extents[index]
            if index == len(lines) - 1 and not operation.terminal:
                button_w = min(card_w - 2 * padding, line_w + 2 * padding)
                button_h = line_h + padding
                button_x = card_x + padding
                button_y = min(text_y - padding // 2,
                               card_y + card_h - button_h - padding)
                self._save_cancel_rect = (
                    button_x, button_y, button_w, button_h)
                dc.SetPen(wx.Pen(wx.Colour(80, 55, 0)))
                dc.SetBrush(wx.Brush(wx.Colour(255, 220, 120)))
                dc.DrawRoundedRectangle(
                    button_x, button_y, button_w, button_h, 3)
                dc.SetTextForeground(wx.BLACK)
                dc.DrawText(line, button_x + padding, button_y + padding // 2)
            else:
                dc.DrawText(line, card_x + padding, text_y)
            text_y += line_h + line_gap
        if operation.terminal:
            self._save_cancel_rect = None
            CanvasPanel._present_success_card(self, "save", operation)
        return True

    def _animation_control_scale(self):
        get_scale = getattr(self, "GetDPIScaleFactor", None)
        if get_scale is not None:
            try:
                return max(0.25, float(get_scale()))
            except (TypeError, ValueError):
                pass
        return 1.0

    def _animation_object_by_id(self, object_id):
        object_id = str(object_id)
        return next((obj for obj in self.image_objects
                     if str(obj.object_id) == object_id), None)

    def _animation_control_layout_for(self, image_object):
        if image_object is None or not image_object.is_animated:
            return None
        return layout_animation_controls(
            image_object.object_id,
            (image_object.x, image_object.y,
             image_object.width, image_object.height),
            CanvasPanel.get_client_dimensions(self),
            CanvasPanel._animation_control_scale(self),
        )

    def _animation_control_layout(self):
        controls = getattr(self, "_animation_controls", None)
        if controls is None:
            return None
        if controls.timeline_dragging and controls.timeline_layout is not None:
            target = CanvasPanel._animation_object_by_id(
                self, controls.timeline_layout.object_id)
            if target is not None and target.is_animated:
                return controls.timeline_layout
        target = CanvasPanel._animation_object_by_id(
            self, controls.target_id)
        if target is None:
            return None
        return CanvasPanel._animation_control_layout_for(self, target)

    def _animation_control_candidate(self, x, y):
        for image_object in reversed(self.image_objects):
            layout = CanvasPanel._animation_control_layout_for(self, image_object)
            if layout is not None and layout.activation.contains(x, y):
                return image_object, layout
        return None, None

    def _sync_animation_control_timer(self):
        timer = getattr(self, "animation_control_timer", None)
        if timer is None:
            return False
        if self._animation_controls.transitioning:
            if not timer.IsRunning():
                timer.Start(FADE_TICK_MS)
            return True
        if timer.IsRunning():
            timer.Stop()
        return False

    def _stop_timeline_preview_timer(self):
        timer = getattr(self, "timeline_preview_timer", None)
        if timer is not None and timer.IsRunning():
            timer.Stop()
            return True
        return False

    def _abandon_timeline_drag(self):
        state = getattr(self, "_animation_controls", None)
        if state is None or not state.timeline_dragging:
            return False
        target = CanvasPanel._animation_object_by_id(self, state.target_id)
        CanvasPanel._stop_timeline_preview_timer(self)
        state.pressed_name = None
        state.clear_timeline_drag()
        if target is not None and target.is_animated:
            _cancel_animation_decode(self, target)
            target.cancel_animation_intent()
        has_capture = getattr(self, "HasCapture", None)
        release_capture = getattr(self, "ReleaseMouse", None)
        if (has_capture is not None and release_capture is not None
                and has_capture()):
            release_capture()
        return True

    def _clear_animation_controls(self):
        state = getattr(self, "_animation_controls", None)
        if state is None:
            return False
        timer = getattr(self, "animation_control_timer", None)
        if timer is not None and timer.IsRunning():
            timer.Stop()
        CanvasPanel._abandon_timeline_drag(self)
        CanvasPanel._stop_timeline_preview_timer(self)
        has_capture = getattr(self, "HasCapture", None)
        release_capture = getattr(self, "ReleaseMouse", None)
        if (has_capture is not None and release_capture is not None
                and has_capture()):
            release_capture()
        state.target_id = None
        state.opacity = 0.0
        state.visible_goal = False
        state.focus_index = None
        state.hover_name = None
        state.pressed_name = None
        state.suppressed_id = None
        state.clear_timeline_drag()
        state.last_update = float(self._monotonic())
        self._animation_control_pointer = None
        set_tooltip = getattr(self, "SetToolTip", None)
        if set_tooltip is not None:
            set_tooltip(None)
        return True

    def _set_animation_control_target(self, target_id, visible):
        state = self._animation_controls
        now = float(self._monotonic())
        state.advance(now)
        target_id = None if target_id is None else str(target_id)
        if target_id is None and state.target_id is not None:
            target_id = state.target_id
        if target_id != state.target_id:
            CanvasPanel._abandon_timeline_drag(self)
            state.target_id = target_id
            state.opacity = 0.0
            state.focus_index = None
            state.hover_name = None
            state.pressed_name = None
            state.clear_timeline_drag()
        state.visible_goal = bool(visible and target_id is not None)
        state.last_update = now
        CanvasPanel._sync_animation_control_timer(self)

    def _animation_controls_interactive(self):
        state = self._animation_controls
        return (state.target_id is not None
                and state.suppressed_id != state.target_id
                and (state.visible_goal or state.opacity > 0.0))

    def _update_animation_control_hover(self, x, y):
        state = self._animation_controls
        before = (state.target_id, state.visible_goal, state.hover_name,
                  state.suppressed_id)
        self._animation_control_pointer = (x, y)
        current_layout = CanvasPanel._animation_control_layout(self)
        if current_layout is None and state.target_id is not None:
            CanvasPanel._clear_animation_controls(self)
            current_layout = None

        if state.retaining and current_layout is not None:
            target_id = state.target_id
            visible = True
        elif current_layout is not None and current_layout.panel.contains(x, y):
            target_id = state.target_id
            visible = state.suppressed_id != target_id
        else:
            target, _layout = CanvasPanel._animation_control_candidate(self, x, y)
            target_id = str(target.object_id) if target is not None else None
            if (state.suppressed_id is not None
                    and target_id == state.suppressed_id):
                visible = False
            else:
                if state.suppressed_id is not None:
                    state.suppressed_id = None
                visible = target_id is not None

        CanvasPanel._set_animation_control_target(self, target_id, visible)
        layout = CanvasPanel._animation_control_layout(self)
        button = (layout.button_at(x, y) if layout is not None
                  and CanvasPanel._animation_controls_interactive(self) else None)
        state.hover_name = (button.name if button is not None else
                            "timeline" if layout is not None
                            and CanvasPanel._animation_controls_interactive(self)
                            and layout.timeline_at(x, y) else None)
        set_tooltip = getattr(self, "SetToolTip", None)
        if set_tooltip is not None:
            set_tooltip(CONTROL_LABELS.get(state.hover_name))
        after = (state.target_id, state.visible_goal, state.hover_name,
                 state.suppressed_id)
        if after != before:
            self.Refresh()
        return target_id

    def _animation_control_button_at(self, x, y):
        if not CanvasPanel._animation_controls_interactive(self):
            return None
        layout = CanvasPanel._animation_control_layout(self)
        return layout.button_at(x, y) if layout is not None else None

    def _animation_control_timeline_at(self, x, y):
        if not CanvasPanel._animation_controls_interactive(self):
            return None
        layout = CanvasPanel._animation_control_layout(self)
        if layout is None or not layout.timeline_at(x, y):
            return None
        return layout

    def _animation_control_enabled(self, image_object, name):
        descriptor = getattr(image_object, "animation", None)
        if descriptor is None:
            return False
        if name == "previous":
            return descriptor.requested_index > 0
        if name == "next":
            return descriptor.requested_index < descriptor.frame_count - 1
        return name in ("play_pause", "hide")

    def _activate_animation_control(self, name):
        state = self._animation_controls
        target = CanvasPanel._animation_object_by_id(self, state.target_id)
        if target is None or not target.is_animated:
            CanvasPanel._clear_animation_controls(self)
            return False
        if not same_canvas_object(self.get_selected_object(), target):
            self.set_selected_object(target)
        if not CanvasPanel._animation_control_enabled(self, target, name):
            self.Refresh()
            return False
        if name == "previous":
            return CanvasPanel._step_animation(self, target.object_id, -1)
        if name == "next":
            return CanvasPanel._step_animation(self, target.object_id, 1)
        if name == "play_pause":
            return CanvasPanel._toggle_animation_playback(self, target.object_id)
        if name == "hide":
            state.suppressed_id = str(target.object_id)
            state.focus_index = None
            state.pressed_name = None
            state.hover_name = None
            CanvasPanel._set_animation_control_target(
                self, target.object_id, False)
            self.Refresh()
            return True
        return False

    def _arm_timeline_preview(self):
        timer = getattr(self, "timeline_preview_timer", None)
        if timer is None:
            return False
        if timer.IsRunning():
            timer.Stop()
        timer.Start(TIMELINE_PREVIEW_DELAY_MS, wx.TIMER_ONE_SHOT)
        return True

    def _begin_timeline_drag(self, layout, pointer_x):
        state = self._animation_controls
        target = CanvasPanel._animation_object_by_id(self, layout.object_id)
        if target is None or not target.is_animated:
            return False
        if not same_canvas_object(self.get_selected_object(), target):
            self.set_selected_object(target)
        state.focus_index = FOCUS_NAMES.index("timeline")
        state.pressed_name = "timeline"
        state.timeline_dragging = True
        state.timeline_layout = layout
        target_index = timeline_frame_at(
            layout.timeline_track, pointer_x, target.animation.frame_count)
        state.timeline_desired_index = target_index
        self.SetFocus()
        if not self.HasCapture():
            self.CaptureMouse()
        return CanvasPanel._seek_animation_frame(
            self, target.object_id, target_index, prepare=True, submit=True)

    def _update_timeline_drag(self, pointer_x):
        state = self._animation_controls
        layout = state.timeline_layout
        target = CanvasPanel._animation_object_by_id(self, state.target_id)
        if (not state.timeline_dragging or layout is None or target is None
                or not target.is_animated):
            CanvasPanel._abandon_timeline_drag(self)
            return False
        target_index = timeline_frame_at(
            layout.timeline_track, pointer_x, target.animation.frame_count)
        if (target_index == state.timeline_desired_index
                and target.animation.requested_index == target_index):
            return True
        state.timeline_desired_index = target_index
        CanvasPanel._seek_animation_frame(
            self, target.object_id, target_index, prepare=False, submit=False)
        CanvasPanel._arm_timeline_preview(self)
        return True

    def on_timeline_preview_timer(self, _event):
        state = getattr(self, "_animation_controls", None)
        if state is None or not state.timeline_dragging:
            return False
        target = CanvasPanel._animation_object_by_id(self, state.target_id)
        if target is None or not target.is_animated:
            CanvasPanel._abandon_timeline_drag(self)
            return False
        return CanvasPanel._submit_animation_intent(self, target)

    def _finish_timeline_drag(self, pointer_x):
        state = self._animation_controls
        if not state.timeline_dragging:
            return False
        CanvasPanel._update_timeline_drag(self, pointer_x)
        target = CanvasPanel._animation_object_by_id(self, state.target_id)
        CanvasPanel._stop_timeline_preview_timer(self)
        state.timeline_dragging = False
        state.timeline_layout = None
        state.pressed_name = None
        if self.HasCapture():
            self.ReleaseMouse()
        if target is None or not target.is_animated:
            state.timeline_desired_index = None
            return False
        state.timeline_desired_index = target.animation.requested_index
        # Release bypasses the standstill delay. The navigator retains this
        # exact target behind any non-interruptible older reconstruction.
        accepted = CanvasPanel._submit_animation_intent(self, target)
        self.Refresh()
        return accepted

    def show_animation_controls(self, object_id=None):
        target = (CanvasPanel._animation_object_by_id(self, object_id)
                  if object_id is not None else self.get_selected_object())
        if target is None or not target.is_animated:
            return False
        state = self._animation_controls
        state.suppressed_id = None
        CanvasPanel._set_animation_control_target(self, target.object_id, True)
        state.focus_index = 0
        self.SetFocus()
        self.Refresh()
        return True

    def on_animation_control_timer(self, _event):
        state = self._animation_controls
        if (state.target_id is not None
                and CanvasPanel._animation_control_layout(self) is None):
            CanvasPanel._clear_animation_controls(self)
            self.Refresh()
            return False
        changed = state.advance()
        if not state.transitioning:
            CanvasPanel._sync_animation_control_timer(self)
            if state.opacity <= 0.0 and not state.visible_goal:
                state.target_id = None
                state.hover_name = None
        if changed:
            self.Refresh()
        return changed

    def on_mouse_leave(self, event):
        self._animation_control_pointer = None
        self._viewport_pointer = None
        self._viewport_hover_handle = None
        if getattr(self, "_viewport_gesture", None) is None:
            CanvasPanel._viewport_set_cursor(self, wx.NullCursor)
        self.Refresh(False)
        state = self._animation_controls
        if not state.retaining:
            CanvasPanel._set_animation_control_target(
                self, state.target_id, False)
            state.hover_name = None
            self.Refresh()
        event.Skip()

    def on_kill_focus(self, event):
        state = self._animation_controls
        if state.focus_index is not None and state.pressed_name is None:
            state.focus_index = None
            if self._animation_control_pointer is None:
                CanvasPanel._set_animation_control_target(
                    self, state.target_id, False)
            self.Refresh()
        event.Skip()

    def on_mouse_capture_lost(self, event):
        if CanvasPanel._cancel_viewport_gesture(self):
            event.Skip()
            return
        if not CanvasPanel._abandon_timeline_drag(self):
            self._animation_controls.pressed_name = None
        self.Refresh()
        event.Skip()

    def _draw_animation_controls(self, dc):
        state = self._animation_controls
        layout = CanvasPanel._animation_control_layout(self)
        if layout is None or state.opacity <= 0.0:
            return False
        alpha = max(0, min(255, int(round(235 * state.opacity))))
        panel_color = wx.Colour(37, 40, 44, alpha)
        dc.SetPen(wx.Pen(wx.Colour(70, 74, 80, alpha)))
        dc.SetBrush(wx.Brush(panel_color))
        radius = max(2, int(round(
            7 * CanvasPanel._animation_control_scale(self))))
        dc.DrawRoundedRectangle(layout.panel.x, layout.panel.y,
                                layout.panel.width, layout.panel.height, radius)

        target = CanvasPanel._animation_object_by_id(self, layout.object_id)
        descriptor = getattr(target, "animation", None)
        if descriptor is None:
            return False
        presentation = animation_control_presentation(descriptor)
        for button in layout.buttons:
            enabled = CanvasPanel._animation_control_enabled(
                self, target, button.name)
            highlighted = (state.hover_name == button.name
                           or state.focus_index == FOCUS_NAMES.index(button.name))
            color = (wx.Colour(44, 125, 190, alpha) if highlighted
                     else wx.Colour(65, 69, 75, alpha))
            if not enabled:
                color = wx.Colour(50, 53, 58, alpha)
            dc.SetPen(wx.Pen(wx.Colour(115, 180, 230, alpha)
                             if state.focus_index == FOCUS_NAMES.index(button.name)
                             else color,
                             2 if state.focus_index == FOCUS_NAMES.index(button.name)
                             else 1))
            dc.SetBrush(wx.Brush(color))
            rect = button.rect
            dc.DrawRoundedRectangle(rect.x, rect.y, rect.width, rect.height,
                                    max(2, radius // 2))
            icon_color = wx.Colour(
                245, 245, 245, alpha if enabled else alpha // 2)
            dc.SetPen(wx.Pen(icon_color, max(1, rect.width // 12)))
            dc.SetBrush(wx.Brush(icon_color))
            cx, cy = rect.x + rect.width // 2, rect.y + rect.height // 2
            extent = max(3, rect.width // 5)
            if button.name in ("previous", "next"):
                direction = -1 if button.name == "previous" else 1
                # Use a step symbol (|< / >|) so frame advance is distinct
                # from Play while retaining the existing vector treatment.
                half_extent = max(2, extent // 2)
                dc.DrawPolygon([
                    wx.Point(cx - direction * half_extent, cy - extent),
                    wx.Point(cx + direction * half_extent, cy),
                    wx.Point(cx - direction * half_extent, cy + extent),
                ])
                # Put the step bar before the triangle for Previous and after
                # it for Next: |< and >| when read left to right.
                bar_x = cx + direction * extent
                dc.DrawLine(bar_x, cy - extent, bar_x, cy + extent)
            elif button.name == "play_pause":
                if presentation.playing:
                    dc.DrawLine(cx - extent // 2, cy - extent,
                                cx - extent // 2, cy + extent)
                    dc.DrawLine(cx + extent // 2, cy - extent,
                                cx + extent // 2, cy + extent)
                else:
                    dc.DrawPolygon([
                        wx.Point(cx - extent, cy - extent),
                        wx.Point(cx + extent, cy),
                        wx.Point(cx - extent, cy + extent),
                    ])
            else:
                dc.DrawLine(cx - extent, cy - extent,
                            cx + extent, cy + extent)
                dc.DrawLine(cx + extent, cy - extent,
                            cx - extent, cy + extent)

        timeline = layout.timeline_rect
        track = layout.timeline_track
        if timeline is not None and track is not None:
            timeline_focused = (
                state.focus_index == FOCUS_NAMES.index("timeline"))
            if timeline_focused:
                dc.SetPen(wx.Pen(wx.Colour(115, 180, 230, alpha), 2))
                dc.SetBrush(wx.TRANSPARENT_BRUSH)
                dc.DrawRoundedRectangle(
                    timeline.x, timeline.y, timeline.width, timeline.height,
                    max(2, radius // 2))

            dc.SetTextForeground(wx.Colour(245, 245, 245, alpha))
            label = presentation.frame_label
            if presentation.pending_label is not None:
                label = f"{label} · {presentation.pending_label}"
            extent = dc.GetTextExtent(label)
            try:
                text_w, text_h = extent.width, extent.height
            except AttributeError:
                text_w, text_h = extent
            if text_w > timeline.width:
                label = (f"{presentation.displayed_index + 1}/"
                         f"{descriptor.frame_count}")
                if presentation.pending_label is not None:
                    label += f" → {presentation.requested_index + 1}"
                extent = dc.GetTextExtent(label)
                try:
                    text_w, text_h = extent.width, extent.height
                except AttributeError:
                    text_w, text_h = extent
            dc.DrawText(
                label, timeline.x + max(0, (timeline.width - text_w) // 2),
                timeline.y + max(0, (track.y - timeline.y - text_h) // 2))

            track_y = track.y + track.height // 2
            dc.SetPen(wx.Pen(wx.Colour(105, 110, 116, alpha),
                             max(1, int(round(2 * CanvasPanel._animation_control_scale(self))))))
            dc.DrawLine(track.x, track_y, track.right - 1, track_y)
            committed_x = timeline_thumb_x(
                track, presentation.displayed_index, descriptor.frame_count)
            requested_x = timeline_thumb_x(
                track, presentation.requested_index, descriptor.frame_count)
            marker_radius = max(2, min(
                track.height // 3,
                int(round(3 * CanvasPanel._animation_control_scale(self)))))
            dc.SetPen(wx.Pen(wx.Colour(210, 214, 218, alpha)))
            dc.SetBrush(wx.Brush(wx.Colour(210, 214, 218, alpha)))
            dc.DrawCircle(committed_x, track_y, marker_radius)
            if presentation.pending_label is not None:
                dc.SetPen(wx.Pen(wx.Colour(115, 180, 230, alpha), 2))
                dc.SetBrush(wx.Brush(wx.Colour(44, 125, 190, alpha)))
                dc.DrawCircle(requested_x, track_y, marker_radius + 1)
        return True

    def _viewport_dip(self, value):
        return max(1, int(round(value * CanvasPanel._animation_control_scale(self))))

    def _viewport_set_cursor(self, cursor):
        setter = getattr(self, "SetCursor", None)
        if setter is not None:
            setter(cursor)

    @staticmethod
    def _viewport_source_token(obj):
        animation = getattr(obj, "animation", None)
        return (obj.source_path, obj.normalize_orientation,
                ("gif", tuple(animation.source_identity)) if animation is not None
                else ("still", obj._source_revision))

    def _viewport_target(self, gesture):
        obj = CanvasPanel._animation_object_by_id(self, gesture["object_id"])
        if (obj is None or CanvasPanel._viewport_source_token(obj)
                != gesture["source_token"]):
            return None
        return obj

    def _viewport_grip_rect(self, geometry):
        frame = geometry.frame
        size = CanvasPanel._viewport_dip(self, 30)
        inset = CanvasPanel._viewport_dip(self, 11)
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        left, top = max(0, frame.x), max(0, frame.y)
        right, bottom = min(canvas_w, frame.right), min(canvas_h, frame.bottom)
        size = min(size, right - left, bottom - top)
        if size <= 0:
            return None
        x = min(max(left, frame.right - size - inset), right - size)
        y = min(max(top, frame.y + inset), bottom - size)
        return ViewRect(x, y, size, size)

    def _viewport_hit(self, x, y):
        obj = self.get_selected_object()
        if obj is None or obj._original_image is None:
            return None, None
        try:
            geometry = viewport_from_object(obj)
            geometry.validate()
        except GeometryError:
            frame = ViewRect(obj.x, obj.y, obj.width, obj.height)
            if hit_handle(frame, (x, y), CanvasPanel._viewport_dip(self, 12)):
                return "invalid", None
            return None, None
        pointer = getattr(self, "_viewport_pointer", None)
        if (pointer is not None and geometry.frame.contains(*pointer)
                and viewport_reduced(geometry)):
            grip = CanvasPanel._viewport_grip_rect(self, geometry)
            if grip is not None and grip.contains(x, y):
                return "reposition", None
        handle = hit_handle(geometry.frame, (x, y),
                            CanvasPanel._viewport_dip(self, 12))
        return ("resize", handle) if handle else (None, None)

    def _draw_viewport_controls(self, dc):
        obj = self.get_selected_object()
        if obj is None or obj._original_image is None:
            return
        try:
            geometry = viewport_from_object(obj)
        except GeometryError:
            return
        try:
            can_reposition = viewport_reduced(geometry)
        except GeometryError:
            can_reposition = False
        visual = CanvasPanel._viewport_dip(self, 8)
        hover = getattr(self, "_viewport_hover_handle", None)
        for name, (cx, cy) in handle_centers(geometry.frame):
            dc.SetPen(wx.Pen(wx.Colour(36, 92, 156), 1))
            dc.SetBrush(wx.Brush(wx.Colour(156, 192, 255) if name == hover
                                 else wx.WHITE))
            dc.DrawRectangle(cx - visual // 2, cy - visual // 2,
                             visual, visual)
        pointer = getattr(self, "_viewport_pointer", None)
        gesture = getattr(self, "_viewport_gesture", None)
        visible = ((pointer is not None and geometry.frame.contains(*pointer)
                    and can_reposition)
                   or (gesture is not None and gesture["kind"] == "reposition"
                       and gesture["object_id"] == obj.object_id))
        if not visible:
            return
        grip = CanvasPanel._viewport_grip_rect(self, geometry)
        if grip is None:
            return
        dc.SetPen(wx.Pen(wx.Colour(140, 170, 215), 1))
        dc.SetBrush(wx.Brush(wx.Colour(51, 56, 64)))
        dc.DrawRoundedRectangle(grip.x, grip.y, grip.width, grip.height,
                                CanvasPanel._viewport_dip(self, 6))
        cx, cy = grip.x + grip.width // 2, grip.y + grip.height // 2
        reach = max(2, min(CanvasPanel._viewport_dip(self, 8),
                           grip.width // 3, grip.height // 3))
        barb = max(1, reach // 3)
        dc.SetPen(wx.Pen(wx.WHITE, max(1, CanvasPanel._viewport_dip(self, 2))))
        dc.DrawLine(cx - reach, cy, cx + reach, cy)
        dc.DrawLine(cx, cy - reach, cx, cy + reach)
        for direction in (-1, 1):
            tip = cx + direction * reach
            dc.DrawLine(tip, cy, tip - direction * barb, cy - barb)
            dc.DrawLine(tip, cy, tip - direction * barb, cy + barb)
            tip = cy + direction * reach
            dc.DrawLine(cx, tip, cx - barb, tip - direction * barb)
            dc.DrawLine(cx, tip, cx + barb, tip - direction * barb)

    def _start_viewport_gesture(self, obj, kind, handle, point):
        if kind == "move" and obj._original_image is None:
            return False
        try:
            start = viewport_from_object(obj)
            if kind == "move":
                try:
                    start.validate()
                except GeometryError:
                    return False  # Existing body move remains usable for legacy scenes.
            else:
                start.validate()
        except GeometryError as exc:
            obj.set_status_overlay(str(exc), "warning")
            self._schedule_overlay_clear(image_object=obj)
            self.Refresh()
            return False
        self._viewport_gesture = {
            "object_id": obj.object_id, "source_token": CanvasPanel._viewport_source_token(obj),
            "kind": kind, "handle": handle, "point": tuple(point),
            "start": start, "changed": False,
        }
        if not self.HasCapture():
            self.CaptureMouse()
        self.SetFocus()
        return True

    def _cancel_viewport_gesture(self, *, restore=True):
        gesture = getattr(self, "_viewport_gesture", None)
        if gesture is None:
            return False
        self._viewport_gesture = None
        obj = CanvasPanel._viewport_target(self, gesture) if restore else None
        if obj is not None and gesture["changed"]:
            apply_viewport(obj, gesture["start"])
        if self.HasCapture():
            self.ReleaseMouse()
        self._viewport_pointer = None
        self._viewport_hover_handle = None
        CanvasPanel._viewport_set_cursor(self, wx.NullCursor)
        self.Refresh()
        return True

    def _finish_viewport_gesture(self):
        gesture = getattr(self, "_viewport_gesture", None)
        if gesture is None:
            return False
        if CanvasPanel._viewport_target(self, gesture) is None:
            return CanvasPanel._cancel_viewport_gesture(self, restore=False)
        self._viewport_gesture = None
        if self.HasCapture():
            self.ReleaseMouse()
        CanvasPanel._viewport_set_cursor(self, wx.NullCursor)
        self.Refresh()
        return True

    def reset_selected_frame(self, object_id=None):
        CanvasPanel._cancel_viewport_gesture(self)
        obj = (CanvasPanel._animation_object_by_id(self, object_id)
               if object_id is not None else self.get_selected_object())
        if obj is None:
            return False
        try:
            geometry = reset_frame_size(viewport_from_object(obj))
        except GeometryError as exc:
            obj.set_status_overlay(str(exc), "warning")
            self._schedule_overlay_clear(image_object=obj)
            self.Refresh()
            return False
        CanvasPanel.prepare_canvas_edit(self, "frame reset")
        apply_viewport(obj, geometry)
        self._viewport_pointer = None
        self._viewport_hover_handle = None
        self.Refresh()
        return True

    def on_left_down(self, event):
        mouse_x, mouse_y = event.GetPosition()

        if getattr(self, "_viewport_gesture", None) is not None:
            return

        timeline_layout = CanvasPanel._animation_control_timeline_at(
            self, mouse_x, mouse_y)
        if timeline_layout is not None:
            CanvasPanel._begin_timeline_drag(
                self, timeline_layout, mouse_x)
            return

        control = CanvasPanel._animation_control_button_at(
            self, mouse_x, mouse_y)
        if control is not None:
            state = self._animation_controls
            state.focus_index = FOCUS_NAMES.index(control.name)
            state.pressed_name = control.name
            self.SetFocus()
            if control.name != "hide" and not self.HasCapture():
                self.CaptureMouse()
            CanvasPanel._activate_animation_control(self, control.name)
            return

        if CanvasPanel._handle_save_card_click(self, mouse_x, mouse_y):
            self.SetFocus()
            event.Skip()
            return
        if CanvasPanel._handle_export_card_click(self, mouse_x, mouse_y):
            self.SetFocus()
            event.Skip()
            return
        if CanvasPanel._handle_scene_card_click(self, mouse_x, mouse_y):
            self.SetFocus()
            event.Skip()
            return
        if CanvasPanel._handle_drop_card_click(self, mouse_x, mouse_y):
            self.SetFocus()
            event.Skip()
            return

        self._viewport_pointer = (mouse_x, mouse_y)
        kind, handle = CanvasPanel._viewport_hit(self, mouse_x, mouse_y)
        selected = self.get_selected_object()
        if kind == "invalid" and selected is not None:
            selected.set_status_overlay(
                "Frame is outside image content; use Reset Frame Size", "warning")
            self._schedule_overlay_clear(image_object=selected)
            self.Refresh()
            return
        if kind is not None and selected is not None:
            CanvasPanel._start_viewport_gesture(
                self, selected, kind, handle, (mouse_x, mouse_y))
            return

        # Check if clicked on any image object
        clicked_obj = None
        for obj in reversed(self.image_objects):  # topmost last
            if obj.contains(mouse_x, mouse_y):
                clicked_obj = obj
                break

        # A click dismisses only ordinary feedback. A running operation must
        # remain visible until it completes, fails, or is canceled.
        if (clicked_obj and clicked_obj.show_status_overlay
                and clicked_obj.status_type != 'processing'):
            logging.debug("Clearing overlay due to click on object with overlay")
            clicked_obj.clear_status_overlay()
            _reschedule_status_timer(self)
            self.Refresh()

        if clicked_obj:
            self._context_object_id = clicked_obj.object_id
            self.set_selected_object(clicked_obj)
            # Bring clicked object to the front
            self.bring_image_object_to_front(clicked_obj)

            # Prepare for dragging
            self.drag_offset = (mouse_x - clicked_obj.x, mouse_y - clicked_obj.y)
            CanvasPanel._start_viewport_gesture(
                self, clicked_obj, "move", None, (mouse_x, mouse_y))
            if getattr(self, "_viewport_gesture", None) is not None:
                self.drag_offset = None
        else:
            self.set_selected_object(None)
            self.drag_offset = None

        self.SetFocus()  # so we can receive key events
        event.Skip()

    def on_left_up(self, event):
        if CanvasPanel._finish_viewport_gesture(self):
            self._viewport_pointer = tuple(event.GetPosition())
            return
        state = self._animation_controls
        if state.timeline_dragging:
            mouse_x, _mouse_y = event.GetPosition()
            CanvasPanel._finish_timeline_drag(self, mouse_x)
            CanvasPanel._update_animation_control_hover(
                self, *event.GetPosition())
            return
        if state.pressed_name is not None:
            state.pressed_name = None
            if self.HasCapture():
                self.ReleaseMouse()
            mouse_x, mouse_y = event.GetPosition()
            CanvasPanel._update_animation_control_hover(self, mouse_x, mouse_y)
            return
        self.drag_offset = None
        self.resizing = False
        event.Skip()

    def on_mouse_move(self, event):
        gesture = getattr(self, "_viewport_gesture", None)
        if gesture is not None:
            obj = CanvasPanel._viewport_target(self, gesture)
            if obj is None:
                CanvasPanel._cancel_viewport_gesture(self, restore=False)
                return
            if event.Dragging() and event.LeftIsDown():
                point = tuple(event.GetPosition())
                start = gesture["start"]
                dx = point[0] - gesture["point"][0]
                dy = point[1] - gesture["point"][1]
                try:
                    if gesture["kind"] == "resize":
                        next_geometry = resize_frame(
                            start, gesture["handle"], point, (32, 32))
                    elif gesture["kind"] == "reposition":
                        next_geometry = reposition_content(start, (dx, dy))
                    else:
                        new_x, new_y = snap_to_nearby_edges(
                            start.frame.x + dx, start.frame.y + dy,
                            start.frame.width, start.frame.height,
                            self.image_objects, self.GetSize())
                        next_geometry = replace(start, frame=replace(
                            start.frame, x=new_x, y=new_y))
                    if (next_geometry.frame != ViewRect(
                            obj.x, obj.y, obj.width, obj.height)
                            or next_geometry.offset != tuple(obj.viewport_offset)):
                        if not gesture["changed"]:
                            CanvasPanel.prepare_canvas_edit(self, "viewport edited")
                            gesture["changed"] = True
                        apply_viewport(obj, next_geometry)
                        self.Refresh()
                except GeometryError:
                    CanvasPanel._cancel_viewport_gesture(self)
                return
            return
        controls = getattr(self, "_animation_controls", None)
        if controls is not None and controls.timeline_dragging:
            mouse_x, _mouse_y = event.GetPosition()
            CanvasPanel._update_timeline_drag(self, mouse_x)
            return
        if event.Dragging() and event.LeftIsDown() and self.drag_offset:
            # We are moving the selected object
            mouse_x, mouse_y = event.GetPosition()
            dx, dy = self.drag_offset
            obj = self.selected_object
            if obj:
                new_x = mouse_x - dx
                new_y = mouse_y - dy
                # Snap to edges if near
                new_x, new_y = snap_to_nearby_edges(new_x, new_y, obj.width, obj.height,
                                                    self.image_objects, self.GetSize())
                CanvasPanel.prepare_canvas_edit(self, "image moved")
                obj.x = new_x
                obj.y = new_y
                self.Refresh()
        elif not (event.Dragging() and event.LeftIsDown()):
            mouse_x, mouse_y = event.GetPosition()
            CanvasPanel._update_animation_control_hover(self, mouse_x, mouse_y)
            old = (getattr(self, "_viewport_pointer", None),
                   getattr(self, "_viewport_hover_handle", None))
            self._viewport_pointer = (mouse_x, mouse_y)
            animation_target = (
                CanvasPanel._animation_control_button_at(self, mouse_x, mouse_y)
                or CanvasPanel._animation_control_timeline_at(self, mouse_x, mouse_y))
            kind, handle = ((None, None) if animation_target else
                            CanvasPanel._viewport_hit(self, mouse_x, mouse_y))
            self._viewport_hover_handle = handle if kind == "resize" else None
            cursors = {
                "n": wx.CURSOR_SIZENS, "s": wx.CURSOR_SIZENS,
                "e": wx.CURSOR_SIZEWE, "w": wx.CURSOR_SIZEWE,
                "nw": wx.CURSOR_SIZENWSE, "se": wx.CURSOR_SIZENWSE,
                "ne": wx.CURSOR_SIZENESW, "sw": wx.CURSOR_SIZENESW,
            }
            if kind == "reposition":
                CanvasPanel._viewport_set_cursor(self, wx.Cursor(wx.CURSOR_HAND))
            elif handle is not None:
                CanvasPanel._viewport_set_cursor(self, wx.Cursor(cursors[handle]))
            else:
                CanvasPanel._viewport_set_cursor(self, wx.NullCursor)
            if old != (self._viewport_pointer, self._viewport_hover_handle):
                self.Refresh(False)
        event.Skip()

    def on_right_down(self, event):
        # If user right-clicks on an object, we'll let the main frame handle the context menu
        # (We do it in MainFrame via EVT_CONTEXT_MENU).
        # But we can also store which object was clicked:
        mouse_x, mouse_y = event.GetPosition()
        clicked_obj = None
        for obj in reversed(self.image_objects):
            if obj.contains(mouse_x, mouse_y):
                clicked_obj = obj
                break
        if clicked_obj:
            self._context_object_id = clicked_obj.object_id
            self.set_selected_object(clicked_obj)
            # bring to front
            self.bring_image_object_to_front(clicked_obj)
        else:
            self._context_object_id = None
        event.Skip()

    def _frame_shortcut_has_canvas_focus(self):
        """Keep punctuation available to editable controls and dialogs."""
        try:
            focus = wx.Window.FindFocus()
        except Exception:
            # Compatibility/headless callers have no wx.App and no editable
            # focus to protect. Production event delivery always has one.
            focus = None
        if focus is None:
            return True
        try:
            if focus.GetTopLevelParent() is not self.GetTopLevelParent():
                return False
        except AttributeError:
            return False
        return focus is self and not isinstance(focus, wx.TextEntry)

    def _retire_navigation_for_frame_step(self, image_object):
        """Invalidate directory/decode callbacks without resetting frame intent."""
        _cancel_navigation_decode(self, image_object)
        image_object._work_generation += 1
        image_object.reset_navigation_intent()
        if (image_object.status_operation == "navigation"
                and image_object.status_type == "processing"):
            image_object.clear_status_overlay()

    def _step_selected_animation(self, step):
        """Route the selected object through the shared targeted action."""
        image_object = self.get_selected_object()
        if image_object is None:
            return False
        return CanvasPanel._step_animation(self, image_object.object_id, step)

    def _step_animation(self, object_id, step):
        """Advance one live runtime object's clamped logical frame intent."""
        image_object = CanvasPanel._animation_object_by_id(self, object_id)
        if image_object is None or not image_object.is_animated:
            return False
        target_index = image_object.animation.requested_index + int(step)
        return CanvasPanel._seek_animation_frame(
            self, object_id, target_index, prepare=True, submit=True)

    def _seek_animation_frame(self, object_id, target_index, *, prepare=True,
                              submit=True):
        """Set one runtime object's absolute frame intent and optionally decode."""
        image_object = CanvasPanel._animation_object_by_id(self, object_id)
        if image_object is None or not image_object.is_animated:
            return False

        if prepare:
            # Exact seeking always freezes committed pixels first. Buffered
            # playback targets never become a manual seek base.
            _cancel_animation_playback(self, image_object)
            CanvasPanel._note_user_interaction(self)
            CanvasPanel._retire_navigation_for_frame_step(self, image_object)
        intent = image_object.set_animation_intent(target_index)
        if intent is None:
            return False
        descriptor, _changed = intent
        target_index = descriptor.requested_index
        if target_index == descriptor.displayed_index:
            _cancel_animation_decode(self, image_object)
            image_object.set_status_overlay(
                f"Frame {descriptor.displayed_index + 1}/{descriptor.frame_count}",
                "info", operation="animation")
            self._schedule_overlay_clear(image_object=image_object)
            self.Refresh()
            return True
        if not submit:
            image_object.set_status_overlay(
                f"Seeking frame {target_index + 1}/{descriptor.frame_count}...",
                "processing", operation="animation")
            _reschedule_status_timer(self)
            self.Refresh()
            return True

        return CanvasPanel._submit_animation_intent(self, image_object)

    def _submit_animation_intent(self, image_object):
        """Submit the object's latest exact target through bounded ownership."""
        descriptor = getattr(image_object, "animation", None)
        if descriptor is None:
            return False
        target_index = descriptor.requested_index
        if target_index == descriptor.displayed_index:
            _cancel_animation_decode(self, image_object)
            self.Refresh()
            return True
        generation = descriptor.request_generation
        context = (
            image_object, image_object.source_path,
            descriptor.source_identity, generation, target_index,
        )
        image_object.set_status_overlay(
            f"Seeking frame {target_index + 1}/{descriptor.frame_count}...",
            "processing", operation="animation")
        _reschedule_status_timer(self)
        self.Refresh()
        callback = getattr(self, "_on_animation_frame_decoded", None)
        if callback is None:
            callback = CanvasPanel._on_animation_frame_decoded.__get__(self)
        accepted = self.file_navigator.request_animation_frame(
            image_object.source_path, target_index, image_object.object_id,
            context, callback,
            expected_source_identity=descriptor.source_identity)
        if accepted:
            return True

        image_object.cancel_animation_intent()
        image_object.set_status_overlay(
            "Frame request was rejected. Press , or . to retry.",
            "warning", operation="animation")
        _reschedule_status_timer(self)
        self.Refresh()
        return False

    def _on_animation_frame_decoded(self, result):
        """Validate and transactionally publish only the latest GIF frame."""
        try:
            image_object, source_path, source_identity, generation, frame_index = (
                result.context)
        except (TypeError, ValueError):
            result.close()
            return False
        descriptor = getattr(image_object, "animation", None)
        if (self.file_navigator.is_shutdown
                or not self.image_objects.contains_object(image_object)
                or not same_canvas_object(self.selected_object, image_object)
                or image_object.source_path != source_path
                or descriptor is None
                or descriptor.source_identity != tuple(source_identity)
                or descriptor.request_generation != generation
                or descriptor.requested_index != frame_index
                or result.frame_index != frame_index):
            result.close()
            logging.debug("Released stale GIF frame result")
            return False

        if result.error is not None or result.pixels is None:
            result.close()
            detail = result.error or "decoder returned no pixels"
            frame_count = descriptor.frame_count
            image_object.cancel_animation_intent()
            image_object.set_status_overlay(
                f"Couldn't load frame {frame_index + 1}/{frame_count}: {detail}. "
                "Press , or . to retry.",
                "warning", operation="animation")
            _reschedule_status_timer(self)
            self.Refresh()
            return False

        geometry = (
            image_object.x, image_object.y, image_object.width,
            image_object.height, image_object.zoom_factor,
            tuple(image_object.viewport_offset),
        )
        pixels = result.take_pixels()
        try:
            image_object.commit_animation_candidate(
                pixels, frame_index, source_identity, generation,
                allow_source_identity_refresh=result.source_refreshed)
        except Exception as exc:
            pixels.close()
            image_object.cancel_animation_intent()
            image_object.set_status_overlay(
                f"Couldn't publish GIF frame: {exc}. Press , or . to retry.",
                "warning", operation="animation")
            _reschedule_status_timer(self)
            self.Refresh()
            return False

        committed_geometry = (
            image_object.x, image_object.y, image_object.width,
            image_object.height, image_object.zoom_factor,
            tuple(image_object.viewport_offset),
        )
        if committed_geometry != geometry:
            raise AssertionError("GIF frame commit changed object geometry")
        descriptor = image_object.animation
        image_object.set_status_overlay(
            f"Frame {descriptor.displayed_index + 1}/{descriptor.frame_count}",
            "info", operation="animation")
        self._schedule_overlay_clear(image_object=image_object)
        self.Refresh()
        return True

    def _toggle_selected_animation_playback(self):
        """Route the selected object through the shared targeted action."""
        image_object = self.get_selected_object()
        if image_object is None:
            return False
        return CanvasPanel._toggle_animation_playback(
            self, image_object.object_id)

    def _toggle_animation_playback(self, object_id):
        """Play or pause one live runtime object without selection swapping."""
        image_object = CanvasPanel._animation_object_by_id(self, object_id)
        if image_object is None or not image_object.is_animated:
            return False
        descriptor = image_object.animation
        if descriptor.playing:
            _cancel_animation_playback(self, image_object)
            descriptor = image_object.animation
            image_object.set_status_overlay(
                f"Paused · Frame {descriptor.displayed_index + 1}/"
                f"{descriptor.frame_count}", "info", operation="animation")
            self._schedule_overlay_clear(image_object=image_object)
            self._reschedule_playback_timer()
            self.Refresh()
            return True

        _cancel_animation_decode(self, image_object)
        image_object.cancel_animation_intent()
        spec = image_object.start_animation_playback(self._monotonic())
        if spec is None:
            return False
        descriptor = image_object.animation
        context = (
            image_object, image_object.source_path,
            descriptor.source_identity, descriptor.playback_generation,
        )
        callback = getattr(self, "_on_playback_slice", None)
        if callback is None:
            callback = CanvasPanel._on_playback_slice.__get__(self)
        accepted = self.file_navigator.start_gif_playback(
            **spec, context=context, callback=callback)
        if not accepted:
            image_object.fail_animation_playback()
            image_object.set_status_overlay(
                "Playback request was rejected. Press Space to retry.",
                "warning", operation="animation")
            _reschedule_status_timer(self)
            self.Refresh()
            return False
        image_object.set_status_overlay(
            f"Playing · Frame {descriptor.displayed_index + 1}/"
            f"{descriptor.frame_count}", "info", operation="animation")
        self._schedule_overlay_clear(image_object=image_object)
        self.Refresh()
        return True

    def _on_playback_slice(self, result):
        """Accept bounded current-generation playback transfers on the GUI."""
        try:
            image_object, source_path, source_identity, generation = result.context
        except (TypeError, ValueError):
            result.close()
            return False
        descriptor = getattr(image_object, "animation", None)
        current = (
            not self.file_navigator.is_shutdown
            and self.image_objects.contains_object(image_object)
            and image_object.source_path == source_path
            and descriptor is not None
            and descriptor.playing
            and descriptor.source_identity == tuple(source_identity)
            and descriptor.playback_generation == generation
        )
        if not current:
            result.close()
            return False

        if result.error:
            result.close()
            self.file_navigator.retire_gif_playback(
                image_object.object_id, generation)
            image_object.fail_animation_playback()
            image_object.set_status_overlay(
                f"GIF playback paused: {result.error}", "warning",
                operation="animation")
            _reschedule_status_timer(self)
            self._reschedule_playback_timer()
            self.Refresh()
            return False

        accepted = []
        for packet in result.packets:
            if (packet.object_id == image_object.object_id
                    and packet.source_path == source_path
                    and packet.source_identity == tuple(source_identity)
                    and packet.playback_generation == generation):
                accepted.append(packet)
            else:
                packet.close()
        result.packets.clear()
        descriptor.playback_buffer.extend(accepted)
        descriptor.playback_buffer.sort(key=lambda packet: packet.due_time)

        if not result.finished:
            self.file_navigator.request_playback_slice(image_object.object_id)
        self._reschedule_playback_timer()
        return bool(accepted) or result.blocked or result.finished

    def _present_due_playback(self, now=None):
        """Adopt the newest complete due frame from each playing object."""
        now = self._monotonic() if now is None else float(now)
        retire_idle = getattr(self.file_navigator, "retire_idle_gif_decoders", None)
        if retire_idle is not None:
            retire_idle(now)
        changed = False
        for image_object in tuple(self.image_objects):
            descriptor = getattr(image_object, "animation", None)
            if descriptor is None or not descriptor.playing:
                continue
            due = [packet for packet in descriptor.playback_buffer
                   if packet.due_time <= now]
            if not due:
                continue
            descriptor.playback_buffer = [
                packet for packet in descriptor.playback_buffer
                if packet.due_time > now]
            newest = due[-1]
            for packet in due[:-1]:
                packet.close()
                descriptor.dropped_frames += 1
            if now > newest.due_time:
                descriptor.late_frames += 1

            geometry = (
                image_object.x, image_object.y, image_object.width,
                image_object.height, image_object.zoom_factor,
                tuple(image_object.viewport_offset),
            )
            pixels = newest.take_pixels()
            try:
                if pixels is None:
                    raise ValueError("playback pixels were already released")
                image_object.commit_playback_candidate(pixels, newest)
            except Exception as exc:
                if pixels is not None:
                    pixels.close()
                self.file_navigator.retire_gif_playback(
                    image_object.object_id, newest.playback_generation)
                image_object.fail_animation_playback()
                image_object.set_status_overlay(
                    f"GIF playback paused: {exc}", "warning",
                    operation="animation")
                _reschedule_status_timer(self)
                continue
            committed_geometry = (
                image_object.x, image_object.y, image_object.width,
                image_object.height, image_object.zoom_factor,
                tuple(image_object.viewport_offset),
            )
            if geometry != committed_geometry:
                raise AssertionError("GIF playback changed object geometry")
            changed = True
            descriptor = image_object.animation
            if newest.terminal:
                self.file_navigator.retire_gif_playback(
                    image_object.object_id, newest.playback_generation)
            elif descriptor.playing:
                self.file_navigator.request_playback_slice(image_object.object_id)
        if changed:
            self.Refresh()
        self._reschedule_playback_timer(now=now)
        return changed

    def on_playback_timer(self, _event):
        return self._present_due_playback()

    def _stop_playback_timer(self):
        timer = getattr(self, "playback_timer", None)
        if timer is not None and timer.IsRunning():
            timer.Stop()

    def _reschedule_playback_timer(self, now=None):
        timer = getattr(self, "playback_timer", None)
        if timer is None:
            return False
        if timer.IsRunning():
            timer.Stop()
        deadlines = [
            packet.due_time
            for image_object in self.image_objects
            for descriptor in (getattr(image_object, "animation", None),)
            if descriptor is not None and descriptor.playing
            for packet in descriptor.playback_buffer
        ]
        if not deadlines:
            return False
        now = self._monotonic() if now is None else float(now)
        delay_ms = max(1, int(math.ceil((min(deadlines) - now) * 1000)))
        timer.Start(delay_ms, wx.TIMER_ONE_SHOT)
        return True

    def _handle_animation_control_key(self, event):
        state = getattr(self, "_animation_controls", None)
        if state is None:
            return False
        keycode = event.GetKeyCode()
        shift = getattr(event, "ShiftDown", lambda: False)()
        modified = (getattr(event, "ControlDown", lambda: False)()
                    or getattr(event, "AltDown", lambda: False)())
        if keycode == wx.WXK_TAB and not modified:
            if (state.focus_index is None
                    and state.target_id is not None
                    and state.visible_goal):
                state.focus_index = len(FOCUS_NAMES) - 1 if shift else 0
                self.Refresh()
                return True
            if state.focus_index is not None:
                next_index = state.focus_index + (-1 if shift else 1)
                if 0 <= next_index < len(FOCUS_NAMES):
                    state.focus_index = next_index
                    self.Refresh()
                    return True
                state.focus_index = None
                if self._animation_control_pointer is None:
                    CanvasPanel._set_animation_control_target(
                        self, state.target_id, False)
                self.Refresh()
                event.Skip()
                return True
        if state.focus_index is None:
            return False
        focused_name = FOCUS_NAMES[state.focus_index]
        if focused_name == "timeline" and not modified:
            target = CanvasPanel._animation_object_by_id(self, state.target_id)
            if target is None or not target.is_animated:
                CanvasPanel._clear_animation_controls(self)
                return False
            descriptor = target.animation
            if keycode in (wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_HOME, wx.WXK_END):
                if keycode == wx.WXK_LEFT:
                    target_index = descriptor.requested_index - 1
                elif keycode == wx.WXK_RIGHT:
                    target_index = descriptor.requested_index + 1
                elif keycode == wx.WXK_HOME:
                    target_index = 0
                else:
                    target_index = descriptor.frame_count - 1
                CanvasPanel._seek_animation_frame(
                    self, target.object_id, target_index,
                    prepare=True, submit=True)
                return True
            if (keycode == wx.WXK_SPACE
                    and not getattr(event, "IsAutoRepeat", lambda: False)()):
                CanvasPanel._toggle_animation_playback(self, target.object_id)
                return True
            if keycode in (wx.WXK_RETURN,
                           getattr(wx, "WXK_NUMPAD_ENTER", wx.WXK_RETURN)):
                CanvasPanel._submit_animation_intent(self, target)
                return True
        if (keycode in (wx.WXK_SPACE, wx.WXK_RETURN,
                        getattr(wx, "WXK_NUMPAD_ENTER", wx.WXK_RETURN))
                and not modified
                and not getattr(event, "IsAutoRepeat", lambda: False)()):
            CanvasPanel._activate_animation_control(
                self, focused_name)
            return True
        # A painted button owns only its navigation/activation keys. Let the
        # canvas handler process unrelated shortcuts such as comma/period;
        # swallowing them here makes frame stepping stop after a mouse click.
        return False

    def on_key_down(self, event):
        # Basic key handling for zoom in/out or other hotkeys
        keycode = event.GetKeyCode()
        if (keycode == wx.WXK_ESCAPE
                and CanvasPanel._cancel_viewport_gesture(self)):
            return
        logging.debug(f"Key pressed: {keycode}, selected_object: {self.selected_object is not None}")

        if CanvasPanel._handle_animation_control_key(self, event):
            return

        # X is an application action, not an image action. Handle it before
        # the selection guard because a skipped child key event is not
        # guaranteed to reach the frame.
        if (keycode in (ord('x'), ord('X'))
                and not getattr(event, "ControlDown", lambda: False)()):
            parent = self.GetTopLevelParent()
            quit_handler = getattr(parent, "on_quit", None)
            if quit_handler is not None:
                quit_handler(None)
            else:
                self.file_navigator.shutdown()
                wx.GetApp().set_exit_code(0)
                parent.Close(force=True)
                wx.CallAfter(wx.GetApp().ExitMainLoop)
            return True

        if keycode in (ord(','), ord('.')):
            blocked = (getattr(event, "ControlDown", lambda: False)()
                       or getattr(event, "AltDown", lambda: False)()
                       or not CanvasPanel._frame_shortcut_has_canvas_focus(self))
            if blocked or not CanvasPanel._step_selected_animation(
                    self, -1 if keycode == ord(',') else 1):
                event.Skip()
            return

        if keycode == wx.WXK_SPACE:
            blocked = (getattr(event, "ControlDown", lambda: False)()
                       or getattr(event, "AltDown", lambda: False)()
                       or getattr(event, "IsAutoRepeat", lambda: False)()
                       or not CanvasPanel._frame_shortcut_has_canvas_focus(self))
            if blocked or not CanvasPanel._toggle_selected_animation_playback(self):
                event.Skip()
            return

        if self.get_selected_object() is None:
            logging.debug("No selected object - skipping key handler")
            event.Skip()
            return

        logging.debug(f"Processing key {keycode} with selected object")
        # e.g. + or = to zoom in, - to zoom out
        if keycode in (wx.WXK_ADD, wx.WXK_NUMPAD_ADD, 61):  # '=' can be 61
            logging.debug(f"Zoom in: selected_object={self.selected_object}, id={id(self.selected_object) if self.selected_object else None}")
            self._zoom_selected_image(zoom_in=True)
            logging.debug(f"After zoom_in: overlay={self.selected_object.show_status_overlay}, message='{self.selected_object.status_message}'")
        elif keycode in (wx.WXK_SUBTRACT, wx.WXK_NUMPAD_SUBTRACT, 45):  # '-' can be 45
            logging.debug(f"Zoom out: selected_object={self.selected_object}, id={id(self.selected_object) if self.selected_object else None}")
            self._zoom_selected_image(zoom_in=False)
            logging.debug(f"After zoom_out: overlay={self.selected_object.show_status_overlay}, message='{self.selected_object.status_message}'")
        else:
            event.Skip()

    def _reset_zoom_wheel_remainder(self, image_object=None):
        """Discard partial Ctrl-wheel input for one object or the canvas."""
        if image_object is None:
            self._zoom_wheel_remainders.clear()
        else:
            self._zoom_wheel_remainders.pop(image_object.object_id, None)

    def _zoom_selected_image(self, zoom_in, *, reset_wheel=True):
        """Apply one keyboard-equivalent zoom step to the selected object."""
        image_object = self.get_selected_object()
        if image_object is None:
            return False
        CanvasPanel._cancel_viewport_gesture(self)
        image_object.load_image()
        # A rejected limit step must leave the crop and frame intact.
        if zoom_in:
            allowed = image_object.zoom_factor * 1.25 <= 5.0
        else:
            allowed = (image_object.zoom_factor * 0.8 >= image_object._minimum_zoom
                       or image_object.zoom_factor > image_object._minimum_zoom)
        if allowed:
            try:
                full_frame = reset_frame_size(viewport_from_object(image_object))
            except GeometryError as exc:
                image_object.set_status_overlay(str(exc), "warning")
                self._schedule_overlay_clear(image_object=image_object)
                self.Refresh()
                return False
            apply_viewport(image_object, full_frame)
        CanvasPanel._note_user_interaction(self)
        # Zoom supersedes queued navigation for this object.  Existing
        # callbacks carrying the previous generation are rejected.
        _cancel_object_work(self, image_object)
        if reset_wheel:
            self._reset_zoom_wheel_remainder(image_object)
        if zoom_in:
            image_object.zoom_in()
        else:
            image_object.zoom_out()
        self.Refresh()
        self._schedule_overlay_clear()
        return True

    @staticmethod
    def _wheel_is_vertical(event):
        get_axis = getattr(event, "GetWheelAxis", None)
        if get_axis is None:
            return True
        return get_axis() == getattr(wx, "MOUSE_WHEEL_VERTICAL", 0)

    def _handle_ctrl_wheel_zoom(self, event):
        """Consume vertical Ctrl-wheel and apply complete wheel notches."""
        if not self._wheel_is_vertical(event):
            return False
        image_object = self.get_selected_object()
        rotation = event.GetWheelRotation()
        delta = event.GetWheelDelta()
        if image_object is None or not isinstance(delta, (int, float)) or delta <= 0:
            return True
        if not isinstance(rotation, (int, float)) or rotation == 0:
            return True

        key = image_object.object_id
        total = self._zoom_wheel_remainders.get(key, 0) + rotation
        notches = int(total / delta)  # signed truncation preserves reversals
        self._zoom_wheel_remainders[key] = total - notches * delta
        for _ in range(abs(notches)):
            self._zoom_selected_image(zoom_in=notches > 0, reset_wheel=False)
        return True

    def on_mouse_wheel(self, event):
        """Handle Ctrl zoom or the existing plain-wheel file navigation."""
        pointer = getattr(self, "_animation_control_pointer", None)
        layout = CanvasPanel._animation_control_layout(self)
        if (pointer is not None and layout is not None
                and CanvasPanel._animation_controls_interactive(self)
                and layout.panel.contains(*pointer)):
            return
        if event.ControlDown() and self._handle_ctrl_wheel_zoom(event):
            # Ctrl vertical input is consumed, including no-selection, limit,
            # and invalid-delta events. Other modifiers do not alter Ctrl;
            # Alt alone retains the existing plain-wheel navigation behavior.
            return
        if not self._wheel_is_vertical(event):
            event.Skip()
            return

        self._reset_zoom_wheel_remainder(self.get_selected_object())
        if (self.settings_manager.get_setting(
                "Navigation", "enable_wheel_navigation", "true").lower() != "true"
                or self.get_selected_object() is None):
            event.Skip()
            return

        rotation = event.GetWheelRotation()
        if rotation > 0:
            self._navigate_to_adjacent_file(previous=True)
        elif rotation < 0:
            self._navigate_to_adjacent_file(previous=False)
        event.Skip()

    def _navigate_to_adjacent_file(self, previous=False):
        """Accumulate logical intent and discover its latest target off-thread."""
        image_object = self.get_selected_object()
        if image_object is None:
            return

        CanvasPanel._note_user_interaction(self)
        _cancel_animation_decode(self, image_object)
        _cancel_animation_playback(self, image_object)
        image_object.cancel_animation_intent()

        step = -1 if previous else 1
        base_path, steps, request_id = image_object.advance_navigation_intent(step)
        if steps == 0:
            _cancel_navigation_decode(self, image_object)
            image_object.reset_navigation_intent()
            image_object.clear_status_overlay()
            _reschedule_status_timer(self)
            self.Refresh()
            return

        context = (
            image_object, image_object._work_generation, base_path, request_id)
        image_object.set_status_overlay(
            "Finding images...", 'processing', operation="navigation")
        _reschedule_status_timer(self)
        self.Refresh()
        if not self.file_navigator.request_navigation(
                base_path, steps, context,
                self._on_navigation_discovered):
            image_object.reset_navigation_intent()
            image_object.set_status_overlay(
                "Navigation request was rejected. Scroll to retry.", 'warning',
                operation="navigation")
            _reschedule_status_timer(self)
            self.Refresh()

    def _on_navigation_discovered(self, result):
        """Schedule decode only for the latest still-current logical target."""
        image_object, work_generation, base_path, request_id = (
            _navigation_context_parts(result.context))
        if (not self.image_objects.contains_object(image_object)
                or not same_canvas_object(self.selected_object, image_object)
                or image_object._work_generation != work_generation
                or image_object.source_path != base_path
                or (request_id is not None
                    and image_object._navigation_request_id != request_id)):
            logging.debug("Discarded stale directory navigation result")
            return

        if result.failure is not None:
            _cancel_navigation_decode(self, image_object)
            image_object.reset_navigation_intent()
            image_object.set_status_overlay(
                result.failure.user_message(), 'warning', operation="navigation")
            _reschedule_status_timer(self)
            logging.warning("Navigation discovery failed during %s: %s",
                            result.failure.operation, result.failure.message)
            self.Refresh()
            return

        target_path = result.target_path
        if (not target_path
                or path_comparison_key(target_path) == path_comparison_key(base_path)):
            _cancel_navigation_decode(self, image_object)
            image_object.reset_navigation_intent()
            if result.wrapped and target_path:
                image_object.set_status_overlay(
                    f"Wrapped to {os.path.basename(target_path)}", 'info',
                    operation="navigation")
                self._schedule_overlay_clear(2000, image_object)
            else:
                image_object.set_status_overlay(
                    "No other supported images found", 'warning',
                    operation="navigation")
            _reschedule_status_timer(self)
            self.Refresh()
            return

        self._apply_navigation_result(
            image_object, target_path, result.wrapped, context=result.context)

    def _apply_navigation_result(self, image_object, target_path, is_wraparound,
                                 context=None):
        """Request candidate pixels without mutating the displayed object."""
        if context is None:
            _, _, request_id = image_object.advance_navigation_intent(1)
            context = (
                image_object, image_object._work_generation,
                image_object.source_path, request_id)
        image_object.set_status_overlay(
            f"Loading {os.path.basename(target_path)}...", 'processing',
            operation="navigation")
        _reschedule_status_timer(self)
        self.Refresh()
        decoded_callback = getattr(self, "_on_navigation_decoded", None)
        if decoded_callback is None:
            decoded_callback = CanvasPanel._on_navigation_decoded.__get__(self)
        accepted = self.file_navigator.request_navigation_decode(
            target_path, image_object.object_id, context,
            decoded_callback, wrapped=is_wraparound,
            apply_orientation=image_object.normalize_orientation)
        if not accepted:
            image_object.set_status_overlay(
                "Image loading request was rejected. Scroll to continue.",
                'warning', operation="navigation")
            _reschedule_status_timer(self)
            self.Refresh()
            return False

        # The successful immutable snapshot is already cached. Speculative
        # neighbors may proceed behind this foreground candidate.
        self.file_navigator.request_preloading(target_path)
        return True

    def _on_navigation_decoded(self, result):
        """Validate, fit, and transactionally publish one decoded candidate."""
        image_object, work_generation, base_path, request_id = (
            _navigation_context_parts(result.context))
        if (self.file_navigator.is_shutdown
                or not self.image_objects.contains_object(image_object)
                or not same_canvas_object(self.selected_object, image_object)
                or image_object._work_generation != work_generation
                or image_object.source_path != base_path
                or (request_id is not None
                    and image_object._navigation_request_id != request_id)):
            result.close()
            logging.debug("Released stale navigation decode result")
            return

        if result.error is not None or result.pixels is None:
            result.close()
            detail = result.error or "decoder returned no pixels"
            image_object.set_status_overlay(
                f"Couldn't load {os.path.basename(result.target_path)}: "
                f"{detail}. Scroll to continue.",
                'warning', operation="navigation")
            _reschedule_status_timer(self)
            self.Refresh()
            return

        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        geometry = fit_geometry(result.pixels.size, (canvas_w, canvas_h))
        if geometry is None:
            result.close()
            image_object.set_status_overlay(
                f"Couldn't fit {os.path.basename(result.target_path)} in the canvas. "
                "Scroll to continue.", 'warning', operation="navigation")
            _reschedule_status_timer(self)
            self.Refresh()
            return

        pixels = result.take_pixels()
        try:
            controls = getattr(self, "_animation_controls", None)
            if (controls is not None
                    and controls.target_id == str(image_object.object_id)):
                CanvasPanel._clear_animation_controls(self)
            image_object.commit_navigation_candidate(
                result.target_path, pixels, geometry, (canvas_w, canvas_h))
            gesture = getattr(self, "_viewport_gesture", None)
            if gesture is not None and gesture["object_id"] == image_object.object_id:
                CanvasPanel._cancel_viewport_gesture(self, restore=False)
        except Exception as exc:
            pixels.close()
            logging.exception("Failed to publish navigation candidate")
            image_object.set_status_overlay(
                f"Navigation failed: {exc}. Scroll to continue.", 'warning',
                operation="navigation")
            _reschedule_status_timer(self)
            self.Refresh()
            return

        self._reset_zoom_wheel_remainder(image_object)
        if result.wrapped:
            image_object.set_status_overlay(
                f"Wrapped to {os.path.basename(result.target_path)}", 'info',
                operation="navigation")
            self._schedule_overlay_clear(2000, image_object)
        else:
            _reschedule_status_timer(self)
        logging.debug("Navigated to: %s (selected object at %s, %s)",
                      result.target_path, image_object.x, image_object.y)
        self.debug_image_objects()
        self.Refresh()

    def _clear_selected_object_overlay(self):
        """Clear the status overlay from the selected object."""
        logging.debug(f"_clear_selected_object_overlay: selected_object={self.selected_object}, id={id(self.selected_object) if self.selected_object else None}")
        if self.selected_object and self.selected_object.show_status_overlay:
            logging.debug(f"Clearing overlay: '{self.selected_object.status_message}' from object id={id(self.selected_object)}")
            self.selected_object.clear_status_overlay()
            _reschedule_status_timer(self)
            self.Refresh()
            logging.debug("Overlay cleared and canvas refreshed")
        else:
            if not self.selected_object:
                logging.debug("No selected object to clear overlay from")
            else:
                logging.debug(f"Selected object has no overlay: show_status_overlay={self.selected_object.show_status_overlay}")

    def on_overlay_timer(self, event):
        """Expire current object statuses/cards and wake for the next deadline."""
        now = getattr(self, "_monotonic", time.monotonic)()
        cleared_any = False
        for obj in tuple(self.image_objects):
            if obj.expire_status_if_due(now):
                logging.debug(
                    "Timer clearing expired overlay from object id=%s", id(obj))
                cleared_any = True

        for kind in ("drop", "scene", "export", "save"):
            operation = getattr(self, f"{kind}_operation", None)
            if CanvasPanel._expire_operation_card(self, kind, operation, now):
                logging.debug("Timer retiring expired %s success card", kind)
                cleared_any = True

        # Older headless cache-test doubles called this handler without the
        # timer/deadline contract. Keep that compatibility path isolated from
        # real CanvasPanel instances, where every timed status has a deadline.
        if (not hasattr(self, "_reschedule_overlay_timer")
                and not hasattr(self, "overlay_clear_timer")):
            for obj in tuple(self.image_objects):
                if (obj.show_status_overlay and obj.status_type != 'processing'
                        and obj.status_deadline is None):
                    obj.clear_status_overlay()
                    cleared_any = True

        if cleared_any:
            self.Refresh()
        _reschedule_status_timer(self, now=now)

    def _get_overlay_timeout_ms(self):
        """Return a safe configured timeout without making settings global."""
        getter = getattr(self.settings_manager, "get_overlay_timeout_ms", None)
        if getter is not None:
            return getter()
        raw = self.settings_manager.get_setting("UI", "overlay_timeout_ms", "1500")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return OVERLAY_TIMEOUT_DEFAULT_MS
        if not OVERLAY_TIMEOUT_MIN_MS <= value <= OVERLAY_TIMEOUT_MAX_MS:
            return OVERLAY_TIMEOUT_DEFAULT_MS
        return value

    def _schedule_overlay_clear(self, delay_ms=None, image_object=None):
        """Assign one target's deadline and schedule the shared timer."""
        if image_object is None:
            image_object = self.get_selected_object()
        if image_object is not None and self.image_objects.contains_object(image_object):
            if delay_ms is None:
                delay_ms = self._get_overlay_timeout_ms()
            else:
                try:
                    delay_ms = int(delay_ms)
                except (TypeError, ValueError):
                    delay_ms = self._get_overlay_timeout_ms()
                if delay_ms <= 0:
                    delay_ms = self._get_overlay_timeout_ms()
            image_object.set_status_deadline(
                self._monotonic() + delay_ms / 1000.0,
                revision=image_object.status_revision)
        _reschedule_status_timer(self)

    def _stop_overlay_timer(self):
        if self.overlay_clear_timer.IsRunning():
            self.overlay_clear_timer.Stop()

    def _reschedule_overlay_timer(self, now=None):
        """Wake the one canvas timer at the earliest current deadline."""
        if now is None:
            now = self._monotonic()
        deadlines = [
            obj.status_deadline for obj in self.image_objects
            if (obj.show_status_overlay and obj.status_type != 'processing'
                and obj.status_deadline is not None)
        ]
        deadlines.extend(
            operation.card_deadline
            for kind in ("drop", "scene", "export", "save")
            for operation in (getattr(self, f"{kind}_operation", None),)
            if (operation is not None
                and CanvasPanel._is_routine_success_card(kind, operation)
                and operation.card_deadline is not None)
        )
        self._stop_overlay_timer()
        if not deadlines:
            return
        delay_ms = max(1, math.ceil((min(deadlines) - now) * 1000))
        self.overlay_clear_timer.Start(delay_ms, wx.TIMER_ONE_SHOT)

    def _clear_overlays_on_interaction(self):
        """Dismiss ordinary feedback for the selected object only."""
        image_object = self.get_selected_object()
        if (image_object is not None and image_object.show_status_overlay
                and image_object.status_type != 'processing'):
            image_object.clear_status_overlay()
            _reschedule_status_timer(self)
            self.Refresh()

    def debug_image_objects(self):
        """Debug method to log the state of all image objects."""
        logging.debug(f"Canvas has {len(self.image_objects)} image objects:")
        for i, obj in enumerate(self.image_objects):
            logging.debug(f"  Object {i}: {obj.source_path} at ({obj.x}, {obj.y}) size ({obj.width}x{obj.height})")
            logging.debug(f"    Selected: {same_canvas_object(obj, self.selected_object)}")
            logging.debug(f"    Has original: {obj._original_image is not None}")
            logging.debug(f"    Has prepared bitmap: {obj._prepared_bitmap is not None}")

    def _reset_navigated_image_properties(self, image_object):
        """Reset image properties as if it was freshly dropped on canvas."""
        if not image_object:
            return

        # Set canvas size for the object
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        image_object.set_canvas_size(canvas_w, canvas_h)

        # A single fit sets scale, frame, crop, and placement together. Do not
        # reset zoom afterward: that would undo fitting and overwrite feedback.
        return image_object.fit_to_bounds(canvas_w, canvas_h)

    def _ensure_image_within_canvas(self, image_object):
        """Ensure the image object stays within canvas boundaries."""
        if not image_object:
            return

        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)

        # Adjust position if image goes outside canvas
        if image_object.x + image_object.width > canvas_w:
            image_object.x = max(0, canvas_w - image_object.width)
        if image_object.y + image_object.height > canvas_h:
            image_object.y = max(0, canvas_h - image_object.height)

        # Ensure top-left corner isn't negative
        image_object.x = max(0, image_object.x)
        image_object.y = max(0, image_object.y)

    def start_preloading_for_object(self, image_object):
        """Start preloading images for the given image object."""
        if image_object and image_object.source_path:
            self.file_navigator.request_preloading(image_object.source_path)

    def on_settings_changed(self):
        """Handle settings changes - refresh caches and restart preloading."""
        CanvasPanel._cancel_viewport_gesture(self)
        CanvasPanel._clear_animation_controls(self)
        CanvasPanel.cancel_scene_operation(
            self, reason="settings changed while the scene was loading")
        CanvasPanel.cancel_export_operation(
            self, reason="settings changed while exporting")
        _cancel_all_duplication(self, reason="settings changed")
        # Clearing the navigator invalidates any outstanding directory result;
        # retire its object-scoped processing feedback as well. Ordinary
        # warnings/info on peer objects remain untouched.
        for obj in tuple(self.image_objects):
            _cancel_object_work(self, obj)

        # Drop entries retain only paths/status while navigator ownership is
        # retired, then are resubmitted into the fresh generation below.
        CanvasPanel._retire_drop_requests_for_clear(self)

        # Clear caches to pick up new sort settings
        self.file_navigator.clear_cache()
        CanvasPanel._pump_drop_operation(self)
        _reschedule_status_timer(self)

        # Restart preloading for selected object if applicable
        if self.selected_object:
            self.start_preloading_for_object(self.selected_object)

    def shutdown_preloading(self):
        """Initiate nonblocking terminal shutdown of navigator-owned work."""
        CanvasPanel._cancel_viewport_gesture(self, restore=False)
        self._stop_overlay_timer()
        CanvasPanel._stop_playback_timer(self)
        CanvasPanel._clear_animation_controls(self)
        _cancel_all_duplication(self, reason="window closed")
        CanvasPanel.cancel_drop_operation(
            self, clear=True, reason="window closed")
        CanvasPanel.cancel_scene_operation(
            self, clear=True, reason="window closed")
        CanvasPanel.cancel_export_operation(
            self, clear=True, reason="window closed")
        CanvasPanel.cancel_save_operation(
            self, clear=True, reason="window closed")
        for obj in tuple(self.image_objects):
            _cancel_object_work(self, obj)
            obj.clear_status_overlay()
        return self.file_navigator.shutdown()

    def _capture_export_snapshot(self):
        """Capture primitive geometry and exact leased pixels on the GUI thread."""
        CanvasPanel._cancel_viewport_gesture(self)
        width, height = CanvasPanel.get_client_dimensions(self)
        if width <= 0 or height <= 0:
            raise ValueError("The canvas must have positive dimensions to export.")
        records = []
        try:
            for obj in tuple(self.image_objects):
                records.append(ExportObjectSnapshot(
                    str(obj.object_id), os.fspath(obj.source_path),
                    int(obj._source_revision), obj.x, obj.y,
                    obj.width, obj.height, obj.zoom_factor,
                    tuple(obj.viewport_offset), obj.lease_source_pixels()))
            return ExportSnapshot(
                int(width), int(height), self.canvas_bg or "#FFFFFF",
                tuple(records))
        except Exception:
            for record in records:
                if record.pixel_lease is not None:
                    record.pixel_lease.release()
            raise

    def begin_export(self, path, format_name):
        """Accept one export snapshot and return before rendering or I/O."""
        current = getattr(self, "export_operation", None)
        if current is not None and not current.terminal:
            raise RuntimeError("An export is already running.")
        if current is not None:
            CanvasPanel._retire_export_card(self)
            _reschedule_status_timer(self)
        if self.file_navigator.is_shutdown:
            raise RuntimeError("The image work service has stopped.")
        destination = resolve_export_path(path, format_name)
        snapshot = CanvasPanel._capture_export_snapshot(self)
        self._export_generation = getattr(self, "_export_generation", 0) + 1
        generation = self._export_generation
        request_key = ("export", generation)
        cancellation = ExportCancellation()
        document_identity, naming_request = CanvasPanel._capture_naming_request(self)
        operation = ExportOperation(
            generation, destination, str(format_name).upper(), snapshot.width,
            snapshot.height, len(snapshot.objects), cancellation, request_key,
            document_identity, naming_request)
        self.export_operation = operation

        def dispatch_progress(progress):
            self.file_navigator._result_dispatch(
                CanvasPanel._on_export_progress.__get__(self),
                (generation, progress))

        task_factory = getattr(self, "_export_task_factory", ExportTask)
        task = task_factory(
            snapshot, destination, operation.format_name, cancellation,
            dispatch_progress)
        def callback(result):
            return CanvasPanel._on_export_finished(self, (generation, result))
        self.Refresh()
        if not self.file_navigator.request_export(task, request_key, callback):
            task.cancel_before_start()
            self.export_operation = None
            raise RuntimeError("The export could not be queued.")
        return True

    def _on_export_progress(self, payload):
        generation, progress = payload
        operation = getattr(self, "export_operation", None)
        if (operation is None or operation.generation != generation
                or operation.terminal):
            return False
        operation.stage = progress.stage
        if progress.stage == "rendering":
            operation.rendered = progress.rendered
            operation.visible = progress.visible
            operation.clipped = progress.clipped
            operation.outside = progress.outside
        operation.touch()
        self.Refresh()
        return True

    def _on_export_finished(self, payload):
        generation, result = payload
        operation = getattr(self, "export_operation", None)
        if (operation is None or operation.generation != generation
                or operation.destination != result.destination):
            return False
        operation.rendered = result.rendered
        operation.visible = result.visible
        operation.clipped = result.clipped
        operation.outside = result.outside
        operation.failures = result.failures
        operation.error = result.error
        operation.committed = result.committed
        operation.stage = result.status
        operation.terminal = True
        if result.status == "success" and result.committed:
            CanvasPanel._record_naming_success(self,
                operation.document_identity, operation.naming_request,
                filename_base(operation.destination))
        operation.touch()
        self.Refresh()
        return True

    def cancel_export_operation(self, *, clear=False, reason="canceled by user"):
        """Retire only export ownership and never report cancel after commit."""
        operation = getattr(self, "export_operation", None)
        if operation is None:
            return False
        if not operation.terminal:
            if operation.cancellation.cancel():
                cancel = getattr(self.file_navigator, "cancel_export_work", None)
                if cancel is not None:
                    cancel(operation.request_key)
                operation.stage = "canceled"
                operation.error = reason
                operation.terminal = True
            else:
                operation.stage = "success"
                operation.committed = True
                operation.terminal = True
            operation.touch()
        if clear:
            self.export_operation = None
            self._export_card_rect = None
            self._export_cancel_rect = None
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def _handle_export_card_click(self, x, y):
        operation = getattr(self, "export_operation", None)
        if operation is None:
            return False
        if (not operation.terminal
                and self._point_in_rect(x, y, self._export_cancel_rect)):
            self.cancel_export_operation()
            return True
        if (operation.terminal
                and self._point_in_rect(x, y, self._export_card_rect)):
            CanvasPanel._retire_export_card(self)
            _reschedule_status_timer(self)
            self.Refresh()
            return True
        return False

    def _export_status_lines(self, operation):
        name = os.path.basename(operation.destination) or operation.destination
        dimensions = f"{operation.width} x {operation.height}  |  {name}"
        clipping = (f"{operation.clipped} clipped  |  "
                    f"{operation.outside} outside")
        if operation.stage == "success":
            return ["Export complete", dimensions, clipping, "Click to dismiss"]
        if operation.stage == "canceled":
            return ["Export canceled", dimensions, clipping, "Click to dismiss"]
        if operation.stage == "failed":
            lines = ["Export failed", dimensions, clipping]
            for failure in operation.failures[:3]:
                source = os.path.basename(failure.source_path) or failure.source_path
                lines.append(
                    f"{source} [{failure.object_id}]: {failure.reason}"[:110])
            if operation.error and not operation.failures:
                lines.append(operation.error.replace("\n", " ")[:110])
            lines.append("Click to dismiss")
            return lines
        if operation.stage == "preparing":
            detail = "Preparing snapshot..."
        elif operation.stage == "rendering":
            detail = f"{operation.rendered}/{operation.total} rendered"
        elif operation.stage == "encoding":
            detail = "Encoding image..."
        else:
            detail = "Writing image..."
        return ["Exporting canvas", detail, dimensions, "Cancel"]

    def export_to_file(self, path):
        """Compatibility-only synchronous export through the shared pipeline."""
        extension = os.path.splitext(os.fspath(path))[1].lower()
        format_name = next((name for name, (_, accepted) in EXPORT_FORMATS.items()
                            if extension in accepted), "PNG")
        destination = resolve_export_path(path, format_name)
        naming_identity, naming_request = CanvasPanel._capture_naming_request(self)
        for obj in self.image_objects:
            obj.load_image()
        snapshot = CanvasPanel._capture_export_snapshot(self)
        result = ExportTask(
            snapshot, destination, format_name, ExportCancellation()).run()
        if result.status != "success":
            detail = result.error or "export failed"
            if result.failures:
                first = result.failures[0]
                detail = f"{first.source_path} [{first.object_id}]: {first.reason}"
            raise OSError(detail)
        CanvasPanel._record_naming_success(self,
            naming_identity, naming_request, filename_base(destination))
        return result

    def _capture_canvas_save_snapshot(self, include_file_identification=None):
        """Capture committed JSON primitives, source identities and GIF frames."""
        CanvasPanel._cancel_viewport_gesture(self)
        if include_file_identification is None:
            getter = getattr(
                self.settings_manager, "get_include_file_identification", None)
            include_file_identification = True if getter is None else getter()
        objects = []
        for obj in tuple(self.image_objects):
            descriptor = getattr(obj, "animation", None)
            frame_index = (descriptor.displayed_index
                           if descriptor is not None and descriptor.frame_count > 1
                           else None)
            identity = getattr(obj, "source_identity", None)
            if identity is None and descriptor is not None:
                identity = descriptor.source_identity
            objects.append(CanvasSaveObjectSnapshot(
                os.fspath(obj.source_path),
                None if identity is None else tuple(identity),
                obj.x, obj.y, obj.width, obj.height, obj.zoom_factor,
                tuple(obj.viewport_offset), bool(obj.normalize_orientation),
                frame_index))
        return CanvasSaveSnapshot(
            tuple(objects), bool(include_file_identification))

    def begin_save_canvas_state(self, path):
        """Accept one immutable save snapshot and return before hashing or I/O."""
        if self.file_navigator.is_shutdown:
            raise RuntimeError("The image work service has stopped.")
        current = getattr(self, "save_operation", None)
        if current is not None and not current.terminal:
            CanvasPanel.cancel_save_operation(
                self, clear=True, reason="superseded by a newer save")
        elif current is not None:
            CanvasPanel._retire_save_card(self)
        destination = os.path.abspath(os.fspath(path))
        snapshot = CanvasPanel._capture_canvas_save_snapshot(self)
        self._save_generation = getattr(self, "_save_generation", 0) + 1
        generation = self._save_generation
        request_key = ("save", generation)
        cancellation = CanvasSaveCancellation()
        document_identity, naming_request = CanvasPanel._capture_naming_request(self)
        total = (len({path_comparison_key(item.source_path)
                      for item in snapshot.objects})
                 if snapshot.include_file_identification else 0)
        operation = SaveOperation(
            generation, destination, total, cancellation, request_key,
            document_identity, naming_request,
            snapshot.include_file_identification,
            stage="hashing" if snapshot.include_file_identification else "writing")
        self.save_operation = operation

        def dispatch_progress(progress):
            self.file_navigator._result_dispatch(
                CanvasPanel._on_save_progress.__get__(self),
                (generation, progress))

        task_factory = getattr(self, "_canvas_save_task_factory", CanvasSaveTask)
        task = task_factory(
            snapshot, destination, cancellation, dispatch_progress)

        def callback(result):
            return CanvasPanel._on_save_finished(self, (generation, result))

        self.Refresh()
        if not self.file_navigator.request_canvas_save(task, request_key, callback):
            task.cancel_before_start()
            self.save_operation = None
            raise RuntimeError("The canvas save could not be queued.")
        return True

    def _on_save_progress(self, payload):
        generation, progress = payload
        operation = getattr(self, "save_operation", None)
        if (operation is None or operation.generation != generation
                or operation.terminal):
            return False
        operation.stage = progress.stage
        operation.completed = progress.completed
        operation.total = progress.total
        operation.touch()
        self.Refresh()
        return True

    def _on_save_finished(self, payload):
        generation, result = payload
        operation = getattr(self, "save_operation", None)
        if (operation is None or operation.generation != generation
                or operation.destination != result.destination
                or operation.document_identity !=
                getattr(self, "_document_identity", 0)):
            return False
        operation.completed = result.completed
        operation.total = result.total
        operation.warnings = result.warnings
        operation.error = result.error
        operation.committed = result.committed
        operation.stage = result.status
        operation.terminal = True
        if result.status == "success" and result.committed:
            CanvasPanel._record_naming_success(
                self, operation.document_identity, operation.naming_request,
                filename_base(operation.destination))
        operation.touch()
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def cancel_save_operation(self, *, clear=False, reason="canceled by user"):
        """Cancel current save ownership without claiming cancel after commit."""
        operation = getattr(self, "save_operation", None)
        if operation is None:
            return False
        if not operation.terminal:
            if operation.cancellation.cancel():
                cancel = getattr(
                    self.file_navigator, "cancel_canvas_save_work", None)
                if cancel is not None:
                    cancel(operation.request_key)
                operation.stage = "canceled"
                operation.error = reason
                operation.terminal = True
            else:
                operation.stage = "success"
                operation.committed = True
                operation.terminal = True
            operation.touch()
        if clear:
            CanvasPanel._retire_save_card(self)
        _reschedule_status_timer(self)
        self.Refresh()
        return True

    def _save_status_lines(self, operation):
        name = os.path.basename(operation.destination) or operation.destination
        if operation.stage == "success":
            if operation.warnings:
                lines = ["Canvas saved with fingerprint warnings", name]
                for warning in operation.warnings[:3]:
                    source = os.path.basename(warning.source_path) or warning.source_path
                    lines.append(f"{source}: {warning.reason}"[:110])
                if len(operation.warnings) > 3:
                    lines.append(f"{len(operation.warnings) - 3} more source files omitted")
                lines.append("Click to dismiss")
                return lines
            detail = (f"{operation.completed}/{operation.total} source files identified"
                      if operation.include_file_identification
                      else "File identification metadata disabled")
            return ["Canvas saved", name, detail, "Click to dismiss"]
        if operation.stage == "canceled":
            return ["Canvas save canceled", name,
                    operation.error or "canceled", "Click to dismiss"]
        if operation.stage == "failed":
            detail = (operation.error or "unknown error").replace("\n", " ")[:110]
            return ["Canvas save failed", name, detail, "Click to dismiss"]
        if operation.stage == "hashing":
            detail = f"{operation.completed}/{operation.total} source files checked"
        else:
            detail = "Writing saved canvas..."
        return ["Saving canvas", name, detail, "Cancel"]

    def save_canvas_state(self, path):
        """Compatibility-only synchronous save for direct non-GUI callers."""
        snapshot = CanvasPanel._capture_canvas_save_snapshot(
            self, include_file_identification=False)
        naming_identity, naming_request = CanvasPanel._capture_naming_request(self)
        write_state(path, [item.record() for item in snapshot.objects])
        CanvasPanel._record_naming_success(self,
            naming_identity, naming_request, filename_base(path))

    def load_canvas_state(self, path):
        CanvasPanel._cancel_viewport_gesture(self)
        """Compatibility-only synchronous state replacement for direct callers."""
        CanvasPanel.cancel_save_operation(
            self, clear=True, reason="document replacement started")
        data = read_state(path)
        loaded_objects = ImageObjectList()
        canvas_w, canvas_h = CanvasPanel.get_client_dimensions(self)
        has_valid_bounds = canvas_w > 0 and canvas_h > 0
        try:
            for item in data:
                obj = ImageObject(
                    item["source_path"],
                    canvas_width=canvas_w if has_valid_bounds else None,
                    canvas_height=canvas_h if has_valid_bounds else None,
                    normalize_orientation=(item.get("source_pixel_normalization") ==
                                           SOURCE_PIXEL_NORMALIZATION_VERSION),
                )
                animation = item.get("animation")
                if animation is None:
                    obj.x = item["x"]
                    obj.y = item["y"]
                    obj.width = item["width"]
                    obj.height = item["height"]
                    obj.zoom_factor = item["zoom_factor"]
                    obj.viewport_offset = tuple(item["viewport_offset"])
                else:
                    pixels = load_animation_frame(
                        item["source_path"], animation["frame_index"])
                    try:
                        obj.commit_scene_candidate(
                            pixels, item,
                            (canvas_w, canvas_h) if has_valid_bounds else None)
                    except Exception:
                        pixels.close()
                        raise
                loaded_objects.append(obj)
        except Exception:
            for obj in loaded_objects:
                obj.dispose_source_pixels()
            raise

        CanvasPanel.cancel_drop_operation(
            self, clear=True, reason="canvas state replaced")
        CanvasPanel.cancel_scene_operation(
            self, clear=True, reason="canvas state replaced")
        _cancel_all_duplication(self, reason="canvas state replaced")
        for obj in self.image_objects:
            _cancel_object_work(self, obj)
            obj.clear_status_overlay()
            obj.dispose_source_pixels()
        CanvasPanel._clear_animation_controls(self)
        self.image_objects = loaded_objects
        self.selected_object = None
        self._zoom_wheel_remainders = {}
        self.marked_object = None
        self.drag_offset = None
        self._document_identity = getattr(self, "_document_identity", 0) + 1
        self._naming_request = 0
        self._last_naming_success = 0
        self._suggested_base = filename_base(path)
        _reschedule_status_timer(self)
        self.Refresh()


class FileDropTarget(wx.FileDropTarget):
    """Custom drop target for image files."""
    def __init__(self, canvas_panel):
        super().__init__()
        self.canvas_panel = canvas_panel

    def OnDropFiles(self, x, y, filenames):
        return self.canvas_panel.accept_drop(x, y, filenames)
