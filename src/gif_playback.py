"""Bounded, worker-owned GIF playback decoding and pixel transfer."""

from dataclasses import dataclass
import os
import threading
import time

from PIL import Image

from .image_pixels import (
    AnimationFrameMetadata,
    _attach_animation_metadata,
    _source_file_identity,
    normalize_source_pixels,
)


DEFAULT_FRAME_DURATION_MS = 100
PLAYBACK_TRANSFER_BUDGET_BYTES = 32 * 1024 * 1024
PLAYBACK_FRAME_CAP = 3
PLAYBACK_SLICE_FRAMES = 2
PLAYBACK_SLICE_SECONDS = 0.008
PLAYBACK_DECODER_IDLE_SECONDS = 5.0


def effective_frame_duration_ms(value):
    """Honor positive GIF durations and use the confirmed 100 ms fallback."""
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = 0
    return duration if duration > 0 else DEFAULT_FRAME_DURATION_MS


def gif_loop_count(value):
    """Return repeats after the first pass; missing metadata means no repeats."""
    if value is None:
        return None
    try:
        loop = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, loop)


def decoded_payload_bytes(pixels):
    channels = {"RGB": 3, "RGBA": 4}.get(pixels.mode)
    if channels is None:
        raise ValueError("playback transfers must be detached RGB or RGBA pixels")
    width, height = pixels.size
    return int(width) * int(height) * channels


class PlaybackTransferBudget:
    """Synchronously charge detached frames until GUI adoption or discard."""

    def __init__(self, limit_bytes=PLAYBACK_TRANSFER_BUDGET_BYTES,
                 release_callback=None):
        self.limit_bytes = int(limit_bytes)
        self._release_callback = release_callback
        self._lock = threading.Lock()
        self.retained_bytes = 0
        self.maximum_bytes = 0
        self.admissions = 0
        self.rejections = 0
        self.oversize_rejections = 0

    def admit(self, byte_count):
        byte_count = int(byte_count)
        with self._lock:
            if byte_count > self.limit_bytes:
                self.rejections += 1
                self.oversize_rejections += 1
                return "oversize"
            if self.retained_bytes + byte_count > self.limit_bytes:
                self.rejections += 1
                return "blocked"
            self.retained_bytes += byte_count
            self.maximum_bytes = max(self.maximum_bytes, self.retained_bytes)
            self.admissions += 1
            return "admitted"

    def release(self, byte_count, session):
        callback = None
        with self._lock:
            self.retained_bytes -= int(byte_count)
            if self.retained_bytes < 0:
                raise AssertionError("playback transfer budget released twice")
            callback = self._release_callback
        if callback is not None:
            callback(session)

    def rollback_admission(self, byte_count):
        """Undo admission before a transfer packet has been constructed."""
        with self._lock:
            self.retained_bytes -= int(byte_count)
            if self.retained_bytes < 0:
                raise AssertionError("playback transfer budget released twice")

    def snapshot(self):
        with self._lock:
            return {
                "limit_bytes": self.limit_bytes,
                "retained_bytes": self.retained_bytes,
                "maximum_bytes": self.maximum_bytes,
                "admissions": self.admissions,
                "rejections": self.rejections,
                "oversize_rejections": self.oversize_rejections,
            }


class GifDecoderSession:
    """One persistent Pillow GIF handle, accessed and closed serially."""

    def __init__(self, path, expected_source_identity):
        self.path = os.fspath(path)
        self.expected_source_identity = tuple(expected_source_identity)
        self._lock = threading.RLock()
        self._source = None
        self._current_index = None
        self._logical_size = None
        self._frame_count = None
        self._loop_count = None
        self.closed = False

    @property
    def frame_count(self):
        return self._frame_count

    @property
    def loop_count(self):
        return self._loop_count

    def _open_locked(self):
        if self._source is not None:
            return
        identity = _source_file_identity(self.path)
        if identity != self.expected_source_identity:
            raise OSError("the GIF source changed while playback was requested")
        source = Image.open(self.path)
        try:
            if getattr(source, "format", None) != "GIF":
                raise ValueError("the selected source is not a GIF")
            frame_count = int(getattr(source, "n_frames", 1))
            if frame_count <= 1:
                raise ValueError("the selected GIF has only one frame")
            source.seek(0)
            logical_size = tuple(source.size)
            loop_count = gif_loop_count(source.info.get("loop"))
        except Exception:
            source.close()
            raise
        self._source = source
        self._current_index = 0
        self._logical_size = logical_size
        self._frame_count = frame_count
        self._loop_count = loop_count
        self.closed = False

    def decode(self, frame_index):
        """Return one detached complete logical canvas and its metadata."""
        with self._lock:
            self._open_locked()
            if _source_file_identity(self.path) != self.expected_source_identity:
                raise OSError("the GIF source changed during playback")
            index = int(frame_index)
            if not 0 <= index < self._frame_count:
                raise IndexError(
                    f"GIF frame {index} is outside 0..{self._frame_count - 1}")
            self._source.seek(index)
            self._current_index = index
            raw_duration = self._source.info.get("duration", 0)
            metadata = AnimationFrameMetadata(
                frame_count=self._frame_count,
                frame_index=index,
                duration_ms=effective_frame_duration_ms(raw_duration),
                logical_size=self._logical_size,
                source_identity=self.expected_source_identity,
                loop_count=self._loop_count,
            )
            pixels = normalize_source_pixels(self._source, apply_orientation=True)
            if tuple(pixels.size) != self._logical_size:
                pixels.close()
                raise ValueError(
                    "the decoded GIF frame does not match its logical canvas")
            return _attach_animation_metadata(pixels, metadata), metadata

    def close(self):
        with self._lock:
            source = self._source
            self._source = None
            self._current_index = None
            self.closed = True
            if source is not None:
                source.close()


class PlaybackFramePacket:
    """Exactly-once ownership transfer from a decoder slice to the GUI."""

    def __init__(self, session, budget, pixels, metadata, due_time,
                 playback_generation, repeat_index, terminal):
        self.session = session
        self.object_id = session.object_id
        self.source_path = session.path
        self.source_identity = session.source_identity
        self.playback_generation = int(playback_generation)
        self.frame_index = metadata.frame_index
        self.duration_ms = metadata.duration_ms
        self.due_time = float(due_time)
        self.repeat_index = int(repeat_index)
        self.terminal = bool(terminal)
        self._budget = budget
        self._pixels = pixels
        self.byte_count = decoded_payload_bytes(pixels)
        self._released = False
        self._lock = threading.Lock()

    @property
    def pixels(self):
        with self._lock:
            return None if self._released else self._pixels

    def take_pixels(self):
        with self._lock:
            if self._released:
                return None
            pixels = self._pixels
            self._pixels = None
            self._released = True
        self.session.release_transfer()
        self._budget.release(self.byte_count, self.session)
        return pixels

    def close(self):
        pixels = self.take_pixels()
        if pixels is not None:
            pixels.close()


@dataclass
class PlaybackSliceOutcome:
    packets: list
    error: str = None
    blocked: bool = False
    finished: bool = False

    def close(self):
        for packet in self.packets:
            packet.close()
        self.packets.clear()


class PlaybackSession:
    """Object-scoped playback schedule and persistent decoder ownership."""

    def __init__(self, object_id, path, source_identity, playback_generation,
                 frame_count, logical_size, loop_count, displayed_index,
                 displayed_duration_ms, repeats_completed, started_at,
                 *, restart=False, decoder_factory=GifDecoderSession):
        self.object_id = object_id
        self.path = os.fspath(path)
        self.source_identity = tuple(source_identity)
        self.playback_generation = int(playback_generation)
        self.frame_count = int(frame_count)
        self.logical_size = tuple(logical_size)
        self.loop_count = gif_loop_count(loop_count)
        self.repeats_completed = int(repeats_completed)
        self.decoder = decoder_factory(self.path, self.source_identity)
        self._state_lock = threading.Lock()
        self._decode_lock = threading.Lock()
        self.canceled = False
        self.blocked = False
        self.finished = False
        self.queued = False
        self.running = False
        self.deferred = False
        self.retained_frames = 0
        self.max_retained_frames = 0
        self.decoded_frames = 0
        self.slices = 0
        self.last_decode_time = float(started_at)

        if restart:
            self.next_index = 0
            self.next_due_time = float(started_at)
            self.repeats_completed = 0
        else:
            self.next_due_time = (
                float(started_at)
                + effective_frame_duration_ms(displayed_duration_ms) / 1000.0)
            if int(displayed_index) < self.frame_count - 1:
                self.next_index = int(displayed_index) + 1
            elif self._can_repeat(self.repeats_completed):
                self.next_index = 0
                self.repeats_completed += 1
            else:
                # A completed finite animation is restarted by the caller.
                self.next_index = 0
                self.next_due_time = float(started_at)
                self.repeats_completed = 0

    def _can_repeat(self, repeats_completed):
        return self.loop_count == 0 or (
            self.loop_count is not None and repeats_completed < self.loop_count)

    def cancel(self):
        with self._state_lock:
            was_active = not self.canceled
            self.canceled = True
            self.blocked = False
        return was_active

    cancel_before_start = cancel

    def wants_work(self):
        with self._state_lock:
            return (not self.canceled and not self.finished and not self.blocked
                    and not self.queued and not self.running and not self.deferred
                    and self.retained_frames < PLAYBACK_FRAME_CAP)

    def mark_queued(self):
        with self._state_lock:
            if (self.canceled or self.finished or self.blocked
                    or self.queued or self.running
                    or self.retained_frames >= PLAYBACK_FRAME_CAP):
                return False
            self.queued = True
            return True

    def mark_deferred(self):
        with self._state_lock:
            self.queued = False
            if self.canceled or self.finished:
                return False
            self.deferred = True
            return True

    def resume_deferred(self):
        with self._state_lock:
            if not self.deferred or self.canceled or self.finished:
                return False
            self.deferred = False
            return True

    def mark_running(self):
        with self._state_lock:
            self.queued = False
            if self.canceled:
                return False
            self.running = True
            return True

    def mark_slice_complete(self):
        with self._state_lock:
            self.queued = False
            self.running = False

    def release_transfer(self):
        with self._state_lock:
            if self.retained_frames <= 0:
                raise AssertionError("playback frame ownership released twice")
            self.retained_frames -= 1
            self.blocked = False

    def unblock_for_capacity(self):
        with self._state_lock:
            if not self.blocked:
                return False
            self.blocked = False
            return True

    def decode_slice(self, budget, *, clock=time.monotonic):
        packets = []
        error = None
        blocked = False
        started = clock()
        with self._decode_lock:
            with self._state_lock:
                if self.canceled:
                    self.decoder.close()
                    return PlaybackSliceOutcome([], finished=True)
                self.slices += 1
            for _ in range(PLAYBACK_SLICE_FRAMES):
                with self._state_lock:
                    if (self.canceled or self.finished
                            or self.retained_frames >= PLAYBACK_FRAME_CAP):
                        break
                    frame_index = self.next_index
                    due_time = self.next_due_time
                    repeat_index = self.repeats_completed
                try:
                    pixels, metadata = self.decoder.decode(frame_index)
                    byte_count = decoded_payload_bytes(pixels)
                    admission = budget.admit(byte_count)
                    if admission != "admitted":
                        pixels.close()
                        if admission == "oversize":
                            error = (
                                "GIF frame needs more than the 32 MiB playback "
                                "transfer budget; playback was paused, but exact "
                                "frame stepping remains available")
                            with self._state_lock:
                                self.finished = True
                        else:
                            blocked = True
                            with self._state_lock:
                                self.blocked = True
                        break

                    with self._state_lock:
                        if self.canceled:
                            budget.rollback_admission(byte_count)
                            pixels.close()
                            break
                        terminal = (frame_index == self.frame_count - 1
                                    and not self._can_repeat(repeat_index))
                        self.retained_frames += 1
                        self.max_retained_frames = max(
                            self.max_retained_frames, self.retained_frames)
                        packet = PlaybackFramePacket(
                            self, budget, pixels, metadata, due_time,
                            self.playback_generation, repeat_index, terminal)
                        packets.append(packet)
                        self.decoded_frames += 1
                        if terminal:
                            self.finished = True
                        elif frame_index == self.frame_count - 1:
                            self.next_index = 0
                            self.repeats_completed += 1
                            self.next_due_time = (
                                due_time + metadata.duration_ms / 1000.0)
                        else:
                            self.next_index = frame_index + 1
                            self.next_due_time = (
                                due_time + metadata.duration_ms / 1000.0)
                except Exception as exc:
                    error = str(exc)
                    with self._state_lock:
                        self.finished = True
                    break
                if clock() - started >= PLAYBACK_SLICE_SECONDS:
                    break
            with self._state_lock:
                canceled = self.canceled
                finished = self.finished
            if canceled or finished or blocked or error:
                self.decoder.close()
        return PlaybackSliceOutcome(
            packets, error=error, blocked=blocked,
            finished=finished or canceled)

    def close_decoder(self):
        with self._decode_lock:
            self.decoder.close()

    def retire_decoder_if_idle(self, now, idle_seconds=PLAYBACK_DECODER_IDLE_SECONDS):
        """Close only idle decoder state while preserving playback intent."""
        with self._state_lock:
            if (self.running or self.queued or self.canceled
                    or float(now) - self.last_decode_time < idle_seconds):
                return False
        self.close_decoder()
        return True

    def record_decode_time(self, now):
        with self._state_lock:
            self.last_decode_time = float(now)


class PlaybackSliceTask:
    """A finite scheduler work item for one persistent playback session."""

    def __init__(self, session, budget, clock=time.monotonic):
        self.session = session
        self._budget = budget
        self._clock = clock

    def cancel(self):
        return self.session.cancel()

    def cancel_before_start(self):
        changed = self.session.cancel()
        self.session.close_decoder()
        return changed

    def run(self):
        if not self.session.mark_running():
            return PlaybackSliceOutcome([], finished=True)
        try:
            outcome = self.session.decode_slice(self._budget, clock=self._clock)
            self.session.record_decode_time(self._clock())
            return outcome
        finally:
            self.session.mark_slice_complete()
