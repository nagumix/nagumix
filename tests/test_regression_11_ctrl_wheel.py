import unittest
from unittest import mock

import wx
from PIL import Image

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.file_navigator import NavigationResult
from src.image_object import ImageObject


class Settings:
    def __init__(self, enabled="true"):
        self.enabled = enabled

    def get_setting(self, section, key, fallback=None):
        if (section, key) == ("Navigation", "enable_wheel_navigation"):
            return self.enabled
        return fallback


class Event:
    def __init__(self, rotation, delta=120, *, ctrl=False, axis=None):
        self.rotation = rotation
        self.delta = delta
        self.ctrl = ctrl
        self.axis = axis
        self.skipped = False

    def ControlDown(self):
        return self.ctrl

    def GetWheelRotation(self):
        return self.rotation

    def GetWheelDelta(self):
        return self.delta

    def GetWheelAxis(self):
        return wx.MOUSE_WHEEL_VERTICAL if self.axis is None else self.axis

    def Skip(self):
        self.skipped = True


class CanvasHarness:
    get_selected_object = CanvasPanel.get_selected_object
    set_selected_object = CanvasPanel.set_selected_object
    on_key_down = CanvasPanel.on_key_down
    on_mouse_wheel = CanvasPanel.on_mouse_wheel
    _zoom_selected_image = CanvasPanel._zoom_selected_image
    _handle_ctrl_wheel_zoom = CanvasPanel._handle_ctrl_wheel_zoom
    _reset_zoom_wheel_remainder = CanvasPanel._reset_zoom_wheel_remainder
    _wheel_is_vertical = staticmethod(CanvasPanel._wheel_is_vertical)
    _on_navigation_discovered = CanvasPanel._on_navigation_discovered

    def __init__(self, objects, *, enabled="true"):
        self.image_objects = ImageObjectList(objects)
        self.selected_object = objects[0] if objects else None
        self.settings_manager = Settings(enabled)
        self.refresh_count = 0
        self.scheduled = 0
        self.navigations = []
        self._zoom_wheel_remainders = {}

    def Refresh(self):
        self.refresh_count += 1

    def _schedule_overlay_clear(self, delay_ms=None):
        self.scheduled += 1

    def _navigate_to_adjacent_file(self, previous=False):
        self.navigations.append(previous)

    def start_preloading_for_object(self, image_object):
        pass


def image(name):
    obj = ImageObject(name, canvas_width=1000, canvas_height=1000)
    obj._original_image = Image.new("RGB", (400, 200), "red")
    obj.width, obj.height = 400, 200
    obj._prepared_bitmap = mock.Mock(GetWidth=lambda: 1, GetHeight=lambda: 1)
    return obj


class TestCtrlWheelZoom(unittest.TestCase):
    def test_keyboard_and_ctrl_wheel_share_transform_feedback_and_peer_cache(self):
        keyboard = image("same.png")
        wheel = image("same.png")
        peer = image("same.png")
        canvas = CanvasHarness([keyboard, wheel, peer])

        canvas.selected_object = keyboard
        canvas.on_key_down(mock.Mock(GetKeyCode=lambda: wx.WXK_ADD))
        canvas.selected_object = wheel
        event = Event(120, ctrl=True)
        canvas.on_mouse_wheel(event)

        self.assertEqual(keyboard.zoom_factor, wheel.zoom_factor)
        self.assertEqual(keyboard.status_message, wheel.status_message)
        self.assertIsNone(keyboard._prepared_bitmap)
        self.assertIsNotNone(peer._prepared_bitmap)
        self.assertFalse(event.skipped)

    def test_limits_and_fitted_minimum_match_keyboard_steps(self):
        obj = image("fit.png")
        keyboard = image("fit-keyboard.png")
        obj.zoom_factor = obj._minimum_zoom = 0.1
        keyboard.zoom_factor = keyboard._minimum_zoom = 0.1
        canvas = CanvasHarness([obj])
        keyboard_canvas = CanvasHarness([keyboard])
        for _ in range(4):
            canvas.on_mouse_wheel(Event(-120, ctrl=True))
        self.assertEqual(obj.zoom_factor, 0.1)
        self.assertIn("Min zoom", obj.status_message)
        for _ in range(20):
            canvas.on_mouse_wheel(Event(120, ctrl=True))
            keyboard_canvas.on_key_down(mock.Mock(GetKeyCode=lambda: wx.WXK_ADD))
        self.assertEqual(obj.zoom_factor, keyboard.zoom_factor)
        self.assertIn("Max zoom", obj.status_message)

    def test_multiple_fractional_and_reversed_notches_are_accumulated(self):
        obj = image("fractional.png")
        canvas = CanvasHarness([obj])
        canvas.on_mouse_wheel(Event(60, ctrl=True))
        self.assertEqual(obj.zoom_factor, 1.0)
        canvas.on_mouse_wheel(Event(60, ctrl=True))
        self.assertGreater(obj.zoom_factor, 1.0)
        after_one = obj.zoom_factor
        canvas.on_mouse_wheel(Event(240, ctrl=True))
        self.assertGreater(obj.zoom_factor, after_one)
        canvas.on_mouse_wheel(Event(-60, ctrl=True))
        canvas.on_mouse_wheel(Event(-60, ctrl=True))
        self.assertAlmostEqual(obj.zoom_factor, after_one * 1.25, places=6)

    def test_ctrl_routing_consumes_invalid_and_unselected_vertical_events(self):
        canvas = CanvasHarness([])
        for event in (Event(0, ctrl=True), Event(120, 0, ctrl=True)):
            canvas.on_mouse_wheel(event)
            self.assertFalse(event.skipped)
        canvas = CanvasHarness([image("disabled.png")], enabled="false")
        event = Event(120, ctrl=True)
        canvas.on_mouse_wheel(event)
        self.assertGreater(canvas.selected_object.zoom_factor, 1.0)
        self.assertEqual(canvas.navigations, [])

    def test_plain_navigation_horizontal_and_modifier_behavior(self):
        canvas = CanvasHarness([image("nav.png")])
        canvas.on_mouse_wheel(Event(120))
        canvas.on_mouse_wheel(Event(-120, ctrl=False))
        self.assertEqual(canvas.navigations, [True, False])
        horizontal = Event(120, ctrl=True, axis=wx.MOUSE_WHEEL_HORIZONTAL)
        canvas.on_mouse_wheel(horizontal)
        self.assertTrue(horizontal.skipped)
        self.assertEqual(len(canvas.navigations), 2)

    def test_pending_navigation_is_invalidated_but_peer_work_is_not(self):
        obj = image("pending.png")
        peer = image("peer.png")
        canvas = CanvasHarness([obj, peer])
        old_generation = obj._work_generation
        peer_generation = peer._work_generation
        canvas.on_mouse_wheel(Event(120, ctrl=True))
        self.assertGreater(obj._work_generation, old_generation)
        self.assertEqual(peer._work_generation, peer_generation)
        stale = mock.Mock(context=(obj, old_generation, obj.source_path))
        canvas._on_navigation_discovered(NavigationResult(
            "replacement.png", False, stale.context))
        self.assertEqual(obj.source_path, "pending.png")
        self.assertNotEqual(obj._work_generation, stale.context[1])

    def test_fractional_remainder_does_not_cross_selection(self):
        first, second = image("one.png"), image("two.png")
        canvas = CanvasHarness([first, second])
        canvas.on_mouse_wheel(Event(60, ctrl=True))
        canvas.set_selected_object(second)
        canvas.on_mouse_wheel(Event(120, ctrl=True))
        self.assertEqual(first.zoom_factor, 1.0)
        self.assertGreater(second.zoom_factor, 1.0)


if __name__ == "__main__":
    unittest.main()
