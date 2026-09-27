"""Writable runtime locations for source and frozen application launches."""

from pathlib import Path
import os
import sys


def is_frozen():
    """Return whether the process is running from a frozen application bundle."""
    return bool(getattr(sys, "frozen", False))


def frozen_user_directory(environ=None):
    """Return the per-user writable directory used by frozen Windows builds."""
    environ = os.environ if environ is None else environ
    local_app_data = environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NaguMIX"
    return Path.home() / "AppData" / "Local" / "NaguMIX"


def settings_path():
    """Keep source compatibility while making frozen settings cwd-independent."""
    if is_frozen():
        return frozen_user_directory() / "nagumix_settings.ini"
    return Path("nagumix_settings.ini")


def diagnostics_path():
    """Return a persistent diagnostic log path for frozen builds."""
    return frozen_user_directory() / "nagumix.log"
