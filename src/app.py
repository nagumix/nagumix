# src/app.py
import wx
import os
import sys
from .main_frame import MainFrame
from .settings_manager import SettingsManager
from .native_style import AppearanceController
from .branding import APP_NAME
import logging


class NaguMIXApp(wx.App):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exit_code = 0  # Track desired exit code

    def OnInit(self):
        """
        Called upon app initialization. Creates the main frame and shows it.
        """

        # Initialize logging logging to stdout
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s - %(levelname)s - %(message)s')
        logging.info("%s started", APP_NAME)

        # Initialize settings manager (loads .ini, sets defaults, etc.)
        self.settings_manager = SettingsManager()
        # wxMSW requires the initial native request before creating windows.
        self.dialog_appearance = AppearanceController(self, self.settings_manager)

        # Detect if running in debugger/development mode first
        is_debug = (hasattr(sys, 'gettrace') and sys.gettrace() is not None) or ('--debug' in sys.argv) or ('debugpy' in sys.modules) or ('pdb' in sys.modules) or ('VSCODE_PID' in os.environ)

        # Create main frame with appropriate style for debug vs production
        if is_debug:
            # Use standard window decorations for debug mode
            frame_title = f"{APP_NAME} (DEBUG)"
            logging.info("Debug mode detected - using standard window style")
        else:
            # Use borderless style for fullscreen mode
            frame_title = APP_NAME

        self.frame = MainFrame(None, title=frame_title,
                               settings_manager=self.settings_manager, debug_mode=is_debug)
        self.SetTopWindow(self.frame)
        self.dialog_appearance.attach(self.frame)

        if is_debug:
            # Show in windowed mode for debugging
            logging.info("Debug mode detected - starting in windowed mode")

            # Log initial state
            logging.debug(f"Frame style: {self.frame.GetWindowStyleFlag()}")
            logging.debug(f"Frame visible before setup: {self.frame.IsShown()}")

            self.frame.SetSize((1200, 800))
            self.frame.Center()

            # Force window visibility and rendering
            self.frame.Show(True)
            logging.debug(f"Frame visible after Show(True): {self.frame.IsShown()}")

            self.frame.Raise()
            self.frame.SetFocus()

            # Additional visibility fixes for debug mode
            self.frame.Iconize(False)  # Ensure not minimized
            self.frame.Maximize(False)  # Ensure not maximized (can cause rendering issues)

            # Force a refresh and update
            self.frame.Refresh()
            self.frame.Update()

            # Try to bring to foreground (Windows specific)
            try:
                import ctypes
                hwnd = self.frame.GetHandle()
                logging.debug(f"Window handle: {hwnd}")
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.BringWindowToTop(hwnd)

                # Additional Windows API calls to force visibility
                ctypes.windll.user32.ShowWindow(hwnd, 1)  # SW_SHOW
                ctypes.windll.user32.UpdateWindow(hwnd)
            except Exception as e:
                logging.debug(f"Could not force window to foreground: {e}")

            # Final position and size check
            pos = self.frame.GetPosition()
            size = self.frame.GetSize()
            logging.info(f"Debug window final position: {pos}, size: {size}")
            logging.info("Debug window visibility fixes applied")
        else:
            # By default, show in fullscreen (no OS window decorations)
            logging.info("Starting in fullscreen mode")
            self.frame.ShowFullScreen(True, style=wx.FULLSCREEN_ALL)
            self.frame.Show()

        return True

    def set_exit_code(self, code):
        """Set the desired exit code for the application."""
        self.exit_code = code

    def OnExit(self):
        controller = getattr(self, "dialog_appearance", None)
        if controller:
            controller.close()
        return 0
