"""Shared source-pixel loading, normalization, and lifetime ownership."""

import logging
import os
import threading
from dataclasses import dataclass

from PIL import Image, ImageOps, features


SOURCE_PIXEL_NORMALIZATION_VERSION = 1
ANIMATION_METADATA_KEY = "_nagumix_animation_metadata"
SOURCE_IDENTITY_METADATA_KEY = "_nagumix_source_identity"

_HEIF_EXTENSIONS = {".heic", ".heif"}
_HEIF_REGISTRATION_LOCK = threading.Lock()
_heif_registration_result = None


@dataclass(frozen=True)
class AnimationFrameMetadata:
    """Immutable GIF metadata carried with one detached, composited frame."""

    frame_count: int
    frame_index: int
    duration_ms: int
    logical_size: tuple
    source_identity: tuple
    # GIF repeat count after the first pass. Missing metadata is ``None``;
    # zero means repeat indefinitely.
    loop_count: object = None


class AnimationSourceChangedError(OSError):
    """Identify whether a GIF changed before or during exact reconstruction."""

    def __init__(self, message, *, phase):
        super().__init__(message)
        self.phase = phase


def get_animation_metadata(image):
    """Return trusted runtime metadata attached by the shared decoder."""
    if image is None:
        return None
    metadata = image.info.get(ANIMATION_METADATA_KEY)
    return metadata if isinstance(metadata, AnimationFrameMetadata) else None


def source_file_identity(path, stat_result=None):
    """Capture a cheap worker-owned identity for stale-source validation."""
    native_path = os.fspath(path)
    stat_result = os.stat(native_path) if stat_result is None else stat_result
    absolute_path = os.path.abspath(native_path)
    # Windows network providers do not all expose a stable device/file ID for
    # UNC paths. macOS SMB shares in particular can report different IDs across
    # separate metadata reads. Path, size, and modification time remain useful
    # change detectors without turning those provider details into false
    # "source changed" failures.
    is_unc = native_path.startswith(("\\\\", "//")) or absolute_path.startswith(
        ("\\\\", "//"))
    return (
        os.path.normcase(absolute_path),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        0 if is_unc else int(getattr(stat_result, "st_dev", 0)),
        0 if is_unc else int(getattr(stat_result, "st_ino", 0)),
    )


def _source_file_identity(path):
    """Compatibility alias retained for decoder integrations."""
    return source_file_identity(path)


def get_source_identity(image):
    """Return the file identity attached to detached decoded source pixels."""
    if image is None:
        return None
    identity = image.info.get(SOURCE_IDENTITY_METADATA_KEY)
    if isinstance(identity, tuple):
        return identity
    animation = get_animation_metadata(image)
    return tuple(animation.source_identity) if animation is not None else None


def _attach_source_identity(pixels, identity):
    pixels.info[SOURCE_IDENTITY_METADATA_KEY] = tuple(identity)
    return pixels


def _gif_frame_metadata(source, path, frame_index):
    """Read GIF sequence metadata after Pillow has positioned the decoder."""
    frame_count = int(getattr(source, "n_frames", 1))
    if frame_count <= 1:
        return None
    duration = source.info.get("duration", 0)
    try:
        duration_ms = max(0, int(duration))
    except (TypeError, ValueError):
        duration_ms = 0
    raw_loop = source.info.get("loop")
    try:
        loop_count = None if raw_loop is None else max(0, int(raw_loop))
    except (TypeError, ValueError):
        loop_count = None
    return AnimationFrameMetadata(
        frame_count=frame_count,
        frame_index=int(frame_index),
        duration_ms=duration_ms,
        logical_size=tuple(source.size),
        source_identity=_source_file_identity(path),
        loop_count=loop_count,
    )


def _attach_animation_metadata(pixels, metadata):
    if metadata is not None:
        pixels.info[ANIMATION_METADATA_KEY] = metadata
    return pixels


def _avif_decoder_available():
    """Return whether this Pillow build exposes its AVIF codec."""
    try:
        return features.check("avif") is True
    except (AttributeError, ValueError):
        # Older Pillow versions do not know the feature name. They cannot
        # provide the decoder required by the .avif source contract.
        return False


def _heif_decoder_available():
    """Register and report the optional libheif-backed Pillow decoder.

    Registration is deliberately lazy so existing formats still work when a
    deployment omits the optional codec.  The lock protects both capability
    probing and Pillow's process-global registration tables when multiple
    decode workers encounter HEIF files together.
    """
    global _heif_registration_result
    if _heif_registration_result is not None:
        return _heif_registration_result
    with _HEIF_REGISTRATION_LOCK:
        if _heif_registration_result is not None:
            return _heif_registration_result
        try:
            import pillow_heif

            lib_info = pillow_heif.libheif_info()
            if not lib_info.get("decoders"):
                _heif_registration_result = False
            else:
                pillow_heif.register_heif_opener(
                    thumbnails=False, depth_images=False, aux_images=False)
                _heif_registration_result = True
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logging.warning("HEIC/HEIF decoding is unavailable: %s", exc)
            _heif_registration_result = False
        return _heif_registration_result


_HEIF_ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


class SourcePixelLease:
    """One exactly-once lease on detached source pixels."""

    def __init__(self, owner):
        self._owner = owner
        self._released = False

    @property
    def pixels(self):
        if self._released:
            return None
        return self._owner.pixels

    def release(self):
        if self._released:
            return False
        self._released = True
        owner = self._owner
        self._owner = None
        owner._release_reference()
        return True

    close = release


class SharedSourcePixels:
    """Reference-counted ownership for one immutable Pillow pixel revision.

    Canvas objects own the initial reference. Background consumers take a
    lease before the canvas owner can be retired by replacement or deletion.
    """

    def __init__(self, pixels):
        if pixels is None:
            raise ValueError("source pixels are required")
        self._pixels = pixels
        self._references = 1
        self._owner_released = False
        self._lock = threading.Lock()

    @property
    def pixels(self):
        with self._lock:
            return self._pixels

    def lease(self):
        with self._lock:
            if self._pixels is None or self._owner_released and self._references == 0:
                return None
            self._references += 1
        return SourcePixelLease(self)

    def release_owner(self):
        with self._lock:
            if self._owner_released:
                return False
            self._owner_released = True
        self._release_reference()
        return True

    def _release_reference(self):
        pixels = None
        with self._lock:
            if self._references <= 0:
                return
            self._references -= 1
            if self._references == 0:
                pixels = self._pixels
                self._pixels = None
        if pixels is not None:
            try:
                pixels.close()
            except Exception as exc:
                logging.warning("Failed to release source pixels: %s", exc)


def normalize_source_pixels(image, *, apply_orientation=True,
                            orientation_override=None):
    """Return detached, static RGB/RGBA pixels from a Pillow image.

    The caller keeps ownership of ``image``. Animated and multipage inputs are
    expected to be positioned at frame/page zero by the file loader.
    """
    working = image.copy()
    working.load()
    if apply_orientation:
        if orientation_override in _HEIF_ORIENTATION_TRANSPOSE:
            oriented = working.transpose(
                _HEIF_ORIENTATION_TRANSPOSE[orientation_override])
            working.close()
            working = oriented
        else:
            working = ImageOps.exif_transpose(working)

    has_alpha = working.mode in ("RGBA", "LA") or (
        working.mode == "P" and "transparency" in working.info
    ) or "A" in working.getbands()
    normalized = working.convert("RGBA" if has_alpha else "RGB")
    normalized.load()

    # exif_transpose removes the applied tag. Clear it explicitly as well so a
    # later normalization pass cannot apply orientation a second time.
    normalized.info.pop("exif", None)
    normalized.info.pop("orientation", None)
    detached = normalized.copy()
    source_identity = get_source_identity(image)
    animation_metadata = get_animation_metadata(image)
    if animation_metadata is not None:
        _attach_animation_metadata(detached, animation_metadata)
    if source_identity is not None:
        _attach_source_identity(detached, source_identity)
    return detached


def load_source_pixels(path, *, apply_orientation=True, capture_animation=True):
    """Load a detached still image using the format's frame policy.

    HEIC/HEIF Pillow plugin instances open at the container's designated
    primary image, so they intentionally do not seek to frame zero.  Other
    animated/multipage formats retain the existing frame-zero policy. Shared
    worker callers capture animated-GIF metadata; the legacy synchronous paint
    fallback can disable that sequence scan while still decoding frame zero.
    """
    source_identity = source_file_identity(path)
    suffix = os.path.splitext(os.fspath(path))[1].lower()
    is_heif = suffix in _HEIF_EXTENSIONS
    if is_heif and not _heif_decoder_available():
        raise OSError(
            "HEIC/HEIF decoding is unavailable; install a pillow-heif wheel "
            "with a libheif decoder"
        )
    if suffix == ".avif":
        if not _avif_decoder_available():
            raise OSError(
                "AVIF decoding is unavailable in this Pillow build; install "
                "a Pillow wheel with libavif support (Pillow 11.3 or newer)"
            )
    with Image.open(path) as source:
        if not is_heif:
            source.seek(0)
        animation_metadata = (
            _gif_frame_metadata(source, path, 0)
            if (capture_animation
                and getattr(source, "format", None) == "GIF")
            else None
        )
        heif_orientation = (
            source.info.get("original_orientation")
            if is_heif and getattr(source, "format", None) == "HEIF"
            else None
        )
        pixels = normalize_source_pixels(
            source,
            apply_orientation=apply_orientation,
            orientation_override=heif_orientation,
        )
    final_identity = source_file_identity(path)
    if final_identity != source_identity:
        pixels.close()
        raise OSError("the source changed while it was decoded")
    return _attach_source_identity(
        _attach_animation_metadata(pixels, animation_metadata), final_identity)


def load_animation_frame(path, frame_index, *, expected_source_identity=None):
    """Reconstruct one complete animated-GIF logical canvas off-thread.

    Pillow's GIF decoder applies palette, transparency, partial-frame, and
    disposal semantics while seeking. Opening a fresh decoder per request
    deliberately avoids mutable seek state and bounds retained memory to the
    requested reconstruction plus the returned detached frame.
    """
    native_path = os.fspath(path)
    if os.path.splitext(native_path)[1].lower() != ".gif":
        raise ValueError("frame stepping is supported only for animated GIFs")
    source_identity = _source_file_identity(native_path)
    if (expected_source_identity is not None
            and tuple(expected_source_identity) != source_identity):
        raise AnimationSourceChangedError(
            "the GIF source changed while its frame was requested",
            phase="before",
        )
    with Image.open(native_path) as source:
        if getattr(source, "format", None) != "GIF":
            raise ValueError("the selected source is not a GIF")
        frame_count = int(getattr(source, "n_frames", 1))
        if frame_count <= 1:
            raise ValueError("the selected GIF has only one frame")
        if not 0 <= int(frame_index) < frame_count:
            raise IndexError(
                f"GIF frame {frame_index} is outside 0..{frame_count - 1}")
        source.seek(int(frame_index))
        metadata = _gif_frame_metadata(source, native_path, int(frame_index))
        if metadata.source_identity != source_identity:
            raise AnimationSourceChangedError(
                "the GIF source changed while its frame was decoded",
                phase="during",
            )
        pixels = normalize_source_pixels(source, apply_orientation=True)
        if tuple(pixels.size) != metadata.logical_size:
            pixels.close()
            raise ValueError("the decoded GIF frame does not match its logical canvas")
        return _attach_source_identity(
            _attach_animation_metadata(pixels, metadata), metadata.source_identity)
