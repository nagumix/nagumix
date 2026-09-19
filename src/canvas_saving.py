"""Bounded source fingerprinting and atomic saved-canvas publication."""

from dataclasses import dataclass
import hashlib
import logging
import os
import threading

from .canvas_state import write_state
from .image_pixels import SOURCE_PIXEL_NORMALIZATION_VERSION, source_file_identity


HASH_CHUNK_BYTES = 1024 * 1024


class CanvasSaveCanceled(Exception):
    """Internal control flow for cancellation before atomic replacement."""


class CanvasSaveCancellation:
    """Serialize cancellation with the single destination commit boundary."""

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
            raise CanvasSaveCanceled("canceled by user")

    def replace(self, temporary_path, destination, replace_file=None):
        replace_file = replace_file or os.replace
        with self._lock:
            if self._canceled:
                raise CanvasSaveCanceled("canceled before destination replacement")
            replace_file(temporary_path, destination)
            self._committed = True


@dataclass(frozen=True)
class CanvasSaveObjectSnapshot:
    source_path: str
    source_identity: object
    x: int
    y: int
    width: int
    height: int
    zoom_factor: float
    viewport_offset: tuple
    normalize_orientation: bool
    animation_frame_index: object = None

    def record(self):
        value = {
            "source_path": self.source_path,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "zoom_factor": self.zoom_factor,
            "viewport_offset": list(self.viewport_offset),
        }
        if self.normalize_orientation:
            value["source_pixel_normalization"] = SOURCE_PIXEL_NORMALIZATION_VERSION
        if self.animation_frame_index is not None:
            value["animation"] = {
                "type": "gif",
                "frame_index": self.animation_frame_index,
                "paused": True,
            }
        return value


@dataclass(frozen=True)
class CanvasSaveSnapshot:
    objects: tuple
    include_file_identification: bool


@dataclass(frozen=True)
class CanvasSaveProgress:
    stage: str
    completed: int
    total: int


@dataclass(frozen=True)
class FingerprintWarning:
    source_path: str
    reason: str


@dataclass(frozen=True)
class CanvasSaveResult:
    destination: str
    status: str
    completed: int
    total: int
    warnings: tuple = ()
    error: str = None
    committed: bool = False


def _comparison_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def fingerprint_source(path, cancellation, *, open_file=open,
                       identity_reader=source_file_identity,
                       fstat=os.fstat, chunk_size=HASH_CHUNK_BYTES):
    """Hash one stable encoded source using bounded reads and identity checks."""
    native_path = os.fspath(path)
    cancellation.check()
    before = identity_reader(native_path)
    digest = hashlib.md5()
    byte_count = 0
    with open_file(native_path, "rb") as stream:
        opened = source_file_identity(native_path, fstat(stream.fileno()))
        if opened != before:
            raise OSError("source changed before hashing started")
        while True:
            cancellation.check()
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
        handle_after = source_file_identity(native_path, fstat(stream.fileno()))
    after = identity_reader(native_path)
    if before != handle_after or before != after or byte_count != before[1]:
        raise OSError("source changed while it was hashed")
    return {"size_bytes": byte_count, "md5": digest.hexdigest()}, before


class CanvasSaveTask:
    """One scheduler-owned canvas save with deduplicated best-effort hashes."""

    def __init__(self, snapshot, destination, cancellation,
                 progress_callback=None, *, fingerprinter=fingerprint_source,
                 writer=write_state):
        self.snapshot = snapshot
        self.destination = os.fspath(destination)
        self.cancellation = cancellation
        self.progress_callback = progress_callback
        self.fingerprinter = fingerprinter
        self.writer = writer
        self._started = False
        self._start_lock = threading.Lock()

    def _report(self, stage, completed, total):
        if not self.cancellation.canceled and self.progress_callback is not None:
            try:
                self.progress_callback(CanvasSaveProgress(stage, completed, total))
            except Exception:
                logging.exception("Failed to dispatch canvas-save progress")

    def cancel_before_start(self):
        self.cancellation.cancel()
        with self._start_lock:
            if self._started:
                return False
            self._started = True
        return True

    def run(self):
        with self._start_lock:
            if self._started:
                return CanvasSaveResult(
                    self.destination, "canceled", 0, 0,
                    error="canceled before work started")
            self._started = True

        groups = {}
        for index, item in enumerate(self.snapshot.objects):
            groups.setdefault(_comparison_key(item.source_path), []).append((index, item))
        total = len(groups) if self.snapshot.include_file_identification else 0
        completed = 0
        warnings = []
        fingerprints = {}
        records = [item.record() for item in self.snapshot.objects]
        try:
            if self.snapshot.include_file_identification:
                self._report("hashing", completed, total)
                for key, references in groups.items():
                    self.cancellation.check()
                    path = references[0][1].source_path
                    expected = [item.source_identity for _, item in references]
                    if all(identity is None for identity in expected):
                        warnings.append(FingerprintWarning(
                            path, "displayed source identity is unavailable"))
                    else:
                        try:
                            fingerprint, observed = self.fingerprinter(
                                path, self.cancellation)
                        except CanvasSaveCanceled:
                            raise
                        except Exception as exc:
                            warnings.append(FingerprintWarning(path, str(exc)))
                        else:
                            fingerprints[key] = (fingerprint, observed)
                    completed += 1
                    self._report("hashing", completed, total)

                warned = {(warning.source_path, warning.reason) for warning in warnings}
                for index, item in enumerate(self.snapshot.objects):
                    outcome = fingerprints.get(_comparison_key(item.source_path))
                    if outcome is None:
                        continue
                    fingerprint, observed = outcome
                    if item.source_identity is None:
                        marker = (item.source_path, "displayed source identity is unavailable")
                        if marker not in warned:
                            warnings.append(FingerprintWarning(*marker))
                            warned.add(marker)
                    elif tuple(item.source_identity) != tuple(observed):
                        marker = (item.source_path, "source changed since it was displayed")
                        if marker not in warned:
                            warnings.append(FingerprintWarning(*marker))
                            warned.add(marker)
                    else:
                        records[index]["source_file"] = dict(fingerprint)

            self.cancellation.check()
            self._report("writing", completed, total)
            self.writer(self.destination, records, cancellation=self.cancellation)
            return CanvasSaveResult(
                self.destination, "success", completed, total,
                tuple(warnings), committed=True)
        except CanvasSaveCanceled as exc:
            return CanvasSaveResult(
                self.destination,
                "success" if self.cancellation.committed else "canceled",
                completed, total, tuple(warnings), str(exc),
                self.cancellation.committed)
        except Exception as exc:
            return CanvasSaveResult(
                self.destination, "failed", completed, total,
                tuple(warnings), str(exc), self.cancellation.committed)
