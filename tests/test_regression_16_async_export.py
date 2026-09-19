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
from src.exporting import (
    ExportCancellation,
    ExportCanceled,
    ExportObjectSnapshot,
    ExportSnapshot,
    ExportTask,
    render_export_snapshot,
    resolve_export_path,
    write_export_image,
)
from src.file_navigator import FileNavigator
from src.image_object import ImageObject
from src.main_frame import MainFrame
from tests.test_regression_14_async_drops import Dispatcher, SettingsStub


class ExportCanvasHarness:
    _capture_export_snapshot = CanvasPanel._capture_export_snapshot
    begin_export = CanvasPanel.begin_export
    _on_export_progress = CanvasPanel._on_export_progress
    _on_export_finished = CanvasPanel._on_export_finished
    cancel_export_operation = CanvasPanel.cancel_export_operation
    _handle_export_card_click = CanvasPanel._handle_export_card_click
    _export_status_lines = CanvasPanel._export_status_lines
    _point_in_rect = staticmethod(CanvasPanel._point_in_rect)

    def __init__(self, navigator, objects=(), bounds=(16, 12)):
        self.file_navigator = navigator
        self.image_objects = ImageObjectList(objects)
        self.canvas_bg = "#ffffff"
        self.bounds = bounds
        self._export_generation = 0
        self.export_operation = None
        self._export_card_rect = None
        self._export_cancel_rect = None
        self.refreshes = 0

    def get_client_dimensions(self):
        return self.bounds

    def GetClientSize(self):
        return wx.Size(*self.bounds)

    def Refresh(self):
        self.refreshes += 1


def object_with_pixels(path, pixels, *, object_id=None, position=(0, 0),
                       frame=None, zoom=1.0, crop=(0, 0)):
    obj = ImageObject(str(path), object_id=object_id)
    obj._original_image = pixels
    obj.x, obj.y = position
    obj.zoom_factor = zoom
    obj.viewport_offset = crop
    obj.width, obj.height = frame or (
        max(1, int(pixels.width * zoom)), max(1, int(pixels.height * zoom)))
    return obj


class BarrierRenderer:
    def __init__(self):
        self.entered = threading.Event()
        self.release_event = threading.Event()
        self.snapshot = None
        self.thread_id = None

    def __call__(self, snapshot, cancellation, progress):
        self.snapshot = snapshot
        self.thread_id = threading.get_ident()
        self.entered.set()
        if not self.release_event.wait(5.0):
            raise TimeoutError("test did not release export renderer")
        cancellation.check()
        return render_export_snapshot(snapshot, cancellation, progress)


class TestExportPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def snapshot(self, objects=(), size=(8, 6), background="#ffffff"):
        records = []
        for obj in objects:
            records.append(ExportObjectSnapshot(
                obj.object_id, obj.source_path, obj._source_revision,
                obj.x, obj.y, obj.width, obj.height, obj.zoom_factor,
                tuple(obj.viewport_offset), obj.lease_source_pixels()))
        return ExportSnapshot(size[0], size[1], background, tuple(records))

    def test_selected_format_extension_and_bytes(self):
        rgba = Image.new("RGBA", (2, 2), (255, 0, 0, 80))
        obj = object_with_pixels("alpha.png", rgba)
        cases = (
            ("PNG", "png", b"\x89PNG", "RGBA"),
            ("JPEG", "jpg", b"\xff\xd8", "RGB"),
            ("WEBP", "webp", b"RIFF", "RGBA"),
            ("BMP", "bmp", b"BM", "RGB"),
        )
        for format_name, suffix, signature, expected_mode in cases:
            with self.subTest(format=format_name):
                path = self.root / f"selected-{format_name.lower()}"
                result = ExportTask(
                    self.snapshot((obj,), background="#00000000"),
                    resolve_export_path(path, format_name), format_name,
                    ExportCancellation()).run()
                self.assertEqual(result.status, "success")
                output = path.with_suffix(f".{suffix}")
                self.assertTrue(output.read_bytes().startswith(signature))
                with Image.open(output) as saved:
                    self.assertEqual(saved.mode, expected_mode)
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve_export_path(self.root / "wrong.jpg", "PNG")

    def test_geometry_crop_duplicates_z_order_clipping_and_empty_canvas(self):
        base = Image.new("RGB", (4, 2), "red")
        base.putpixel((2, 0), (0, 255, 0))
        lower = object_with_pixels(
            "same.png", base.copy(), object_id="lower", position=(-1, 1),
            frame=(2, 2), crop=(1, 0))
        upper = object_with_pixels(
            "same.png", Image.new("RGB", (2, 2), "blue"),
            object_id="upper", position=(0, 1))
        outside = object_with_pixels(
            "outside.png", Image.new("RGB", (1, 1), "yellow"),
            position=(-5, -5))
        snap = self.snapshot((lower, upper, outside), size=(4, 4))
        composite, result = render_export_snapshot(snap, ExportCancellation())
        try:
            self.assertEqual(result.rendered, 3)
            self.assertEqual((result.visible, result.clipped, result.outside), (1, 1, 1))
            self.assertEqual(composite.getpixel((0, 1))[:3], (0, 0, 255))
        finally:
            composite.close()
            snap.release()

        empty = self.snapshot(size=(3, 2), background="#123456")
        path = self.root / "empty.png"
        result = ExportTask(empty, str(path), "PNG", ExportCancellation()).run()
        self.assertEqual(result.status, "success")
        with Image.open(path) as saved:
            self.assertEqual(saved.getpixel((2, 1))[:3], (18, 52, 86))

    def test_legacy_and_normalized_pixels_are_exported_as_accepted(self):
        raw = Image.new("RGB", (2, 3), "black")
        raw.putpixel((0, 0), (255, 0, 0))
        normalized = raw.transpose(Image.Transpose.ROTATE_270)
        legacy = object_with_pixels("oriented.jpg", raw, object_id="legacy")
        modern = object_with_pixels(
            "oriented.jpg", normalized, object_id="normalized", position=(3, 0))
        snap = self.snapshot((legacy, modern), size=(6, 3))
        composite, result = render_export_snapshot(snap, ExportCancellation())
        try:
            self.assertFalse(result.failures)
            self.assertEqual(composite.getpixel((0, 0))[:3], (255, 0, 0))
            self.assertEqual(composite.getpixel((5, 0))[:3], (255, 0, 0))
        finally:
            composite.close()
            snap.release()

    def test_missing_pixels_and_invalid_transform_fail_without_silent_skip(self):
        missing = ImageObject("missing.png", object_id="missing-id")
        invalid = object_with_pixels(
            "invalid.png", Image.new("RGB", (1, 1), "red"),
            object_id="invalid-id")
        invalid.zoom_factor = 0
        destination = self.root / "failure.png"
        destination.write_bytes(b"previous")
        result = ExportTask(
            self.snapshot((missing, invalid)), str(destination), "PNG",
            ExportCancellation()).run()
        self.assertEqual(result.status, "failed")
        self.assertEqual(destination.read_bytes(), b"previous")
        self.assertEqual(
            {failure.object_id for failure in result.failures},
            {"missing-id", "invalid-id"})

    def test_atomic_failures_preserve_destination_and_remove_owned_temp(self):
        destination = self.root / "atomic.png"
        image = Image.new("RGBA", (2, 2), "red")
        failures = (
            mock.patch.object(Image.Image, "save", side_effect=OSError("encode")),
            mock.patch("src.exporting.os.fsync", side_effect=OSError("flush")),
            mock.patch("builtins.open", side_effect=OSError("write")),
        )
        for failure in failures:
            destination.write_bytes(b"previous")
            with failure:
                with self.assertRaises(OSError):
                    write_export_image(
                        image, destination, "PNG", ExportCancellation())
            self.assertEqual(destination.read_bytes(), b"previous")
            self.assertEqual(list(self.root.glob(".atomic.png.*.tmp")), [])

        destination.write_bytes(b"previous")
        with self.assertRaisesRegex(OSError, "replace"):
            write_export_image(
                image, destination, "PNG", ExportCancellation(),
                replace_file=lambda *_: (_ for _ in ()).throw(OSError("replace")))
        self.assertEqual(destination.read_bytes(), b"previous")
        self.assertEqual(list(self.root.glob(".atomic.png.*.tmp")), [])
        image.close()

    def test_injected_renderer_failure_releases_snapshot(self):
        obj = object_with_pixels("render.png", Image.new("RGB", (1, 1), "red"))
        snap = self.snapshot((obj,))
        lease = snap.objects[0].pixel_lease
        task = ExportTask(
            snap, str(self.root / "render-failure.png"), "PNG",
            ExportCancellation(), renderer=lambda *_: (_ for _ in ()).throw(
                RuntimeError("render injection")))
        result = task.run()
        self.assertEqual(result.status, "failed")
        self.assertIn("render injection", result.error)
        self.assertIsNone(lease.pixels)

    def test_cancel_before_worker_start_releases_snapshot_without_output(self):
        obj = object_with_pixels("queued.png", Image.new("RGB", (1, 1), "red"))
        snap = self.snapshot((obj,))
        lease = snap.objects[0].pixel_lease
        destination = self.root / "queued.png"
        task = ExportTask(
            snap, str(destination), "PNG", ExportCancellation())
        self.assertTrue(task.cancel_before_start())
        result = task.run()
        self.assertEqual(result.status, "canceled")
        self.assertIsNone(lease.pixels)
        self.assertFalse(destination.exists())

    def test_commit_boundary_reports_cancel_before_and_success_after(self):
        source = self.root / "commit.tmp"
        destination = self.root / "commit.png"
        source.write_bytes(b"new")
        destination.write_bytes(b"old")
        canceled = ExportCancellation()
        self.assertTrue(canceled.cancel())
        with self.assertRaises(ExportCanceled):
            canceled.replace(source, destination)
        self.assertEqual(destination.read_bytes(), b"old")

        source.write_bytes(b"new")
        committed = ExportCancellation()
        committed.replace(source, destination)
        self.assertTrue(committed.committed)
        self.assertFalse(committed.cancel())
        self.assertEqual(destination.read_bytes(), b"new")


class TestAsyncCanvasExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def setUp(self):
        self.owned = []

    def tearDown(self):
        for navigator, barrier in self.owned:
            if barrier is not None:
                barrier.release_event.set()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(5.0))

    def make_canvas(self, obj, barrier=None, bounds=(8, 6)):
        dispatcher = Dispatcher()
        navigator = FileNavigator(
            SettingsStub("0"), result_dispatch=dispatcher)
        canvas = ExportCanvasHarness(navigator, (obj,), bounds)
        if barrier is not None:
            canvas._export_task_factory = lambda *args: ExportTask(
                *args, renderer=barrier)
        self.owned.append((navigator, barrier))
        return canvas, navigator, dispatcher

    @staticmethod
    def drain(dispatcher):
        while True:
            with dispatcher.condition:
                item = dispatcher.items.pop(0) if dispatcher.items else None
            if item is None:
                return
            item[0](item[1])

    def wait_terminal(self, canvas, dispatcher, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain(dispatcher)
            if canvas.export_operation is not None and canvas.export_operation.terminal:
                return True
            with dispatcher.condition:
                dispatcher.condition.wait(0.01)
        return False

    def test_production_request_snapshots_then_edit_delete_replace_and_resize(self):
        accepted_pixels = Image.new("RGB", (2, 2), "red")
        obj = object_with_pixels("accepted.png", accepted_pixels, position=(1, 1))
        barrier = BarrierRenderer()
        canvas, _, dispatcher = self.make_canvas(obj, barrier)
        destination = self.root / "stable.png"
        gui_thread = threading.get_ident()

        started = time.monotonic()
        self.assertTrue(canvas.begin_export(destination, "PNG"))
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(barrier.entered.wait(2.0))
        self.assertNotEqual(barrier.thread_id, gui_thread)
        self.assertNotIn(canvas, barrier.snapshot.objects)
        record = barrier.snapshot.objects[0]
        self.assertEqual(record.source_revision, obj._source_revision)

        obj.x = 5
        obj.zoom_factor = 2
        canvas.bounds = (30, 20)
        canvas.image_objects.remove(obj)
        obj.dispose_source_pixels()
        self.assertEqual(record.pixels.getpixel((0, 0)), (255, 0, 0))
        obj._original_image = Image.new("RGB", (2, 2), "blue")
        barrier.release_event.set()
        self.assertTrue(self.wait_terminal(canvas, dispatcher))
        self.assertEqual(canvas.export_operation.stage, "success")
        self.assertEqual((canvas.export_operation.width, canvas.export_operation.height), (8, 6))
        with Image.open(destination) as saved:
            self.assertEqual(saved.size, (8, 6))
            self.assertEqual(saved.getpixel((1, 1))[:3], (255, 0, 0))
            self.assertEqual(saved.getpixel((5, 1))[:3], (255, 255, 255))
        self.assertIsNone(record.pixels)

    def test_one_active_export_real_progress_cancel_and_fresh_export(self):
        obj = object_with_pixels("one.png", Image.new("RGB", (2, 2), "green"))
        barrier = BarrierRenderer()
        canvas, _, dispatcher = self.make_canvas(obj, barrier)
        first = self.root / "first.png"
        self.assertTrue(canvas.begin_export(first, "PNG"))
        self.assertTrue(barrier.entered.wait(2.0))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            canvas.begin_export(self.root / "second.png", "PNG")
        self.assertTrue(canvas.cancel_export_operation())
        self.assertEqual(canvas.export_operation.stage, "canceled")
        self.assertFalse(first.exists())
        barrier.release_event.set()
        self.assertTrue(canvas.file_navigator.wait_for_workers(5.0))
        self.drain(dispatcher)

        canvas._export_task_factory = ExportTask
        self.assertTrue(canvas.begin_export(self.root / "fresh.png", "PNG"))
        self.assertTrue(self.wait_terminal(canvas, dispatcher))
        self.assertEqual(canvas.export_operation.stage, "success")
        self.assertEqual(canvas.export_operation.rendered, 1)

    def test_settings_clear_and_close_cancel_without_releasing_live_canvas_pixels(self):
        for reason in ("settings changed", "window closed"):
            with self.subTest(reason=reason):
                obj = object_with_pixels(
                    f"{reason}.png", Image.new("RGB", (2, 2), "purple"))
                barrier = BarrierRenderer()
                canvas, navigator, dispatcher = self.make_canvas(obj, barrier)
                destination = self.root / f"{reason}.png"
                canvas.begin_export(destination, "PNG")
                self.assertTrue(barrier.entered.wait(2.0))
                canvas.cancel_export_operation(clear=reason == "window closed", reason=reason)
                navigator.clear_cache()
                self.assertEqual(obj._original_image.getpixel((0, 0)), (128, 0, 128))
                barrier.release_event.set()
                self.assertTrue(navigator.wait_for_workers(5.0))
                self.drain(dispatcher)
                self.assertFalse(destination.exists())

    def test_scheduler_bounds_and_navigation_remains_serviceable(self):
        obj = object_with_pixels("source.png", Image.new("RGB", (1, 1), "red"))
        barrier = BarrierRenderer()
        canvas, navigator, dispatcher = self.make_canvas(obj, barrier)
        canvas.begin_export(self.root / "bounded.png", "PNG")
        self.assertTrue(barrier.entered.wait(2.0))
        target = self.root / "target.png"
        Image.new("RGB", (1, 1), "blue").save(target)
        results = []
        self.assertTrue(navigator.request_navigation_decode(
            str(target), "object", (obj, obj._work_generation, obj.source_path),
            results.append))
        with dispatcher.condition:
            if not dispatcher.items:
                dispatcher.condition.wait(2.0)
        state = navigator.preload_state()
        self.assertLessEqual(state["active"], 3)
        self.assertLessEqual(state["pending"], 10)
        self.assertEqual(state["export"], 1)
        barrier.release_event.set()
        self.assertTrue(self.wait_terminal(canvas, dispatcher))

    def test_failure_summary_names_duplicate_runtime_ids(self):
        first = ImageObject("duplicate.png", object_id="first")
        second = ImageObject("duplicate.png", object_id="second")
        dispatcher = Dispatcher()
        navigator = FileNavigator(SettingsStub("0"), result_dispatch=dispatcher)
        self.owned.append((navigator, None))
        canvas = ExportCanvasHarness(navigator, (first, second))
        destination = self.root / "duplicates.png"
        destination.write_bytes(b"old")
        canvas.begin_export(destination, "PNG")
        self.assertTrue(self.wait_terminal(canvas, dispatcher))
        self.assertEqual(canvas.export_operation.stage, "failed")
        lines = canvas._export_status_lines(canvas.export_operation)
        self.assertTrue(any("[first]" in line for line in lines))
        self.assertTrue(any("[second]" in line for line in lines))
        self.assertEqual(destination.read_bytes(), b"old")

    def test_main_frame_destroys_chooser_before_async_start_and_conflict_error(self):
        class Dialog:
            def __init__(self, path, index):
                self.path = str(path)
                self.index = index
                self.destroyed = False

            def ShowModal(self):
                return wx.ID_OK

            def GetPath(self):
                return self.path

            def GetFilterIndex(self):
                return self.index

            def Destroy(self):
                self.destroyed = True

        for path, index, should_start in (
                (self.root / "chosen", 0, True),
                (self.root / "conflict.jpg", 0, False)):
            with self.subTest(path=path):
                dialog = Dialog(path, index)
                panel = mock.Mock()
                panel.begin_export.side_effect = (
                    None if should_start else AssertionError("must not start"))
                frame = mock.Mock(canvas_panel=panel)

                def begin(destination, format_name):
                    self.assertTrue(dialog.destroyed)
                    self.assertEqual(format_name, "PNG")
                    self.assertTrue(str(destination).endswith(".png"))

                if should_start:
                    panel.begin_export.side_effect = begin
                with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog), \
                        mock.patch("src.main_frame.wx.MessageBox") as message:
                    MainFrame.on_export_canvas(frame, None)
                self.assertTrue(dialog.destroyed)
                self.assertEqual(panel.begin_export.called, should_start)
                self.assertEqual(message.called, not should_start)


@unittest.skipUnless(
    os.environ.get("NAGUMIX_GUI_TESTS") == "1",
    "Set NAGUMIX_GUI_TESTS=1 to run visible export feedback",
)
class TestVisibleExportFeedback(unittest.TestCase):
    def test_paused_export_acknowledges_and_cancel_is_clickable(self):
        app = wx.App.Get() or wx.App(False)
        frame = MainFrame(None, "Regression 16 feedback", SettingsStub("0"), debug_mode=True)
        frame.SetClientSize((260, 150))
        frame.Show()
        obj = object_with_pixels("visible.png", Image.new("RGB", (2, 2), "red"))
        frame.canvas_panel.add_image_object(obj)
        barrier = BarrierRenderer()
        frame.canvas_panel._export_task_factory = lambda *args: ExportTask(
            *args, renderer=barrier)
        output = Path(tempfile.gettempdir()) / "nagumix-regression_16-visible.png"
        painted = threading.Event()
        canceled = threading.Event()
        elapsed = []
        original_draw = CanvasPanel._draw_export_status
        started = time.monotonic()

        def observed_draw(panel, dc):
            result = original_draw(panel, dc)
            if result and panel.export_operation and not panel.export_operation.terminal:
                elapsed.append(time.monotonic() - started)
                painted.set()
            return result

        loop = wx.GUIEventLoop()
        activator = wx.EventLoopActivator(loop)
        try:
            frame.Show()
            with mock.patch.object(CanvasPanel, "_draw_export_status", observed_draw):
                wx.CallAfter(frame.canvas_panel.begin_export, output, "PNG")

                def finish_when_visible():
                    if painted.is_set() and frame.canvas_panel._export_cancel_rect:
                        x, y, _, _ = frame.canvas_panel._export_cancel_rect
                        if frame.canvas_panel._handle_export_card_click(x + 1, y + 1):
                            canceled.set()
                            loop.Exit()
                    elif time.monotonic() - started > 2.0:
                        loop.Exit()
                    else:
                        wx.CallLater(5, finish_when_visible)

                wx.CallLater(5, finish_when_visible)
                loop.Run()
            self.assertTrue(barrier.entered.is_set())
            self.assertTrue(painted.is_set())
            self.assertTrue(canceled.is_set())
            self.assertLess(elapsed[0], 0.1)
            print(f"Regression 16 visible export acknowledgement: {elapsed[0] * 1000:.1f} ms")
            self.assertEqual(frame.canvas_panel.export_operation.stage, "canceled")
        finally:
            del activator
            barrier.release_event.set()
            frame.canvas_panel.shutdown_preloading()
            frame.canvas_panel.file_navigator.wait_for_workers(5.0)
            frame.Destroy()
            if output.exists():
                output.unlink()


if __name__ == "__main__":
    unittest.main()
