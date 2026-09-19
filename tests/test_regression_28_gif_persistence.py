import copy
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image
import wx

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.canvas_state import CanvasStateError, read_state, validate_state, write_state
from src.exporting import ExportCancellation, render_export_snapshot
from src.file_navigator import DuplicationDecodeResult, FileNavigator
from src.image_object import ImageObject
from src.image_pixels import load_animation_frame, load_source_pixels
from tests.test_regression_14_async_drops import Dispatcher, SettingsStub
from tests.test_regression_15_async_scene_loading import SceneCanvasHarness, record
from tests.test_regression_18_filename_suggestions import NamingHarness
from tests.test_regression_20_duplicate import DuplicateCanvasHarness
from tests.test_regression_27_gif_frames import (
    BlockingAnimationLoader,
    DISPOSAL_GIF,
    RANDOM_GIF,
    SINGLE_GIF,
    animated_object,
)
from tests.test_duplicate_identity import CanvasStub


def animation(frame_index):
    return {"type": "gif", "frame_index": frame_index, "paused": True}


def displayed_object(path, frame_index, **transform):
    obj = animated_object(path)
    if frame_index:
        descriptor = obj.animation
        descriptor.request_generation += 1
        descriptor.requested_index = frame_index
        pixels = load_animation_frame(
            path, frame_index,
            expected_source_identity=descriptor.source_identity)
        obj.commit_animation_candidate(
            pixels, frame_index, descriptor.source_identity,
            descriptor.request_generation)
    for name, value in transform.items():
        setattr(obj, name, value)
    return obj


class TestAnimationStateContract(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        self.addCleanup(lambda: shutil.rmtree(self.root))

    def test_optional_record_round_trips_without_changing_legacy_shape(self):
        legacy = record(DISPOSAL_GIF)
        current = dict(legacy, animation=animation(2), ignored_future="value")
        original = copy.deepcopy(current)
        self.assertEqual(validate_state([current]), [
            dict(legacy, animation=animation(2))])
        self.assertEqual(current, original)

        path = self.root / "scene.json"
        write_state(path, [current])
        self.assertEqual(read_state(path), [dict(legacy, animation=animation(2))])
        self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), list)
        self.assertNotIn("animation", validate_state([legacy])[0])

    def test_invalid_animation_metadata_is_rejected_without_coercion(self):
        invalid = (
            None,
            "gif",
            {},
            {"type": "gif", "frame_index": 0},
            dict(animation(0), extra=True),
            {"type": "video", "frame_index": 0, "paused": True},
            {"type": True, "frame_index": 0, "paused": True},
            {"type": "gif", "frame_index": -1, "paused": True},
            {"type": "gif", "frame_index": True, "paused": True},
            {"type": "gif", "frame_index": 1.0, "paused": True},
            {"type": "gif", "frame_index": 0, "paused": False},
            {"type": "gif", "frame_index": 0, "paused": 1},
            {"type": "gif", "frame_index": 0, "paused": "true"},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CanvasStateError):
                validate_state([dict(record(DISPOSAL_GIF), animation=value)])

    def test_save_uses_committed_frame_while_seek_is_pending(self):
        obj = displayed_object(DISPOSAL_GIF, 1)
        obj.advance_animation_intent(1)
        self.assertEqual(
            (obj.animation.displayed_index, obj.animation.requested_index),
            (1, 2))
        canvas = NamingHarness()
        canvas.image_objects = [obj]
        path = self.root / "pending.json"
        canvas.save_canvas_state(path)
        self.assertEqual(read_state(path)[0]["animation"], animation(1))

        canvas._suggested_base = "pending"
        before = path.read_bytes()
        with mock.patch("src.canvas_panel.write_state",
                        side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                canvas.save_canvas_state(self.root / "failed.json")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(canvas._suggested_base, "pending")

    def test_synchronous_helper_restores_frame_and_is_transactional_on_failure(self):
        valid = self.root / "valid.json"
        saved = record(
            DISPOSAL_GIF, x=-12, y=91, width=23, height=17,
            zoom_factor=2.5, viewport_offset=[3, 2], animation=animation(3))
        write_state(valid, [saved])
        canvas = CanvasStub((120, 80))
        CanvasPanel.load_canvas_state(canvas, valid)
        restored = canvas.image_objects[0]
        self.assertEqual(restored.animation.displayed_index, 3)
        self.assertEqual(restored._original_image.getpixel((5, 3)),
                         (240, 210, 20, 255))
        self.assertEqual(
            (restored.x, restored.y, restored.width, restored.height,
             restored.zoom_factor, restored.viewport_offset),
            (-12, 91, 23, 17, 2.5, (3, 2)))

        still = self.root / "still.png"
        corrupt = self.root / "corrupt.gif"
        Image.new("RGB", (4, 3), "green").save(still)
        corrupt.write_bytes(b"not a gif")
        failures = (
            record(DISPOSAL_GIF, animation=animation(4)),
            record(SINGLE_GIF, animation=animation(0)),
            record(still, animation=animation(0)),
            record(corrupt, animation=animation(0)),
            record(self.root / "missing.gif", animation=animation(0)),
        )
        collection = canvas.image_objects
        for index, invalid_record in enumerate(failures):
            path = self.root / f"invalid-{index}.json"
            write_state(path, [invalid_record])
            with self.subTest(record=invalid_record), self.assertRaises(Exception):
                CanvasPanel.load_canvas_state(canvas, path)
            self.assertIs(canvas.image_objects, collection)
            self.assertIs(canvas.image_objects[0], restored)
            self.assertIsNotNone(restored._original_image)

    def test_legacy_synchronous_gif_keeps_frame_zero_behavior(self):
        path = self.root / "legacy.json"
        write_state(path, [record(DISPOSAL_GIF)])
        canvas = CanvasStub((120, 80))
        CanvasPanel.load_canvas_state(canvas, path)
        restored = canvas.image_objects[0]
        self.assertIsNone(restored._original_image)
        restored.load_image()
        self.assertEqual(restored._original_image.getpixel((1, 1)),
                         (220, 20, 20, 255))
        self.assertIsNone(restored.animation)

    def test_source_identity_change_during_persisted_decode_is_rejected(self):
        import src.image_pixels as image_pixels

        identity = image_pixels._source_file_identity(DISPOSAL_GIF)
        changed = (identity[0], identity[1] + 1, identity[2])
        with mock.patch("src.image_pixels._source_file_identity",
                        side_effect=(identity, changed)):
            with self.assertRaisesRegex(OSError, "source changed"):
                load_animation_frame(DISPOSAL_GIF, 2)


class TestAsyncAnimationSceneRestoration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        self.still = self.root / "still.png"
        Image.new("RGB", (5, 3), (90, 110, 130)).save(self.still)
        self.owned = []

    def tearDown(self):
        for navigator, loader in self.owned:
            if isinstance(loader, BlockingAnimationLoader):
                loader.release_all()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(5.0))
        shutil.rmtree(self.root)

    def make_canvas(self, *, objects=(), animation_loader=None):
        dispatcher = Dispatcher()
        navigator = FileNavigator(
            SettingsStub("0"), animation_loader=animation_loader,
            result_dispatch=dispatcher)
        self.owned.append((navigator, animation_loader))
        return SceneCanvasHarness(navigator, objects), navigator, dispatcher

    def write_scene(self, name, records):
        path = self.root / name
        write_state(path, records)
        return path

    @staticmethod
    def drain_until_terminal(canvas, dispatcher, timeout=5.0):
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
    def drain_one(dispatcher, timeout=2.0):
        if not dispatcher.wait(timeout=timeout):
            return False
        dispatcher.drain_one()
        return True

    @staticmethod
    def old_object():
        old = ImageObject("old.png")
        old._original_image = Image.new("RGB", (4, 4), "purple")
        return old

    def test_async_load_restores_distinct_frames_transforms_and_first_pixels(self):
        first = displayed_object(
            DISPOSAL_GIF, 1, x=-8, y=70, width=19, height=13,
            zoom_factor=2.0, viewport_offset=(2, 1))
        second = displayed_object(
            DISPOSAL_GIF, 3, x=31, y=-6, width=17, height=11,
            zoom_factor=0.75, viewport_offset=(1, 2))
        still = ImageObject(str(self.still))
        still._original_image = load_source_pixels(self.still)
        still.x, still.y, still.width, still.height = 4, 5, 5, 3
        source = NamingHarness()
        source.image_objects = [first, second, still]
        scene = self.root / "saved.json"
        source.save_canvas_state(scene)

        old = self.old_object()
        canvas, _, dispatcher = self.make_canvas(objects=(old,))
        self.assertTrue(canvas.begin_load_canvas_state(scene))
        self.assertIs(canvas.image_objects[0], old)
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertTrue(canvas.scene_operation.committed)
        restored = list(canvas.image_objects)
        self.assertEqual([obj.source_path for obj in restored],
                         [str(DISPOSAL_GIF), str(DISPOSAL_GIF), str(self.still)])
        self.assertEqual(
            [obj.animation.displayed_index for obj in restored[:2]], [1, 3])
        self.assertIsNot(restored[0].animation, restored[1].animation)
        self.assertEqual(restored[0]._original_image.getpixel((1, 1)),
                         (20, 210, 30, 255))
        self.assertEqual(restored[1]._original_image.getpixel((5, 3)),
                         (240, 210, 20, 255))
        self.assertEqual(
            (restored[0].x, restored[0].y, restored[0].width,
             restored[0].height, restored[0].zoom_factor,
             restored[0].viewport_offset),
            (-8, 70, 19, 13, 2.0, (2, 1)))
        with mock.patch("src.image_object.load_source_pixels",
                        side_effect=AssertionError("unexpected frame-zero reload")):
            self.assertIsNotNone(restored[0].get_pil_cropped())
            self.assertIsNotNone(restored[1].get_pil_cropped())

    def test_post_load_step_duplicate_and_export_keep_independent_snapshots(self):
        scene = self.write_scene("independent.json", [
            record(DISPOSAL_GIF, animation=animation(1)),
            record(DISPOSAL_GIF, x=20, animation=animation(2)),
        ])
        canvas, _, dispatcher = self.make_canvas()
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        first, second = canvas.image_objects
        canvas.set_selected_object(first)

        snapshot = CanvasPanel._capture_export_snapshot(canvas)
        self.assertTrue(CanvasPanel._step_selected_animation(canvas, 1))
        self.assertEqual(first.animation.requested_index, 2)
        self.assertEqual(first.animation.displayed_index, 1)
        self.assertEqual(second.animation.displayed_index, 2)
        composite, result = render_export_snapshot(snapshot, ExportCancellation())
        try:
            self.assertEqual(result.rendered, 2)
            self.assertEqual(snapshot.objects[0].pixel_lease.pixels.getpixel((1, 1)),
                             (20, 210, 30, 255))
        finally:
            composite.close()
            snapshot.release()

        self.assertTrue(self.drain_one(dispatcher))
        self.assertEqual(first.animation.displayed_index, 2)
        self.assertEqual(second.animation.displayed_index, 2)

        duplicate_canvas = DuplicateCanvasHarness((first,))
        duplicate_canvas.selected_object = first
        self.assertTrue(duplicate_canvas.begin_duplicate(first))
        _, _, context, callback, task = duplicate_canvas.file_navigator.requests[0]
        callback(DuplicationDecodeResult(
            first.source_path, context, pixels=task.run()))
        duplicate = duplicate_canvas.image_objects[1]
        self.assertEqual(duplicate.animation.displayed_index, 2)
        duplicate.advance_animation_intent(1)
        self.assertEqual(duplicate.animation.requested_index, 3)
        self.assertEqual(first.animation.requested_index, 2)

    def test_invalid_animation_sources_fail_without_partial_publication(self):
        corrupt = self.root / "corrupt.gif"
        corrupt.write_bytes(b"broken")
        cases = (
            record(DISPOSAL_GIF, animation=animation(4)),
            record(SINGLE_GIF, animation=animation(0)),
            record(self.still, animation=animation(0)),
            record(corrupt, animation=animation(0)),
            record(self.root / "missing.gif", animation=animation(0)),
        )
        for index, bad in enumerate(cases):
            with self.subTest(index=index):
                old = self.old_object()
                canvas, _, dispatcher = self.make_canvas(objects=(old,))
                scene = self.write_scene(
                    f"bad-{index}.json",
                    [record(self.still), bad])
                canvas.begin_load_canvas_state(scene)
                self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
                self.assertFalse(canvas.scene_operation.committed)
                self.assertIs(canvas.image_objects[0], old)
                self.assertIsNotNone(old._original_image)
                self.assertTrue(all(
                    entry.image_object is None
                    for entry in canvas.scene_operation.entries))
                details = " ".join(
                    entry.error or "" for entry in canvas.scene_operation.entries)
                self.assertTrue(details.strip())

    def test_edit_cancels_blocked_frame_restore_and_releases_result(self):
        loader = BlockingAnimationLoader()
        loader.block(DISPOSAL_GIF, 2)
        old = self.old_object()
        canvas, navigator, dispatcher = self.make_canvas(
            objects=(old,), animation_loader=loader)
        scene = self.write_scene(
            "blocked.json", [record(DISPOSAL_GIF, animation=animation(2))])
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(self.drain_one(dispatcher))
        self.assertTrue(loader.wait_for_calls(1))
        canvas.prepare_canvas_edit()
        self.assertIsNone(canvas.scene_operation)
        self.assertIs(canvas.image_objects[0], old)
        loader.release_all()
        self.assertTrue(navigator.wait_for_workers(5.0))
        self.assertEqual(dispatcher.items, [])
        self.assertEqual(len(loader.returned), 1)
        with self.assertRaises(ValueError):
            loader.returned[0].getpixel((0, 0))

    def test_new_scene_supersedes_blocked_frame_restore(self):
        loader = BlockingAnimationLoader()
        loader.block(DISPOSAL_GIF, 2)
        old = self.old_object()
        canvas, _, dispatcher = self.make_canvas(
            objects=(old,), animation_loader=loader)
        blocked = self.write_scene(
            "blocked-old.json", [record(DISPOSAL_GIF, animation=animation(2))])
        newer = self.write_scene("newer.json", [record(self.still, x=44)])
        canvas.begin_load_canvas_state(blocked)
        self.assertTrue(self.drain_one(dispatcher))
        self.assertTrue(loader.wait_for_calls(1))
        canvas.begin_load_canvas_state(newer)
        loader.release_all()
        self.assertTrue(self.drain_until_terminal(canvas, dispatcher))
        self.assertEqual([obj.source_path for obj in canvas.image_objects],
                         [str(self.still)])
        self.assertEqual(canvas.image_objects[0].x, 44)

    def test_shutdown_is_nonblocking_during_frame_restore(self):
        loader = BlockingAnimationLoader()
        loader.block(RANDOM_GIF, 3)
        old = self.old_object()
        canvas, navigator, dispatcher = self.make_canvas(
            objects=(old,), animation_loader=loader)
        scene = self.write_scene(
            "closing.json", [record(RANDOM_GIF, animation=animation(3))])
        canvas.begin_load_canvas_state(scene)
        self.assertTrue(self.drain_one(dispatcher))
        self.assertTrue(loader.wait_for_calls(1))
        started = time.monotonic()
        self.assertTrue(canvas.shutdown_preloading())
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertIsNone(canvas.scene_operation)
        self.assertIs(canvas.image_objects[0], old)
        loader.release_all()
        self.assertTrue(navigator.wait_for_workers(5.0))
        self.assertEqual(dispatcher.items, [])


if __name__ == "__main__":
    unittest.main()
