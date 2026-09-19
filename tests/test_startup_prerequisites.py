import types
import unittest
from unittest import mock

import main


class TestStartupPrerequisites(unittest.TestCase):
    def test_non_windows_does_not_probe_windows_runtime(self):
        loader = mock.Mock(side_effect=AssertionError("loader should not run"))
        with mock.patch.object(main.sys, "platform", "linux"):
            self.assertTrue(main._windows_vc_runtime_available(loader=loader))
        loader.assert_not_called()

    def test_windows_runtime_probe_reports_available_dll(self):
        loader = mock.Mock(return_value=mock.sentinel.runtime)
        with mock.patch.object(main.sys, "platform", "win32"):
            self.assertTrue(main._windows_vc_runtime_available(loader=loader))
        loader.assert_called_once_with("MSVCP140.dll")

    def test_windows_runtime_probe_reports_missing_dll(self):
        loader = mock.Mock(side_effect=OSError("DLL was not found"))
        with mock.patch.object(main.sys, "platform", "win32"):
            self.assertFalse(main._windows_vc_runtime_available(loader=loader))

    def test_missing_runtime_warns_and_stops_before_app_startup(self):
        with (mock.patch.object(main, "_windows_vc_runtime_available",
                                return_value=False),
              mock.patch.object(main, "_show_windows_vc_runtime_warning") as warning):
            self.assertEqual(main.main(), 1)
        warning.assert_called_once_with()

    def test_available_runtime_starts_app_and_returns_its_exit_code(self):
        app = mock.Mock(exit_code=7)
        app_module = types.ModuleType("src.app")
        app_module.NaguMIXApp = mock.Mock(return_value=app)
        with (mock.patch.object(main, "_windows_vc_runtime_available",
                                return_value=True),
              mock.patch.dict(main.sys.modules, {"src.app": app_module})):
            self.assertEqual(main.main(), 7)
        app_module.NaguMIXApp.assert_called_once_with(False)
        app.MainLoop.assert_called_once_with()

    def test_warning_uses_win32_without_importing_wx(self):
        message_box = mock.Mock()
        main._show_windows_vc_runtime_warning(message_box=message_box)
        message_box.assert_called_once()
        _owner, message, title, flags = message_box.call_args.args
        self.assertIn("Visual C++ v14 Redistributable (x64)", message)
        self.assertIn("Microsoft.VCRedist.2015+.x64", message)
        self.assertIn("https://aka.ms/vc14/vc_redist.x64.exe", message)
        self.assertEqual(title, "NaguMIX startup requirement")
        self.assertEqual(flags, 0x00000030)


if __name__ == "__main__":
    unittest.main()
