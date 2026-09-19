"""Small, injectable adapters for revealing a source in a file manager."""

from dataclasses import dataclass
import ntpath
import os
import platform
import posixpath
import shutil
import subprocess


@dataclass(frozen=True)
class RevealResult:
    launched: bool
    selected: bool
    missing: bool = False
    message: str = ""


def _path_module(system):
    """Return the path rules for the requested target platform."""
    return ntpath if system == "windows" else posixpath


def _absolute_source(source_path, *, system, cwd=None):
    """Resolve a path using the requested target platform's path rules."""
    path_module = _path_module(system)
    path = os.fspath(source_path)
    if not path_module.isabs(path):
        base = cwd if cwd is not None else os.getcwd()
        path = path_module.abspath(path_module.join(base, path))
    return path


def _is_unc(path):
    return path.startswith(("\\\\", "//"))


def _platform_name(system=None):
    return (system or platform.system()).lower()


def reveal_source(source_path, *, system=None, cwd=None, popen=None,
                  exists=None, isdir=None, which=None):
    """Launch the native file browser without waiting for it.

    ``exists``/``isdir`` are intentionally skipped for UNC paths: a slow or
    unavailable share must not block the GUI merely to choose a command.
    """
    if source_path is None or str(source_path) == "":
        return RevealResult(False, False, message="This object has no source file.")

    system_name = _platform_name(system)
    path_module = _path_module(system_name)
    path = _absolute_source(source_path, system=system_name, cwd=cwd)
    unc = _is_unc(path)
    exists = exists or os.path.lexists
    isdir = isdir or os.path.isdir
    which = which or shutil.which
    file_exists = True if unc else bool(exists(path))
    parent = path_module.dirname(path) or path_module.curdir
    parent_exists = True if unc else bool(isdir(parent))
    missing = not file_exists
    target = path

    selected = False
    if system_name == "windows":
        executable = "explorer.exe"
        args = [executable, "/select," + target] if file_exists else [executable, parent]
        selected = file_exists
    elif system_name == "darwin":
        executable = "open"
        args = [executable, "-R", target] if file_exists else [executable, parent]
        selected = file_exists
    elif system_name == "linux":
        executable = which("xdg-open") or "xdg-open"
        if not parent_exists:
            return RevealResult(False, False, missing=missing,
                                message="The source file and its containing folder are unavailable.")
        args = [executable, parent]
    else:
        return RevealResult(False, False, missing=missing,
                            message="File-manager reveal is not supported on this platform.")

    if not unc and not file_exists and not parent_exists:
        return RevealResult(False, False, missing=True,
                            message="The source file and its containing folder are unavailable.")

    try:
        (popen or subprocess.Popen)(args)
    except (OSError, subprocess.SubprocessError) as exc:
        return RevealResult(False, False, missing=missing,
                            message=f"Could not launch the file manager: {exc}")

    if missing:
        message = "The source file is missing; opened its containing folder."
    elif system_name == "linux":
        message = "Opened the containing folder; this file manager does not provide native file selection."
    elif selected:
        message = "Reveal requested; file selection is handled by the file manager."
    else:
        message = "Opened the containing folder."
    return RevealResult(True, selected, missing=missing, message=message)
