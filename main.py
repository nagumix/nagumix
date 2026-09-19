# main.py
import ctypes
import sys

from src.branding import APP_NAME


_WINDOWS_VC_RUNTIME_DLL = "MSVCP140.dll"
_WINDOWS_VC_REDIST_URL = "https://aka.ms/vc14/vc_redist.x64.exe"
_WINDOWS_VC_REDIST_WINGET_COMMAND = (
    "winget install --id Microsoft.VCRedist.2015+.x64 --exact --source winget"
)
_WINDOWS_VC_RUNTIME_MESSAGE = (
    f"{APP_NAME} needs the Microsoft Visual C++ v14 Redistributable (x64) "
    "to start.\n\n"
    f"Install the current x64 Redistributable, then start {APP_NAME} again.\n\n"
    f"With winget:\n{_WINDOWS_VC_REDIST_WINGET_COMMAND}\n\n"
    f"Or download it from Microsoft:\n{_WINDOWS_VC_REDIST_URL}"
)


def _windows_vc_runtime_available(loader=None):
    """Return whether the Windows C++ runtime required by wxPython can load."""
    if sys.platform != "win32":
        return True
    if loader is None:
        loader = ctypes.WinDLL
    try:
        loader(_WINDOWS_VC_RUNTIME_DLL)
    except OSError:
        return False
    return True


def _show_windows_vc_runtime_warning(message_box=None):
    """Show a wx-independent warning while the native GUI runtime is missing."""
    try:
        if message_box is None:
            message_box = ctypes.windll.user32.MessageBoxW
        # MB_OK | MB_ICONWARNING
        message_box(None, _WINDOWS_VC_RUNTIME_MESSAGE,
                    f"{APP_NAME} startup requirement", 0x00000030)
    except Exception:
        print(_WINDOWS_VC_RUNTIME_MESSAGE, file=sys.stderr)


def main():
    if not _windows_vc_runtime_available():
        _show_windows_vc_runtime_warning()
        return 1

    # Keep wx imports after the native-runtime check so a missing runtime can
    # still produce a readable Win32 warning in source and portable launches.
    from src.app import NaguMIXApp

    app = NaguMIXApp(False)  # False => don't redirect stdout/stderr
    app.MainLoop()
    # Exit with the code set by the application
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
