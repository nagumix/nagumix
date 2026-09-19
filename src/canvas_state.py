"""Validation and atomic persistence for the legacy canvas-state list."""

import json
import math
import os
import re
import tempfile


class CanvasStateError(ValueError):
    """A document cannot safely be interpreted as canvas state."""


MAX_PIXEL_VALUE = 2**31 - 1  # wx integer coordinate range
MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def validate_state(data):
    """Return validated records without changing field meanings or paths."""
    if isinstance(data, dict) and "schema_version" in data:
        raise CanvasStateError(
            f"Unsupported canvas schema version {data['schema_version']!r}. "
            "This release reads the legacy JSON list format."
        )
    if not isinstance(data, list):
        raise CanvasStateError("Canvas state must be a JSON list of image objects.")
    records = []
    for index, item in enumerate(data, 1):
        prefix = f"Image {index}"
        if not isinstance(item, dict):
            raise CanvasStateError(f"{prefix}: expected an object.")

        def pixel(name, value, minimum):
            if type(value) is not int or not minimum <= value <= MAX_PIXEL_VALUE:
                raise CanvasStateError(
                    f"{prefix}: {name} must be an integer between "
                    f"{minimum} and {MAX_PIXEL_VALUE}."
                )
            return value

        path = item.get("source_path")
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            raise CanvasStateError(f"{prefix}: source_path must be a nonempty file path.")
        record = {"source_path": path}
        for name in ("x", "y", "width", "height"):
            minimum = -MAX_PIXEL_VALUE if name in ("x", "y") else 1
            record[name] = pixel(name, item.get(name), minimum)
        zoom = item.get("zoom_factor")
        try:
            valid_zoom = (type(zoom) in (int, float) and
                          math.isfinite(zoom) and 0 < zoom <= 5)
        except OverflowError:
            valid_zoom = False
        if not valid_zoom:
            raise CanvasStateError(f"{prefix}: zoom_factor must be finite, above 0 and at most 5.")
        record["zoom_factor"] = zoom
        offset = item.get("viewport_offset")
        if not isinstance(offset, (list, tuple)) or len(offset) != 2:
            raise CanvasStateError(f"{prefix}: viewport_offset must contain two integers.")
        record["viewport_offset"] = [pixel("viewport_offset", value, 0) for value in offset]
        normalization = item.get("source_pixel_normalization")
        if normalization is not None:
            if type(normalization) is not int or normalization != 1:
                raise CanvasStateError(
                    f"{prefix}: unsupported source_pixel_normalization "
                    f"{normalization!r}."
                )
            record["source_pixel_normalization"] = normalization
        if "source_file" in item:
            source_file = item["source_file"]
            if not isinstance(source_file, dict):
                raise CanvasStateError(f"{prefix}: source_file must be an object.")
            size = source_file.get("size_bytes")
            if type(size) is not int or size < 0:
                raise CanvasStateError(
                    f"{prefix}: source_file size_bytes must be a nonnegative integer.")
            digest = source_file.get("md5")
            if not isinstance(digest, str) or MD5_PATTERN.fullmatch(digest) is None:
                raise CanvasStateError(
                    f"{prefix}: source_file md5 must be 32 lowercase hexadecimal characters.")
            # Like unknown object-level fields in the legacy list, unknown
            # metadata fields are ignored when the record is canonicalized.
            record["source_file"] = {"size_bytes": size, "md5": digest}
        if "animation" in item:
            animation = item["animation"]
            if not isinstance(animation, dict):
                raise CanvasStateError(f"{prefix}: animation must be an object.")
            fields = {"type", "frame_index", "paused"}
            if set(animation) != fields:
                raise CanvasStateError(
                    f"{prefix}: animation must contain exactly type, "
                    "frame_index and paused."
                )
            if animation["type"] != "gif":
                raise CanvasStateError(
                    f"{prefix}: unsupported animation type "
                    f"{animation['type']!r}."
                )
            frame_index = animation["frame_index"]
            if (type(frame_index) is not int
                    or not 0 <= frame_index <= MAX_PIXEL_VALUE):
                raise CanvasStateError(
                    f"{prefix}: animation frame_index must be an integer "
                    f"between 0 and {MAX_PIXEL_VALUE}."
                )
            if animation["paused"] is not True:
                raise CanvasStateError(
                    f"{prefix}: animation paused must be true."
                )
            record["animation"] = {
                "type": "gif",
                "frame_index": frame_index,
                "paused": True,
            }
        records.append(record)
    return records


def read_state(path):
    with open(path, "r", encoding="utf-8") as stream:
        return validate_state(json.load(stream))


def write_state(path, data, cancellation=None):
    """Replace the destination only after validation and a complete disk write."""
    records = validate_state(data)
    destination = os.path.abspath(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(destination),
                prefix=".nagumix-state-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(records, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if cancellation is None:
            os.replace(temporary, destination)
        else:
            cancellation.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
