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

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.file_navigator import (
    DirectoryDiscovery,
    DirectorySnapshot,
    FileNavigator,
    NavigationDecodeResult,
    path_comparison_key,
)
from src.image_object import ImageObject
from src.image_pixels import load_source_pixels
from src.main_frame import MainFrame


class SettingsStub:
    def __init__(self, preload_count="0"):
        self.preload_count = preload_count

    def get_setting(self, section, key, fallback=None):
        values = {
            ("Navigation", "preload_count"): self.preload_count,
            ("Navigation", "enable_wheel_navigation"): "true",
            ("Navigation", "sort_method"): "name_asc",
            ("Canvas", "background_color"): "#303030",
            ("UI", "overlay_timeout_ms"): "1500",
        }
        return values.get((section, key), fallback)

    def save(self):
        pass


class Dispatcher:
    """Queue worker results until the test's simulated GUI thread drains them."""

    def __init__(self):
        self.condition = threading.Condition()
        self.items = []
        self.dispatch_threads = []
        self.drain_started = threading.Event()

    def __call__(self, callback, result):
        with self.condition:
            self.items.append((callback, result))
            self.dispatch_threads.append(threading.current_thread().name)
            self.condition.notify_all()

    def wait(self, count=1, timeout=2.0):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.items) >= count, timeout)

    def drain_one(self):
        with self.condition:
            callback, result = self.items.pop(0)
        callback(result)
        return result

    def drain_until_idle(self, navigator, timeout=3.0):
        self.drain_started.set()
        deadline = time.monotonic() + timeout
        terminal_seen = False
        while time.monotonic() < deadline:
            with self.condition:
                item = self.items.pop(0) if self.items else None
            if item is not None:
                callback, result = item
                callback(result)
                terminal_seen = terminal_seen or isinstance(
                    result, NavigationDecodeResult)
                continue
            state = navigator.preload_state()
            if (terminal_seen and state["jobs"] == 0
                    and not state["discovery_active"]):
                return True
            with self.condition:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining:
                    self.condition.wait(min(0.05, remaining))
        return False


class DispatchGate:
    """Hold one callback after worker retirement to expose dispatch races."""

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_next = True

    def __call__(self, callback, result):
        if self.block_next:
            self.block_next = False
            self.entered.set()
            if not self.release.wait(2.0):
                raise TimeoutError("test did not release callback dispatch")
        self.dispatcher(callback, result)


class FakeTimer:
    def __init__(self):
        self.running = False

    def IsRunning(self):
        return self.running

    def Start(self, *_args):
        self.running = True

    def Stop(self):
        self.running = False


class CanvasHarness:
    get_selected_object = CanvasPanel.get_selected_object
    set_selected_object = CanvasPanel.set_selected_object
    _navigate_to_adjacent_file = CanvasPanel._navigate_to_adjacent_file
    _on_navigation_discovered = CanvasPanel._on_navigation_discovered
    _apply_navigation_result = CanvasPanel._apply_navigation_result
    _on_navigation_decoded = CanvasPanel._on_navigation_decoded
    _draw_canvas_navigation_status = CanvasPanel._draw_canvas_navigation_status
    _reset_zoom_wheel_remainder = CanvasPanel._reset_zoom_wheel_remainder
    _get_overlay_timeout_ms = CanvasPanel._get_overlay_timeout_ms
    _schedule_overlay_clear = CanvasPanel._schedule_overlay_clear
    _stop_overlay_timer = CanvasPanel._stop_overlay_timer
    _reschedule_overlay_timer = CanvasPanel._reschedule_overlay_timer
    _zoom_selected_image = CanvasPanel._zoom_selected_image
    remove_image_object = CanvasPanel.remove_image_object
    on_settings_changed = CanvasPanel.on_settings_changed
    shutdown_preloading = CanvasPanel.shutdown_preloading
    load_canvas_state = CanvasPanel.load_canvas_state
    debug_image_objects = CanvasPanel.debug_image_objects
    start_preloading_for_object = CanvasPanel.start_preloading_for_object

    def __init__(self, image_objects, navigator, *, bounds=(100, 80)):
        self.image_objects = ImageObjectList(image_objects)
        self.selected_object = image_objects[0] if image_objects else None
        self.marked_object = None
        self.drag_offset = None
        self._zoom_wheel_remainders = {}
        self.file_navigator = navigator
        self.settings_manager = navigator.settings_manager
        self.overlay_clear_timer = FakeTimer()
        self._monotonic = time.monotonic
        self.bounds = bounds
        self.refresh_count = 0

    def Refresh(self):
        self.refresh_count += 1

    def get_client_dimensions(self):
        return self.bounds

    def GetClientSize(self):
        return self.bounds

    def GetSize(self):
        return self.bounds


class PathBlockingLoader:
    def __init__(self, delegate=load_source_pixels):
        self.delegate = delegate
        self.condition = threading.Condition()
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.releases = {}
        self.on_call = None

    def block(self, path):
        self.releases[path] = threading.Event()

    def release(self, path):
        self.releases[path].set()

    def release_all(self):
        for event in self.releases.values():
            event.set()

    def __call__(self, path, **kwargs):
        with self.condition:
            self.calls.append(path)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
        try:
            if self.on_call is not None:
                self.on_call(path)
            release = self.releases.get(path)
            if release is not None and not release.wait(5.0):
                raise TimeoutError(f"test did not release {path}")
            return self.delegate(path, **kwargs)
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def wait_for_calls(self, count, timeout=2.0):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.calls) >= count, timeout)


class TestAsyncTransactionalNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)
        cls.root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        cls.a = cls.root / "a.png"
        cls.b = cls.root / "b.png"
        cls.bad = cls.root / "b-bad.png"
        cls.c = cls.root / "c.png"
        cls.oriented = cls.root / "oriented.jpg"
        Image.new("RGB", (40, 20), "red").save(cls.a)
        alpha = Image.new("RGBA", (20, 40), (0, 200, 0, 100))
        alpha.save(cls.b)
        cls.bad.write_bytes(b"not an image")
        pixels = Image.new("RGB", (60, 10), "blue")
        pixels.putpixel((59, 9), (255, 255, 0))
        pixels.save(cls.c)
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (3, 2), "orange").save(cls.oriented, exif=exif)

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

    def navigator(self, *, loader=None, reader=None, preload_count="0",
                  result_dispatch=None):
        dispatcher = Dispatcher()
        navigator = FileNavigator(
            SettingsStub(preload_count), image_loader=loader,
            directory_reader=reader,
            result_dispatch=result_dispatch or dispatcher)
        self.owned.append((navigator, loader if isinstance(loader, PathBlockingLoader) else None))
        return navigator, dispatcher

    def object_for(self, path=None):
        obj = ImageObject(str(path or self.a), canvas_width=100, canvas_height=80)
        obj.load_image()
        obj.width, obj.height = 37, 19
        obj.x, obj.y = 80, 70
        obj.zoom_factor = 1.75
        obj.viewport_offset = (3, 2)
        return obj

    def cache_snapshot(self, navigator, current, files=None):
        files = tuple(str(path) for path in (files or (self.a, self.b, self.c)))
        snapshot = DirectorySnapshot(
            str(self.root), files,
            tuple(path_comparison_key(path) for path in files), True)
        with navigator._lock:
            navigator._file_cache[navigator._directory_key_for(str(current))] = snapshot

    def test_cold_decode_keeps_old_composition_until_transactional_commit(self):
        loader = PathBlockingLoader()
        loader.block(str(self.b))
        navigator, dispatcher = self.navigator(loader=loader)
        self.cache_snapshot(navigator, self.a)
        obj = self.object_for()
        old = (obj.source_path, obj._original_image, obj.x, obj.y, obj.width,
               obj.height, obj.zoom_factor, obj.viewport_offset)
        canvas = CanvasHarness([obj], navigator)

        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()  # discovery -> foreground decode
        self.assertTrue(loader.wait_for_calls(1))
        self.assertEqual(
            (obj.source_path, obj._original_image, obj.x, obj.y, obj.width,
             obj.height, obj.zoom_factor, obj.viewport_offset), old)
        self.assertEqual(obj.status_type, "processing")
        self.assertIn("Loading", obj.status_message)

        loader.release(str(self.b))
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertEqual(obj.source_path, str(self.b))
        self.assertEqual((obj.width, obj.height), (20, 40))
        self.assertEqual((obj.x, obj.y), (80, 40))
        self.assertEqual(obj.viewport_offset, (0, 0))
        self.assertEqual(obj._original_image.mode, "RGBA")
        self.assertEqual(obj._original_image.getpixel((0, 0))[3], 100)

    def test_cached_pixels_are_transferred_on_a_worker_without_gui_copy(self):
        loader = PathBlockingLoader()
        navigator, dispatcher = self.navigator(loader=loader)
        self.assertTrue(navigator.request_preloads([str(self.b)]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(loader.calls, [str(self.b)])
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        self.cache_snapshot(navigator, self.a)

        with mock.patch.object(Image.Image, "copy", side_effect=AssertionError(
                "foreground cache transfer must not copy source pixels")):
            canvas._navigate_to_adjacent_file(previous=False)
            self.assertTrue(dispatcher.wait())
            dispatcher.drain_one()
            self.assertTrue(dispatcher.wait())
            dispatcher.drain_one()

        self.assertEqual(loader.calls, [str(self.b)])
        self.assertEqual(obj.source_path, str(self.b))
        self.assertTrue(any(name.startswith("nagumix-foreground")
                            for name in dispatcher.dispatch_threads))

    def test_corrupt_candidate_preserves_a_and_next_continues_to_c(self):
        navigator, dispatcher = self.navigator(loader=load_source_pixels)
        self.cache_snapshot(navigator, self.a, (self.a, self.bad, self.b, self.c))
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        old_pixels = obj._original_image

        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.a))
        self.assertIs(obj._original_image, old_pixels)
        self.assertEqual(obj.status_type, "warning")
        self.assertIn(self.bad.name, obj.status_message)

        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.b))
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.c))
        exported = obj.get_pil_cropped()
        self.addCleanup(exported.close)
        self.assertEqual(exported.getpixel((59, 9))[:3], (255, 255, 0))

    def test_drain_waits_for_callback_after_worker_retirement(self):
        dispatcher = Dispatcher()
        gate = DispatchGate(dispatcher)
        navigator, _ = self.navigator(
            loader=load_source_pixels, result_dispatch=gate)
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)

        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(gate.entered.wait(1.0))

        drained = []
        finished = threading.Event()

        def drain():
            drained.append(dispatcher.drain_until_idle(navigator))
            finished.set()

        thread = threading.Thread(target=drain)
        thread.start()
        try:
            self.assertTrue(dispatcher.drain_started.wait(1.0))
            self.assertFalse(finished.wait(0.2))
            gate.release.set()
            self.assertTrue(finished.wait(2.0))
            self.assertEqual(drained, [True])
            self.assertEqual(obj.source_path, str(self.a))
            self.assertEqual(obj.status_type, "warning")
        finally:
            gate.release.set()
            thread.join(2.0)

    def test_delayed_discovery_coalesces_next_next_to_latest_target(self):
        entered = threading.Event()
        release = threading.Event()

        def reader(current):
            entered.set()
            self.assertTrue(release.wait(5.0))
            directory = os.path.dirname(current)
            files = tuple(str(path) for path in (self.a, self.b, self.c))
            return DirectoryDiscovery(snapshot=DirectorySnapshot(
                directory, files, tuple(path_comparison_key(path) for path in files), True))

        navigator, dispatcher = self.navigator(loader=load_source_pixels, reader=reader)
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(entered.wait(2.0))
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertEqual(obj.source_path, str(self.a))
        release.set()
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.c))

    def test_next_previous_cancels_delayed_decode_without_flashing_candidate(self):
        loader = PathBlockingLoader()
        loader.block(str(self.b))
        navigator, dispatcher = self.navigator(loader=loader)
        self.cache_snapshot(navigator, self.a)
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(1))

        canvas._navigate_to_adjacent_file(previous=True)
        self.assertEqual(obj.source_path, str(self.a))
        self.assertFalse(obj.show_status_overlay)
        loader.release(str(self.b))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(dispatcher.items, [])
        self.assertEqual(obj.source_path, str(self.a))

    def test_reverse_completion_only_commits_latest_request(self):
        loader = PathBlockingLoader()
        loader.block(str(self.bad))
        loader.block(str(self.b))
        navigator, dispatcher = self.navigator(loader=loader)
        self.cache_snapshot(navigator, self.a, (self.a, self.bad, self.b))
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)

        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(1))
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(2))

        loader.release(str(self.b))
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertEqual(obj.source_path, str(self.b))
        loader.release(str(self.bad))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(dispatcher.items, [])

    def test_invalid_bounds_release_candidate_and_preserve_old_pixels(self):
        navigator, dispatcher = self.navigator(loader=load_source_pixels)
        self.cache_snapshot(navigator, self.a)
        obj = self.object_for()
        old_pixels = obj._original_image
        canvas = CanvasHarness([obj], navigator, bounds=(0, 0))
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.a))
        self.assertIs(obj._original_image, old_pixels)
        self.assertIn("fit", obj.status_message.lower())

    def test_foreground_decode_normalizes_exif_orientation(self):
        navigator, dispatcher = self.navigator(loader=load_source_pixels)
        results = []
        self.assertTrue(navigator.request_navigation_decode(
            str(self.oriented), "orientation", "context", results.append))
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertEqual(len(results), 1)
        candidate = results[0]
        self.addCleanup(candidate.close)
        self.assertEqual(candidate.pixels.size, (2, 3))
        self.assertEqual(candidate.pixels.mode, "RGB")

    def test_previous_wrap_feedback_names_committed_target(self):
        navigator, dispatcher = self.navigator(loader=load_source_pixels)
        self.cache_snapshot(navigator, self.a)
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        canvas._navigate_to_adjacent_file(previous=True)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.c))
        self.assertEqual(obj.status_message, f"Wrapped to {self.c.name}")
        self.assertIsNotNone(obj.status_deadline)

    def test_duplicate_source_objects_receive_independent_pixels_and_caches(self):
        navigator, dispatcher = self.navigator(loader=load_source_pixels)
        self.cache_snapshot(navigator, self.a)
        first, second = self.object_for(), self.object_for()
        canvas = CanvasHarness([first, second], navigator)
        first._prepared_bitmap = object()
        second._prepared_bitmap = object()
        canvas.set_selected_object(first)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertIsNone(first._prepared_bitmap)
        self.assertIsNotNone(second._prepared_bitmap)
        canvas.set_selected_object(second)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(first.source_path, second.source_path)
        self.assertIsNot(first._original_image, second._original_image)
        first._original_image.putpixel((0, 0), (1, 2, 3, 4))
        self.assertNotEqual(first._original_image.getpixel((0, 0)),
                            second._original_image.getpixel((0, 0)))

    def test_zoom_cancels_decode_and_fresh_navigation_still_succeeds(self):
        loader = PathBlockingLoader()
        loader.block(str(self.b))
        navigator, dispatcher = self.navigator(loader=loader)
        self.cache_snapshot(navigator, self.a)
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(1))

        canvas._zoom_selected_image(zoom_in=True)
        zoomed = obj.zoom_factor
        loader.release(str(self.b))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(dispatcher.items, [])
        self.assertEqual(obj.source_path, str(self.a))
        self.assertEqual(obj.zoom_factor, zoomed)

        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.b))

    def test_selection_and_deletion_cancel_owned_decode(self):
        loader = PathBlockingLoader()
        loader.block(str(self.b))
        navigator, dispatcher = self.navigator(loader=loader)
        self.cache_snapshot(navigator, self.a)
        first, second = self.object_for(), self.object_for(self.c)
        canvas = CanvasHarness([first, second], navigator)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(1))
        canvas.set_selected_object(second)
        loader.release(str(self.b))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(dispatcher.items, [])
        self.assertEqual(first.source_path, str(self.a))

        canvas.set_selected_object(first)
        loader.block(str(self.b))
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(2))
        self.assertTrue(canvas.remove_image_object(first))
        loader.release(str(self.b))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(dispatcher.items, [])

    def test_settings_clear_releases_dispatched_candidate_and_allows_fresh_request(self):
        navigator, dispatcher = self.navigator(loader=load_source_pixels)
        self.cache_snapshot(navigator, self.a)
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(dispatcher.wait())
        candidate = dispatcher.items[0][1]
        candidate_pixels = candidate.pixels

        canvas.on_settings_changed()
        dispatcher.drain_one()
        self.assertIsNone(candidate.pixels)
        with self.assertRaises(ValueError):
            candidate_pixels.getpixel((0, 0))
        self.assertEqual(obj.source_path, str(self.a))

        self.cache_snapshot(navigator, self.a)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(self.b))

    def test_state_replacement_cancels_running_decode_and_ignores_late_result(self):
        loader = PathBlockingLoader()
        loader.block(str(self.b))
        navigator, dispatcher = self.navigator(loader=loader)
        self.cache_snapshot(navigator, self.a)
        obj = self.object_for()
        canvas = CanvasHarness([obj], navigator)
        canvas._navigate_to_adjacent_file(previous=False)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(1))

        state = self.root / "replacement-state.json"
        state.write_text(
            '[{"source_path": "replacement.png", "x": 1, "y": 2, '
            '"width": 3, "height": 4, "zoom_factor": 1.0, '
            '"viewport_offset": [0, 0]}]', encoding="utf-8")
        canvas.load_canvas_state(str(state))
        loader.release(str(self.b))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(dispatcher.items, [])
        self.assertEqual(len(canvas.image_objects), 1)
        self.assertEqual(canvas.image_objects[0].source_path, "replacement.png")

    def test_canvas_level_status_uses_same_object_status_off_canvas(self):
        navigator, _ = self.navigator(loader=load_source_pixels)
        obj = self.object_for()
        obj.x, obj.y, obj.width, obj.height = -500, -500, 1, 1
        obj.set_status_overlay(
            "Loading candidate.png...", "processing", operation="navigation")
        canvas = CanvasHarness([obj], navigator)
        dc = mock.Mock()
        dc.GetTextExtent.return_value = wx.Size(120, 12)
        self.assertTrue(canvas._draw_canvas_navigation_status(dc))
        dc.DrawText.assert_called_once_with("Loading candidate.png...", 18, 18)


class TestForegroundScheduler(unittest.TestCase):
    def tearDown(self):
        for navigator, loader in getattr(self, "owned", []):
            loader.release_all()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(5.0))

    def own(self, navigator, loader):
        self.owned = getattr(self, "owned", []) + [(navigator, loader)]
        return navigator

    @staticmethod
    def pixels(_path, **_kwargs):
        return Image.new("RGB", (2, 2), "purple")

    def test_three_total_workers_bounded_queue_and_foreground_priority(self):
        loader = PathBlockingLoader(self.pixels)
        preload_paths = [f"preload-{index}.png" for index in range(13)]
        for path in preload_paths[:3]:
            loader.block(path)
        navigator = self.own(FileNavigator(SettingsStub("5"), image_loader=loader), loader)
        self.assertTrue(navigator.request_preloads(preload_paths))
        self.assertTrue(loader.wait_for_calls(3))

        results = []
        self.assertTrue(navigator.request_navigation_decode(
            "foreground.png", "object", "request", results.append))
        state = navigator.preload_state()
        self.assertEqual(state["active"], 3)
        self.assertLessEqual(state["pending"], navigator.MAX_PENDING_PRELOADS)
        self.assertEqual(state["queued"][0], "foreground.png")

        loader.release(preload_paths[0])
        self.assertTrue(loader.wait_for_calls(4))
        self.assertEqual(loader.calls[3], "foreground.png")
        self.assertLessEqual(loader.max_active, navigator.MAX_ACTIVE_DECODES)
        self.assertTrue(navigator.wait_for_workers(0.0) is False)

    def test_matching_queued_preload_is_promoted_and_decoded_once(self):
        loader = PathBlockingLoader(self.pixels)
        blockers = [f"active-{index}.png" for index in range(3)]
        for path in blockers:
            loader.block(path)
        target = "target.png"
        navigator = self.own(FileNavigator(SettingsStub("5"), image_loader=loader), loader)
        self.assertTrue(navigator.request_preloads(blockers + [target]))
        self.assertTrue(loader.wait_for_calls(3))
        results = []
        self.assertTrue(navigator.request_navigation_decode(
            target, "object", "request", results.append))
        self.assertEqual(navigator.preload_state()["queued"][0], target)
        loader.release(blockers[0])
        self.assertTrue(loader.wait_for_calls(4))
        self.assertEqual(loader.calls.count(target), 1)
        deadline = time.monotonic() + 2.0
        while not results and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(results), 1)
        self.addCleanup(results[0].close)

    def test_clear_and_shutdown_drop_paused_foreground_results_nonblocking(self):
        for operation in ("clear", "shutdown"):
            with self.subTest(operation=operation):
                loader = PathBlockingLoader(self.pixels)
                loader.block("blocked.png")
                navigator = self.own(
                    FileNavigator(SettingsStub(), image_loader=loader), loader)
                results = []
                navigator.request_navigation_decode(
                    "blocked.png", operation, operation, results.append)
                self.assertTrue(loader.wait_for_calls(1))
                started = time.monotonic()
                if operation == "clear":
                    navigator.clear_cache()
                else:
                    navigator.shutdown()
                self.assertLess(time.monotonic() - started, 0.5)
                loader.release("blocked.png")
                self.assertTrue(navigator.wait_for_workers(2.0))
                self.assertEqual(results, [])


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1 to run visible navigation feedback")
class TestVisibleNavigationFeedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_paused_decode_acknowledges_tiny_off_canvas_selection(self):
        # perf_counter is monotonic and retains high resolution when Windows
        # time.monotonic maps to a coarser system tick.
        phase_clock = time.perf_counter
        phase_clock_info = time.get_clock_info("perf_counter")
        self.assertTrue(phase_clock_info.monotonic)

        def is_feedback_present(shown, processing, draw_returned,
                                paint_returned, pixel):
            if not (shown and processing and draw_returned and paint_returned
                    and pixel is not None):
                return False
            red, green, blue = pixel[:3]
            return red >= 200 and green >= 100 and blue < 80

        settings = SettingsStub("0")
        frame_ref = [None]
        canvas_ref = [None]
        readiness_loop = wx.GUIEventLoop()
        startup = {}
        startup_check_pending = [False]
        started = [None]
        feedback_loop_ref = [None]
        active = [True]
        phases = {}
        visible_elapsed = []
        status_drawn = [False]
        surface_check_pending = [False]
        worker_heartbeat = threading.Event()
        worker_state = {}
        feedback_presented = threading.Event()
        scheduled = []
        frame = None
        original_navigator = None
        loader = None
        navigator = None
        readiness_activator = None
        feedback_activator = None
        cleanup_state = {"done": False, "workers_stopped": True}

        def mark(name):
            phases.setdefault(name, phase_clock())

        def sample_startup_surface(panel):
            if not active[0] or panel is not canvas_ref[0]:
                return
            try:
                startup["pixel"] = tuple(
                    wx.ClientDC(panel).GetPixel(12, 12).Get()[:3])
            except Exception as exc:
                startup["error"] = repr(exc)
            frame = frame_ref[0]
            startup.update({
                "completed": True,
                "frame_shown": bool(frame.IsShownOnScreen()),
                "canvas_shown": bool(panel.IsShownOnScreen()),
                "iconized": bool(frame.IsIconized()),
                "client_size": tuple(panel.GetClientSize()),
                "scale": float(panel.GetContentScaleFactor()),
                "time": phase_clock(),
            })
            readiness_loop.Exit()

        def sample_feedback_surface(panel, draw_returned, paint_returned_at):
            surface_check_pending[0] = False
            if not active[0] or panel is not canvas_ref[0]:
                return
            mark("paint_handler_complete")
            status = panel.selected_object
            try:
                pixel = tuple(wx.ClientDC(panel).GetPixel(12, 12).Get()[:3])
            except Exception as exc:
                phases.setdefault("surface_read_error", repr(exc))
                return
            processing = bool(
                status is not None and status.status_type == "processing"
                and status.status_operation == "navigation")
            shown = bool(frame_ref[0].IsShownOnScreen()
                         and panel.IsShownOnScreen())
            if is_feedback_present(
                    shown, processing, draw_returned, True, pixel):
                now = phase_clock()
                visible_elapsed.append(now - started[0])
                phases.setdefault("visible_pixel", pixel)
                phases.setdefault("visible_surface", now)
                phases.setdefault("paint_return", paint_returned_at)
                feedback_presented.set()
                if worker_heartbeat.is_set() and feedback_loop_ref[0] is not None:
                    feedback_loop_ref[0].Exit()

        def decoder_heartbeat_on_gui():
            if not active[0]:
                return
            with loader.condition:
                worker_state["active"] = loader.active > 0
                worker_state["release_gate_closed"] = (
                    not loader.releases[target].is_set())
            mark("decoder_heartbeat")
            worker_heartbeat.set()
            if feedback_presented.is_set() and feedback_loop_ref[0] is not None:
                feedback_loop_ref[0].Exit()

        def notify_decoder_call(path):
            if path == target:
                wx.CallAfter(decoder_heartbeat_on_gui)

        def cleanup_test_resources():
            nonlocal readiness_activator, feedback_activator
            if cleanup_state["done"]:
                return
            cleanup_state["done"] = True
            active[0] = False
            for timer in scheduled:
                timer.Stop()
            if loader is not None:
                loader.release_all()
            navigators = [item for item in (navigator, original_navigator)
                          if item is not None]
            if navigator is not None and original_navigator is not None:
                navigators = [navigator, original_navigator]
            for owned_navigator in navigators:
                owned_navigator.shutdown()
            try:
                for owned_navigator in navigators:
                    stopped = owned_navigator.wait_for_workers(3.0)
                    cleanup_state["workers_stopped"] &= stopped
            finally:
                if frame is not None:
                    frame.Destroy()
                readiness_activator = None
                feedback_activator = None
                wx.Yield()

        original_paint = CanvasPanel.on_paint

        def observed_paint(panel, event):
            target_canvas = panel is canvas_ref[0]
            if target_canvas:
                status_drawn[0] = False
                if started[0] is not None:
                    mark("paint_entry")
            result = original_paint(panel, event)
            if not target_canvas or not active[0]:
                return result
            if started[0] is None:
                if not startup_check_pending[0]:
                    startup_check_pending[0] = True
                    wx.CallAfter(sample_startup_surface, panel)
            elif status_drawn[0] and not surface_check_pending[0]:
                surface_check_pending[0] = True
                paint_returned_at = phase_clock()
                wx.CallAfter(
                    sample_feedback_surface, panel, True, paint_returned_at)
            return result

        with mock.patch.object(CanvasPanel, "on_paint", observed_paint):
            frame = MainFrame(None, "Regression 13 feedback", settings,
                              debug_mode=True)
        self.addCleanup(cleanup_test_resources)
        frame_ref[0] = frame
        frame.SetClientSize((220, 120))
        canvas = frame.canvas_panel
        canvas_ref[0] = canvas
        original_navigator = canvas.file_navigator
        original_navigator.shutdown()
        self.assertTrue(original_navigator.wait_for_workers(2.0))
        loader = PathBlockingLoader(
            lambda _path, **_kwargs: Image.new("RGB", (4, 4), "blue"))
        target = "b.png"
        loader.block(target)
        loader.on_call = notify_decoder_call
        navigator = FileNavigator(
            settings, image_loader=loader,
            result_dispatch=lambda callback, result: wx.CallAfter(callback, result))
        canvas.file_navigator = navigator
        obj = ImageObject("a.png", canvas_width=220, canvas_height=120)
        obj._original_image = Image.new("RGB", (1, 1), "red")
        obj.x, obj.y, obj.width, obj.height = -500, -500, 1, 1
        original_pixels = obj._original_image
        original_geometry = (
            obj.x, obj.y, obj.width, obj.height, obj.zoom_factor,
            obj.viewport_offset)
        canvas.image_objects.append(obj)
        canvas.selected_object = obj
        snapshot = DirectorySnapshot(
            "", ("a.png", target),
            (path_comparison_key("a.png"), path_comparison_key(target)), True)
        navigator._file_cache[navigator._directory_key_for("a.png")] = snapshot

        startup_watchdog = None
        feedback_watchdog = None
        original_navigate = CanvasPanel._navigate_to_adjacent_file
        original_set_status = ImageObject.set_status_overlay
        original_refresh = CanvasPanel.Refresh
        original_draw = CanvasPanel._draw_canvas_navigation_status

        def observed_navigate(panel, *args, **kwargs):
            if panel is canvas:
                mark("handler_entry")
            return original_navigate(panel, *args, **kwargs)

        def observed_set_status(image_object, message, status_type="info",
                                operation=None):
            result = original_set_status(
                image_object, message, status_type, operation)
            if (image_object is obj and status_type == "processing"
                    and operation == "navigation"):
                mark("status_updated")
            return result

        def observed_refresh(panel, *args, **kwargs):
            selected = getattr(panel, "selected_object", None)
            if (panel is canvas and started[0] is not None and selected is not None
                    and selected.status_type == "processing"
                    and selected.status_operation == "navigation"):
                mark("paint_requested")
            return original_refresh(panel, *args, **kwargs)

        def observed_draw(panel, dc):
            result = original_draw(panel, dc)
            if (panel is canvas and result and panel.selected_object is not None
                    and panel.selected_object.status_type == "processing"
                    and panel.selected_object.status_operation == "navigation"):
                status_drawn[0] = True
                mark("status_draw_return")
            return result

        try:
            readiness_activator = wx.EventLoopActivator(readiness_loop)
            shown_at = phase_clock()
            frame.Show()
            startup_watchdog = wx.CallLater(2000, readiness_loop.Exit)
            scheduled.append(startup_watchdog)
            readiness_loop.Run()
            startup_watchdog.Stop()
            del readiness_activator
            readiness_activator = None
            self.assertTrue(startup.get("completed"),
                            "No completed initial canvas paint was observed")
            self.assertTrue(startup.get("frame_shown"))
            self.assertTrue(startup.get("canvas_shown"))
            self.assertFalse(startup.get("iconized"))
            self.assertGreater(startup.get("client_size", (0, 0))[0], 0)
            self.assertGreater(startup.get("client_size", (0, 0))[1], 0)
            background_pixel = startup.get("pixel")
            self.assertIsNotNone(background_pixel, startup.get("error"))
            self.assertEqual(background_pixel, (48, 48, 48))
            # Controlled missing-presentation case: a helper return and a
            # completed paint still fail when the real client surface is blank.
            self.assertFalse(is_feedback_present(
                startup["frame_shown"] and startup["canvas_shown"],
                True, True, True, background_pixel))

            loop = wx.GUIEventLoop()
            feedback_loop_ref[0] = loop
            feedback_activator = wx.EventLoopActivator(loop)
            with mock.patch.object(
                    CanvasPanel, "_navigate_to_adjacent_file", observed_navigate), \
                 mock.patch.object(
                    ImageObject, "set_status_overlay", observed_set_status), \
                 mock.patch.object(CanvasPanel, "Refresh", observed_refresh), \
                 mock.patch.object(
                    CanvasPanel, "_draw_canvas_navigation_status", observed_draw):
                started[0] = phase_clock()
                wx.CallAfter(canvas._navigate_to_adjacent_file, False)

                def feedback_timeout():
                    phases.setdefault("watchdog", phase_clock())
                    loop.Exit()

                feedback_watchdog = wx.CallLater(2000, feedback_timeout)
                scheduled.append(feedback_watchdog)
                loop.Run()
            self.assertTrue(feedback_presented.is_set(),
                            "Navigation feedback missed the visible client surface")
            self.assertTrue(worker_heartbeat.is_set(),
                            "The held decoder did not yield a GUI heartbeat")
            self.assertTrue(worker_state.get("active"))
            self.assertTrue(worker_state.get("release_gate_closed"))
            self.assertEqual(obj.status_type, "processing")
            self.assertEqual(obj.status_operation, "navigation")
            self.assertLess(visible_elapsed[0], 0.1)
            print(
                "Regression 13 phases (ms): "
                f"show-to-ready={(startup['time'] - shown_at) * 1000:.3f}, "
                f"queue-to-handler={(phases['handler_entry'] - started[0]) * 1000:.3f}, "
                f"handler-to-status={(phases['status_updated'] - phases['handler_entry']) * 1000:.3f}, "
                f"status-to-refresh={(phases['paint_requested'] - phases['status_updated']) * 1000:.3f}, "
                f"refresh-to-paint={(phases['paint_entry'] - phases['paint_requested']) * 1000:.3f}, "
                f"paint-return-to-check={(phases['paint_handler_complete'] - phases['paint_return']) * 1000:.3f}, "
                f"request-to-visible={visible_elapsed[0] * 1000:.3f}, "
                f"clock-resolution-ns={phase_clock_info.resolution * 1_000_000_000:.0f}, "
                f"scale={startup['scale']:.1f}, "
                f"shown={startup['frame_shown'] and startup['canvas_shown']}, "
                f"pixel={phases['visible_pixel']}"
            )
            self.assertEqual(obj.source_path, "a.png")
            self.assertIs(obj._original_image, original_pixels)
            self.assertEqual(obj._original_image.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(
                (obj.x, obj.y, obj.width, obj.height, obj.zoom_factor,
                 obj.viewport_offset), original_geometry)
        finally:
            cleanup_test_resources()
            if feedback_activator is not None:
                del feedback_activator
            if readiness_activator is not None:
                del readiness_activator
        self.assertTrue(cleanup_state["workers_stopped"],
                        "The test-owned navigation worker did not stop")


if __name__ == "__main__":
    unittest.main()
