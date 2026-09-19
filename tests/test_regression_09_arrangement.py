import configparser
import os
import tempfile
import unittest
from unittest import mock

import wx
from PIL import Image

from src.arrangement import arrange_no_resize, arrange_with_resize
from src.image_object import ImageObject
from src.main_frame import MainFrame
from src.settings_dialog import SettingsDialog
from src.settings_manager import (
    ARRANGEMENT_DEFAULTS,
    ARRANGEMENT_MAX_PX,
    SettingsManager,
)


def real_object(size, *, position=(17, 19)):
    obj = ImageObject("in-memory")
    obj._original_image = Image.new("RGB", size, "red")
    obj.x, obj.y = position
    obj.width, obj.height = size
    obj.zoom_factor = 1.0
    obj.viewport_offset = (0, 0)
    return obj


def transform(obj):
    return (obj.x, obj.y, obj.width, obj.height,
            obj.zoom_factor, obj.viewport_offset)


class WorkingDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__))
        self.previous_cwd = os.getcwd()
        os.chdir(self.root.name)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.root.cleanup()


class TestArrangementSettings(WorkingDirectoryTest):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def write_ini(self, text):
        with open("nagumix_settings.ini", "w", encoding="utf-8") as stream:
            stream.write(text)

    def test_defaults_independent_zero_and_persistence(self):
        manager = SettingsManager()
        self.assertEqual(manager.get_arrangement_settings(), ARRANGEMENT_DEFAULTS)
        manager.set_arrangement_settings(0, 37)
        manager.save()
        self.assertEqual(
            SettingsManager().get_arrangement_settings(),
            {"spacing": 0, "outer_margin": 37},
        )
        manager.set_arrangement_settings(29, 0)
        self.assertEqual(
            manager.get_arrangement_settings(),
            {"spacing": 29, "outer_margin": 0},
        )

    def test_missing_partial_and_invalid_stored_values_use_defaults(self):
        cases = [
            ("[Canvas]\nbackground_color=#fff\n", ARRANGEMENT_DEFAULTS),
            ("[Arrangement]\nspacing=0\n", {"spacing": 0, "outer_margin": 10}),
            ("[Arrangement]\nspacing=nope\nouter_margin=-1\n", ARRANGEMENT_DEFAULTS),
            (f"[Arrangement]\nspacing={ARRANGEMENT_MAX_PX + 1}\n"
             "outer_margin=12.5\n", ARRANGEMENT_DEFAULTS),
        ]
        for ini, expected in cases:
            with self.subTest(ini=ini):
                self.write_ini(ini)
                self.assertEqual(SettingsManager().get_arrangement_settings(), expected)

    def test_direct_edit_validation_is_atomic(self):
        manager = SettingsManager()
        for values in ((-1, 2), (2, -1), (1001, 2), (2, "bad"), (True, 2)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                manager.set_arrangement_settings(*values)
            self.assertEqual(manager.get_arrangement_settings(), ARRANGEMENT_DEFAULTS)

    def test_dialog_cancel_and_apply(self):
        manager = SettingsManager()
        obj = real_object((20, 10))
        before = transform(obj)
        dialog = SettingsDialog(None, manager)
        dialog.arrangement_spacing_spin.SetValue(0)
        dialog.arrangement_margin_spin.SetValue(23)
        dialog.Destroy()
        self.assertEqual(manager.get_arrangement_settings(), ARRANGEMENT_DEFAULTS)

        dialog = SettingsDialog(None, manager)
        dialog.arrangement_spacing_spin.SetValue(0)
        dialog.arrangement_margin_spin.SetValue(23)
        with mock.patch.object(dialog, "EndModal") as end_modal:
            dialog.on_ok(None)
        end_modal.assert_called_once_with(wx.ID_OK)
        dialog.Destroy()
        self.assertEqual(
            SettingsManager().get_arrangement_settings(),
            {"spacing": 0, "outer_margin": 23},
        )
        self.assertEqual(transform(obj), before)


class TestArrangementLayouts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_no_resize_exact_spacing_boundaries_and_zero(self):
        objects = [real_object((20, 10)), real_object((20, 10)), real_object((20, 10))]
        self.assertTrue(arrange_no_resize(objects, (54, 30), spacing=4, outer_margin=3))
        self.assertEqual([(obj.x, obj.y) for obj in objects], [(3, 3), (27, 3), (3, 17)])
        self.assertEqual(objects[1].x - (objects[0].x + objects[0].width), 4)
        self.assertEqual(objects[2].y - (objects[0].y + objects[0].height), 4)

        pair = [real_object((20, 10)), real_object((20, 10))]
        self.assertTrue(arrange_no_resize(pair, (40, 10), spacing=0, outer_margin=0))
        self.assertEqual([(obj.x, obj.y) for obj in pair], [(0, 0), (20, 0)])
        self.assertEqual(pair[-1].x + pair[-1].width, 40)

    def test_no_resize_overflow_is_atomic_and_has_no_blank_first_row(self):
        objects = [real_object((51, 10)), real_object((10, 10))]
        before = [transform(obj) for obj in objects]
        self.assertFalse(arrange_no_resize(objects, (50, 50), 0, 0))
        self.assertEqual([transform(obj) for obj in objects], before)

        objects = [real_object((20, 20)), real_object((20, 20)), real_object((20, 20))]
        before = [transform(obj) for obj in objects]
        self.assertFalse(arrange_no_resize(objects, (45, 35), 5, 0))
        self.assertEqual([transform(obj) for obj in objects], before)

    def test_resized_exact_edges_remainder_zero_and_single_image(self):
        # 102 - 2*5 - 3 = 89, allocated deterministically as 45 and 44.
        objects = [real_object((45, 41)), real_object((44, 41))]
        self.assertTrue(arrange_with_resize(objects, (102, 51), 3, 5))
        self.assertEqual([transform(obj)[:4] for obj in objects],
                         [(5, 5, 45, 41), (53, 5, 44, 41)])
        self.assertEqual(objects[1].x - (objects[0].x + objects[0].width), 3)
        self.assertEqual(objects[-1].x + objects[-1].width, 97)

        pair = [real_object((50, 20)), real_object((50, 20))]
        self.assertTrue(arrange_with_resize(pair, (100, 20), 0, 0))
        self.assertEqual([transform(obj)[:4] for obj in pair],
                         [(0, 0, 50, 20), (50, 0, 50, 20)])

        single = real_object((90, 40))
        self.assertTrue(arrange_with_resize([single], (100, 50), 5, 5))
        self.assertEqual(transform(single)[:4], (5, 5, 90, 40))

    def test_resized_mixed_shapes_and_small_images_are_not_enlarged(self):
        objects = [real_object((80, 20)), real_object((20, 80)),
                   real_object((5, 5))]
        self.assertTrue(arrange_with_resize(objects, (101, 81), 3, 4))
        self.assertEqual((objects[2].width, objects[2].height), (5, 5))
        for obj in objects:
            self.assertEqual(obj.viewport_offset, (0, 0))
            self.assertLessEqual(obj.x + obj.width, 97)
            self.assertLessEqual(obj.y + obj.height, 77)
            self.assertIsNotNone(obj.get_pil_cropped())

    def test_empty_invalid_and_too_small_layouts(self):
        self.assertTrue(arrange_no_resize([], (10, 10), 0, 0))
        self.assertTrue(arrange_with_resize([], (10, 10), 0, 0))
        for function in (arrange_no_resize, arrange_with_resize):
            for args in [((10, 10), -1, 0), ((10, 10), 0, -1),
                         ((0, 10), 0, 0), ((10, 10), 1.5, 0)]:
                with self.subTest(function=function.__name__, args=args):
                    self.assertFalse(function([], *args))
        self.assertFalse(arrange_with_resize([real_object((1, 1))], (10, 10), 0, 5))
        self.assertFalse(arrange_with_resize(
            [real_object((1, 1)), real_object((1, 1))], (3, 3), 2, 0))

    def test_unreadable_resized_source_is_atomic(self):
        good = real_object((30, 20))
        missing = ImageObject("regression_09-missing-source.png")
        good.viewport_offset = (4, 3)
        before = [transform(good), transform(missing)]
        with self.assertLogs(level="ERROR"):
            self.assertFalse(arrange_with_resize([good, missing], (100, 100), 2, 2))
        self.assertEqual([transform(good), transform(missing)], before)

    def test_position_only_reuses_bitmap_and_resize_invalidates_it(self):
        obj = real_object((20, 10))
        bitmap = obj._get_prepared_bitmap()
        key = obj._prepared_bitmap_key
        self.assertTrue(arrange_no_resize([obj], (30, 20), 0, 0))
        self.assertIs(obj._get_prepared_bitmap(), bitmap)
        self.assertEqual(obj._prepared_bitmap_key, key)

        self.assertTrue(arrange_with_resize([obj], (10, 10), 0, 0))
        self.assertIsNone(obj._prepared_bitmap)
        self.assertNotEqual((obj.width, obj.height), (20, 10))


class TestArrangementController(unittest.TestCase):
    def frame_stub(self):
        frame = mock.Mock()
        frame.settings_manager.get_arrangement_settings.return_value = {
            "spacing": 7, "outer_margin": 9}
        frame.canvas_panel.image_objects = [mock.sentinel.image]
        frame.canvas_panel.get_client_dimensions.return_value = (321, 123)
        return frame

    def test_controller_passes_client_size_settings_and_refreshes_on_success(self):
        for handler_name, function_path in (
                ("on_arrange_no_resize", "src.main_frame.arrange_no_resize"),
                ("on_arrange_with_resize", "src.main_frame.arrange_with_resize")):
            frame = self.frame_stub()
            with self.subTest(handler=handler_name), \
                    mock.patch("src.main_frame.wx.MessageBox", return_value=wx.YES), \
                    mock.patch(function_path, return_value=True) as arrange:
                getattr(MainFrame, handler_name)(frame, None)
            arrange.assert_called_once_with(
                frame.canvas_panel.image_objects, (321, 123), 7, 9)
            frame.canvas_panel.Refresh.assert_called_once_with()

    def test_controller_reports_failure_without_refresh(self):
        for handler_name, function_path in (
                ("on_arrange_no_resize", "src.main_frame.arrange_no_resize"),
                ("on_arrange_with_resize", "src.main_frame.arrange_with_resize")):
            frame = self.frame_stub()
            with self.subTest(handler=handler_name), \
                    mock.patch("src.main_frame.wx.MessageBox",
                               side_effect=[wx.YES, wx.OK]) as message, \
                    mock.patch(function_path, return_value=False):
                getattr(MainFrame, handler_name)(frame, None)
            self.assertEqual(message.call_count, 2)
            frame.canvas_panel.Refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
