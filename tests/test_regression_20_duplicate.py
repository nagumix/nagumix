import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image
import wx

from src.canvas_panel import (
    CanvasPanel,
    DuplicationDecodeResult,
    ImageObjectList,
)
from src.file_navigator import FileNavigator
from src.image_object import ImageObject
from tests.test_regression_14_async_drops import SettingsStub


class DeferredNavigator:
    def __init__(self):
        self.is_shutdown = False
        self.requests = []
        self.canceled = []

    def request_duplication(self, path, request_key, context, callback, task):
        self.requests.append((path, request_key, context, callback, task))
        return True

    def cancel_duplication_work(self, request_key):
        self.canceled.append(request_key)
        for _, key, _, _, task in self.requests:
            if key == request_key:
                task.cancel_before_start()
                return True
        return False


class DuplicateCanvasHarness:
    begin_duplicate = CanvasPanel.begin_duplicate
    _capture_duplication_snapshot = CanvasPanel._capture_duplication_snapshot
    _capture_frame_metadata = staticmethod(CanvasPanel._capture_frame_metadata)
    _on_duplication_finished = CanvasPanel._on_duplication_finished
    cancel_duplication_operation = CanvasPanel.cancel_duplication_operation
    cancel_all_duplication_operations = CanvasPanel.cancel_all_duplication_operations
    remove_image_object = CanvasPanel.remove_image_object
    prepare_canvas_edit = CanvasPanel.prepare_canvas_edit

    def __init__(self, objects=()):
        self.file_navigator = DeferredNavigator()
        self.image_objects = ImageObjectList(objects)
        self.selected_object = None
        self.marked_object = None
        self._document_identity = 0
        self._interaction_revision = 0
        self._duplication_generation = 0
        self._duplication_operations = {}
        self._zoom_wheel_remainders = {}
        self._monotonic = lambda: 100.0
        self.refreshes = 0

    def Refresh(self):
        self.refreshes += 1

    def _schedule_overlay_clear(self, image_object=None, delay_ms=None):
        return True


def object_with_pixels(path="source.png", *, color=(10, 20, 30, 255),
                       object_id=None):
    obj = ImageObject(path, object_id=object_id)
    obj._original_image = Image.new("RGBA", (3, 2), color)
    obj.x, obj.y = -7, 11
    obj.width, obj.height = 17, 13
    obj.zoom_factor = 2.75
    obj._minimum_zoom = 0.4
    obj.viewport_offset = (2, 1)
    return obj


class TestDuplicatePipeline(unittest.TestCase):
    def complete(self, canvas, request_index=0):
        _, _, context, callback, task = canvas.file_navigator.requests[request_index]
        copied = task.run()
        callback(DuplicationDecodeResult(
            "source.png", context, pixels=copied))

    def test_snapshot_copy_is_independent_and_preserves_invocation_metadata(self):
        source = object_with_pixels()
        canvas = DuplicateCanvasHarness((source,))
        canvas.selected_object = source
        canvas.begin_duplicate(source)
        original_pixels = source._original_image
        source._original_image = Image.new("RGBA", (3, 2), "green")

        self.complete(canvas)
        duplicate = canvas.image_objects[1]
        self.assertIsNot(duplicate.object_id, source.object_id)
        self.assertEqual((duplicate.x, duplicate.y, duplicate.width,
                          duplicate.height, duplicate.zoom_factor,
                          duplicate.viewport_offset),
                         (-7, 11, 17, 13, 2.75, (2, 1)))
        self.assertEqual(duplicate.source_path, "source.png")
        self.assertIsNot(duplicate._original_image, original_pixels)
        self.assertEqual(duplicate._original_image.getpixel((0, 0)),
                         (10, 20, 30, 255))
        self.assertIs(canvas.selected_object, duplicate)
        duplicate._original_image.putpixel((0, 0), (1, 2, 3, 4))
        self.assertEqual(source._original_image.getpixel((0, 0)),
                         (0, 128, 0, 255))

    def test_repeated_request_reuses_pending_and_same_path_objects_are_distinct(self):
        first = object_with_pixels(object_id="first")
        second = object_with_pixels(object_id="second")
        canvas = DuplicateCanvasHarness((first, second))
        self.assertTrue(canvas.begin_duplicate(first))
        self.assertTrue(canvas.begin_duplicate(first))
        self.assertEqual(len(canvas.file_navigator.requests), 1)
        self.assertTrue(canvas.begin_duplicate(second))
        self.assertEqual(len(canvas.file_navigator.requests), 2)

    def test_newer_selection_is_preserved_when_copy_commits(self):
        first = object_with_pixels(object_id="first")
        second = object_with_pixels(object_id="second")
        canvas = DuplicateCanvasHarness((first, second))
        canvas.selected_object = first
        canvas.begin_duplicate(first)
        canvas._interaction_revision += 1
        canvas.selected_object = second
        self.complete(canvas)
        self.assertIs(canvas.selected_object, second)
        self.assertEqual(len(canvas.image_objects), 3)

    def test_missing_pixels_and_deleted_source_do_not_insert(self):
        missing = ImageObject("missing.png")
        canvas = DuplicateCanvasHarness((missing,))
        self.assertFalse(canvas.begin_duplicate(missing))
        self.assertEqual(canvas.file_navigator.requests, [])
        self.assertEqual(len(canvas.image_objects), 1)

        source = object_with_pixels()
        canvas = DuplicateCanvasHarness((source,))
        canvas.begin_duplicate(source)
        self.assertTrue(canvas.remove_image_object(source))
        self.assertEqual(canvas.file_navigator.canceled,
                         [canvas.file_navigator.requests[0][1]])
        self.assertEqual(canvas.image_objects, [])

    def test_document_replacement_and_settings_style_clear_cancel_pending_work(self):
        source = object_with_pixels()
        canvas = DuplicateCanvasHarness((source,))
        canvas.begin_duplicate(source)
        canvas._document_identity += 1
        self.assertTrue(canvas.cancel_all_duplication_operations(
            reason="scene replaced"))
        self.assertEqual(canvas.image_objects, [source])
        self.assertEqual(len(canvas.file_navigator.requests), 1)


class TestDuplicateScheduler(unittest.TestCase):
    def test_copy_runs_on_shared_bounded_worker_and_releases_lease(self):
        source = object_with_pixels()
        lease = source.lease_source_pixels()
        snapshot = SimpleNamespace(pixels=lease.pixels, release=lease.release)

        from src.canvas_panel import DuplicationCancellation, DuplicationTask
        task = DuplicationTask(snapshot, cancellation=DuplicationCancellation())
        results = []
        finished = threading.Event()
        main_thread = threading.get_ident()
        navigator = FileNavigator(SettingsStub("0"),
                                   result_dispatch=lambda callback, result: callback(result))
        try:
            self.assertTrue(navigator.request_duplication(
                source.source_path, ("duplicate", 1, source.object_id),
                (0, 1, source.object_id),
                lambda result: (results.append((result, threading.get_ident())),
                                finished.set()), task))
            self.assertTrue(finished.wait(2.0))
            result, worker_thread = results[0]
            self.assertNotEqual(worker_thread, main_thread)
            self.assertIsNot(result.pixels, source._original_image)
            result.close()
            self.assertIsNone(lease.pixels)
            self.assertLessEqual(navigator.preload_state()["active"],
                                 navigator.MAX_ACTIVE_DECODES)
        finally:
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(2.0))
            source.dispose_source_pixels()


class MenuCaptureFrame(__import__("src.main_frame", fromlist=["MainFrame"]).MainFrame):
    def PopupMenu(self, menu, *args, **kwargs):
        self.menu_labels = [item.GetItemLabel() for item in menu.GetMenuItems()]
        return True


class TestDuplicateMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        class Settings:
            def get_setting(self, section, key, fallback=None):
                return fallback

            def save(self):
                pass

        self.frame = MenuCaptureFrame(None, "Regression 20", Settings(), debug_mode=True)
        self.frame.SetClientSize((240, 160))
        self.frame.Layout()

    def tearDown(self):
        self.frame.canvas_panel.overlay_clear_timer.Stop()
        self.frame.canvas_panel.file_navigator.shutdown()
        self.frame.Destroy()

    def test_duplicate_has_stable_menu_item_and_clicked_target_dispatch(self):
        first = object_with_pixels(object_id="first")
        second = object_with_pixels(object_id="second")
        self.frame.canvas_panel.add_image_object(first)
        self.frame.canvas_panel.add_image_object(second)
        self.frame.canvas_panel._context_object_id = second.object_id
        self.frame.on_right_click(None)
        self.assertIn("Duplicate", self.frame.menu_labels)

        with mock.patch.object(self.frame.canvas_panel,
                               "begin_duplicate", return_value=True) as duplicate:
            self.assertTrue(self.frame.on_duplicate_object(None))
        self.assertIs(duplicate.call_args.args[0], second)

        self.frame.canvas_panel.remove_image_object(second)
        with mock.patch.object(self.frame.canvas_panel,
                               "begin_duplicate") as duplicate:
            self.assertFalse(self.frame.on_duplicate_object(None))
            duplicate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
