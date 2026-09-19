import os
import unittest
from unittest import mock

import wx

from src.canvas_panel import CanvasPanel, FileDropTarget
from src.image_object import ImageObject
from src.main_frame import MainFrame


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_IMAGE = os.path.join(
    PROJECT_ROOT, "tests", "fixtures", "regression_02_legacy_image.ppm"
)
LEGACY_STATE = os.path.join(
    PROJECT_ROOT, "tests", "fixtures", "regression_02_legacy_state.json"
)


class SettingsStub:
    def get_setting(self, section, key, fallback=None):
        return fallback

    def save(self):
        pass


class MenuTestFrame(MainFrame):
    """Main frame whose popup can be opened without blocking the test."""

    def PopupMenu(self, menu, *args, **kwargs):
        return True


class TestResetSizeFallbacks(unittest.TestCase):
    def test_absent_zero_and_negative_bounds_restore_source_dimensions(self):
        for bounds in ((None, None), (0, 0), (-10, 4), (4, -10)):
            with self.subTest(bounds=bounds):
                obj = ImageObject(FIXTURE_IMAGE)
                obj.width = -1
                obj.height = 0
                obj.set_canvas_size(*bounds)

                self.assertTrue(obj.reset_size(fit_to_canvas=True))
                self.assertEqual((obj.width, obj.height), (8, 4))
                self.assertGreater(obj.width, 0)
                self.assertGreater(obj.height, 0)

    def test_missing_source_is_a_harmless_failed_reset(self):
        obj = ImageObject(os.path.join(PROJECT_ROOT, "tests", "missing.png"))
        original_dimensions = (obj.width, obj.height)

        with self.assertLogs(level="ERROR"):
            result = obj.reset_size(fit_to_canvas=True)
        self.assertFalse(result)
        self.assertEqual((obj.width, obj.height), original_dimensions)


class TestResetCommandDispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.frame = MenuTestFrame(
            None, "Regression 02 reset tests", SettingsStub(), debug_mode=True
        )
        self.frame.SetClientSize((160, 100))
        self.frame.Layout()
        wx.Yield()

    def tearDown(self):
        self.frame.canvas_panel.overlay_clear_timer.Stop()
        self.frame.canvas_panel.file_navigator.clear_cache()
        self.frame.Destroy()
        wx.Yield()

    def dispatch(self, command_id):
        event = wx.CommandEvent(wx.wxEVT_MENU, int(command_id))
        self.assertTrue(self.frame.GetEventHandler().ProcessEvent(event))

    def add_selected_object(self):
        # Reset tests do not depend on the asynchronous drop lifecycle. Build
        # the same successfully fitted object directly so they remain focused
        # on menu routing and live canvas bounds.
        canvas_w, canvas_h = self.frame.canvas_panel.get_client_dimensions()
        obj = ImageObject(FIXTURE_IMAGE, canvas_width=canvas_w,
                          canvas_height=canvas_h)
        self.assertTrue(obj.fit_to_bounds(canvas_w, canvas_h))
        self.frame.canvas_panel.add_image_object(obj)
        self.frame.canvas_panel.selected_object = obj
        return obj

    def test_all_reset_commands_dispatch_to_current_selection(self):
        target = self.add_selected_object()
        other = ImageObject(FIXTURE_IMAGE)
        self.frame.canvas_panel.add_image_object(other)

        target.width, target.height = 2, 1
        target.zoom_factor = 2.0
        target.viewport_offset = (3, 2)
        target._visible_image = object()
        other._visible_image = object()

        with mock.patch.object(CanvasPanel, "Refresh", autospec=True) as refresh:
            self.dispatch(MainFrame.RESET_SIZE_ID)
            self.assertEqual(refresh.call_count, 1)
        canvas_w, canvas_h = self.frame.canvas_panel.get_client_dimensions()
        self.assertEqual(target.canvas_w, canvas_w)
        self.assertEqual(target.canvas_h, canvas_h)
        self.assertEqual(target.width, min(8, canvas_w))
        self.assertEqual(target.height, min(4, canvas_h))
        self.assertIsNone(target._visible_image)
        self.assertIsNotNone(other._visible_image)

        target._visible_image = object()
        with mock.patch.object(CanvasPanel, "Refresh", autospec=True) as refresh:
            self.dispatch(MainFrame.RESET_ZOOM_ID)
            self.assertEqual(refresh.call_count, 1)
        self.assertEqual(target.zoom_factor, 1.0)
        self.assertEqual(target.status_message, "Zoom reset to 100%")
        self.assertTrue(self.frame.canvas_panel.overlay_clear_timer.IsRunning())
        self.assertIsNone(target._visible_image)

        target._visible_image = object()
        with mock.patch.object(CanvasPanel, "Refresh", autospec=True) as refresh:
            self.dispatch(MainFrame.RESET_OFFSET_ID)
            self.assertEqual(refresh.call_count, 1)
        self.assertEqual(target.viewport_offset, (0, 0))
        self.assertIsNone(target._visible_image)

    def test_resize_updates_metadata_without_changing_saved_transform(self):
        obj = self.add_selected_object()
        saved_transform = (7, 9, 3, 2, 1.75, (2, 1))
        (obj.x, obj.y, obj.width, obj.height,
         obj.zoom_factor, obj.viewport_offset) = saved_transform

        self.frame.canvas_panel.SetClientSize((6, 3))
        size_event = wx.SizeEvent(wx.Size(6, 3), self.frame.canvas_panel.GetId())
        self.frame.canvas_panel.GetEventHandler().ProcessEvent(size_event)

        self.assertEqual((obj.canvas_w, obj.canvas_h), (6, 3))
        self.assertEqual(
            (obj.x, obj.y, obj.width, obj.height,
             obj.zoom_factor, obj.viewport_offset),
            saved_transform,
        )

        # Deliberately stale metadata must not be used by the command handler.
        obj.set_canvas_size(999, 999)
        self.dispatch(MainFrame.RESET_SIZE_ID)
        self.assertEqual((obj.canvas_w, obj.canvas_h), (6, 3))
        self.assertEqual((obj.width, obj.height), (6, 3))

    def test_legacy_load_preserves_transform_then_resets_with_client_bounds(self):
        self.frame.canvas_panel.SetClientSize((6, 3))
        old_cwd = os.getcwd()
        try:
            os.chdir(PROJECT_ROOT)
            self.frame.canvas_panel.load_canvas_state(LEGACY_STATE)
        finally:
            os.chdir(old_cwd)

        loaded = self.frame.canvas_panel.image_objects[0]
        self.assertEqual((loaded.canvas_w, loaded.canvas_h), (6, 3))
        self.assertEqual(
            (loaded.x, loaded.y, loaded.width, loaded.height,
             loaded.zoom_factor, loaded.viewport_offset),
            (4, 5, 3, 2, 1.75, (2, 1)),
        )

        self.frame.canvas_panel.selected_object = loaded
        self.dispatch(MainFrame.RESET_SIZE_ID)
        self.assertEqual((loaded.width, loaded.height), (6, 3))
        self.assertEqual((loaded.x, loaded.y), (0, 0))

    def test_reopened_menus_do_not_capture_or_duplicate_reset_targets(self):
        first = self.add_selected_object()
        second = ImageObject(FIXTURE_IMAGE)
        self.frame.canvas_panel.add_image_object(second)
        method_names = ("reset_size", "reset_zoom", "reset_viewport_offset")
        command_ids = (
            MainFrame.RESET_SIZE_ID,
            MainFrame.RESET_ZOOM_ID,
            MainFrame.RESET_OFFSET_ID,
        )
        calls = {
            first.object_id: dict.fromkeys(method_names, 0),
            second.object_id: dict.fromkeys(method_names, 0),
        }

        for obj in (first, second):
            for method_name in method_names:
                original = getattr(obj, method_name)

                def counted_reset(*args, original=original, obj=obj,
                                  method_name=method_name, **kwargs):
                    calls[obj.object_id][method_name] += 1
                    return original(*args, **kwargs)

                setattr(obj, method_name, counted_reset)

        self.frame.on_right_click(None)
        self.frame.on_right_click(None)
        for command_id in command_ids:
            self.dispatch(command_id)
        self.assertEqual(calls[first.object_id], dict.fromkeys(method_names, 1))
        self.assertEqual(calls[second.object_id], dict.fromkeys(method_names, 0))

        self.frame.canvas_panel.selected_object = second
        self.frame.on_right_click(None)
        self.frame.on_right_click(None)
        for command_id in command_ids:
            self.dispatch(command_id)
        self.assertEqual(calls[first.object_id], dict.fromkeys(method_names, 1))
        self.assertEqual(calls[second.object_id], dict.fromkeys(method_names, 1))

    def test_no_selection_and_stale_selection_commands_are_harmless(self):
        stale = ImageObject(FIXTURE_IMAGE)
        for selection in (None, stale):
            with self.subTest(selection=selection):
                self.frame.canvas_panel.selected_object = selection
                with mock.patch.object(CanvasPanel, "Refresh", autospec=True) as refresh:
                    self.dispatch(MainFrame.RESET_SIZE_ID)
                    self.dispatch(MainFrame.RESET_ZOOM_ID)
                    self.dispatch(MainFrame.RESET_OFFSET_ID)
                    refresh.assert_not_called()

        missing = ImageObject(os.path.join(PROJECT_ROOT, "tests", "missing.png"))
        self.frame.canvas_panel.add_image_object(missing)
        self.frame.canvas_panel.selected_object = missing
        with mock.patch.object(CanvasPanel, "Refresh", autospec=True) as refresh:
            with self.assertLogs(level="ERROR"):
                self.dispatch(MainFrame.RESET_SIZE_ID)
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
