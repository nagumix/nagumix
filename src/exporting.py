"""Snapshot rendering and atomic image export without wx dependencies."""

from dataclasses import dataclass, field
import logging
import os
import tempfile
import threading

from PIL import Image

from .image_geometry import render_pil_crop


EXPORT_FORMATS = {
    "PNG": (".png", frozenset((".png",))),
    "JPEG": (".jpg", frozenset((".jpg", ".jpeg"))),
    "WEBP": (".webp", frozenset((".webp",))),
    "BMP": (".bmp", frozenset((".bmp",))),
}


class ExportCanceled(Exception):
    """Internal control flow for cancellation before the commit boundary."""


class ExportCancellation:
    """Coordinate cancellation with the single atomic replacement boundary."""

    def __init__(self):
        self._lock = threading.Lock()
        self._canceled = False
        self._committed = False

    @property
    def canceled(self):
        with self._lock:
            return self._canceled

    @property
    def committed(self):
        with self._lock:
            return self._committed

    def cancel(self):
        with self._lock:
            if self._committed:
                return False
            self._canceled = True
            return True

    def check(self):
        if self.canceled:
            raise ExportCanceled("canceled by user")

    def replace(self, temporary_path, destination, replace_file=None):
        """Check cancellation and commit while holding only the commit lock."""
        replace_file = replace_file or os.replace
        with self._lock:
            if self._canceled:
                raise ExportCanceled("canceled before destination replacement")
            replace_file(temporary_path, destination)
            self._committed = True


@dataclass(frozen=True)
class ExportObjectSnapshot:
    object_id: str
    source_path: str
    source_revision: int
    x: int
    y: int
    width: int
    height: int
    zoom_factor: float
    viewport_offset: tuple
    pixel_lease: object = field(repr=False, compare=False)

    @property
    def pixels(self):
        return self.pixel_lease.pixels if self.pixel_lease is not None else None


@dataclass
class ExportSnapshot:
    width: int
    height: int
    background: object
    objects: tuple
    _released: bool = field(default=False, init=False, repr=False)
    _release_lock: object = field(default_factory=threading.Lock, init=False,
                                  repr=False)

    def release(self):
        """Release every pixel lease exactly once."""
        with self._release_lock:
            if self._released:
                return False
            self._released = True
        for record in self.objects:
            if record.pixel_lease is not None:
                record.pixel_lease.release()
        return True


@dataclass(frozen=True)
class ExportProgress:
    stage: str
    rendered: int
    total: int
    visible: int = 0
    clipped: int = 0
    outside: int = 0


@dataclass(frozen=True)
class ExportFailure:
    object_id: str
    source_path: str
    reason: str


@dataclass(frozen=True)
class ExportResult:
    destination: str
    width: int
    height: int
    format_name: str
    status: str
    rendered: int
    total: int
    visible: int = 0
    clipped: int = 0
    outside: int = 0
    failures: tuple = ()
    error: str = None
    committed: bool = False


def resolve_export_path(path, format_name):
    """Return a path matching the explicitly selected save-dialog format."""
    selected = str(format_name).upper()
    try:
        canonical, accepted = EXPORT_FORMATS[selected]
    except KeyError as exc:
        raise ValueError(f"Unsupported export format: {format_name}") from exc
    destination = os.fspath(path)
    root, extension = os.path.splitext(destination)
    if not extension:
        return destination + canonical
    if extension.lower() not in accepted:
        expected = "/".join(sorted(accepted))
        raise ValueError(
            f"The filename extension '{extension}' does not match the selected "
            f"{selected} format ({expected}). Correct the filename or format.")
    return destination


def _placement_kind(record, rendered_size, canvas_size):
    left, top = record.x, record.y
    right = left + rendered_size[0]
    bottom = top + rendered_size[1]
    canvas_w, canvas_h = canvas_size
    intersection_w = min(right, canvas_w) - max(left, 0)
    intersection_h = min(bottom, canvas_h) - max(top, 0)
    if intersection_w <= 0 or intersection_h <= 0:
        return "outside"
    if left < 0 or top < 0 or right > canvas_w or bottom > canvas_h:
        return "clipped"
    return "visible"


def render_export_snapshot(snapshot, cancellation, progress_callback=None):
    """Render one immutable viewport snapshot with shared production geometry."""
    cancellation.check()
    composite = Image.new(
        "RGBA", (snapshot.width, snapshot.height), snapshot.background or "#FFFFFF")
    rendered = visible = clipped = outside = 0
    failures = []
    total = len(snapshot.objects)
    try:
        for record in snapshot.objects:
            cancellation.check()
            crop = None
            try:
                crop = render_pil_crop(
                    record.pixels,
                    record.zoom_factor,
                    (record.width, record.height),
                    record.viewport_offset,
                )
                if crop.mode != "RGBA":
                    converted = crop.convert("RGBA")
                    crop.close()
                    crop = converted
                placement = _placement_kind(
                    record, crop.size, (snapshot.width, snapshot.height))
                if placement == "visible":
                    visible += 1
                elif placement == "clipped":
                    clipped += 1
                else:
                    outside += 1
                composite.alpha_composite(crop, dest=(record.x, record.y))
                rendered += 1
            except ExportCanceled:
                raise
            except Exception as exc:
                failures.append(ExportFailure(
                    record.object_id, record.source_path, str(exc)))
            finally:
                if crop is not None:
                    crop.close()
            if progress_callback is not None:
                progress_callback(ExportProgress(
                    "rendering", rendered, total, visible, clipped, outside))

        if failures:
            return composite, ExportResult(
                "", snapshot.width, snapshot.height, "", "failed",
                rendered, total, visible, clipped, outside, tuple(failures),
                "one or more canvas objects could not be rendered")
        return composite, ExportResult(
            "", snapshot.width, snapshot.height, "", "rendered",
            rendered, total, visible, clipped, outside)
    except BaseException:
        composite.close()
        raise


def write_export_image(image, destination, format_name, cancellation,
                       progress_callback=None, *, replace_file=None):
    """Encode, sync, and atomically replace a destination from a sibling file."""
    directory = os.path.dirname(os.path.abspath(destination))
    basename = os.path.basename(destination)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{basename}.", suffix=".tmp", dir=directory)
    os.close(descriptor)
    committed = False
    try:
        cancellation.check()
        if progress_callback is not None:
            progress_callback(ExportProgress("encoding", 0, 0))
        encoded = image
        if format_name in ("JPEG", "BMP"):
            encoded = image.convert("RGB")
        try:
            encoded.save(temporary_path, format=format_name)
        finally:
            if encoded is not image:
                encoded.close()

        cancellation.check()
        if progress_callback is not None:
            progress_callback(ExportProgress("writing", 0, 0))
        with open(temporary_path, "rb+") as output:
            output.flush()
            os.fsync(output.fileno())
        cancellation.replace(temporary_path, destination, replace_file)
        committed = True
    finally:
        if not committed:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


class ExportTask:
    """One scheduler-owned export operation and its leased snapshot."""

    def __init__(self, snapshot, destination, format_name, cancellation,
                 progress_callback=None, *, renderer=render_export_snapshot,
                 writer=write_export_image):
        self.snapshot = snapshot
        self.destination = destination
        self.format_name = format_name
        self.cancellation = cancellation
        self.progress_callback = progress_callback
        self.renderer = renderer
        self.writer = writer
        self._started = False
        self._start_lock = threading.Lock()

    def _report_progress(self, progress):
        if not self.cancellation.canceled and self.progress_callback is not None:
            try:
                self.progress_callback(progress)
            except Exception:
                logging.exception("Failed to dispatch export progress")

    def cancel_before_start(self):
        self.cancellation.cancel()
        with self._start_lock:
            if self._started:
                return False
            self._started = True
        self.snapshot.release()
        return True

    def run(self):
        with self._start_lock:
            if self._started:
                return ExportResult(
                    self.destination, self.snapshot.width, self.snapshot.height,
                    self.format_name, "canceled", 0, len(self.snapshot.objects),
                    error="canceled before work started")
            self._started = True

        composite = None
        partial = ExportResult(
            self.destination, self.snapshot.width, self.snapshot.height,
            self.format_name, "failed", 0, len(self.snapshot.objects))
        try:
            composite, partial = self.renderer(
                self.snapshot, self.cancellation, self._report_progress)
            if partial.failures:
                return ExportResult(
                    self.destination, partial.width, partial.height,
                    self.format_name, "failed", partial.rendered, partial.total,
                    partial.visible, partial.clipped, partial.outside,
                    partial.failures, partial.error)
            self.cancellation.check()
            self.writer(
                composite, self.destination, self.format_name,
                self.cancellation, self._report_progress)
            return ExportResult(
                self.destination, partial.width, partial.height,
                self.format_name, "success", partial.rendered, partial.total,
                partial.visible, partial.clipped, partial.outside,
                committed=True)
        except ExportCanceled as exc:
            return ExportResult(
                self.destination, self.snapshot.width, self.snapshot.height,
                self.format_name,
                "success" if self.cancellation.committed else "canceled",
                partial.rendered, partial.total, partial.visible,
                partial.clipped, partial.outside, error=str(exc),
                committed=self.cancellation.committed)
        except Exception as exc:
            return ExportResult(
                self.destination, self.snapshot.width, self.snapshot.height,
                self.format_name, "failed", partial.rendered, partial.total,
                partial.visible, partial.clipped, partial.outside,
                partial.failures, str(exc), committed=self.cancellation.committed)
        finally:
            if composite is not None:
                composite.close()
            self.snapshot.release()
