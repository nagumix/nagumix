"""Focused persistence, dialog, and settings-application boundaries."""
import copy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import wx

from src.main_frame import MainFrame
from src.native_style import AppearanceController, DARK_COLORS, LIGHT_COLORS
from src.settings_dialog import SettingsDialog
from src.settings_manager import APPEARANCE_MODES, SettingsManager


class OwnedSettings(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.directory.name)
        self.manager = SettingsManager()

    def tearDown(self):
        os.chdir(self.previous)
        self.directory.cleanup()


class TestPersistence(OwnedSettings):
    def test_appearance_default_invalid_normalization_and_round_trip(self):
        self.assertEqual(self.manager.get_appearance_mode(), "system")
        for value in ("", "sepia", None):
            self.manager.set_setting("UI", "appearance_mode", value)
            self.assertEqual(self.manager.get_appearance_mode(), "system")
        for value in APPEARANCE_MODES:
            self.manager.set_appearance_mode(" " + value.upper() + " ")
            self.manager.save()
            self.assertEqual(SettingsManager().get_appearance_mode(), value)
        with self.assertRaises(ValueError):
            self.manager.set_appearance_mode("sepia")

    def test_all_values_round_trip_and_unknown_ini_survives(self):
        self.manager.set_setting("Navigation", "preload_cache_mb", "731")
        self.manager.set_setting("External", "custom", "retained")
        draft = dict(background="#123456", mode="dark", timeout=9876,
                     position="bottom_right", wheel=False, sort="size_desc",
                     preload=5, spacing=0, margin=999,
                     include_file_identification=False)
        self.manager.save_dialog_draft(draft)
        loaded = SettingsManager()
        self.assertEqual(loaded.get_dialog_draft(), draft)
        self.assertEqual(loaded.get_preload_cache_mb(), 731)
        self.assertEqual(loaded.get_setting("External", "custom"), "retained")

    def test_complete_validation_does_not_mutate_earlier_valid_fields(self):
        original = self.manager.get_dialog_draft()
        invalid = dict(timeout=99, preload=6, spacing=-1, margin=1001,
                       mode="nope", sort="nope", position="nope", wheel=1,
                       include_file_identification=1)
        for key, value in invalid.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.manager.save_dialog_draft({**original, "background": "#123456", key: value})
            self.assertEqual(self.manager.get_dialog_draft(), original)
            self.assertFalse(Path(self.manager.default_file_name).exists())

    def test_replace_failure_preserves_disk_memory_and_path_then_retry(self):
        self.manager.save()
        path = Path(self.manager.loaded_path)
        data = path.read_bytes()
        original = self.manager.get_dialog_draft()
        with mock.patch("src.settings_manager.os.replace", side_effect=OSError("write denied")):
            with self.assertRaises(OSError):
                self.manager.save_dialog_draft({**original, "mode": "dark", "margin": 72})
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.manager.get_dialog_draft(), original)
        self.assertEqual(list(Path('.').glob('*.tmp')), [])
        self.manager.save_dialog_draft({**original, "margin": 72})
        self.assertEqual(SettingsManager().get_dialog_draft()["margin"], 72)


class TestApplicationBoundary(OwnedSettings):
    def frame(self):
        canvas = SimpleNamespace(on_settings_changed=mock.Mock(), Refresh=mock.Mock(),
                                 canvas_bg="#FFFFFF")
        return SimpleNamespace(settings_manager=self.manager, canvas_panel=canvas)

    def test_unchanged_and_cosmetic_changes_do_not_enter_cancellation_path(self):
        frame = self.frame()
        before = self.manager.application_snapshot()
        MainFrame._apply_accepted_settings(frame, before)
        frame.canvas_panel.Refresh.assert_not_called()
        for key, value in dict(mode="dark", position="bottom_left", timeout=1600,
                               background="#ABCDEF", spacing=14, margin=22, wheel=False).items():
            before = self.manager.application_snapshot()
            self.manager.save_dialog_draft({**self.manager.get_dialog_draft(), key: value})
            MainFrame._apply_accepted_settings(frame, before)
        # The only entry to cache clear / navigation, export, scene, duplication
        # cancellation and GIF work retirement is never called by these saves.
        frame.canvas_panel.on_settings_changed.assert_not_called()
        self.assertEqual(frame.canvas_panel.canvas_bg, "#ABCDEF")
        self.assertEqual(frame.canvas_panel.Refresh.call_count, 3)

    def test_sort_preload_and_budget_changes_keep_existing_invalidation(self):
        frame = self.frame()
        for key, value in (("sort_method", "date_desc"), ("preload_count", "4"),
                           ("preload_cache_mb", "12")):
            before = self.manager.application_snapshot()
            self.manager.set_setting("Navigation", key, value)
            MainFrame._apply_accepted_settings(frame, before)
        self.assertEqual(frame.canvas_panel.on_settings_changed.call_count, 3)

    def test_modal_cancel_never_applies_and_always_destroys(self):
        frame = self.frame()
        dialog = mock.Mock()
        dialog.ShowModal.return_value = wx.ID_CANCEL
        with mock.patch("src.main_frame.SettingsDialog", return_value=dialog):
            MainFrame.on_open_settings(frame, None)
        dialog.Destroy.assert_called_once()
        frame.canvas_panel.on_settings_changed.assert_not_called()
        frame.canvas_panel.Refresh.assert_not_called()


class FakeWindow:
    def __init__(self):
        self.bindings = {}
        self.palettes = []

    def Bind(self, event, handler):
        self.bindings[event] = handler

    def Unbind(self, event, handler):
        self.bindings.pop(event, None)

    def IsBeingDeleted(self):
        return False

    def apply_dialog_appearance(self, palette):
        self.palettes.append(palette)


class TestAppearanceLifecycle(OwnedSettings):
    def controller(self):
        app = mock.Mock()
        app.SetAppearance.return_value = wx.App.AppearanceResult.Ok
        self.signal = False
        result = AppearanceController(app, self.manager, lambda: self.signal)
        self.addCleanup(result.close)
        return result, app

    def test_system_events_coalesce_do_not_persist_or_recurse_and_detach(self):
        controller, app = self.controller()
        window = FakeWindow()
        controller.attach(window)
        queued = []
        self.signal = True
        event = mock.Mock()
        with mock.patch("src.native_style.wx.CallAfter", side_effect=queued.append):
            for _ in range(3):
                controller.on_system_changed(event)
        self.assertEqual(len(queued), 1)
        queued.pop()()
        self.assertEqual(window.palettes, [LIGHT_COLORS, DARK_COLORS])
        self.assertEqual(app.SetAppearance.call_count, 1)
        self.assertFalse(Path(self.manager.default_file_name).exists())
        destroy = mock.Mock()
        destroy.GetEventObject.return_value = window
        window.bindings[wx.EVT_WINDOW_DESTROY](destroy)
        self.assertFalse(controller._windows)
        self.assertFalse(window.bindings)
        controller.close()
        controller._flush_system_change()

    def test_saved_explicit_modes_ignore_os_and_style_existing_and_future_dialogs(self):
        controller, app = self.controller()
        current = FakeWindow()
        controller.attach(current)
        self.manager.set_appearance_mode("dark")
        controller.apply_saved()
        self.signal = False
        controller.on_system_changed(mock.Mock())
        controller._flush_system_change()
        future = FakeWindow()
        controller.attach(future)
        self.assertEqual(current.palettes[-1], DARK_COLORS)
        self.assertEqual(future.palettes, [DARK_COLORS])
        self.manager.set_appearance_mode("system")
        controller.apply_saved()
        self.assertEqual(current.palettes[-1], LIGHT_COLORS)
        self.assertEqual(app.SetAppearance.call_count, 3)

    def test_unsupported_native_change_keeps_coherent_active_mode_until_restart(self):
        controller, app = self.controller()
        app.SetAppearance.return_value = wx.App.AppearanceResult.CannotChange
        self.manager.set_appearance_mode("dark")
        controller.apply_saved()
        self.assertEqual(controller.palette, LIGHT_COLORS)
        self.assertEqual(controller.mode, "system")
        self.assertEqual(controller.pending_mode, "dark")
        self.assertEqual(controller.native_result, wx.App.AppearanceResult.CannotChange)
        restarted_app = mock.Mock()
        restarted_app.SetAppearance.return_value = wx.App.AppearanceResult.Ok
        restarted = AppearanceController(restarted_app, self.manager, lambda: False)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.mode, "dark")
        self.assertEqual(restarted.palette, DARK_COLORS)
        self.assertIsNone(restarted.pending_mode)
        # The active System mode still follows the OS while the saved explicit
        # preference is pending. Reverting to active mode clears the pending mode.
        self.signal = True
        controller._flush_system_change()
        self.assertEqual(controller.palette, DARK_COLORS)
        self.manager.set_appearance_mode("system")
        controller.apply_saved()
        self.assertIsNone(controller.pending_mode)


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1", "Set NAGUMIX_GUI_TESTS=1")
class TestNativeDialog(OwnedSettings):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def dialog(self):
        dialog = SettingsDialog(None, self.manager)
        self.addCleanup(lambda: dialog.Destroy() if dialog else None)
        return dialog

    def test_unvisited_pages_and_drafts_across_categories(self):
        self.manager.set_sort_method("size_desc")
        self.manager.set_arrangement_settings(33, 44)
        self.manager.set_setting("Navigation", "preload_count", "5")
        dialog = self.dialog()
        self.assertEqual(dialog.built_pages, {0})
        dialog.bg_color_text.SetValue("#112233")
        with mock.patch.object(dialog, "EndModal"):
            dialog.on_ok(None)
        self.assertEqual(SettingsManager().get_sort_method(), "size_desc")
        self.assertEqual(SettingsManager().get_arrangement_settings(), {"spacing": 33, "outer_margin": 44})
        self.assertEqual(SettingsManager().get_dialog_draft()["preload"], 5)
        dialog.select_page(2)
        dialog.arrangement_spacing_spin.SetValue(88)
        dialog.select_page(1)
        dialog.select_page(2)
        self.assertEqual(dialog.arrangement_spacing_spin.GetValue(), 88)

    def test_background_picker_sync_and_cancel_preserve_saved_color(self):
        dialog = self.dialog()
        dialog.bg_color_text.SetValue('#123456')
        self.assertEqual(dialog.color_picker.GetColour(), wx.Colour('#123456'))
        dialog.color_picker.SetColour(wx.Colour('#556677'))
        if wx.Platform == '__WXMAC__':
            from wx.lib.colourselect import ColourSelectEvent
            event = ColourSelectEvent(dialog.color_picker.GetId(), wx.Colour('#556677'))
        else:
            event = wx.ColourPickerEvent(dialog.color_picker, dialog.color_picker.GetId(), wx.Colour('#556677'))
            event.SetEventType(wx.wxEVT_COLOURPICKER_CHANGED)
        dialog.color_picker.ProcessWindowEvent(event)
        self.assertEqual(dialog.bg_color_text.GetValue(), '#556677')
        with mock.patch.object(dialog, 'EndModal'):
            dialog.on_cancel(None)
        self.assertEqual(self.manager.get_dialog_draft()['background'], '#303030')

    def test_all_controls_bind_to_current_keys_and_ranges(self):
        dialog = self.dialog()
        for index in range(4):
            dialog.select_page(index)
        expected = dict(background="#abcdef", timeout=777, mode="dark", position="bottom_left",
                        wheel=False, sort="date_desc", preload=4, spacing=0, margin=1000,
                        include_file_identification=True)
        for key, value in expected.items():
            ctrl = dialog.controls[key]
            if isinstance(ctrl, wx.Choice):
                from src.settings_dialog import CHOICE_VALUES
                ctrl.SetSelection(CHOICE_VALUES[key].index(value))
            else:
                ctrl.SetValue(value)
        with mock.patch.object(dialog, "EndModal") as finish:
            dialog.on_ok(None)
        finish.assert_called_once_with(wx.ID_OK)
        self.assertEqual(SettingsManager().get_dialog_draft(), expected)
        self.assertEqual(dialog.object_info_choice.GetCount(), 9)
        self.assertEqual(dialog.sort_choice.GetCount(), 8)

    def test_failed_save_keeps_draft_and_does_not_accept_theme(self):
        dialog = self.dialog()
        before = copy.deepcopy(self.manager.config)
        dialog.controls['mode'].SetSelection(1)
        with mock.patch.object(self.manager, '_write_config', side_effect=OSError('denied')), \
                mock.patch('src.settings_dialog.wx.MessageBox'), \
                mock.patch.object(dialog, 'EndModal') as finish:
            dialog.on_ok(None)
        finish.assert_not_called()
        self.assertEqual(dict(self.manager.config['UI']), dict(before['UI']))
        self.assertEqual(dialog.controls['mode'].GetSelection(), 1)

    def test_invalid_numeric_editor_text_is_rejected_before_save(self):
        dialog = self.dialog()
        dialog.overlay_timeout_spin.SetValue('not a number')
        with mock.patch('src.settings_dialog.wx.MessageBox') as error, \
                mock.patch.object(dialog, 'EndModal') as finish:
            dialog.on_ok(None)
        error.assert_called_once()
        finish.assert_not_called()
        self.assertFalse(Path(self.manager.default_file_name).exists())

    def test_cancel_draft_during_system_change_retains_current_saved_appearance(self):
        signal = [False]
        native = mock.Mock()
        native.SetAppearance.return_value = wx.App.AppearanceResult.Ok
        controller = AppearanceController(native, self.manager, lambda: signal[0])
        with mock.patch.object(self.app, 'dialog_appearance', controller, create=True):
            dialog = self.dialog()
            dialog.controls['mode'].SetSelection(1)
            self.assertEqual(controller.palette, LIGHT_COLORS)
            signal[0] = True
            controller._flush_system_change()
            self.assertEqual(dialog._dialog_colors, DARK_COLORS)
            dialog.Close()
            self.app.ProcessPendingEvents()
            self.assertEqual(self.manager.get_appearance_mode(), 'system')
            self.assertEqual(controller.palette, DARK_COLORS)
            self.assertFalse(controller._windows)
        controller.close()

    def test_keyboard_save_and_escape_from_editors(self):
        for key, expected in ((wx.WXK_RETURN, wx.ID_OK), (wx.WXK_ESCAPE, wx.ID_CANCEL)):
            dialog = self.dialog()
            dialog.bg_color_text.SetValue('#224466')
            timed_out = []

            def send():
                dialog.Raise()
                dialog.bg_color_text.SetFocus()
                if os.name == 'nt':
                    import ctypes
                    ctypes.windll.user32.SetForegroundWindow(dialog.GetHandle())
                self.app.Yield()
                wx.UIActionSimulator().Char(key)

            def timeout():
                if dialog.IsModal():
                    timed_out.append(True)
                    dialog.EndModal(wx.ID_CANCEL)

            action = wx.CallLater(100, send)
            watchdog = wx.CallLater(2000, timeout)
            result = dialog.ShowModal()
            action.Stop()
            watchdog.Stop()
            self.assertFalse(timed_out, f'key={key}, focus={wx.Window.FindFocus()}')
            self.assertEqual(result, expected)

    def test_sidebar_keyboard_activation_and_picker_focus_keep_drafts(self):
        dialog = self.dialog()
        def send():
            dialog.Raise()
            dialog.nav[1].SetFocus()
            if os.name == 'nt':
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(dialog.GetHandle())
            self.app.Yield()
            wx.UIActionSimulator().Char(wx.WXK_SPACE)

        action = wx.CallLater(100, send)
        stop = wx.CallLater(500, dialog.EndModal, wx.ID_CANCEL)
        dialog.ShowModal()
        action.Stop()
        stop.Stop()
        self.assertEqual(dialog.book.GetSelection(), 1)
        dialog.select_page(0)
        dialog.color_picker.SetFocus()
        dialog.color_picker.SetColour(wx.Colour('#556677'))
        if wx.Platform == '__WXMAC__':
            from wx.lib.colourselect import ColourSelectEvent
            event = ColourSelectEvent(dialog.color_picker.GetId(), wx.Colour('#556677'))
        else:
            event = wx.ColourPickerEvent(dialog.color_picker, dialog.color_picker.GetId(), wx.Colour('#556677'))
            event.SetEventType(wx.wxEVT_COLOURPICKER_CHANGED)
        dialog.color_picker.ProcessWindowEvent(event)
        self.assertEqual(dialog.bg_color_text.GetValue(), '#556677')
        self.assertEqual(self.manager.get_dialog_draft()['background'], '#303030')

    def test_minimum_size_wraps_long_copy_and_keeps_actions_and_native_heights(self):
        dialog = self.dialog()
        dialog.SetClientSize(dialog.FromDIP((640, 520)))
        dialog.Show()
        for index in range(4):
            dialog.select_page(index)
            dialog.reflow()
            self.app.Yield()
            page = dialog.pages[index][0]
            self.assertLessEqual(page.GetVirtualSize().width, page.GetClientSize().width)
            for item, _ in dialog.wrapped:
                if page.IsDescendant(item):
                    self.assertGreaterEqual(item.GetSize().height, item.GetBestSize().height,
                                            item.GetLabel())
            for button in (dialog.save_button, dialog.cancel_button):
                rectangle = button.GetScreenRect()
                self.assertTrue(dialog.GetScreenRect().Contains(rectangle))
        self.assertLess(dialog.preload_spin.GetSize().height, dialog.FromDIP(36))
        self.assertLess(dialog.enable_wheel_nav.GetSize().height, dialog.FromDIP(36))


if __name__ == '__main__':
    unittest.main()
