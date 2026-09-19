import os
import tempfile
import unittest

from src.image_object import ImageObject, layout_status_overlay
from src.settings_manager import (
    OBJECT_INFO_POSITION_DEFAULT,
    OBJECT_INFO_POSITIONS,
    SettingsManager,
)


class InfoPositionRegressionTests(unittest.TestCase):
    def test_all_nine_positions_anchor_and_clamp_to_canvas(self):
        expected = {
            "top_left": (18, 28), "top_center": (90, 28), "top_right": (162, 28),
            "middle_left": (18, 90), "center": (90, 90), "middle_right": (162, 90),
            "bottom_left": (18, 152), "bottom_center": (90, 152), "bottom_right": (162, 152),
        }
        for position, point in expected.items():
            self.assertEqual(
                layout_status_overlay((10, 20, 240, 180), (80, 40), (250, 200), position, 8)[:2],
                point, position)

    def test_off_canvas_and_long_box_are_safe(self):
        self.assertEqual(
            layout_status_overlay((-100, -80, 30, 20), (500, 400), (120, 90), "top_left", 8),
            (0, 0, 120, 90))
        self.assertIsNone(layout_status_overlay((0, 0, 10, 10), (10, 10), (0, 90)))

    def test_settings_default_validation_and_round_trip_are_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            try:
                os.chdir(directory)
                manager = SettingsManager()
                self.assertEqual(manager.get_object_info_position(), OBJECT_INFO_POSITION_DEFAULT)
                manager.set_object_info_position("bottom_right")
                self.assertEqual(manager.get_object_info_position(), "bottom_right")
                manager.save()
                manager = SettingsManager()
                self.assertEqual(manager.get_object_info_position(), "bottom_right")
                manager.set_setting("UI", "object_info_position", "invalid")
                self.assertEqual(manager.get_object_info_position(), OBJECT_INFO_POSITION_DEFAULT)
                with self.assertRaises(ValueError):
                    manager.set_object_info_position("not-a-position")
                self.assertEqual(len(OBJECT_INFO_POSITIONS), 9)
            finally:
                os.chdir(old_cwd)

    def test_existing_object_draw_path_receives_current_choice_without_pixel_state_change(self):
        obj = ImageObject("unused.png")
        obj.x, obj.y, obj.width, obj.height = 30, 40, 100, 80
        calls = []
        obj.draw_bitmap = lambda _dc: True
        obj.draw_decorations = lambda _dc, canvas_size, info_position, scale: calls.append(
            (canvas_size, info_position, scale))
        obj.draw(object(), (320, 200), "bottom_left", 1.0)
        self.assertEqual(calls, [((320, 200), "bottom_left", 1.0)])
        self.assertEqual((obj.x, obj.y, obj.width, obj.height), (30, 40, 100, 80))


if __name__ == "__main__":
    unittest.main()
