"""Runtime-only shared filename suggestions for canvas documents."""

import os
from datetime import datetime

RELEVANT_SUFFIXES = frozenset((".json", ".png", ".jpg", ".jpeg", ".webp", ".bmp"))


def filename_base(path):
    name = os.path.basename(os.fspath(path))
    root, suffix = os.path.splitext(name)
    return root if suffix.lower() in RELEVANT_SUFFIXES else name


def timestamp_base(clock=datetime.now):
    return clock().strftime("nagumix-%Y-%m-%d-%H%M")


def suggested_filename(base, suffix, clock=datetime.now):
    base = base or timestamp_base(clock)
    suffix = str(suffix)
    if not suffix.startswith("."):
        suffix = "." + suffix
    return base + suffix
