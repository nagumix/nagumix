import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.file_navigator import NavigationDecodeResult
from src.image_object import ImageObject
from src.settings_manager import SettingsManager


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeTimer:
    def __init__(self):
        self.running = False
        self.starts = []
        self.stop_count = 0

    def IsRunning(self):
        return self.running

    def Start(self, delay_ms, mode):
        self.running = True
        self.starts.append((delay_ms, mode))

    def Stop(self):
        self.running = False
        self.stop_count += 1


class Settings:
    def __init__(self, timeout="1500"):
        self.timeout = timeout

    def get_setting(self, section, key, fallback=None):
        if (section, key) == ("UI", "overlay_timeout_ms"):
            return self.timeout
        return fallback


class Event:
    def __init__(self, position=(1, 1)):
        self.position = position
        self.skipped = False

    def GetPosition(self):
        return self.position

    def Skip(self):
        self.skipped = True


class CanvasHarness:
    get_selected_object = CanvasPanel.get_selected_object
    set_selected_object = CanvasPanel.set_selected_object
    on_left_down = CanvasPanel.on_left_down
    on_overlay_timer = CanvasPanel.on_overlay_timer
    _schedule_overlay_clear = CanvasPanel._schedule_overlay_clear
    _reschedule_overlay_timer = CanvasPanel._reschedule_overlay_timer
    _stop_overlay_timer = CanvasPanel._stop_overlay_timer
    _get_overlay_timeout_ms = CanvasPanel._get_overlay_timeout_ms
    _navigate_to_adjacent_file = CanvasPanel._navigate_to_adjacent_file
    _on_navigation_discovered = CanvasPanel._on_navigation_discovered
    _apply_navigation_result = CanvasPanel._apply_navigation_result
    _on_navigation_decoded = CanvasPanel._on_navigation_decoded
    _reset_navigated_image_properties = CanvasPanel._reset_navigated_image_properties
    _reset_zoom_wheel_remainder = CanvasPanel._reset_zoom_wheel_remainder
    debug_image_objects = CanvasPanel.debug_image_objects
    bring_image_object_to_front = CanvasPanel.bring_image_object_to_front
    remove_image_object = CanvasPanel.remove_image_object
    on_settings_changed = CanvasPanel.on_settings_changed
    shutdown_preloading = CanvasPanel.shutdown_preloading

    def __init__(self, objects, *, timeout="1500"):
        self.image_objects = ImageObjectList(objects)
        self.selected_object = objects[0] if objects else None
        self.marked_object = None
        self.settings_manager = Settings(timeout)
        self._monotonic = FakeClock()
        from src.animation_controls import AnimationControlState
        self._animation_controls = AnimationControlState(self._monotonic)
        self._zoom_wheel_remainders = {}
        self.overlay_clear_timer = FakeTimer()
        self.refresh_count = 0
        self.file_navigator = mock.Mock()
        self.file_navigator.request_navigation.return_value = True
        self.file_navigator.is_shutdown = False

        def decode(path, _request_key, context, callback, *, wrapped=False,
                   apply_orientation=True):
            callback(NavigationDecodeResult(
                path, wrapped, context,
                pixels=Image.new("RGB", (400, 200), "red")))
            return True

        self.file_navigator.request_navigation_decode.side_effect = decode

    def Refresh(self):
        self.refresh_count += 1

    def SetFocus(self):
        pass

    def start_preloading_for_object(self, image_object):
        pass

    def get_client_dimensions(self):
        return 1000, 1000

    def GetSize(self):
        return 1000, 1000


def image(name, *, source_pixels=False):
    obj = ImageObject(name, canvas_width=1000, canvas_height=1000)
    if source_pixels:
        obj._original_image = Image.new("RGB", (400, 200), "red")
        obj.width, obj.height = 400, 200
    return obj


class TestStatusLifetimes(unittest.TestCase):
    def test_duplicate_source_objects_expire_independently(self):
        first, second = image("same.png"), image("same.png")
        canvas = CanvasHarness([first, second])

        first.set_status_overlay("first")
        canvas._schedule_overlay_clear(1000, first)
        second.set_status_overlay("second")
        canvas._schedule_overlay_clear(3000, second)

        canvas._monotonic.advance(1.1)
        canvas.on_overlay_timer(None)
        self.assertFalse(first.show_status_overlay)
        self.assertTrue(second.show_status_overlay)
        self.assertIn(canvas.overlay_clear_timer.starts[-1][0], (1900, 1901))

    def test_replacing_or_rescheduling_one_message_does_not_use_old_expiry(self):
        obj = image("replace.png")
        canvas = CanvasHarness([obj])
        obj.set_status_overlay("old")
        canvas._schedule_overlay_clear(1000, obj)
        old_revision = obj.status_revision

        canvas._monotonic.advance(0.5)
        obj.set_status_overlay("new")
        canvas._schedule_overlay_clear(2000, obj)
        self.assertGreater(obj.status_revision, old_revision)
        self.assertAlmostEqual(obj.status_deadline, 102.5)

        canvas._monotonic.advance(0.6)
        canvas.on_overlay_timer(None)
        self.assertEqual(obj.status_message, "new")
        canvas._monotonic.advance(1.4)
        canvas.on_overlay_timer(None)
        self.assertFalse(obj.show_status_overlay)

    def test_dismissing_one_message_keeps_peer_timer_and_processing_click(self):
        dismiss, peer = image("dismiss.png"), image("peer.png")
        peer.x = 300
        canvas = CanvasHarness([dismiss, peer])
        dismiss.set_status_overlay("dismiss me")
        canvas._schedule_overlay_clear(1000, dismiss)
        peer.set_status_overlay("peer")
        canvas._schedule_overlay_clear(3000, peer)

        canvas.selected_object = dismiss
        canvas.on_left_down(Event())
        self.assertFalse(dismiss.show_status_overlay)
        self.assertTrue(peer.show_status_overlay)
        self.assertTrue(canvas.overlay_clear_timer.IsRunning())

        dismiss.set_status_overlay("working", "processing")
        canvas.on_left_down(Event())
        self.assertTrue(dismiss.show_status_overlay)
        self.assertEqual(dismiss.status_type, "processing")

    def test_early_timer_delivery_and_processing_have_no_ordinary_expiry(self):
        processing, timed = image("processing.png"), image("timed.png")
        canvas = CanvasHarness([processing, timed])
        processing.set_status_overlay("Finding images...", "processing")
        timed.set_status_overlay("timed")
        canvas._schedule_overlay_clear(1000, timed)

        canvas._monotonic.advance(0.5)
        canvas.on_overlay_timer(None)
        self.assertTrue(processing.show_status_overlay)
        self.assertTrue(timed.show_status_overlay)
        self.assertEqual(canvas.overlay_clear_timer.starts[-1][0], 500)

        canvas._monotonic.advance(0.5)
        canvas.on_overlay_timer(None)
        self.assertTrue(processing.show_status_overlay)
        self.assertFalse(timed.show_status_overlay)
        self.assertFalse(canvas.overlay_clear_timer.IsRunning())

    def test_explicit_completion_and_cancel_clear_processing_without_deadlines(self):
        obj = image("complete.png")
        canvas = CanvasHarness([obj])
        obj.set_status_overlay("Loading...", "processing")
        canvas._schedule_overlay_clear(300, obj)
        self.assertIsNone(obj.status_deadline)
        obj.clear_status_overlay()
        self.assertFalse(obj.show_status_overlay)

        obj.set_status_overlay("Finding images...", "processing")
        generation = obj._work_generation
        obj.cancel_pending_work()
        self.assertEqual(obj._work_generation, generation + 1)
        self.assertFalse(obj.show_status_overlay)

    def test_source_replacement_clears_old_status_and_deadline(self):
        obj = image("old.png")
        canvas = CanvasHarness([obj])
        obj.set_status_overlay("old source")
        canvas._schedule_overlay_clear(3000, obj)
        obj.change_source_path("new.png")
        self.assertFalse(obj.show_status_overlay)
        self.assertIsNone(obj.status_deadline)

    def test_invalid_timeout_setting_uses_existing_default(self):
        for invalid in ("not-a-number", "0", "-1", "10001"):
            with self.subTest(invalid=invalid):
                obj = image("timeout.png")
                canvas = CanvasHarness([obj], timeout=invalid)
                obj.set_status_overlay("feedback")
                canvas._schedule_overlay_clear()
                self.assertEqual(canvas.overlay_clear_timer.starts[-1][0], 1500)

        manager = SettingsManager()
        manager.set_setting("UI", "overlay_timeout_ms", "bad")
        self.assertEqual(manager.get_overlay_timeout_ms(), 1500)

    def test_zoom_assigns_deadline_only_to_selected_object(self):
        selected = image("selected.png", source_pixels=True)
        peer = image("peer.png")
        canvas = CanvasHarness([selected, peer])
        peer.set_status_overlay("peer")
        canvas._schedule_overlay_clear(3000, peer)

        canvas._zoom_selected_image = CanvasPanel._zoom_selected_image.__get__(canvas)
        canvas._zoom_selected_image(zoom_in=True)
        self.assertEqual(selected.status_message, "Zoom: 125%")
        self.assertAlmostEqual(selected.status_deadline, 101.5)
        self.assertAlmostEqual(peer.status_deadline, 103.0)

    def test_navigation_terminal_paths_are_explicit_and_readable(self):
        obj = image("current.png")
        canvas = CanvasHarness([obj])
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertEqual(obj.status_type, "processing")
        canvas.file_navigator.request_navigation.return_value = False
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertEqual(obj.status_type, "warning")
        self.assertIn("retry", obj.status_message.lower())
        self.assertIsNone(obj.status_deadline)

        context = (obj, obj._work_generation, obj.source_path)
        failure = SimpleNamespace(
            context=context,
            operation="enumeration",
            message="Access denied",
            failure=SimpleNamespace(
                user_message=lambda: "Could not enumerate images. Scroll to retry.",
                operation="enumeration", message="Access denied"),
        )
        canvas._on_navigation_discovered(failure)
        self.assertIn("retry", obj.status_message.lower())

    def test_navigation_success_clears_processing_and_wrap_is_timed(self):
        obj = image("current.png", source_pixels=True)
        canvas = CanvasHarness([obj])
        context = (obj, obj._work_generation, obj.source_path)

        result = SimpleNamespace(
            context=context, failure=None, target_path="next.png", wrapped=False)
        obj.set_status_overlay("Loading...", "processing")
        canvas._on_navigation_discovered(result)
        self.assertFalse(obj.show_status_overlay)
        self.assertEqual(obj.source_path, "next.png")

        obj.source_path = "current.png"
        obj._work_generation += 1
        context = (obj, obj._work_generation, obj.source_path)
        result = SimpleNamespace(
            context=context, failure=None, target_path="first.png", wrapped=True)
        canvas._on_navigation_discovered(result)
        self.assertEqual(obj.status_message, "Wrapped to first.png")
        self.assertEqual(canvas.overlay_clear_timer.starts[-1][0], 2000)

    def test_settings_clear_deletion_and_shutdown_retire_processing_and_timer(self):
        first, second = image("first.png"), image("second.png")
        canvas = CanvasHarness([first, second])
        first.set_status_overlay("Finding images...", "processing")
        second.set_status_overlay("ordinary")
        canvas._schedule_overlay_clear(3000, second)
        canvas.on_settings_changed()
        self.assertFalse(first.show_status_overlay)
        self.assertTrue(second.show_status_overlay)

        first.set_status_overlay("Finding images...", "processing")
        self.assertTrue(canvas.remove_image_object(first))
        self.assertFalse(first.show_status_overlay)
        self.assertTrue(canvas.overlay_clear_timer.IsRunning())

        canvas.shutdown_preloading()
        self.assertFalse(second.show_status_overlay)
        self.assertFalse(canvas.overlay_clear_timer.IsRunning())


if __name__ == "__main__":
    unittest.main()
