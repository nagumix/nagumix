import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image
import wx

from src.canvas_panel import CanvasPanel, FileDropTarget, ImageObjectList
from src.file_navigator import DirectorySnapshot, FileNavigator, path_comparison_key
from src.image_object import ImageObject
from src.image_pixels import load_source_pixels
from src.main_frame import MainFrame


class SettingsStub:
    def __init__(self, preload_count="0"):
        self.preload_count = preload_count

    def get_setting(self, section, key, fallback=None):
        return {
            ("Navigation", "preload_count"): self.preload_count,
            ("Navigation", "enable_wheel_navigation"): "true",
            ("Navigation", "sort_method"): "name_asc",
            ("Canvas", "background_color"): "#FFFFFF",
            ("UI", "overlay_timeout_ms"): "1500",
        }.get((section, key), fallback)

    def save(self):
        pass


class Dispatcher:
    def __init__(self):
        self.condition = threading.Condition()
        self.items = []

    def __call__(self, callback, result):
        with self.condition:
            self.items.append((callback, result))
            self.condition.notify_all()

    def wait(self, count=1, timeout=3.0):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.items) >= count, timeout)

    def drain_one(self, index=0):
        with self.condition:
            callback, result = self.items.pop(index)
        callback(result)
        return result

    def drain_until_terminal(self, canvas, navigator, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.condition:
                item = self.items.pop(0) if self.items else None
            if item is not None:
                item[0](item[1])
                continue
            operation = canvas.drop_operation
            if operation is not None and operation.terminal:
                return True
            with self.condition:
                self.condition.wait(0.01)
        return False


class BlockingLoader:
    def __init__(self, delegate=load_source_pixels):
        self.delegate = delegate
        self.condition = threading.Condition()
        self.calls = []
        self.releases = {}
        self.active = 0
        self.max_active = 0

    def block(self, path):
        self.releases[path] = threading.Event()

    def release(self, path):
        self.releases[path].set()

    def release_all(self):
        for event in self.releases.values():
            event.set()

    def wait_for_calls(self, count, timeout=3.0):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.calls) >= count, timeout)

    def __call__(self, path, **kwargs):
        with self.condition:
            self.calls.append(path)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
        try:
            release = self.releases.get(path)
            if release is not None and not release.wait(5.0):
                raise TimeoutError(f"test did not release {path}")
            return self.delegate(path, **kwargs)
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


class FakeTimer:
    def IsRunning(self):
        return False

    def Start(self, *_args):
        pass

    def Stop(self):
        pass


class DropCanvasHarness:
    MAX_DROP_IN_FLIGHT = CanvasPanel.MAX_DROP_IN_FLIGHT
    get_selected_object = CanvasPanel.get_selected_object
    set_selected_object = CanvasPanel.set_selected_object
    add_image_object = CanvasPanel.add_image_object
    _note_user_interaction = CanvasPanel._note_user_interaction
    _drop_request_key = staticmethod(CanvasPanel._drop_request_key)
    accept_drop = CanvasPanel.accept_drop
    _pump_drop_operation = CanvasPanel._pump_drop_operation
    _find_drop_entry = CanvasPanel._find_drop_entry
    _on_drop_decoded = CanvasPanel._on_drop_decoded
    _insert_drop_object = CanvasPanel._insert_drop_object
    _finalize_drop_operation = CanvasPanel._finalize_drop_operation
    cancel_drop_operation = CanvasPanel.cancel_drop_operation
    _retire_drop_requests_for_clear = CanvasPanel._retire_drop_requests_for_clear
    _point_in_rect = staticmethod(CanvasPanel._point_in_rect)
    _handle_drop_card_click = CanvasPanel._handle_drop_card_click
    _drop_status_lines = CanvasPanel._drop_status_lines
    _draw_drop_status = CanvasPanel._draw_drop_status
    on_settings_changed = CanvasPanel.on_settings_changed
    shutdown_preloading = CanvasPanel.shutdown_preloading
    load_canvas_state = CanvasPanel.load_canvas_state
    export_to_file = CanvasPanel.export_to_file
    _stop_overlay_timer = CanvasPanel._stop_overlay_timer

    def __init__(self, navigator, objects=(), bounds=(120, 80)):
        self.file_navigator = navigator
        self.settings_manager = navigator.settings_manager
        self.image_objects = ImageObjectList(objects)
        self.selected_object = objects[0] if objects else None
        self.marked_object = None
        self.drag_offset = None
        self._zoom_wheel_remainders = {}
        self._interaction_revision = 0
        self._drop_generation = 0
        self.drop_operation = None
        self._drop_card_rect = None
        self._drop_cancel_rect = None
        self.overlay_clear_timer = FakeTimer()
        self.canvas_bg = "#FFFFFF"
        self.bounds = bounds
        self.refresh_count = 0
        self.preload_targets = []

    def Refresh(self):
        self.refresh_count += 1

    def get_client_dimensions(self):
        return self.bounds

    def GetClientSize(self):
        return self.bounds

    def GetSize(self):
        return self.bounds

    def start_preloading_for_object(self, image_object):
        self.preload_targets.append(image_object)


class TestAsyncDrops(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)
        cls.root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        cls.rgb = cls.root / "rgb.png"
        cls.rgba = cls.root / "rgba.png"
        cls.oriented = cls.root / "oriented.jpg"
        cls.corrupt = cls.root / "corrupt.png"
        cls.missing = cls.root / "missing.png"
        cls.unsupported = cls.root / "notes.txt"
        cls.directory = cls.root / "folder.png"
        Image.new("RGB", (60, 20), "red").save(cls.rgb)
        Image.new("RGBA", (20, 40), (0, 200, 0, 77)).save(cls.rgba)
        exif = Image.Exif()
        exif[274] = 6
        oriented = Image.new("RGB", (3, 2), "blue")
        oriented.putpixel((2, 1), (255, 255, 0))
        oriented.save(cls.oriented, exif=exif)
        cls.corrupt.write_bytes(b"not image pixels")
        cls.unsupported.write_text("not submitted", encoding="utf-8")
        cls.directory.mkdir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def setUp(self):
        self.owned = []

    def tearDown(self):
        for navigator, loader in self.owned:
            if loader is not None:
                loader.release_all()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(5.0))

    def make_canvas(self, *, loader=load_source_pixels, objects=(), bounds=(120, 80),
                    preload_count="0"):
        dispatcher = Dispatcher()
        navigator = FileNavigator(
            SettingsStub(preload_count), image_loader=loader,
            result_dispatch=dispatcher)
        self.owned.append((navigator, loader if isinstance(loader, BlockingLoader) else None))
        return DropCanvasHarness(navigator, objects, bounds), navigator, dispatcher

    def test_mixed_real_files_insert_only_valid_in_input_order_and_select_last(self):
        canvas, navigator, dispatcher = self.make_canvas()
        paths = [self.rgb, self.unsupported, self.rgba, self.corrupt,
                 self.missing, self.oriented, self.directory, self.rgb]
        target = FileDropTarget.__new__(FileDropTarget)
        target.canvas_panel = canvas
        started = time.monotonic()
        self.assertTrue(target.OnDropFiles(110, -20, [str(path) for path in paths]))
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(dispatcher.drain_until_terminal(canvas, navigator))

        operation = canvas.drop_operation
        self.assertEqual((operation.total, operation.completed), (8, 8))
        self.assertEqual((operation.succeeded, operation.failed), (4, 4))
        self.assertEqual(
            [Path(obj.source_path).name for obj in canvas.image_objects],
            ["rgb.png", "rgba.png", "oriented.jpg", "rgb.png"])
        self.assertIs(canvas.selected_object, canvas.image_objects[-1])
        self.assertEqual(canvas.preload_targets, [canvas.selected_object])
        self.assertIsNot(canvas.image_objects[0]._original_image,
                         canvas.image_objects[-1]._original_image)
        canvas.image_objects[0]._original_image.putpixel((0, 0), (1, 2, 3))
        self.assertNotEqual(canvas.image_objects[0]._original_image.getpixel((0, 0)),
                            canvas.image_objects[-1]._original_image.getpixel((0, 0)))
        self.assertEqual(canvas.image_objects[2]._original_image.size, (2, 3))
        for obj in canvas.image_objects:
            self.assertEqual(obj.viewport_offset, (0, 0))
            self.assertLessEqual(obj.x + obj.width, 120)
            self.assertLessEqual(obj.y + obj.height, 80)
        preview = canvas.image_objects[1].get_pil_cropped()
        self.addCleanup(preview.close)
        self.assertEqual(preview.getpixel((0, 0))[3], 77)
        exported = self.root / "regression_14-export.png"
        canvas.export_to_file(str(exported))
        with Image.open(exported) as composite:
            self.assertEqual(composite.size, (120, 80))

    def test_out_of_order_results_and_appended_drop_preserve_arrival_order(self):
        loader = BlockingLoader(
            lambda _path, **_kwargs: Image.new("RGB", (4, 4), "purple"))
        paths = [str(self.root / f"order-{index}.png") for index in range(4)]
        for path in paths:
            loader.block(path)
        canvas, navigator, dispatcher = self.make_canvas(loader=loader)
        self.assertTrue(canvas.accept_drop(5, 6, paths[:2]))
        self.assertTrue(loader.wait_for_calls(2))
        self.assertTrue(canvas.accept_drop(40, 30, paths[2:]))
        self.assertEqual(canvas.drop_operation.total, 4)
        self.assertTrue(loader.wait_for_calls(3))

        loader.release(paths[2])
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(4))
        loader.release(paths[3])
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        loader.release(paths[1])
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        loader.release(paths[0])
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()

        self.assertTrue(canvas.drop_operation.terminal)
        self.assertEqual([obj.source_path for obj in canvas.image_objects], paths)
        positions = [entry.position for entry in canvas.drop_operation.entries]
        self.assertEqual(positions, [(5, 6), (25, 26), (40, 30), (60, 50)])

    def test_user_selection_during_loading_is_not_stolen(self):
        existing = ImageObject("existing.png", canvas_width=120, canvas_height=80)
        existing._original_image = Image.new("RGB", (2, 2), "black")
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (3, 3), "red"))
        paths = ["first.png", "second.png"]
        for path in paths:
            loader.block(path)
        canvas, navigator, dispatcher = self.make_canvas(loader=loader, objects=[existing])
        canvas.accept_drop(0, 0, paths)
        self.assertTrue(loader.wait_for_calls(2))
        loader.release(paths[0])
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        canvas.set_selected_object(existing)
        loader.release(paths[1])
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertIs(canvas.selected_object, existing)
        self.assertEqual(canvas.preload_targets, [existing])

    def test_cancel_after_partial_success_rejects_late_results_and_fresh_drop_works(self):
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (3, 3), "red"))
        paths = [f"cancel-{index}.png" for index in range(4)]
        for path in paths:
            loader.block(path)
        canvas, navigator, dispatcher = self.make_canvas(loader=loader)
        canvas.accept_drop(0, 0, paths)
        self.assertTrue(loader.wait_for_calls(3))
        loader.release(paths[0])
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(4))
        self.assertTrue(canvas.cancel_drop_operation())
        self.assertEqual((canvas.drop_operation.succeeded,
                          canvas.drop_operation.canceled_count), (1, 3))
        loader.release_all()
        self.assertTrue(navigator.wait_for_workers(3.0))
        self.assertEqual(dispatcher.items, [])
        self.assertEqual(len(canvas.image_objects), 1)

        fresh = "fresh.png"
        canvas.accept_drop(0, 0, [fresh])
        self.assertTrue(dispatcher.drain_until_terminal(canvas, navigator))
        self.assertEqual(canvas.drop_operation.succeeded, 1)
        self.assertEqual(canvas.image_objects[-1].source_path, fresh)

    def test_cancel_closes_already_dispatched_pixels_and_leaves_navigation_owned(self):
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (3, 3), "red"))
        loader.block("drop.png")
        loader.block("nav.png")
        canvas, navigator, dispatcher = self.make_canvas(loader=loader)
        canvas.accept_drop(0, 0, ["drop.png"])
        navigation_results = []
        navigator.request_navigation_decode(
            "nav.png", "navigation", "context", navigation_results.append)
        self.assertTrue(loader.wait_for_calls(2))
        loader.release("drop.png")
        self.assertTrue(dispatcher.wait())
        candidate = dispatcher.items[0][1]
        pixels = candidate.pixels
        canvas.cancel_drop_operation()
        dispatcher.drain_one()
        self.assertIsNone(candidate.pixels)
        with self.assertRaises(ValueError):
            pixels.getpixel((0, 0))

        loader.release("nav.png")
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertEqual(len(navigation_results), 1)
        navigation_results[0].close()

    def test_large_drop_retains_paths_bounded_and_navigation_runs_first(self):
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (2, 2), "purple"))
        paths = [f"bulk-{index}.png" for index in range(20)]
        for path in paths[:3]:
            loader.block(path)
        navigation = "navigation.png"
        loader.block(navigation)
        canvas, navigator, dispatcher = self.make_canvas(loader=loader)
        canvas.accept_drop(0, 0, paths)
        self.assertTrue(loader.wait_for_calls(3))
        state = navigator.preload_state()
        self.assertEqual(state["drop"], 3)
        self.assertEqual(state["active"], 3)
        self.assertLessEqual(state["pending"], navigator.MAX_PENDING_PRELOADS)
        self.assertEqual(sum(entry.state == "accepted"
                             for entry in canvas.drop_operation.entries), 17)

        navigation_results = []
        self.assertTrue(navigator.request_navigation_decode(
            navigation, "nav", "context", navigation_results.append))
        loader.release(paths[0])
        self.assertTrue(loader.wait_for_calls(4))
        self.assertEqual(loader.calls[3], navigation)
        loader.release(navigation)
        self.assertTrue(dispatcher.wait(2))
        self.assertLessEqual(loader.max_active, navigator.MAX_ACTIVE_DECODES)
        navigation_item = next(index for index, item in enumerate(dispatcher.items)
                               if item[1].target_path == navigation)
        dispatcher.drain_one(navigation_item)
        self.assertEqual(len(navigation_results), 1)
        navigation_results[0].close()

    def test_completed_results_waiting_for_gui_are_bounded_by_submission_window(self):
        canvas, navigator, dispatcher = self.make_canvas(
            loader=lambda _path, **_kwargs: Image.new("RGB", (2, 2), "blue"))
        canvas.accept_drop(0, 0, [f"fast-{index}.png" for index in range(30)])
        self.assertTrue(dispatcher.wait(3))
        self.assertEqual(len(dispatcher.items), CanvasPanel.MAX_DROP_IN_FLIGHT)
        self.assertLessEqual(navigator.preload_state()["pending"],
                             navigator.MAX_PENDING_PRELOADS)

    def test_invalid_bounds_pause_submission_then_resume_without_losing_entry(self):
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (8, 4), "blue"))
        loader.block("paused.png")
        canvas, navigator, dispatcher = self.make_canvas(loader=loader, bounds=(120, 80))
        canvas.accept_drop(-500, 500, ["paused.png"])
        self.assertTrue(loader.wait_for_calls(1))
        canvas.bounds = (0, 0)
        loader.release("paused.png")
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertEqual(canvas.drop_operation.entries[0].state, "waiting_bounds")
        self.assertEqual(len(canvas.image_objects), 0)
        canvas.bounds = (120, 80)
        canvas._pump_drop_operation()
        self.assertTrue(dispatcher.drain_until_terminal(canvas, navigator))
        self.assertEqual(len(canvas.image_objects), 1)
        self.assertEqual((canvas.image_objects[0].x, canvas.image_objects[0].y), (0, 76))

    def test_settings_clear_resubmits_active_entries_and_state_replacement_cancels(self):
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (3, 3), "red"))
        loader.block("settings.png")
        canvas, navigator, dispatcher = self.make_canvas(loader=loader)
        canvas.accept_drop(0, 0, ["settings.png"])
        self.assertTrue(loader.wait_for_calls(1))
        canvas.on_settings_changed()
        loader.release("settings.png")
        self.assertTrue(loader.wait_for_calls(2))
        self.assertTrue(dispatcher.drain_until_terminal(canvas, navigator))
        self.assertEqual(canvas.drop_operation.succeeded, 1)

        loader.block("state.png")
        canvas.accept_drop(0, 0, ["state.png"])
        self.assertTrue(loader.wait_for_calls(3))
        state = self.root / "regression_14-replacement.json"
        state.write_text("[]", encoding="utf-8")
        canvas.load_canvas_state(str(state))
        loader.release("state.png")
        self.assertTrue(navigator.wait_for_workers(3.0))
        self.assertEqual(dispatcher.items, [])
        self.assertIsNone(canvas.drop_operation)
        self.assertEqual(len(canvas.image_objects), 0)

    def test_shutdown_is_nonblocking_and_clears_drop_feedback(self):
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (3, 3), "red"))
        loader.block("slow.png")
        canvas, navigator, _ = self.make_canvas(loader=loader)
        canvas.accept_drop(0, 0, ["slow.png"])
        self.assertTrue(loader.wait_for_calls(1))
        started = time.monotonic()
        self.assertTrue(canvas.shutdown_preloading())
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIsNone(canvas.drop_operation)
        loader.release("slow.png")

    def test_card_is_clamped_summarizes_failures_and_cancel_hit_is_scoped(self):
        loader = BlockingLoader(lambda _path, **_kwargs: Image.new("RGB", (3, 3), "red"))
        loader.block("card.png")
        canvas, _, _ = self.make_canvas(loader=loader, bounds=(240, 140))
        canvas.accept_drop(-500, 500, ["card.png", "bad.xyz"])
        dc = mock.Mock()
        dc.GetTextExtent.side_effect = lambda text: wx.Size(len(text) * 5, 12)
        self.assertTrue(canvas._draw_drop_status(dc))
        x, y, width, height = canvas._drop_card_rect
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, 240)
        self.assertLessEqual(y + height, 140)
        cancel_x, cancel_y, _, _ = canvas._drop_cancel_rect
        self.assertFalse(canvas._handle_drop_card_click(239, 0))
        self.assertTrue(canvas._handle_drop_card_click(cancel_x + 1, cancel_y + 1))
        self.assertTrue(canvas.drop_operation.terminal)
        self.assertEqual(canvas.drop_operation.canceled_count, 1)
        self.assertEqual(canvas.drop_operation.failed, 1)


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1 to run visible drop feedback")
class TestVisibleDropFeedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_empty_canvas_off_canvas_drop_paints_card_and_cancel_handler(self):
        settings = SettingsStub("0")
        frame = MainFrame(None, "Regression 14 feedback", settings, debug_mode=True)
        frame.SetClientSize((240, 140))
        canvas = frame.canvas_panel
        original = canvas.file_navigator
        original.shutdown()
        self.assertTrue(original.wait_for_workers(2.0))
        loader = BlockingLoader(
            lambda _path, **_kwargs: Image.new("RGB", (4, 4), "blue"))
        loader.block("visible.png")
        navigator = FileNavigator(
            settings, image_loader=loader,
            result_dispatch=lambda callback, result: wx.CallAfter(callback, result))
        canvas.file_navigator = navigator
        painted = threading.Event()
        canceled = threading.Event()
        elapsed = []
        original_draw = CanvasPanel._draw_drop_status
        started = time.monotonic()

        def observed_draw(panel, dc):
            result = original_draw(panel, dc)
            if result and panel.drop_operation and not panel.drop_operation.terminal:
                elapsed.append(time.monotonic() - started)
                painted.set()
            return result

        loop = wx.GUIEventLoop()
        activator = wx.EventLoopActivator(loop)
        try:
            frame.Show()
            with mock.patch.object(CanvasPanel, "_draw_drop_status", observed_draw):
                wx.CallAfter(canvas.accept_drop, -500, 500, ["visible.png"])

                def finish_when_visible():
                    if painted.is_set() and not canceled.is_set():
                        x, y, _, _ = canvas._drop_cancel_rect
                        if canvas._handle_drop_card_click(x + 1, y + 1):
                            canceled.set()
                    if canceled.is_set() or time.monotonic() - started > 2.0:
                        loop.Exit()
                    else:
                        wx.CallLater(10, finish_when_visible)

                wx.CallLater(10, finish_when_visible)
                loop.Run()
            self.assertTrue(painted.is_set())
            self.assertTrue(canceled.is_set())
            self.assertLess(elapsed[0], 0.1)
            self.assertEqual(len(canvas.image_objects), 0)
            self.assertTrue(canvas.drop_operation.terminal)
            print(f"Regression 14 visible acknowledgement: {elapsed[0] * 1000:.1f} ms")
        finally:
            loader.release("visible.png")
            navigator.shutdown()
            navigator.wait_for_workers(3.0)
            frame.Destroy()
            del activator
            wx.Yield()


if __name__ == "__main__":
    unittest.main()
