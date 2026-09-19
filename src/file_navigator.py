# src/file_navigator.py
from collections import deque
from dataclasses import dataclass, field
import logging
import ntpath
import os
import re
import threading
import time
from types import MappingProxyType
from typing import Callable, List, Optional

from PIL import Image

from .canvas_state import read_state
from .image_pixels import (
    AnimationSourceChangedError,
    load_animation_frame,
    load_source_pixels,
)
from .gif_playback import (
    GifDecoderSession,
    PLAYBACK_TRANSFER_BUDGET_BYTES,
    PlaybackSession,
    PlaybackSliceOutcome,
    PlaybackSliceTask,
    PlaybackTransferBudget,
)
from .settings_manager import (
    PRELOAD_CACHE_DEFAULT_MB,
    PRELOAD_CACHE_MAX_MB,
    PRELOAD_CACHE_MIN_MB,
    SORT_METHOD_DEFAULT,
    SORT_METHODS,
)


@dataclass(eq=False)
class _PreloadJob:
    """One navigator-owned decode request."""

    job_id: int
    path: str
    generation: int
    purpose: str = "preload"
    request_key: object = None
    context: object = None
    callback: Optional[Callable] = None
    apply_orientation: bool = True
    transferred_image: Optional[Image.Image] = None
    wrapped: bool = False
    superseded: bool = False
    work_callable: Optional[Callable] = None
    state: str = "queued"
    thread: Optional[threading.Thread] = None


@dataclass(eq=False)
class _DecodedCacheEntry:
    """One cache-owned decoded payload and its deterministic LRU metadata."""

    path: str
    image: Image.Image
    byte_count: int
    access_order: int
    insertion_order: int


@dataclass(frozen=True)
class DirectoryFailure:
    """A directory discovery failure, distinct from an empty directory."""

    operation: str
    message: str
    error_code: Optional[int] = None

    @classmethod
    def from_exception(cls, operation, exc):
        code = getattr(exc, "winerror", None)
        if code is None:
            code = getattr(exc, "errno", None)
        return cls(operation, str(exc), code)

    def user_message(self):
        detail = self.message
        if self.error_code is not None and str(self.error_code) not in detail:
            detail = f"{detail} (error {self.error_code})"
        return f"Can't browse this folder: {detail}. Scroll to retry."


@dataclass(frozen=True)
class DirectorySnapshot:
    """One immutable, successfully enumerated directory view."""

    directory: str
    files: tuple[str, ...]
    comparison_keys: tuple[str, ...]
    source_exists: bool
    index_by_key: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "index_by_key", MappingProxyType({
            key: index for index, key in enumerate(self.comparison_keys)
        }))

    def index_for(self, path):
        return self.index_by_key.get(path_comparison_key(path), -1)


@dataclass(frozen=True)
class DirectoryDiscovery:
    """Result of directory discovery; exactly one field is populated."""

    snapshot: Optional[DirectorySnapshot] = None
    failure: Optional[DirectoryFailure] = None

    @property
    def succeeded(self):
        return self.snapshot is not None


@dataclass(frozen=True)
class NavigationResult:
    """Completed async navigation request delivered to the UI owner."""

    target_path: Optional[str]
    wrapped: bool
    context: object
    failure: Optional[DirectoryFailure] = None


@dataclass
class NavigationDecodeResult:
    """Detached candidate pixels transferred from a worker to the UI owner."""

    target_path: str
    wrapped: bool
    context: object
    pixels: Optional[Image.Image] = None
    error: Optional[str] = None

    def take_pixels(self):
        """Transfer candidate ownership exactly once to the consumer."""
        pixels = self.pixels
        self.pixels = None
        return pixels

    def close(self):
        """Release an unconsumed or rejected candidate deterministically."""
        pixels = self.take_pixels()
        if pixels is not None:
            pixels.close()


@dataclass
class DropDecodeResult:
    """Detached dropped-file pixels transferred from a worker to the UI."""

    target_path: str
    context: object
    pixels: Optional[Image.Image] = None
    error: Optional[str] = None

    def take_pixels(self):
        """Transfer candidate ownership exactly once to the consumer."""
        pixels = self.pixels
        self.pixels = None
        return pixels

    def close(self):
        """Release an unconsumed or rejected candidate deterministically."""
        pixels = self.take_pixels()
        if pixels is not None:
            pixels.close()


@dataclass
class SceneDocumentResult:
    """Validated scene records returned from the shared worker boundary."""

    target_path: str
    context: object
    records: Optional[list] = None
    error: Optional[str] = None

    def close(self):
        self.records = None


@dataclass
class SceneDecodeResult:
    """Detached scene-entry pixels transferred from a worker to the UI."""

    target_path: str
    context: object
    pixels: Optional[Image.Image] = None
    error: Optional[str] = None

    def take_pixels(self):
        pixels = self.pixels
        self.pixels = None
        return pixels

    def close(self):
        pixels = self.take_pixels()
        if pixels is not None:
            pixels.close()


@dataclass
class DuplicationDecodeResult:
    """Detached copied pixels transferred from a duplication worker."""

    target_path: str
    context: object
    pixels: Optional[Image.Image] = None
    error: Optional[str] = None

    def take_pixels(self):
        pixels = self.pixels
        self.pixels = None
        return pixels

    def close(self):
        pixels = self.take_pixels()
        if pixels is not None:
            pixels.close()


@dataclass
class AnimationDecodeResult:
    """One detached composited GIF frame transferred to the GUI owner."""

    target_path: str
    frame_index: int
    context: object
    pixels: Optional[Image.Image] = None
    error: Optional[str] = None
    source_refreshed: bool = False

    def take_pixels(self):
        pixels = self.pixels
        self.pixels = None
        return pixels

    def close(self):
        pixels = self.take_pixels()
        if pixels is not None:
            pixels.close()


@dataclass
class PlaybackSliceResult:
    """A finite set of byte-charged frames transferred to the GUI owner."""

    context: object
    packets: list
    error: Optional[str] = None
    blocked: bool = False
    finished: bool = False

    def close(self):
        for packet in self.packets:
            packet.close()
        self.packets.clear()


class AnimationFrameTask:
    """Cancelable wrapper around one worker-owned GIF reconstruction."""

    def __init__(self, path, frame_index, source_identity, loader):
        self.path = path
        self.frame_index = int(frame_index)
        self.source_identity = (
            None if source_identity is None else tuple(source_identity))
        self._loader = loader
        self._lock = threading.Lock()
        self._canceled = False
        self.source_refreshed = False

    def cancel(self):
        with self._lock:
            self._canceled = True
        return True

    cancel_before_start = cancel

    def run(self):
        with self._lock:
            if self._canceled:
                return None
        try:
            pixels = self._loader(
                self.path, self.frame_index,
                expected_source_identity=self.source_identity)
        except AnimationSourceChangedError as exc:
            if exc.phase != "before" or self.source_identity is None:
                raise
            with self._lock:
                if self._canceled:
                    return None
            # Retry one stable replacement without the object's stale
            # expectation. The loader still rejects changes during this
            # reconstruction; publication validates the GIF structure.
            pixels = self._loader(
                self.path, self.frame_index,
                expected_source_identity=None)
            self.source_refreshed = True
        with self._lock:
            canceled = self._canceled
        if canceled:
            pixels.close()
            return None
        return pixels


@dataclass(frozen=True)
class _DeferredAnimationRequest:
    """One replaceable exact-frame target waiting behind owned work."""

    path: str
    frame_index: int
    request_key: object
    context: object
    callback: Callable
    source_identity: tuple


@dataclass(eq=False)
class _DiscoveryJob:
    """One bounded directory read, optionally serving navigation/preload."""

    job_id: int
    current_path: str
    directory_key: str
    generation: int
    steps: int = 0
    context: object = None
    callback: Optional[Callable] = None
    preload: bool = False
    state: str = "queued"
    thread: Optional[threading.Thread] = None


def path_comparison_key(path):
    """Canonicalize only for comparison; never replace a user-facing path."""
    value = os.fspath(path)
    windows_like = value.startswith(("\\\\", "//")) or (
        len(value) >= 2 and value[1] == ":")
    if windows_like:
        value = value.replace("/", "\\")
        if value.lower().startswith("\\\\?\\unc\\"):
            value = "\\\\" + value[8:]
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.normpath(value))


def _navigation_context_key(context):
    """Identify one object's logical discovery request across intent revisions."""
    try:
        image_object, generation, base_path = context[:3]
        return image_object.object_id, generation, path_comparison_key(base_path)
    except (AttributeError, TypeError, ValueError):
        return context


class FileNavigator:
    """Handles file navigation and owns bounded discovery and preloading."""

    SUPPORTED_EXTENSIONS = {
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tif', '.tiff', '.tga',
        '.avif', '.heic', '.heif',
    }
    MAX_ACTIVE_DECODES = 3
    MAX_PENDING_PRELOADS = 10

    def __init__(self, settings_manager, *, image_loader: Optional[Callable] = None,
                 animation_loader: Optional[Callable] = None,
                 playback_decoder_factory=None,
                 playback_budget_bytes=PLAYBACK_TRANSFER_BUDGET_BYTES,
                 monotonic_clock=None,
                 state_reader: Optional[Callable] = None,
                 directory_reader: Optional[Callable] = None,
                 result_dispatch: Optional[Callable] = None):
        self.settings_manager = settings_manager
        self._image_loader = image_loader or load_source_pixels
        self._animation_loader = animation_loader or load_animation_frame
        self._playback_decoder_factory = (
            playback_decoder_factory or GifDecoderSession)
        self._monotonic = monotonic_clock or time.monotonic
        self._state_reader = state_reader or read_state
        self._directory_reader = directory_reader or self._read_directory
        self._result_dispatch = result_dispatch or (lambda callback, result: callback(result))
        self._file_cache = {}  # comparison-only directory key -> DirectorySnapshot
        self._preload_cache = {}  # path -> _DecodedCacheEntry owned by this navigator
        self._cache_leases = {}  # image identity -> active compatibility copies
        self._retired_cache_images = {}  # image identity -> retired _DecodedCacheEntry
        self._retained_cache_bytes = 0
        self._retired_cache_bytes = 0
        self._next_cache_order = 1
        self._cache_admissions = 0
        self._cache_evictions = 0
        self._cache_rejections = 0
        self._cache_oversize_rejections = 0
        self._cache_pinned_rejections = 0
        self._cache_hits = 0
        self._last_cache_admission = None

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._generation = 0
        self._shutdown = False
        self._next_job_id = 1
        self._active_discovery = None
        self._pending_discovery = None
        self._active_decodes = 0
        self._pending_jobs = deque()
        self._jobs = {}  # job_id -> queued/running job
        self._current_jobs = {}  # path -> newest job in the current generation
        self._foreground_jobs = {}  # request identity -> newest foreground job
        self._drop_jobs = {}  # distinct drop-entry identity -> owned decode job
        self._scene_jobs = {}  # scene document/entry identity -> owned job
        self._export_jobs = {}  # at most one canvas export request
        self._save_jobs = {}  # at most one current canvas-save request
        self._duplication_jobs = {}  # source object identity -> one copy job
        self._animation_jobs = {}  # object identity -> latest GIF frame job
        self._deferred_animation_requests = {}  # object identity -> latest target
        self._playback_jobs = {}  # object identity -> at most one slice
        self._playback_sessions = {}  # runtime object identity -> decoder session
        self._playback_budget = PlaybackTransferBudget(
            playback_budget_bytes, self._on_playback_capacity_released)
        self._playback_slice_order = []
        self._owned_threads = set()

    def start_gif_playback(self, *, object_id, path, source_identity,
                           playback_generation, frame_count, logical_size,
                           loop_count, displayed_index, displayed_duration_ms,
                           repeats_completed, started_at, restart, context,
                           callback):
        """Replace one object's playback session and queue its first slice."""
        with self._condition:
            if self._shutdown:
                return False
            self._cancel_playback_locked(object_id)
            session = PlaybackSession(
                object_id, path, source_identity, playback_generation,
                frame_count, logical_size, loop_count, displayed_index,
                displayed_duration_ms, repeats_completed, started_at,
                restart=restart,
                decoder_factory=self._playback_decoder_factory)
            session.context = context
            session.callback = callback
            self._playback_sessions[object_id] = session
            accepted = self._request_playback_slice_locked(session)
            if not accepted:
                self._playback_sessions.pop(object_id, None)
                session.cancel()
                session.close_decoder()
            return accepted

    def request_playback_slice(self, object_id):
        """Queue one fair finite slice when this session can accept frames."""
        with self._condition:
            session = self._playback_sessions.get(object_id)
            if session is None or self._shutdown:
                return False
            return self._request_playback_slice_locked(session)

    def _request_playback_slice_locked(self, session):
        if not session.mark_queued():
            return False
        task = PlaybackSliceTask(
            session, self._playback_budget, clock=self._monotonic)
        job = self._new_job_locked(
            session.path, purpose="playback", request_key=session.object_id,
            context=session.context, callback=session.callback,
            register_current=False, work_callable=task)
        available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
        if (not available_start
                and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS):
            evicted = next((candidate for candidate in reversed(self._pending_jobs)
                            if candidate.purpose == "preload"), None)
            if evicted is None:
                session.mark_slice_complete()
                self._cancel_queued_job_locked(job)
                return False
            self._pending_jobs.remove(evicted)
            self._cancel_queued_job_locked(evicted)

        # Playback is speculative lookahead: retain round-robin order behind
        # foreground work and ahead of ordinary preloads.
        insert_at = next(
            (index for index, candidate in enumerate(self._pending_jobs)
             if candidate.purpose == "preload"),
            len(self._pending_jobs),
        )
        self._pending_jobs.insert(insert_at, job)
        self._playback_jobs[session.object_id] = job
        self._start_available_jobs_locked()
        self._condition.notify_all()
        return job.state not in ("failed", "canceled")

    def _on_playback_capacity_released(self, _released_session):
        """Wake every globally budget-blocked session after bytes are freed."""
        with self._condition:
            if self._shutdown:
                return
            for session in tuple(self._playback_sessions.values()):
                if session.unblock_for_capacity():
                    self._request_playback_slice_locked(session)

    def _evict_speculative_job_locked(self):
        """Free one pending slot for foreground work without stopping playback."""
        victim = next((
            candidate for candidate in reversed(self._pending_jobs)
            if candidate.purpose in ("preload", "playback")
        ), None)
        if victim is None:
            return False
        self._pending_jobs.remove(victim)
        if victim.purpose != "playback":
            self._cancel_queued_job_locked(victim)
            return True

        victim.state = "deferred"
        self._jobs.pop(victim.job_id, None)
        if self._playback_jobs.get(victim.request_key) is victim:
            self._playback_jobs.pop(victim.request_key, None)
        session = victim.work_callable.session
        session.mark_deferred()
        session.close_decoder()
        return True

    def _wake_deferred_playback_locked(self):
        """Refill available scheduler capacity in stable object order."""
        for session in tuple(self._playback_sessions.values()):
            if (len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS
                    and self._active_decodes >= self.MAX_ACTIVE_DECODES):
                break
            if session.resume_deferred():
                self._request_playback_slice_locked(session)

    def cancel_gif_playback(self, object_id):
        """Invalidate one session without waiting for an executing Pillow call."""
        with self._condition:
            return self._cancel_playback_locked(object_id)

    def _cancel_playback_locked(self, object_id):
        session = self._playback_sessions.pop(object_id, None)
        job = self._playback_jobs.pop(object_id, None)
        if session is None and job is None:
            return False
        if session is not None:
            session.cancel()
        if job is not None:
            job.superseded = True
            task = job.work_callable
            if task is not None:
                task.cancel()
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
        if session is not None and (job is None or job.state != "running"):
            session.close_decoder()
        self._condition.notify_all()
        return True

    def retire_gif_playback(self, object_id, playback_generation):
        """Drop navigator ownership after completion or a handled failure."""
        with self._condition:
            session = self._playback_sessions.get(object_id)
            if (session is None
                    or session.playback_generation != playback_generation):
                return False
            return self._cancel_playback_locked(object_id)

    def retire_idle_gif_decoders(self, now):
        """Bound open compositor/file state without changing playback intent."""
        with self._condition:
            sessions = tuple(self._playback_sessions.values())
        return sum(session.retire_decoder_if_idle(now) for session in sessions)

    def _directory_for(self, file_path):
        return os.path.dirname(os.fspath(file_path))

    def _directory_key_for(self, file_path):
        return path_comparison_key(self._directory_for(file_path))

    def discover_directory(self, file_path: str) -> DirectoryDiscovery:
        """Synchronously read through the injectable discovery boundary.

        Production GUI paths use ``request_navigation`` or
        ``request_preloading`` so this potentially blocking call is not made on
        the event thread.
        """
        directory = self._directory_for(file_path)
        if not directory:
            return DirectoryDiscovery(failure=DirectoryFailure(
                "parent", "The image path has no parent directory"))

        directory_key = path_comparison_key(directory)
        with self._lock:
            lookup_generation = self._generation
            cached = self._file_cache.get(directory_key)
        if cached is not None:
            return DirectoryDiscovery(snapshot=cached)

        try:
            result = self._directory_reader(file_path)
        except OSError as exc:
            result = DirectoryDiscovery(
                failure=DirectoryFailure.from_exception("enumeration", exc))

        if result.failure is not None:
            logging.warning("Cannot discover directory %s: %s", directory, result.failure.message)
            return result

        with self._lock:
            if not self._shutdown and self._generation == lookup_generation:
                self._file_cache[directory_key] = result.snapshot
        return result

    def _read_directory(self, file_path: str) -> DirectoryDiscovery:
        """Read one directory using ordinary path APIs without path rewriting."""
        directory = self._directory_for(file_path)
        try:
            source_exists = os.path.exists(file_path)
        except OSError:
            source_exists = False

        try:
            with os.scandir(directory) as iterator:
                directory_entries = list(iterator)
        except OSError as exc:
            return DirectoryDiscovery(
                failure=DirectoryFailure.from_exception("enumeration", exc))

        entries = []
        for entry in directory_entries:
            name = entry.name
            if os.path.splitext(name)[1].lower() not in self.SUPPORTED_EXTENSIONS:
                continue
            full_path = entry.path
            try:
                # On the affected SMB provider, a separate os.stat()/isfile()
                # reports real JPEGs as directories. Windows scandir retains
                # the correct file attributes from directory enumeration.
                if not entry.is_file():
                    continue
            except OSError:
                continue
            try:
                metadata = entry.stat()
                modified = getattr(metadata, "st_mtime", None)
                size = getattr(metadata, "st_size", None)
                if not isinstance(modified, (int, float)):
                    modified = None
                if not isinstance(size, int) or size < 0:
                    size = None
            except OSError:
                modified = None
                size = None
            # Keep a file when metadata races or permissions make its stat
            # unavailable. Sorting places the unknown value last.
            entries.append((full_path, modified, size))

        sort_method = self._configured_sort_method()
        entries = self._sort_entries(entries, sort_method)
        files = []
        keys = []
        seen = set()
        for path, *_ in entries:
            key = path_comparison_key(path)
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
            keys.append(key)
        return DirectoryDiscovery(snapshot=DirectorySnapshot(
            directory, tuple(files), tuple(keys), source_exists))

    def _configured_sort_method(self):
        getter = getattr(self.settings_manager, "get_sort_method", None)
        value = (getter() if getter is not None else self.settings_manager.get_setting(
            "Navigation", "sort_method", SORT_METHOD_DEFAULT))
        value = str(value).strip().lower()
        if value not in SORT_METHODS:
            logging.warning("Unknown sort method: %s, using %s", value,
                            SORT_METHOD_DEFAULT)
            return SORT_METHOD_DEFAULT
        return value

    @staticmethod
    def _basename(path):
        value = os.fspath(path)
        windows_like = value.startswith(("\\\\", "//")) or (
            len(value) >= 2 and value[1] == ":")
        if windows_like:
            return ntpath.basename(value.replace("/", "\\"))
        return os.path.basename(value)

    @classmethod
    def _alphabetical_name_key(cls, path):
        return cls._basename(path).casefold()

    @classmethod
    def _natural_name_key(cls, path):
        """Compare filename text case-insensitively with numeric components."""
        parts = re.split(r"(\d+)", cls._alphabetical_name_key(path))
        key = []
        for part in parts:
            if part.isdigit():
                # Numeric value wins; shorter equal-value spellings put image2
                # before image02, with the original token as a final tie-break.
                key.append((1, int(part), len(part), part))
            else:
                key.append((0, part))
        return tuple(key)

    @staticmethod
    def _path_tie_key(path):
        return path_comparison_key(path)

    def _sort_by_name(self, entries, *, natural, descending=False):
        primary = self._natural_name_key if natural else self._alphabetical_name_key
        # Stable passes keep the path tie-breaker ascending in either direction.
        result = sorted(entries, key=lambda item: self._path_tie_key(item[0]))
        return sorted(result, key=lambda item: primary(item[0]), reverse=descending)

    def _sort_by_metadata(self, entries, metadata_index, descending=False):
        known = [item for item in entries if len(item) > metadata_index
                 and item[metadata_index] is not None]
        unknown = [item for item in entries if len(item) <= metadata_index
                   or item[metadata_index] is None]
        # Natural filename order is the deterministic tie-break for equal
        # metadata, and unknown metadata is always after known entries.
        known = sorted(known, key=lambda item: self._path_tie_key(item[0]))
        known = sorted(known, key=lambda item: self._natural_name_key(item[0]))
        known = sorted(known, key=lambda item: item[metadata_index],
                       reverse=descending)
        return known + self._sort_by_name(unknown, natural=True)

    def _sort_entries(self, entries, sort_method):
        sort_method = str(sort_method).strip().lower()
        if sort_method not in SORT_METHODS:
            logging.warning("Unknown sort method: %s, using %s", sort_method,
                            SORT_METHOD_DEFAULT)
            sort_method = SORT_METHOD_DEFAULT
        if sort_method == "natural_asc":
            return self._sort_by_name(entries, natural=True)
        if sort_method == "natural_desc":
            return self._sort_by_name(entries, natural=True, descending=True)
        if sort_method == "name_asc":
            return self._sort_by_name(entries, natural=False)
        if sort_method == "name_desc":
            return self._sort_by_name(entries, natural=False, descending=True)
        if sort_method == "date_asc":
            return self._sort_by_metadata(entries, 1)
        if sort_method == "date_desc":
            return self._sort_by_metadata(entries, 1, descending=True)
        if sort_method == "size_asc":
            return self._sort_by_metadata(entries, 2)
        return self._sort_by_metadata(entries, 2, descending=True)

    def get_files_in_directory(self, file_path: str) -> tuple[List[str], int]:
        """Compatibility sync API returning a successful snapshot and index."""
        result = self.discover_directory(file_path)
        if not result.succeeded:
            return [], -1
        snapshot = result.snapshot
        return list(snapshot.files), snapshot.index_for(file_path)

    def _sort_files(self, files: List[str], sort_method: str) -> List[str]:
        """Retained helper for callers/tests that supply paths directly."""
        entries = []
        for path in files:
            try:
                metadata = os.stat(path)
                modified = getattr(metadata, "st_mtime", None)
                size = getattr(metadata, "st_size", None)
            except OSError:
                modified = size = None
            entries.append((path, modified, size))
        return [path for path, *_ in self._sort_entries(entries, sort_method)]

    def get_next_file(self, current_path: str) -> tuple[Optional[str], bool]:
        """Return the next file and whether navigation wrapped."""
        files_list, current_index = self.get_files_in_directory(current_path)
        if not files_list or current_index == -1:
            return None, False
        if current_index == len(files_list) - 1:
            return files_list[0], True
        return files_list[current_index + 1], False

    def get_previous_file(self, current_path: str) -> tuple[Optional[str], bool]:
        """Return the previous file and whether navigation wrapped."""
        files_list, current_index = self.get_files_in_directory(current_path)
        if not files_list or current_index == -1:
            return None, False
        if current_index == 0:
            return files_list[-1], True
        return files_list[current_index - 1], False

    def start_preloading(self, current_path: str) -> bool:
        """Replace queued work with the nearest eligible neighbors.

        Neighbor priority is deterministic: next one, previous one, next two,
        previous two, and so on. Already-running work remains owned until it
        completes, but queued work that is no longer a neighbor is canceled.
        """
        if self.is_shutdown:
            return False
        if self.settings_manager.get_setting(
                "Navigation", "enable_wheel_navigation", "true").lower() != "true":
            return False

        preload_count = int(self.settings_manager.get_setting(
            "Navigation", "preload_count", "2"))
        if preload_count <= 0:
            return False

        files_list, current_index = self.get_files_in_directory(current_path)
        if not files_list or current_index == -1:
            return False

        max_preload = min(preload_count, 5, len(files_list) // 2)
        neighbors = []
        seen = {current_path}
        for distance in range(1, max_preload + 1):
            for index in (
                    (current_index + distance) % len(files_list),
                    (current_index - distance) % len(files_list)):
                path = files_list[index]
                if path not in seen:
                    seen.add(path)
                    neighbors.append(path)
        return self.request_preloads(neighbors)

    def request_preloading(self, current_path: str) -> bool:
        """Discover off-thread, then preload from the successful snapshot."""
        if not self._preloading_enabled():
            return False
        with self._condition:
            if self._shutdown:
                return False
            cached = self._file_cache.get(self._directory_key_for(current_path))
            if cached is None:
                return self._queue_discovery_locked(current_path, preload=True)
        return self._start_preloading_from_snapshot(cached, current_path)

    def request_navigation(self, current_path: str, step: int, context,
                           callback: Callable) -> bool:
        """Request bounded asynchronous directory discovery/navigation."""
        if step == 0:
            return False
        with self._condition:
            if self._shutdown:
                return False
            cached = self._file_cache.get(self._directory_key_for(current_path))
            if cached is None:
                return self._queue_discovery_locked(
                    current_path, steps=step, context=context, callback=callback)
        self._dispatch_navigation(cached, current_path, step, context, callback)
        return True

    def _queue_discovery_locked(self, current_path, *, steps=0, context=None,
                                callback=None, preload=False):
        directory_key = self._directory_key_for(current_path)
        for job in (self._active_discovery, self._pending_discovery):
            if (job is not None and job.generation == self._generation
                    and job.directory_key == directory_key
                    and (job.callback is None or callback is None
                         or _navigation_context_key(job.context)
                         == _navigation_context_key(context))):
                if (isinstance(context, tuple) and len(context) >= 4
                        and hasattr(context[0], "object_id")):
                    job.steps = steps
                else:
                    job.steps += steps
                if callback is not None:
                    job.context = context
                    job.callback = callback
                    job.current_path = current_path
                elif preload:
                    job.current_path = current_path
                job.preload = job.preload or preload
                return True

        job = _DiscoveryJob(
            self._next_job_id, current_path, directory_key, self._generation,
            steps=steps, context=context, callback=callback, preload=preload)
        self._next_job_id += 1
        if self._active_discovery is None:
            return self._start_discovery_locked(job)
        else:
            # Only the latest not-yet-started directory request is relevant.
            self._pending_discovery = job
        self._condition.notify_all()
        return True

    def _start_discovery_locked(self, job):
        job.state = "running"
        thread = threading.Thread(
            target=self._run_discovery,
            args=(job,),
            name=f"nagumix-discovery-{job.job_id}",
            daemon=True,
        )
        job.thread = thread
        self._active_discovery = job
        self._owned_threads.add(thread)
        try:
            thread.start()
        except Exception:
            self._owned_threads.discard(thread)
            self._active_discovery = None
            job.state = "failed"
            logging.exception("Failed to start directory discovery worker")
            return False
        return True

    def _run_discovery(self, job):
        try:
            result = self._directory_reader(job.current_path)
        except OSError as exc:
            result = DirectoryDiscovery(
                failure=DirectoryFailure.from_exception("enumeration", exc))
        except Exception as exc:
            logging.exception("Unexpected directory discovery failure")
            result = DirectoryDiscovery(
                failure=DirectoryFailure("enumeration", str(exc)))
        self._finish_discovery(job, result)

    def _finish_discovery(self, job, result):
        dispatch = None
        preload = None
        with self._condition:
            publish = (
                not self._shutdown
                and job.generation == self._generation
                and self._active_discovery is job
            )
            if publish and result.succeeded:
                self._file_cache[job.directory_key] = result.snapshot
            if publish and job.callback is not None:
                dispatch = (result, job.current_path, job.steps,
                            job.context, job.callback)
            if publish and job.preload and result.succeeded:
                preload = (result.snapshot, job.current_path)

            job.state = "completed" if publish else "discarded"
            if self._active_discovery is job:
                self._active_discovery = None
            pending = self._pending_discovery
            self._pending_discovery = None
            if (pending is not None and not self._shutdown
                    and pending.generation == self._generation):
                self._start_discovery_locked(pending)
            self._condition.notify_all()

        if preload is not None:
            self._start_preloading_from_snapshot(*preload)
        if dispatch is not None:
            outcome, current_path, steps, context, callback = dispatch
            if outcome.succeeded:
                self._dispatch_navigation(
                    outcome.snapshot, current_path, steps, context, callback)
            else:
                self._result_dispatch(callback, NavigationResult(
                    None, False, context, outcome.failure))

    def _dispatch_navigation(self, snapshot, current_path, steps, context, callback):
        current_index = snapshot.index_for(current_path)
        if current_index < 0 or not snapshot.files:
            result = NavigationResult(None, False, context)
        else:
            raw_index = current_index + steps
            target_index = raw_index % len(snapshot.files)
            result = NavigationResult(
                snapshot.files[target_index],
                raw_index < 0 or raw_index >= len(snapshot.files),
                context,
            )
        self._result_dispatch(callback, result)

    def _preloading_enabled(self):
        if self.is_shutdown:
            return False
        if self.settings_manager.get_setting(
                "Navigation", "enable_wheel_navigation", "true").lower() != "true":
            return False
        return int(self.settings_manager.get_setting(
            "Navigation", "preload_count", "2")) > 0

    def _start_preloading_from_snapshot(self, snapshot, current_path):
        if not self._preloading_enabled():
            return False
        current_index = snapshot.index_for(current_path)
        if current_index < 0 or not snapshot.files:
            return False
        preload_count = int(self.settings_manager.get_setting(
            "Navigation", "preload_count", "2"))
        max_preload = min(preload_count, 5, len(snapshot.files) // 2)
        neighbors = []
        seen = {path_comparison_key(current_path)}
        for distance in range(1, max_preload + 1):
            for index in ((current_index + distance) % len(snapshot.files),
                          (current_index - distance) % len(snapshot.files)):
                path = snapshot.files[index]
                key = path_comparison_key(path)
                if key not in seen:
                    seen.add(key)
                    neighbors.append(path)
        return self.request_preloads(neighbors)

    def request_preloads(self, paths) -> bool:
        """Atomically retain the highest-priority bounded set of ``paths``.

        This is the small reusable scheduling boundary for later foreground
        loading work. It owns no wx resources and never waits for I/O.
        """
        desired = list(dict.fromkeys(paths))
        with self._condition:
            if self._shutdown:
                return False
            self._prune_finished_threads_locked()

            requested_pending = [
                job for job in self._pending_jobs
                if job.generation == self._generation
                and job.state == "queued" and job.purpose != "preload"
            ]
            existing_queued = {
                job.path: job for job in self._pending_jobs
                if job.generation == self._generation
                and job.state == "queued" and job.purpose == "preload"
            }
            desired_set = set(desired)
            for job in tuple(self._pending_jobs):
                if job.purpose == "preload" and job.path not in desired_set:
                    self._cancel_queued_job_locked(job)

            available_starts = max(0, self.MAX_ACTIVE_DECODES - self._active_decodes)
            request_limit = max(
                0, self.MAX_PENDING_PRELOADS + available_starts
                - len(requested_pending))
            new_pending = deque()
            for path in desired:
                if path in self._preload_cache:
                    continue
                current = self._current_jobs.get(path)
                if current is not None and current.state == "running":
                    continue

                job = existing_queued.get(path)
                if job is None or job.state != "queued":
                    if len(new_pending) >= request_limit:
                        break
                    job = self._new_job_locked(path)
                if len(new_pending) >= request_limit:
                    self._cancel_queued_job_locked(job)
                    break
                new_pending.append(job)

            retained = set(requested_pending) | set(new_pending)
            for job in tuple(self._pending_jobs):
                if job not in retained:
                    self._cancel_queued_job_locked(job)
            self._pending_jobs = deque(requested_pending)
            self._pending_jobs.extend(new_pending)
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return True

    def request_navigation_decode(self, path: str, request_key, context,
                                  callback: Callable, *, wrapped=False,
                                  apply_orientation=True) -> bool:
        """Prioritize one replaceable foreground candidate decode.

        A matching cached image is removed from the cache and transferred to
        this job. A matching queued/running normalized preload is promoted.
        Neither route copies full source pixels on the GUI thread.
        """
        with self._condition:
            if self._shutdown:
                return False
            self._prune_finished_threads_locked()
            previous = self._foreground_jobs.get(request_key)
            if previous is not None:
                previous.superseded = True
                if previous.state == "queued":
                    try:
                        self._pending_jobs.remove(previous)
                    except ValueError:
                        pass
                    self._cancel_queued_job_locked(previous)

            job = None
            if apply_orientation:
                current = self._current_jobs.get(path)
                if (current is not None and current.generation == self._generation
                        and current.purpose == "preload"
                        and current.state in ("queued", "running")):
                    job = current
                    job.purpose = "foreground"
                    job.request_key = request_key
                    job.context = context
                    job.callback = callback
                    job.wrapped = wrapped
                    if job.state == "queued":
                        self._pending_jobs.remove(job)
                        self._pending_jobs.appendleft(job)

            if job is None:
                transferred = None
                if apply_orientation:
                    transferred = self._take_cache_image_locked(path)
                job = self._new_job_locked(
                    path, purpose="foreground", request_key=request_key,
                    context=context, callback=callback,
                    apply_orientation=apply_orientation,
                    transferred_image=transferred, wrapped=wrapped,
                    register_current=False)

                available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
                if (not available_start
                        and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS):
                    if not self._evict_speculative_job_locked():
                        self._cancel_queued_job_locked(job)
                        return False
                self._pending_jobs.appendleft(job)

            self._foreground_jobs[request_key] = job
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return job.state != "failed"

    def cancel_navigation_decode(self, request_key) -> bool:
        """Invalidate queued/running foreground work without waiting for I/O."""
        with self._condition:
            job = self._foreground_jobs.pop(request_key, None)
            if job is None:
                return False
            job.superseded = True
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
            self._condition.notify_all()
            return True

    def request_animation_frame(self, path: str, frame_index: int, request_key,
                                context, callback: Callable, *,
                                expected_source_identity) -> bool:
        """Keep one owned exact decode plus one replaceable target per object."""
        request = _DeferredAnimationRequest(
            path, int(frame_index), request_key, context, callback,
            tuple(expected_source_identity))
        with self._condition:
            if self._shutdown:
                return False
            self._prune_finished_threads_locked()
            previous = self._animation_jobs.get(request_key)
            if previous is not None:
                if self._animation_job_matches(previous, request):
                    return True
                previous.superseded = True
                task = previous.work_callable
                if task is not None:
                    task.cancel()
                if previous.state == "queued":
                    try:
                        self._pending_jobs.remove(previous)
                    except ValueError:
                        pass
                    self._cancel_queued_job_locked(previous)
                elif previous.state == "running":
                    self._deferred_animation_requests[request_key] = request
                    self._condition.notify_all()
                    return True

            self._deferred_animation_requests.pop(request_key, None)
            if not self._queue_animation_request_locked(request):
                # Waiting intent is outside the bounded worker queue. A worker
                # completion admits it without a GUI polling loop.
                self._deferred_animation_requests[request_key] = request
            self._condition.notify_all()
            return True

    @staticmethod
    def _animation_job_matches(job, request):
        task = job.work_callable
        return (not job.superseded
                and job.path == request.path
                and job.context == request.context
                and task is not None
                and task.frame_index == request.frame_index
                and task.source_identity == request.source_identity)

    def _queue_animation_request_locked(self, request):
        available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
        if (not available_start
                and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS
                and not self._evict_speculative_job_locked()):
            return False
        task = AnimationFrameTask(
            request.path, request.frame_index, request.source_identity,
            self._animation_loader)
        job = self._new_job_locked(
            request.path, purpose="animation", request_key=request.request_key,
            context=request.context, callback=request.callback,
            register_current=False, work_callable=task)
        self._pending_jobs.appendleft(job)
        self._animation_jobs[request.request_key] = job
        self._start_available_jobs_locked()
        return job.state not in ("failed", "canceled")

    def _wake_deferred_animation_locked(self):
        """Admit exact targets only after earlier work releases capacity."""
        for request_key, request in tuple(
                self._deferred_animation_requests.items()):
            if request_key in self._animation_jobs:
                continue
            if not self._queue_animation_request_locked(request):
                break
            self._deferred_animation_requests.pop(request_key, None)

    def cancel_animation_frame(self, request_key) -> bool:
        """Retire one object's queued/running frame reconstruction."""
        with self._condition:
            deferred = self._deferred_animation_requests.pop(request_key, None)
            job = self._animation_jobs.pop(request_key, None)
            if job is None:
                return deferred is not None
            job.superseded = True
            task = job.work_callable
            if task is not None:
                task.cancel()
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
            self._condition.notify_all()
            return True

    def request_drop_decode(self, path: str, request_key, context,
                            callback: Callable, *, apply_orientation=True) -> bool:
        """Queue one independently owned drop decode behind navigation work.

        Drop callers feed entries incrementally.  Navigation jobs are inserted
        at the front, drop jobs sit ahead of speculative preloads, and the
        shared active/pending limits remain authoritative.
        """
        with self._condition:
            if self._shutdown or request_key in self._drop_jobs:
                return False
            self._prune_finished_threads_locked()

            job = None
            if apply_orientation:
                current = self._current_jobs.get(path)
                if (current is not None and current.generation == self._generation
                        and current.purpose == "preload"
                        and current.state in ("queued", "running")):
                    job = current
                    job.purpose = "drop"
                    job.request_key = request_key
                    job.context = context
                    job.callback = callback
                    if job.state == "queued":
                        self._pending_jobs.remove(job)
                        self._insert_drop_job_locked(job)

            if job is None:
                transferred = None
                if apply_orientation:
                    transferred = self._take_cache_image_locked(path)
                job = self._new_job_locked(
                    path, purpose="drop", request_key=request_key,
                    context=context, callback=callback,
                    apply_orientation=apply_orientation,
                    transferred_image=transferred, register_current=False)

                available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
                if (not available_start
                        and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS):
                    if not self._evict_speculative_job_locked():
                        self._cancel_queued_job_locked(job)
                        return False
                self._insert_drop_job_locked(job)

            self._drop_jobs[request_key] = job
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return job.state != "failed"

    def _insert_drop_job_locked(self, job):
        """Place drop work after interactive requests and before preloads."""
        insertion = next((index for index, candidate in enumerate(self._pending_jobs)
                          if candidate.purpose in (
                              "export", "scene", "scene_document", "preload")),
                         len(self._pending_jobs))
        self._pending_jobs.insert(insertion, job)

    def cancel_drop_decode(self, request_key) -> bool:
        """Invalidate one queued/running dropped-file decode without waiting."""
        with self._condition:
            job = self._drop_jobs.pop(request_key, None)
            if job is None:
                return False
            job.superseded = True
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
            self._condition.notify_all()
            return True

    def request_scene_document(self, path: str, request_key, context,
                               callback: Callable) -> bool:
        """Queue scene JSON reading/validation in the shared bounded scheduler."""
        with self._condition:
            if self._shutdown or request_key in self._scene_jobs:
                return False
            self._prune_finished_threads_locked()
            job = self._new_job_locked(
                path, purpose="scene_document", request_key=request_key,
                context=context, callback=callback, register_current=False)
            if not self._admit_scene_job_locked(job):
                return False
            self._scene_jobs[request_key] = job
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return job.state != "failed"

    def request_scene_decode(self, path: str, request_key, context,
                             callback: Callable, *, apply_orientation=True,
                             animation=None) -> bool:
        """Queue one independently owned scene entry ahead of preloads."""
        with self._condition:
            if self._shutdown or request_key in self._scene_jobs:
                return False
            self._prune_finished_threads_locked()
            transferred = None
            task = None
            if animation is not None:
                task = AnimationFrameTask(
                    path, animation["frame_index"], None,
                    self._animation_loader)
            elif apply_orientation:
                transferred = self._take_cache_image_locked(path)
            job = self._new_job_locked(
                path, purpose="scene", request_key=request_key,
                context=context, callback=callback,
                apply_orientation=apply_orientation,
                transferred_image=transferred, register_current=False,
                work_callable=task)
            if not self._admit_scene_job_locked(job):
                return False
            self._scene_jobs[request_key] = job
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return job.state != "failed"

    def request_export(self, task, request_key, callback: Callable) -> bool:
        """Queue one export through the shared active/pending work boundary."""
        with self._condition:
            if self._shutdown or self._export_jobs:
                return False
            self._prune_finished_threads_locked()
            job = self._new_job_locked(
                task.destination, purpose="export", request_key=request_key,
                callback=callback, register_current=False,
                work_callable=task)
            available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
            if (not available_start
                    and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS):
                if not self._evict_speculative_job_locked():
                    self._cancel_queued_job_locked(job)
                    return False
            insertion = next((index for index, candidate in enumerate(self._pending_jobs)
                              if candidate.purpose in (
                                  "scene", "scene_document", "preload")),
                             len(self._pending_jobs))
            self._pending_jobs.insert(insertion, job)
            self._export_jobs[request_key] = job
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return job.state != "failed"

    def cancel_export_work(self, request_key) -> bool:
        """Cancel only the queued/running export without waiting for its calls."""
        with self._condition:
            job = self._export_jobs.pop(request_key, None)
            if job is None:
                return False
            job.superseded = True
            task = job.work_callable
            if task is not None:
                task.cancellation.cancel()
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
            self._condition.notify_all()
            return True

    def request_canvas_save(self, task, request_key, callback: Callable) -> bool:
        """Supersede and queue one canvas save in the shared bounded service."""
        with self._condition:
            if self._shutdown:
                return False
            self._prune_finished_threads_locked()
            for old_key, old_job in tuple(self._save_jobs.items()):
                self._save_jobs.pop(old_key, None)
                old_job.superseded = True
                if old_job.work_callable is not None:
                    old_job.work_callable.cancellation.cancel()
                if old_job.state == "queued":
                    try:
                        self._pending_jobs.remove(old_job)
                    except ValueError:
                        pass
                    self._cancel_queued_job_locked(old_job)
            job = self._new_job_locked(
                task.destination, purpose="save", request_key=request_key,
                callback=callback, register_current=False,
                work_callable=task)
            available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
            if (not available_start
                    and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS):
                if not self._evict_speculative_job_locked():
                    self._cancel_queued_job_locked(job)
                    return False
            insertion = next((index for index, candidate in enumerate(self._pending_jobs)
                              if candidate.purpose in (
                                  "scene", "scene_document", "export",
                                  "duplicate", "preload")),
                             len(self._pending_jobs))
            self._pending_jobs.insert(insertion, job)
            self._save_jobs[request_key] = job
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return job.state != "failed"

    def cancel_canvas_save_work(self, request_key) -> bool:
        """Cancel a queued/running canvas save at its next safe boundary."""
        with self._condition:
            job = self._save_jobs.pop(request_key, None)
            if job is None:
                return False
            job.superseded = True
            if job.work_callable is not None:
                job.work_callable.cancellation.cancel()
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
            self._condition.notify_all()
            return True

    def request_duplication(self, path: str, request_key, context,
                            callback: Callable, task) -> bool:
        """Queue one bounded copy of an already-owned source-pixel lease."""
        with self._condition:
            if self._shutdown or request_key in self._duplication_jobs:
                return False
            self._prune_finished_threads_locked()
            job = self._new_job_locked(
                path, purpose="duplicate", request_key=request_key,
                context=context, callback=callback, register_current=False,
                work_callable=task)
            available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
            if (not available_start
                    and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS):
                if not self._evict_speculative_job_locked():
                    self._cancel_queued_job_locked(job)
                    return False
            insertion = next((index for index, candidate in enumerate(self._pending_jobs)
                              if candidate.purpose in (
                                  "scene", "scene_document", "export", "preload")),
                             len(self._pending_jobs))
            self._pending_jobs.insert(insertion, job)
            self._duplication_jobs[request_key] = job
            self._start_available_jobs_locked()
            self._condition.notify_all()
            return job.state != "failed"

    # Keep the verb used by callers/tests obvious while allowing older
    # terminology in integrations that describe this as a duplicate request.
    request_duplicate = request_duplication

    def cancel_duplication_work(self, request_key) -> bool:
        """Cancel one queued/running duplication without waiting for copying."""
        with self._condition:
            job = self._duplication_jobs.pop(request_key, None)
            if job is None:
                return False
            job.superseded = True
            task = job.work_callable
            if task is not None:
                task.cancel()
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
            self._condition.notify_all()
            return True

    cancel_duplicate_work = cancel_duplication_work

    def _admit_scene_job_locked(self, job):
        available_start = self._active_decodes < self.MAX_ACTIVE_DECODES
        if (not available_start
                and len(self._pending_jobs) >= self.MAX_PENDING_PRELOADS):
            if not self._evict_speculative_job_locked():
                self._cancel_queued_job_locked(job)
                return False
        insertion = next((index for index, candidate in enumerate(self._pending_jobs)
                          if candidate.purpose == "preload"),
                         len(self._pending_jobs))
        self._pending_jobs.insert(insertion, job)
        return True

    def cancel_scene_work(self, request_key) -> bool:
        """Invalidate one queued/running scene job without waiting for I/O."""
        with self._condition:
            job = self._scene_jobs.pop(request_key, None)
            if job is None:
                return False
            job.superseded = True
            task = job.work_callable
            if task is not None:
                task.cancel()
            if job.state == "queued":
                try:
                    self._pending_jobs.remove(job)
                except ValueError:
                    pass
                self._cancel_queued_job_locked(job)
            self._condition.notify_all()
            return True

    def _new_job_locked(self, path: str, *, purpose="preload", request_key=None,
                        context=None, callback=None, apply_orientation=True,
                        transferred_image=None, wrapped=False,
                        register_current=True, work_callable=None) -> _PreloadJob:
        job = _PreloadJob(
            self._next_job_id, path, self._generation, purpose=purpose,
            request_key=request_key, context=context, callback=callback,
            apply_orientation=apply_orientation,
            transferred_image=transferred_image, wrapped=wrapped,
            work_callable=work_callable)
        self._next_job_id += 1
        self._jobs[job.job_id] = job
        if register_current:
            self._current_jobs[path] = job
        return job

    def _cancel_queued_job_locked(self, job: _PreloadJob):
        if job.state != "queued":
            return
        job.state = "canceled"
        self._jobs.pop(job.job_id, None)
        if self._foreground_jobs.get(job.request_key) is job:
            self._foreground_jobs.pop(job.request_key, None)
        if self._drop_jobs.get(job.request_key) is job:
            self._drop_jobs.pop(job.request_key, None)
        if self._scene_jobs.get(job.request_key) is job:
            self._scene_jobs.pop(job.request_key, None)
        if self._export_jobs.get(job.request_key) is job:
            self._export_jobs.pop(job.request_key, None)
        if self._save_jobs.get(job.request_key) is job:
            self._save_jobs.pop(job.request_key, None)
        if self._duplication_jobs.get(job.request_key) is job:
            self._duplication_jobs.pop(job.request_key, None)
        if self._animation_jobs.get(job.request_key) is job:
            self._animation_jobs.pop(job.request_key, None)
        if self._playback_jobs.get(job.request_key) is job:
            self._playback_jobs.pop(job.request_key, None)
        if self._current_jobs.get(job.path) is job:
            self._current_jobs.pop(job.path, None)
        self._close_image_locked(job.transferred_image)
        job.transferred_image = None
        if job.work_callable is not None:
            job.work_callable.cancel_before_start()

    def _start_available_jobs_locked(self):
        while (not self._shutdown
               and self._active_decodes < self.MAX_ACTIVE_DECODES
               and self._pending_jobs):
            job = self._pending_jobs.popleft()
            if job.state != "queued" or job.generation != self._generation:
                self._cancel_queued_job_locked(job)
                continue

            job.state = "running"
            thread = threading.Thread(
                target=self._run_preload,
                args=(job,),
                name=f"nagumix-{job.purpose}-{job.job_id}",
                daemon=True,
            )
            job.thread = thread
            self._active_decodes += 1
            self._owned_threads.add(thread)
            try:
                thread.start()
            except Exception:
                self._active_decodes -= 1
                self._owned_threads.discard(thread)
                job.state = "queued"
                self._cancel_queued_job_locked(job)
                job.state = "failed"
                logging.exception("Failed to start preload worker for %s", job.path)

    def _run_preload(self, job: _PreloadJob):
        image = job.transferred_image
        job.transferred_image = None
        error = None
        if image is None:
            try:
                if job.work_callable is not None:
                    if job.purpose == "playback":
                        with self._lock:
                            self._playback_slice_order.append(job.request_key)
                    image = job.work_callable.run()
                elif job.purpose == "scene_document":
                    image = self._state_reader(job.path)
                elif job.apply_orientation:
                    image = self._image_loader(job.path)
                else:
                    image = self._image_loader(job.path, apply_orientation=False)
            except Exception as exc:
                error = exc
                if job.purpose == "scene_document":
                    logging.warning("Failed to read canvas state %s: %s", job.path, exc)
                elif job.purpose == "export":
                    logging.warning("Failed to export canvas to %s: %s", job.path, exc)
                elif job.purpose == "save":
                    logging.warning("Failed to save canvas to %s: %s", job.path, exc)
                elif job.purpose == "duplicate":
                    logging.warning("Failed to duplicate image %s: %s", job.path, exc)
                elif job.purpose == "animation":
                    logging.warning("Failed to reconstruct GIF frame %s: %s", job.path, exc)
                elif job.purpose == "playback":
                    logging.warning("Failed to decode GIF playback %s: %s", job.path, exc)
                else:
                    logging.warning("Failed to decode image %s: %s", job.path, exc)
        self._finish_job(job, image, error)

    def _finish_job(self, job: _PreloadJob, image, error):
        dispatch = None
        with self._condition:
            if job.state == "running":
                self._active_decodes -= 1

            generation_current = (
                not self._shutdown and job.generation == self._generation)
            if job.purpose in (
                    "foreground", "drop", "scene", "scene_document", "export", "save",
                    "duplicate", "animation", "playback"):
                if job.purpose == "foreground":
                    owner = self._foreground_jobs
                elif job.purpose == "drop":
                    owner = self._drop_jobs
                elif job.purpose in ("scene", "scene_document"):
                    owner = self._scene_jobs
                elif job.purpose == "duplicate":
                    owner = self._duplication_jobs
                elif job.purpose == "animation":
                    owner = self._animation_jobs
                elif job.purpose == "playback":
                    owner = self._playback_jobs
                elif job.purpose == "save":
                    owner = self._save_jobs
                else:
                    owner = self._export_jobs
                publish = (
                    generation_current and not job.superseded
                    and owner.get(job.request_key) is job)
                if publish:
                    if job.purpose == "foreground":
                        result = NavigationDecodeResult(
                            job.path, job.wrapped, job.context, pixels=image,
                            error=None if error is None else str(error))
                    elif job.purpose == "drop":
                        result = DropDecodeResult(
                            job.path, job.context, pixels=image,
                            error=None if error is None else str(error))
                    elif job.purpose == "scene_document":
                        result = SceneDocumentResult(
                            job.path, job.context, records=image,
                            error=None if error is None else str(error))
                    elif job.purpose == "scene":
                        result = SceneDecodeResult(
                            job.path, job.context, pixels=image,
                            error=None if error is None else str(error))
                    elif job.purpose == "duplicate":
                        result = DuplicationDecodeResult(
                            job.path, job.context, pixels=image,
                            error=None if error is None else str(error))
                    elif job.purpose == "animation":
                        result = AnimationDecodeResult(
                            job.path, job.work_callable.frame_index, job.context,
                            pixels=image,
                            error=None if error is None else str(error),
                            source_refreshed=job.work_callable.source_refreshed)
                    elif job.purpose == "playback":
                        outcome = image
                        if not isinstance(outcome, PlaybackSliceOutcome):
                            outcome = PlaybackSliceOutcome(
                                [], error=(str(error) if error is not None
                                           else "playback worker returned no result"),
                                finished=True)
                        result = PlaybackSliceResult(
                            job.context, outcome.packets,
                            error=(outcome.error if error is None else str(error)),
                            blocked=outcome.blocked,
                            finished=outcome.finished)
                    else:
                        result = image
                    dispatch = (job.callback, result)
                    image = None
                    job.state = "completed" if error is None else "failed"
                else:
                    if job.purpose == "playback":
                        close = getattr(image, "close", None)
                        if close is not None:
                            close()
                    elif job.purpose not in ("scene_document", "save"):
                        self._close_image_locked(image)
                    image = None
                    job.state = "discarded"
            else:
                publish = (
                    error is None and image is not None and generation_current
                    and self._current_jobs.get(job.path) is job)
                if publish:
                    admitted = self._admit_cache_image_locked(job.path, image)
                    image = None
                    job.state = "completed" if admitted else "discarded"
                    if admitted:
                        logging.debug("Preloaded image: %s", job.path)
                else:
                    self._close_image_locked(image)
                    image = None
                    job.state = "failed" if error is not None else "discarded"

            self._jobs.pop(job.job_id, None)
            if self._foreground_jobs.get(job.request_key) is job:
                self._foreground_jobs.pop(job.request_key, None)
            if self._drop_jobs.get(job.request_key) is job:
                self._drop_jobs.pop(job.request_key, None)
            if self._scene_jobs.get(job.request_key) is job:
                self._scene_jobs.pop(job.request_key, None)
            if self._export_jobs.get(job.request_key) is job:
                self._export_jobs.pop(job.request_key, None)
            if self._save_jobs.get(job.request_key) is job:
                self._save_jobs.pop(job.request_key, None)
            if self._duplication_jobs.get(job.request_key) is job:
                self._duplication_jobs.pop(job.request_key, None)
            if self._animation_jobs.get(job.request_key) is job:
                self._animation_jobs.pop(job.request_key, None)
            if self._playback_jobs.get(job.request_key) is job:
                self._playback_jobs.pop(job.request_key, None)
            if self._current_jobs.get(job.path) is job:
                self._current_jobs.pop(job.path, None)
            self._wake_deferred_animation_locked()
            self._start_available_jobs_locked()
            self._wake_deferred_playback_locked()
            self._condition.notify_all()

        if dispatch is not None:
            callback, result = dispatch
            try:
                self._result_dispatch(callback, result)
            except Exception:
                close = getattr(result, "close", None)
                if close is not None:
                    close()
                if job.purpose == "playback":
                    self.cancel_gif_playback(job.request_key)
                logging.exception("Failed to dispatch background result")

    @staticmethod
    def _close_image_locked(image):
        if image is None:
            return
        try:
            image.close()
        except Exception as exc:
            logging.warning("Failed to release preloaded image: %s", exc)

    def get_preloaded_image(self, file_path: str) -> Optional[Image.Image]:
        """Return an independent compatibility copy without copying under lock."""
        with self._lock:
            entry = self._preload_cache.get(file_path)
            if entry is None:
                return None
            cached_image = entry.image
            image_id = id(cached_image)
            self._cache_leases[image_id] = self._cache_leases.get(image_id, 0) + 1
        copied = None
        try:
            copied = cached_image.copy()
            return copied
        except Exception as exc:
            logging.warning("Failed to copy preloaded image %s: %s", file_path, exc)
            return None
        finally:
            with self._lock:
                if copied is not None:
                    retained = self._preload_cache.get(file_path)
                    if retained is entry:
                        entry.access_order = self._next_cache_order_locked()
                    self._cache_hits += 1
                self._release_cache_lease_locked(image_id)

    def clear_cache(self):
        """Invalidate this generation and permit fresh preload requests."""
        with self._condition:
            if self._shutdown:
                return False
            self._generation += 1
            # Saving captures its own immutable document/settings snapshot and
            # is not invalidated by later navigation/settings cache changes.
            for job in self._save_jobs.values():
                job.generation = self._generation
            self._file_cache.clear()
            self._pending_discovery = None
            self._dispose_cache_locked()
            for job in self._export_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancellation.cancel()
            for job in self._duplication_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancel()
            for job in self._animation_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancel()
            for session in self._playback_sessions.values():
                session.cancel()
            for job in self._scene_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancel()
            self._cancel_all_pending_locked(preserve_purposes={"save"})
            self._current_jobs.clear()
            self._foreground_jobs.clear()
            self._drop_jobs.clear()
            self._scene_jobs.clear()
            self._export_jobs.clear()
            self._duplication_jobs.clear()
            self._animation_jobs.clear()
            self._deferred_animation_requests.clear()
            self._playback_jobs.clear()
            playback_sessions = tuple(self._playback_sessions.values())
            self._playback_sessions.clear()
            self._condition.notify_all()
        for session in playback_sessions:
            if not session.running:
                session.close_decoder()
        return True

    def shutdown(self):
        """Terminal, idempotent, and nonblocking preload shutdown.

        Queued work is canceled immediately. A Pillow decode or operating-system
        read that has already started cannot be forcibly interrupted; its result
        is discarded and released when it returns.
        """
        with self._condition:
            if self._shutdown:
                return False
            self._shutdown = True
            self._generation += 1
            self._file_cache.clear()
            self._pending_discovery = None
            self._dispose_cache_locked()
            for job in self._export_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancellation.cancel()
            for job in self._save_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancellation.cancel()
            for job in self._duplication_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancel()
            for job in self._animation_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancel()
            for session in self._playback_sessions.values():
                session.cancel()
            for job in self._scene_jobs.values():
                if job.work_callable is not None:
                    job.work_callable.cancel()
            self._cancel_all_pending_locked()
            self._current_jobs.clear()
            self._foreground_jobs.clear()
            self._drop_jobs.clear()
            self._scene_jobs.clear()
            self._export_jobs.clear()
            self._save_jobs.clear()
            self._duplication_jobs.clear()
            self._animation_jobs.clear()
            self._deferred_animation_requests.clear()
            self._playback_jobs.clear()
            playback_sessions = tuple(self._playback_sessions.values())
            self._playback_sessions.clear()
            self._condition.notify_all()
        for session in playback_sessions:
            if not session.running:
                session.close_decoder()
        return True

    def wait_for_workers(self, timeout: float = 5.0) -> bool:
        """Wait up to ``timeout`` seconds for every owned worker to finish."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._condition:
                self._prune_finished_threads_locked()
                alive = [thread for thread in self._owned_threads if thread.is_alive()]
                if (not alive and self._active_decodes == 0 and not self._pending_jobs
                        and self._active_discovery is None
                        and self._pending_discovery is None):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            alive[0].join(remaining)

    def _dispose_cache_locked(self):
        for path in tuple(self._preload_cache):
            self._remove_cache_entry_locked(path)

    def _next_cache_order_locked(self):
        order = self._next_cache_order
        self._next_cache_order += 1
        return order

    @staticmethod
    def _decoded_payload_bytes(image):
        """Measure detached RGB/RGBA pixels without materializing a byte copy."""
        candidate = image
        if not hasattr(candidate, "size") or not hasattr(candidate, "mode"):
            candidate = getattr(candidate, "image", candidate)
        width, height = candidate.size
        channels = {"RGB": 3, "RGBA": 4}.get(candidate.mode)
        if channels is None or width < 0 or height < 0:
            raise ValueError("decoded cache payload must be RGB or RGBA")
        return width * height * channels

    def _preload_cache_budget_mb(self):
        getter = getattr(self.settings_manager, "get_preload_cache_mb", None)
        if getter is not None:
            return getter()
        raw = self.settings_manager.get_setting(
            "Navigation", "preload_cache_mb", str(PRELOAD_CACHE_DEFAULT_MB))
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return PRELOAD_CACHE_DEFAULT_MB
        if not PRELOAD_CACHE_MIN_MB <= value <= PRELOAD_CACHE_MAX_MB:
            return PRELOAD_CACHE_DEFAULT_MB
        return value

    def _cache_total_bytes_locked(self):
        return self._retained_cache_bytes + self._retired_cache_bytes

    def _record_cache_admission_locked(self, path, outcome, byte_count,
                                       evicted=()):
        self._last_cache_admission = {
            "path": path,
            "outcome": outcome,
            "bytes": byte_count,
            "evicted": tuple(evicted),
        }

    def _admit_cache_image_locked(self, path, image):
        """Own ``image`` only when it fits the latest cache payload budget."""
        try:
            byte_count = self._decoded_payload_bytes(image)
        except (AttributeError, TypeError, ValueError):
            self._cache_rejections += 1
            self._record_cache_admission_locked(path, "invalid_payload", 0)
            self._close_image_locked(image)
            return False

        budget_bytes = self._preload_cache_budget_mb() * 1024 * 1024
        previous = self._preload_cache.get(path)
        if (previous is not None
                and self._cache_leases.get(id(previous.image), 0)):
            self._cache_rejections += 1
            self._cache_pinned_rejections += 1
            self._record_cache_admission_locked(
                path, "pinned_rejection", byte_count)
            self._close_image_locked(image)
            return False
        if previous is not None:
            self._remove_cache_entry_locked(path)

        if byte_count > budget_bytes:
            self._cache_rejections += 1
            self._cache_oversize_rejections += 1
            outcome = "retention_disabled" if budget_bytes == 0 else "oversize_rejection"
            self._record_cache_admission_locked(path, outcome, byte_count)
            self._close_image_locked(image)
            return False

        evicted = []
        while self._cache_total_bytes_locked() + byte_count > budget_bytes:
            candidates = [
                entry for entry in self._preload_cache.values()
                if not self._cache_leases.get(id(entry.image), 0)
            ]
            if not candidates:
                self._cache_rejections += 1
                self._cache_pinned_rejections += 1
                self._record_cache_admission_locked(
                    path, "pinned_rejection", byte_count, evicted)
                self._close_image_locked(image)
                return False
            victim = min(
                candidates,
                key=lambda entry: (entry.access_order, entry.insertion_order),
            )
            evicted.append(victim.path)
            self._remove_cache_entry_locked(victim.path)
            self._cache_evictions += 1

        order = self._next_cache_order_locked()
        self._preload_cache[path] = _DecodedCacheEntry(
            path, image, byte_count, order, order)
        self._retained_cache_bytes += byte_count
        self._cache_admissions += 1
        self._record_cache_admission_locked(path, "admitted", byte_count, evicted)
        return True

    def _remove_cache_entry_locked(self, path):
        """Remove retained ownership, retiring it when a copy reader is active."""
        entry = self._preload_cache.pop(path, None)
        if entry is None:
            return None
        self._retained_cache_bytes -= entry.byte_count
        image_id = id(entry.image)
        if self._cache_leases.get(image_id, 0):
            self._retired_cache_images[image_id] = entry
            self._retired_cache_bytes += entry.byte_count
        else:
            self._close_image_locked(entry.image)
        return entry

    def _take_cache_image_locked(self, path):
        """Transfer one unleased cache payload and remove its charge once."""
        entry = self._preload_cache.get(path)
        if entry is None or self._cache_leases.get(id(entry.image), 0):
            return None
        self._preload_cache.pop(path)
        self._retained_cache_bytes -= entry.byte_count
        self._cache_hits += 1
        return entry.image

    def _release_cache_lease_locked(self, image_id):
        remaining = self._cache_leases.get(image_id, 1) - 1
        if remaining:
            self._cache_leases[image_id] = remaining
            return
        self._cache_leases.pop(image_id, None)
        retired = self._retired_cache_images.pop(image_id, None)
        if retired is not None:
            self._retired_cache_bytes -= retired.byte_count
            self._close_image_locked(retired.image)

    def _cancel_all_pending_locked(self, preserve_purposes=frozenset()):
        retained = deque()
        for job in tuple(self._pending_jobs):
            if job.purpose in preserve_purposes:
                retained.append(job)
            else:
                self._cancel_queued_job_locked(job)
        self._pending_jobs = retained

    def _prune_finished_threads_locked(self):
        self._owned_threads = {
            thread for thread in self._owned_threads if thread.is_alive()
        }

    def invalidate_directory_cache(self, directory: str):
        """Invalidate cached directory enumeration for one directory."""
        with self._lock:
            self._file_cache.pop(path_comparison_key(directory), None)

    @property
    def is_shutdown(self):
        with self._lock:
            return self._shutdown

    def preload_state(self):
        """Return a synchronized diagnostic snapshot for tests/measurements."""
        with self._lock:
            self._prune_finished_threads_locked()
            budget_mb = self._preload_cache_budget_mb()
            lru_entries = sorted(
                self._preload_cache.values(),
                key=lambda entry: (entry.access_order, entry.insertion_order),
            )
            return {
                "generation": self._generation,
                "shutdown": self._shutdown,
                "active": self._active_decodes,
                "pending": len(self._pending_jobs),
                "jobs": len(self._jobs),
                "workers": len(self._owned_threads),
                "cached": tuple(self._preload_cache),
                "cache_budget_mb": budget_mb,
                "cache_budget_bytes": budget_mb * 1024 * 1024,
                "cache_retained_bytes": self._retained_cache_bytes,
                "cache_retired_bytes": self._retired_cache_bytes,
                "cache_total_bytes": self._cache_total_bytes_locked(),
                "cache_retained_entries": len(self._preload_cache),
                "cache_retired_entries": len(self._retired_cache_images),
                "cache_lru": tuple(entry.path for entry in lru_entries),
                "cache_admissions": self._cache_admissions,
                "cache_evictions": self._cache_evictions,
                "cache_rejections": self._cache_rejections,
                "cache_oversize_rejections": self._cache_oversize_rejections,
                "cache_pinned_rejections": self._cache_pinned_rejections,
                "cache_hits": self._cache_hits,
                "cache_last_admission": (
                    None if self._last_cache_admission is None
                    else dict(self._last_cache_admission)),
                "queued": tuple(job.path for job in self._pending_jobs),
                "foreground": sum(
                    job.purpose == "foreground" for job in self._jobs.values()),
                "drop": sum(
                    job.purpose == "drop" for job in self._jobs.values()),
                "scene": sum(
                    job.purpose in ("scene", "scene_document")
                    for job in self._jobs.values()),
                "export": sum(
                    job.purpose == "export" for job in self._jobs.values()),
                "save": sum(
                    job.purpose == "save" for job in self._jobs.values()),
                "duplicate": sum(
                    job.purpose == "duplicate" for job in self._jobs.values()),
                "animation": sum(
                    job.purpose == "animation" for job in self._jobs.values()),
                "animation_deferred": len(self._deferred_animation_requests),
                "playback": sum(
                    job.purpose == "playback" for job in self._jobs.values()),
                "playback_sessions": len(self._playback_sessions),
                "playback_objects": tuple(self._playback_sessions),
                "playback_session_frames": {
                    object_id: session.retained_frames
                    for object_id, session in self._playback_sessions.items()
                },
                "playback_session_max_frames": {
                    object_id: session.max_retained_frames
                    for object_id, session in self._playback_sessions.items()
                },
                "playback_transfer": self._playback_budget.snapshot(),
                "playback_slice_order": tuple(self._playback_slice_order),
                "discovery_active": self._active_discovery is not None,
                "discovery_pending": self._pending_discovery is not None,
            }
