import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image
import wx

from src.canvas_panel import (
    CanvasPanel,
    ImageObjectList,
    SceneLoadOperation,
)
from src.file_navigator import FileNavigator
from src.image_object import ImageObject
from src.image_pixels import load_source_pixels
from src.main_frame import MainFrame
from tests.test_regression_14_async_drops import (
    BlockingLoader,
    Dispatcher,
    DropCanvasHarness,
    SettingsStub,
)


def record(path, **overrides):
    value = {
        "source_path": str(path),
        "x": 3,
        "y": 4,
        "width": 3,
        "height": 2,
        "zoom_factor": 1.0,
        "viewport_offset": [0, 0],
        "source_pixel_normalization": 1,
    }
    value.update(overrides)
    return value


class BlockingReader:
    def __init__(self):
        self.entered = threading.Event()
        self.release_event = threading.Event()

    def __call__(self, path):
        self.entered.set()
        if not self.release_event.wait(5.0):
            raise TimeoutError("test did not release scene reader")
        from src.canvas_state import read_state
        return read_state(path)


class SceneCanvasHarness(DropCanvasHarness):
    MAX_SCENE_IN_FLIGHT = CanvasPanel.MAX_SCENE_IN_FLIGHT
    prepare_canvas_edit = CanvasPanel.prepare_canvas_edit
    _scene_document_request_key = staticmethod(CanvasPanel._scene_document_request_key)
    _scene_entry_request_key = staticmethod(CanvasPanel._scene_entry_request_key)
    begin_load_canvas_state = CanvasPanel.begin_load_canvas_state
    _on_scene_document_read = CanvasPanel._on_scene_document_read
    _pump_scene_operation = CanvasPanel._pump_scene_operation
    _find_scene_entry = CanvasPanel._find_scene_entry
    _on_scene_entry_decoded = CanvasPanel._on_scene_entry_decoded
    _finalize_scene_operation = CanvasPanel._finalize_scene_operation
    _commit_scene_operation = CanvasPanel._commit_scene_operation
    _release_scene_candidates = CanvasPanel._release_scene_candidates
    cancel_scene_operation = CanvasPanel.cancel_scene_operation
    _handle_scene_card_click = CanvasPanel._handle_scene_card_click
    _scene_status_lines = CanvasPanel._scene_status_lines
    _draw_scene_status = CanvasPanel._draw_scene_status
    bring_image_object_to_front = CanvasPanel.bring_image_object_to_front
    remove_image_object = CanvasPanel.remove_image_object
    swap_image_objects = CanvasPanel.swap_image_objects
    _reset_zoom_wheel_remainder = CanvasPanel._reset_zoom_wheel_remainder
    _zoom_selected_image = CanvasPanel._zoom_selected_image
    _navigate_to_adjacent_file = CanvasPanel._navigate_to_adjacent_file
    _on_navigation_discovered = CanvasPanel._on_navigation_discovered
    _get_overlay_timeout_ms = CanvasPanel._get_overlay_timeout_ms
    _schedule_overlay_clear = CanvasPanel._schedule_overlay_clear
    on_mouse_move = CanvasPanel.on_mouse_move

    def __init__(self, navigator, objects=(), bounds=(120, 80)):
        super().__init__(navigator, objects, bounds)
        self._scene_generation = 0
        self.scene_operation = None
        self._scene_card_rect = None
        self._scene_cancel_rect = None
        self._monotonic = time.monotonic


class TestAsyncSceneLoading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)
        cls.root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        cls.red = cls.root / "red.png"
        cls.alpha = cls.root / "alpha.png"
        cls.oriented = cls.root / "oriented.jpg"
        cls.corrupt = cls.root / "corrupt.png"
        Image.new("RGB", (6, 4), "red").save(cls.red)
        Image.new("RGBA", (4, 5), (0, 200, 0, 77)).save(cls.alpha)
        oriented = Image.new("RGB", (3, 2), "blue")
        oriented.putpixel((2, 1), (255, 255, 0))
        exif = Image.Exif()
        exif[274] = 6
        oriented.save(cls.oriented, exif=exif, quality=100, subsampling=0)
        cls.corrupt.write_bytes(b"not image data")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def setUp(self):
        self.owned = []

    def tearDown(self):
        for navigator, loader, reader in self.owned:
            if isinstance(loader, BlockingLoader):
                loader.release_all()
            if isinstance(reader, BlockingReader):
                reader.release_event.set()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(5.0))

    def make_canvas(self, *, loader=load_source_pixels, reader=None,
                    objects=(), bounds=(120, 80)):
        dispatcher = Dispatcher()
        navigator = FileNavigator(
            SettingsStub("0"), image_loader=loader,
            state_reader=reader, result_dispatch=dispatcher)
        self.owned.append((navigator, loader, reader))
        return SceneCanvasHarness(navigator, objects, bounds), navigator, dispatcher

    def write_scene(self, name, records):
        path = self.root / name
        path.write_text(json.dumps(records), encoding="utf-8")
        return path

    def drain_until_terminal(self, canvas, dispatcher, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with dispatcher.condition:
                item = dispatcher.items.pop(0) if dispatcher.items else None
            if item is not None:
                item[0](item[1])
                continue
            if canvas.scene_operation is not None and canvas.scene_operation.terminal:
                return True
            with dispatcher.condition:
                dispatcher.condition.wait(0.01)
        return False

    @staticmethod
    def old_object(path="old.png", color="purple"):
        obj = ImageObject(path)
        obj.commit_scene_candidate(
            Image.new("RGB", (4, 4), color),
            record(path, x=9, y=8, width=4, height=4), (120, 80))
        return obj

    def test_reader_is_async_old_scene_paints_and_selection_only_continues(self):
        reader = BlockingReader()
        old = self.old_object()
        scene = self.write_scene("paused-read.json", [record(self.red)])
        canvas, _, dispatcher = self.make_canvas(reader=reader, objects=(old,))

        started = time.monotonic()
        self.assertTrue(canvas.begin_load_canvas_state(scene))
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(reader.entered.wait(2.0))
        self.assertIs(canvas.image_objects[0], old)
        self.assertEqual(old._original_image.getpixel((0, 0)), (128, 0, 128))
        canvas.set_selected_object(old)
        self.assertFalse(canvas.scene_operation.terminal)
        self.assertEqual(canvas._scene_status_lines(canvas.scene_operation)[1],
                         "Reading and validating document...")

        reader.release_event.set()
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertTrue(canvas.scene_operation.committed)

    def test_exact_transform_order_duplicates_orientation_alpha_and_no_reload(self):
        records = [
            record(self.red, x=-8, y=70, width=2, height=2,
                   zoom_factor=2.5, viewport_offset=[3, 1]),
            record(self.red, x=11, y=12, width=6, height=4,
                   zoom_factor=0.25),
            record(self.alpha, x=20, y=3, width=3, height=4,
                   zoom_factor=1.75, viewport_offset=[1, 2]),
            record(self.oriented, x=40, y=-9, width=2, height=3,
                   zoom_factor=1.0),
            record(self.oriented, x=55, y=6, width=3, height=2,
                   zoom_factor=1.0, source_pixel_normalization=None),
        ]
        records[-1].pop("source_pixel_normalization")
        scene = self.write_scene("complete.json", records)
        old = self.old_object()
        canvas, _, dispatcher = self.make_canvas(objects=(old,))

        self.assertTrue(canvas.begin_load_canvas_state(scene))
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        objects = list(canvas.image_objects)
        self.assertEqual([obj.source_path for obj in objects],
                         [item["source_path"] for item in records])
        for obj, item in zip(objects, records):
            self.assertEqual(
                (obj.x, obj.y, obj.width, obj.height, obj.zoom_factor,
                 obj.viewport_offset),
                (item["x"], item["y"], item["width"], item["height"],
                 item["zoom_factor"], tuple(item["viewport_offset"])))
        self.assertNotEqual(objects[0].object_id, objects[1].object_id)
        self.assertIsNot(objects[0]._original_image, objects[1]._original_image)
        self.assertEqual(objects[2]._original_image.mode, "RGBA")
        self.assertEqual(objects[2]._original_image.getpixel((0, 0))[3], 77)
        self.assertEqual(objects[3]._original_image.size, (2, 3))
        self.assertEqual(objects[4]._original_image.size, (3, 2))
        self.assertIsNone(old._original_image)

        with mock.patch("src.image_object.load_source_pixels",
                        side_effect=AssertionError("unexpected disk decode")):
            for obj in objects:
                obj.load_image()
            preview = objects[1].get_pil_cropped()
        self.assertIsNotNone(preview)

    def test_committed_scene_preview_and_export_have_expected_landmarks(self):
        records = [
            record(self.red, x=1, y=2, width=3, height=2,
                   zoom_factor=0.5),
            record(self.alpha, x=10, y=4, width=4, height=5,
                   zoom_factor=1.0),
        ]
        scene = self.write_scene("landmarks.json", records)
        canvas, _, dispatcher = self.make_canvas(bounds=(30, 20))
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertEqual(canvas.image_objects[0].get_pil_cropped().getpixel((0, 0))[:3],
                         (255, 0, 0))

        exported = self.root / "scene-export.png"
        canvas.export_to_file(exported)
        with Image.open(exported) as image:
            self.assertEqual(image.getpixel((1, 2))[:3], (255, 0, 0))
            alpha_landmark = image.getpixel((10, 4))[:3]
            self.assertGreater(alpha_landmark[1], alpha_landmark[0])
            self.assertGreater(alpha_landmark[1], alpha_landmark[2])

    def test_reverse_completion_is_staged_until_all_entries_are_ready(self):
        loader = BlockingLoader(load_source_pixels)
        loader.block(str(self.red))
        loader.block(str(self.alpha))
        scene = self.write_scene(
            "reverse.json", [record(self.red), record(self.alpha, x=22)])
        old = self.old_object()
        canvas, _, dispatcher = self.make_canvas(loader=loader, objects=(old,))
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(2))

        loader.release(str(self.alpha))
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertIs(canvas.image_objects[0], old)
        self.assertFalse(canvas.scene_operation.terminal)
        loader.release(str(self.red))
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertEqual([obj.source_path for obj in canvas.image_objects],
                         [str(self.red), str(self.alpha)])

    def test_decode_failures_are_summarized_without_partial_replacement(self):
        scene = self.write_scene(
            "failures.json",
            [record(self.red), record(self.corrupt), record(self.root / "missing.png")])
        old = self.old_object()
        canvas, _, dispatcher = self.make_canvas(objects=(old,))
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))

        operation = canvas.scene_operation
        self.assertEqual(operation.failed, 2)
        self.assertFalse(operation.committed)
        self.assertEqual(list(canvas.image_objects), [old])
        self.assertIsNotNone(old._original_image)
        lines = "\n".join(canvas._scene_status_lines(operation))
        self.assertIn("corrupt.png", lines)
        self.assertIn("missing.png", lines)
        self.assertTrue(all(entry.image_object is None for entry in operation.entries))

    def test_malformed_future_and_empty_documents_are_transactional(self):
        old = self.old_object()
        for name, contents in (
                ("malformed.json", "["),
                ("future.json", '{"schema_version": 99}')):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(contents, encoding="utf-8")
                canvas, _, dispatcher = self.make_canvas(objects=(old,))
                canvas.begin_load_canvas_state(path)
                self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
                self.assertEqual(list(canvas.image_objects), [old])
                self.assertIn("failed", canvas.scene_operation.stage)

        empty = self.write_scene("empty.json", [])
        canvas, _, dispatcher = self.make_canvas(objects=(self.old_object(),))
        canvas.begin_load_canvas_state(empty)
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertEqual(list(canvas.image_objects), [])
        self.assertTrue(canvas.scene_operation.committed)

    def test_cancel_supersede_settings_clear_and_shutdown_release_ownership(self):
        reader = BlockingReader()
        scene = self.write_scene("slow.json", [record(self.red)])
        old = self.old_object()
        canvas, navigator, _ = self.make_canvas(reader=reader, objects=(old,))
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(reader.entered.wait(2.0))
        self.assertTrue(canvas.cancel_scene_operation(reason="test cancel"))
        self.assertEqual(list(canvas.image_objects), [old])
        self.assertTrue(canvas.scene_operation.canceled)

        reader.release_event.set()
        self.assertTrue(navigator.wait_for_workers(3.0))

        reader2 = BlockingReader()
        canvas2, navigator2, _ = self.make_canvas(reader=reader2, objects=(self.old_object(),))
        canvas2.begin_load_canvas_state(scene)
        self.assertTrue(reader2.entered.wait(2.0))
        canvas2.on_settings_changed()
        self.assertTrue(canvas2.scene_operation.terminal)
        self.assertIn("settings changed", canvas2.scene_operation.error)
        started = time.monotonic()
        canvas2.shutdown_preloading()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIsNone(canvas2.scene_operation)
        reader2.release_event.set()
        self.assertTrue(navigator2.wait_for_workers(3.0))

    def test_newer_scene_supersedes_late_old_document(self):
        first_reader = BlockingReader()
        old_scene = self.write_scene("old-request.json", [record(self.red)])
        new_scene = self.write_scene("new-request.json", [record(self.alpha, x=44)])
        canvas, navigator, dispatcher = self.make_canvas(
            reader=first_reader, objects=(self.old_object(),))
        canvas.begin_load_canvas_state(old_scene)
        self.assertTrue(first_reader.entered.wait(2.0))

        navigator._state_reader = __import__(
            "src.canvas_state", fromlist=["read_state"]).read_state
        canvas.begin_load_canvas_state(new_scene)
        first_reader.release_event.set()
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertEqual([obj.source_path for obj in canvas.image_objects],
                         [str(self.alpha)])
        self.assertEqual(canvas.image_objects[0].x, 44)

    def test_edit_invalidation_boundary_and_selection_only_policy(self):
        canvas, _, _ = self.make_canvas(objects=(self.old_object(),))
        operation = SceneLoadOperation(1, "pending.json")
        canvas.scene_operation = operation
        canvas.set_selected_object(canvas.image_objects[0])
        self.assertFalse(operation.terminal)
        canvas._note_user_interaction()
        self.assertTrue(operation.canceled)

        for action in ("remove", "swap", "order"):
            with self.subTest(action=action):
                first = self.old_object("first.png", "red")
                second = self.old_object("second.png", "blue")
                canvas, _, _ = self.make_canvas(objects=(first, second))
                operation = SceneLoadOperation(1, "pending.json")
                canvas.scene_operation = operation
                if action == "remove":
                    canvas.remove_image_object(first)
                elif action == "swap":
                    canvas.swap_image_objects(first, second)
                else:
                    canvas.bring_image_object_to_front(first)
                self.assertTrue(operation.canceled)

    def test_drop_navigation_zoom_drag_arrange_reset_and_background_cancel_scene(self):
        class DragEvent:
            def Dragging(self):
                return True

            def LeftIsDown(self):
                return True

            def GetPosition(self):
                return 25, 26

            def Skip(self):
                pass

        actions = ("drop", "navigation", "zoom", "drag", "arrange", "reset", "background")
        for action in actions:
            with self.subTest(action=action):
                obj = self.old_object()
                canvas, _, _ = self.make_canvas(objects=(obj,))
                canvas.selected_object = obj
                operation = SceneLoadOperation(1, "pending.json")
                canvas.scene_operation = operation
                if action == "drop":
                    canvas.accept_drop(0, 0, [str(self.red)])
                elif action == "navigation":
                    with mock.patch.object(canvas.file_navigator, "request_navigation",
                                           return_value=False):
                        canvas._navigate_to_adjacent_file()
                elif action == "zoom":
                    canvas._zoom_selected_image(True)
                elif action == "drag":
                    canvas.drag_offset = (1, 1)
                    canvas.on_mouse_move(DragEvent())
                elif action == "arrange":
                    frame = SimpleNamespace(
                        canvas_panel=canvas,
                        settings_manager=mock.Mock(
                            get_arrangement_settings=lambda: {
                                "spacing": 10, "outer_margin": 10}),
                    )
                    with mock.patch("src.main_frame.wx.MessageBox", return_value=wx.YES), \
                            mock.patch("src.main_frame.arrange_no_resize", return_value=True):
                        MainFrame.on_arrange_no_resize(frame, None)
                elif action == "reset":
                    frame = SimpleNamespace(canvas_panel=canvas)
                    MainFrame._reset_selected_object(
                        frame, lambda target: target.reset_viewport_offset())
                else:
                    canvas.on_settings_changed()
                self.assertTrue(operation.canceled)

    def test_scene_start_retires_active_drop_and_navigation_results(self):
        loader = BlockingLoader(load_source_pixels)
        loader.block(str(self.red))
        scene = self.write_scene("wins-race.json", [record(self.alpha)])
        old = self.old_object()
        canvas, navigator, dispatcher = self.make_canvas(
            loader=loader, objects=(old,))

        canvas.accept_drop(0, 0, [str(self.red)])
        self.assertTrue(loader.wait_for_calls(1))
        old.set_status_overlay("Loading candidate...", "processing",
                               operation="navigation")
        late_navigation = []
        self.assertTrue(navigator.request_navigation_decode(
            str(self.red), old.object_id, (old, old._work_generation, old.source_path),
            late_navigation.append))
        canvas.begin_load_canvas_state(scene)
        self.assertIsNone(canvas.drop_operation)
        self.assertFalse(old.show_status_overlay)

        loader.release(str(self.red))
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertEqual([obj.source_path for obj in canvas.image_objects],
                         [str(self.alpha)])
        self.assertEqual(late_navigation, [])

    def test_minimized_bounds_preserve_saved_transform_and_shared_limits(self):
        loader = BlockingLoader(load_source_pixels)
        paths = []
        records = []
        for index in range(14):
            path = self.root / f"bounded-{index}.png"
            shutil.copyfile(self.red, path)
            loader.block(str(path))
            paths.append(str(path))
            records.append(record(path, x=-index, y=100 + index,
                                  zoom_factor=0.2, viewport_offset=[2, 1]))
        scene = self.write_scene("bounded.json", records)
        canvas, navigator, dispatcher = self.make_canvas(
            loader=loader, bounds=(0, 0))
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(dispatcher.wait())
        dispatcher.drain_one()
        self.assertTrue(loader.wait_for_calls(3))
        state = navigator.preload_state()
        self.assertLessEqual(state["active"], 3)
        self.assertLessEqual(state["pending"], 10)
        self.assertEqual(len(loader.calls), 3)

        loader.release_all()
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertEqual(len(canvas.image_objects), 14)
        self.assertEqual(
            (canvas.image_objects[7].x, canvas.image_objects[7].y,
             canvas.image_objects[7].zoom_factor,
             canvas.image_objects[7].viewport_offset,
             canvas.image_objects[7].canvas_w),
            (-7, 107, 0.2, (2, 1), None))

    def test_file_dialog_routes_load_to_async_production_entry(self):
        dialog = mock.Mock()
        dialog.ShowModal.return_value = wx.ID_OK
        dialog.GetPath.return_value = "scene.json"
        frame = mock.Mock()
        frame.canvas_panel.begin_load_canvas_state = mock.Mock()
        frame._run_state_dialog = lambda chosen, operation, action: (
            MainFrame._run_state_dialog(frame, chosen, operation, action))
        with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog):
            MainFrame.on_load_canvas_state(frame, None)
        dialog.Destroy.assert_called_once()
        frame.canvas_panel.begin_load_canvas_state.assert_called_once_with("scene.json")


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1 to run visible scene feedback")
class TestVisibleSceneFeedback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_scene_card_paints_and_cancels_without_extra_clicks(self):
        settings = SettingsStub("0")
        frame = MainFrame(None, "Regression 15 feedback", settings, debug_mode=True)
        frame.SetClientSize((260, 150))
        canvas = frame.canvas_panel
        original = canvas.file_navigator
        original.shutdown()
        self.assertTrue(original.wait_for_workers(2.0))
        reader = BlockingReader()
        navigator = FileNavigator(
            settings, state_reader=reader,
            result_dispatch=lambda callback, result: wx.CallAfter(callback, result))
        canvas.file_navigator = navigator
        scene_root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        scene = scene_root / "visible.json"
        scene.write_text("[]", encoding="utf-8")
        painted = threading.Event()
        elapsed = []
        original_draw = CanvasPanel._draw_scene_status
        started = time.monotonic()

        def observed_draw(panel, dc):
            result = original_draw(panel, dc)
            if result and panel.scene_operation and not panel.scene_operation.terminal:
                elapsed.append(time.monotonic() - started)
                painted.set()
            return result

        loop = wx.GUIEventLoop()
        activator = wx.EventLoopActivator(loop)
        try:
            frame.Show()
            with mock.patch.object(CanvasPanel, "_draw_scene_status", observed_draw):
                wx.CallAfter(canvas.begin_load_canvas_state, str(scene))

                def finish_when_visible():
                    if painted.is_set() and canvas._scene_cancel_rect:
                        x, y, _, _ = canvas._scene_cancel_rect
                        canvas._handle_scene_card_click(x + 1, y + 1)
                        loop.Exit()
                    elif time.monotonic() - started > 2.0:
                        loop.Exit()
                    else:
                        wx.CallLater(10, finish_when_visible)

                wx.CallLater(10, finish_when_visible)
                loop.Run()
            self.assertTrue(painted.is_set())
            self.assertTrue(canvas.scene_operation.canceled)
            self.assertLess(elapsed[0], 0.1)
            print(f"Regression 15 visible acknowledgement: {elapsed[0] * 1000:.1f} ms")
        finally:
            reader.release_event.set()
            navigator.shutdown()
            navigator.wait_for_workers(3.0)
            frame.Destroy()
            del activator
            wx.Yield()
            shutil.rmtree(scene_root)


if __name__ == "__main__":
    unittest.main()
