import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
import json

from PIL import Image
import wx

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.exporting import (
    ExportCancellation,
    ExportObjectSnapshot,
    ExportSnapshot,
    render_export_snapshot,
)
from src.file_navigator import (
    AnimationDecodeResult,
    DuplicationDecodeResult,
    FileNavigator,
)
from src.image_object import ImageObject
from src.image_pixels import (
    get_animation_metadata,
    load_animation_frame,
    load_source_pixels,
)
from tests.test_regression_13_async_navigation import FakeTimer, SettingsStub
from tests.test_regression_18_filename_suggestions import NamingHarness
from tests.test_regression_20_duplicate import DuplicateCanvasHarness


FIXTURES = Path(__file__).parent / "fixtures"
DISPOSAL_GIF = FIXTURES / "regression_27_disposal.gif"
RANDOM_GIF = FIXTURES / "regression_27_random_seek.gif"
SINGLE_GIF = FIXTURES / "regression_27_single.gif"


class KeyEvent:
    def __init__(self, keycode, *, control=False, alt=False):
        self.keycode = keycode
        self.control = control
        self.alt = alt
        self.skipped = 0

    def GetKeyCode(self):
        return self.keycode

    def ControlDown(self):
        return self.control

    def AltDown(self):
        return self.alt

    def Skip(self):
        self.skipped += 1


class DeferredAnimationNavigator:
    def __init__(self):
        self.is_shutdown = False
        self.requests = []
        self.animation_cancels = []
        self.navigation_cancels = []

    def request_animation_frame(self, path, frame_index, request_key, context,
                                callback, *, expected_source_identity):
        self.requests.append((
            path, frame_index, request_key, context, callback,
            tuple(expected_source_identity)))
        return True

    def cancel_animation_frame(self, request_key):
        self.animation_cancels.append(request_key)
        return True

    def cancel_navigation_decode(self, request_key):
        self.navigation_cancels.append(request_key)
        return True

    def request_preloading(self, _path):
        return True

    def clear_cache(self):
        return True

    def shutdown(self):
        self.is_shutdown = True
        return True


class AnimationCanvasHarness:
    get_selected_object = CanvasPanel.get_selected_object
    set_selected_object = CanvasPanel.set_selected_object
    _step_selected_animation = CanvasPanel._step_selected_animation
    _retire_navigation_for_frame_step = CanvasPanel._retire_navigation_for_frame_step
    _on_animation_frame_decoded = CanvasPanel._on_animation_frame_decoded
    on_key_down = CanvasPanel.on_key_down
    remove_image_object = CanvasPanel.remove_image_object
    prepare_canvas_edit = CanvasPanel.prepare_canvas_edit
    cancel_scene_operation = CanvasPanel.cancel_scene_operation
    _get_overlay_timeout_ms = CanvasPanel._get_overlay_timeout_ms
    _reschedule_overlay_timer = CanvasPanel._reschedule_overlay_timer
    _stop_overlay_timer = CanvasPanel._stop_overlay_timer
    on_settings_changed = CanvasPanel.on_settings_changed
    shutdown_preloading = CanvasPanel.shutdown_preloading
    start_preloading_for_object = CanvasPanel.start_preloading_for_object

    def __init__(self, objects=()):
        self.image_objects = ImageObjectList(objects)
        self.selected_object = objects[0] if objects else None
        self.marked_object = None
        self.drag_offset = None
        self.file_navigator = DeferredAnimationNavigator()
        self.settings_manager = SettingsStub("0")
        self.overlay_clear_timer = FakeTimer()
        self._monotonic = time.monotonic
        self._interaction_revision = 0
        self._zoom_wheel_remainders = {}
        self._duplication_operations = {}
        self.drop_operation = None
        self.scene_operation = None
        self.export_operation = None
        self.refresh_count = 0
        self.scheduled = []

    def Refresh(self):
        self.refresh_count += 1

    def _schedule_overlay_clear(self, delay_ms=None, image_object=None):
        self.scheduled.append((delay_ms, image_object))
        return True

    def GetTopLevelParent(self):
        return self


def animated_object(path=DISPOSAL_GIF, *, object_id=None):
    obj = ImageObject(str(path), object_id=object_id)
    obj._original_image = load_source_pixels(path)
    obj.x, obj.y = 7, 9
    obj.width, obj.height = 23, 17
    obj.zoom_factor = 2.5
    obj.viewport_offset = (3, 2)
    return obj


def complete_request(canvas, request_index, *, error=None):
    path, frame_index, _, context, callback, identity = (
        canvas.file_navigator.requests[request_index])
    pixels = None if error else load_animation_frame(
        path, frame_index, expected_source_identity=identity)
    result = AnimationDecodeResult(
        path, frame_index, context, pixels=pixels, error=error)
    return callback(result), result


class TestGifFrameDecode(unittest.TestCase):
    def test_initial_metadata_and_single_frame_policy(self):
        first = load_source_pixels(DISPOSAL_GIF)
        self.addCleanup(first.close)
        metadata = get_animation_metadata(first)
        self.assertEqual(first.mode, "RGBA")
        self.assertEqual(first.size, (6, 4))
        self.assertEqual(
            (metadata.frame_count, metadata.frame_index, metadata.duration_ms),
            (4, 0, 80))
        self.assertEqual(first.getpixel((5, 0)), (0, 0, 0, 0))

        still = load_source_pixels(SINGLE_GIF)
        self.addCleanup(still.close)
        self.assertIsNone(get_animation_metadata(still))
        self.assertEqual(still.getpixel((2, 2))[:3], (12, 100, 220))

    def test_legacy_paint_fallback_does_not_scan_animation_metadata(self):
        obj = ImageObject(str(DISPOSAL_GIF))
        with mock.patch("src.image_pixels._gif_frame_metadata",
                        side_effect=AssertionError("unexpected sequence scan")):
            obj.load_image()
        self.assertIsNotNone(obj._original_image)
        self.assertFalse(obj.is_animated)

    def test_disposal_transparency_palette_and_complete_canvas_landmarks(self):
        first = load_source_pixels(DISPOSAL_GIF)
        identity = get_animation_metadata(first).source_identity
        first.close()
        expected = {
            0: ((220, 20, 20, 255), (220, 20, 20, 255), (220, 20, 20, 255)),
            1: ((20, 210, 30, 255), (220, 20, 20, 255), (220, 20, 20, 255)),
            2: ((0, 0, 0, 0), (30, 60, 220, 255), (0, 0, 0, 0)),
            3: ((0, 0, 0, 0), (0, 0, 0, 0), (240, 210, 20, 255)),
        }
        for index in (3, 1, 2, 0):
            with self.subTest(index=index):
                frame = load_animation_frame(
                    DISPOSAL_GIF, index, expected_source_identity=identity)
                try:
                    self.assertEqual(frame.size, (6, 4))
                    self.assertEqual(
                        (frame.getpixel((1, 1)), frame.getpixel((3, 1)),
                         frame.getpixel((5, 3))),
                        expected[index])
                    self.assertEqual(get_animation_metadata(frame).frame_index, index)
                finally:
                    frame.close()

    def test_random_backward_seeks_reconstruct_accumulated_partial_updates(self):
        first = load_source_pixels(RANDOM_GIF)
        identity = get_animation_metadata(first).source_identity
        first.close()
        expected = {
            0: ((30, 30, 30, 255), (30, 30, 30, 255), (30, 30, 30, 255)),
            1: ((180, 40, 180, 255), (30, 30, 30, 255), (30, 30, 30, 255)),
            2: ((180, 40, 180, 255), (20, 180, 180, 255), (30, 30, 30, 255)),
            3: ((180, 40, 180, 255), (20, 180, 180, 255), (240, 120, 20, 255)),
        }
        for index in (3, 0, 2, 1, 3):
            frame = load_animation_frame(
                RANDOM_GIF, index, expected_source_identity=identity)
            try:
                self.assertEqual(
                    (frame.getpixel((0, 0)), frame.getpixel((2, 1)),
                     frame.getpixel((4, 2))),
                    expected[index])
            finally:
                frame.close()

        with Image.open(RANDOM_GIF) as source:
            extents = []
            for index in range(source.n_frames):
                source.seek(index)
                extents.append(source.tile[0][1])
            self.assertTrue(any(
                box != (0, 0, *source.size) for box in extents[1:]))

    def test_wrong_type_bounds_and_source_identity_are_rejected(self):
        first = load_source_pixels(DISPOSAL_GIF)
        identity = get_animation_metadata(first).source_identity
        first.close()
        with self.assertRaisesRegex(ValueError, "only for animated GIFs"):
            load_animation_frame(__file__, 0)
        with self.assertRaisesRegex(IndexError, "outside"):
            load_animation_frame(
                DISPOSAL_GIF, 4, expected_source_identity=identity)
        changed = (identity[0], identity[1] + 1, identity[2])
        with self.assertRaisesRegex(OSError, "source changed"):
            load_animation_frame(
                DISPOSAL_GIF, 1, expected_source_identity=changed)

    def test_source_handle_is_closed_after_decode(self):
        root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        self.addCleanup(lambda: shutil.rmtree(root))
        source = root / "animation.gif"
        moved = root / "moved.gif"
        shutil.copyfile(DISPOSAL_GIF, source)
        first = load_source_pixels(source)
        identity = get_animation_metadata(first).source_identity
        first.close()
        frame = load_animation_frame(
            source, 2, expected_source_identity=identity)
        frame.close()
        source.replace(moved)
        self.assertTrue(moved.is_file())


class TestAnimationObjectAndCanvas(unittest.TestCase):
    def test_object_intent_is_separate_and_commit_preserves_geometry_and_lease(self):
        obj = animated_object()
        old_pixels = obj._original_image
        lease = obj.lease_source_pixels()
        geometry = (obj.x, obj.y, obj.width, obj.height,
                    obj.zoom_factor, obj.viewport_offset)
        descriptor, changed = obj.advance_animation_intent(1)
        self.assertTrue(changed)
        self.assertEqual((descriptor.displayed_index, descriptor.requested_index), (0, 1))
        frame = load_animation_frame(
            obj.source_path, 1,
            expected_source_identity=descriptor.source_identity)
        obj.commit_animation_candidate(
            frame, 1, descriptor.source_identity, descriptor.request_generation)
        self.assertEqual(obj.animation.frame_duration_ms, 90)
        self.assertEqual(
            (obj.x, obj.y, obj.width, obj.height,
             obj.zoom_factor, obj.viewport_offset), geometry)
        self.assertIsNot(obj._original_image, old_pixels)
        self.assertIs(lease.pixels, old_pixels)
        lease.release()

    def test_keys_step_both_directions_clamp_and_skip_still_or_no_selection(self):
        obj = animated_object()
        canvas = AnimationCanvasHarness((obj,))
        with mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                               return_value=True):
            event = KeyEvent(ord('.'))
            canvas.on_key_down(event)
            self.assertEqual(event.skipped, 0)
            self.assertEqual(canvas.file_navigator.requests[-1][1], 1)
            complete_request(canvas, 0)
            event = KeyEvent(ord(','))
            canvas.on_key_down(event)
            self.assertEqual(canvas.file_navigator.requests[-1][1], 0)
            complete_request(canvas, 1)
            canvas.on_key_down(KeyEvent(ord(',')))
        self.assertEqual(obj.animation.displayed_index, 0)
        self.assertEqual(len(canvas.file_navigator.requests), 2)
        self.assertEqual(obj.status_message, "Frame 1/4")

        still = ImageObject(str(SINGLE_GIF))
        still._original_image = load_source_pixels(SINGLE_GIF)
        canvas = AnimationCanvasHarness((still,))
        with mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                               return_value=True):
            event = KeyEvent(ord('.'))
            canvas.on_key_down(event)
        self.assertEqual(event.skipped, 1)
        canvas.selected_object = None
        event = KeyEvent(ord(','))
        canvas.on_key_down(event)
        self.assertEqual(event.skipped, 1)

    def test_pending_next_next_and_reverse_completion_publish_only_latest(self):
        obj = animated_object()
        canvas = AnimationCanvasHarness((obj,))
        old_revision = obj._source_revision
        canvas._step_selected_animation(1)
        canvas._step_selected_animation(1)
        self.assertEqual([request[1] for request in canvas.file_navigator.requests], [1, 2])
        self.assertEqual(obj.animation.requested_index, 2)
        self.assertEqual(obj._source_revision, old_revision)

        published, stale = complete_request(canvas, 0)
        self.assertFalse(published)
        self.assertIsNone(stale.pixels)
        self.assertEqual(obj.animation.displayed_index, 0)
        published, _ = complete_request(canvas, 1)
        self.assertTrue(published)
        self.assertEqual(obj.animation.displayed_index, 2)
        self.assertEqual(obj._original_image.getpixel((3, 1)), (30, 60, 220, 255))

        canvas._step_selected_animation(-1)
        canvas._step_selected_animation(1)
        self.assertEqual(obj.animation.requested_index, 2)
        self.assertEqual(obj.animation.displayed_index, 2)
        self.assertIn(obj.object_id, canvas.file_navigator.animation_cancels)

    def test_failure_keeps_frame_and_fresh_step_retries(self):
        obj = animated_object()
        canvas = AnimationCanvasHarness((obj,))
        old_pixels = obj._original_image
        canvas._step_selected_animation(1)
        published, _ = complete_request(canvas, 0, error="broken data")
        self.assertFalse(published)
        self.assertIs(obj._original_image, old_pixels)
        self.assertEqual(obj.animation.requested_index, 0)
        self.assertIn("broken data", obj.status_message)
        canvas._step_selected_animation(1)
        self.assertEqual(canvas.file_navigator.requests[-1][1], 1)

    def test_selection_deletion_settings_clear_and_shutdown_retire_seek(self):
        first = animated_object(object_id="first")
        second = animated_object(RANDOM_GIF, object_id="second")
        canvas = AnimationCanvasHarness((first, second))
        canvas._step_selected_animation(1)
        canvas.set_selected_object(second)
        self.assertEqual(first.animation.requested_index, first.animation.displayed_index)
        self.assertIn("first", canvas.file_navigator.animation_cancels)

        canvas._step_selected_animation(1)
        self.assertTrue(canvas.remove_image_object(second))
        self.assertIn("second", canvas.file_navigator.animation_cancels)
        self.assertIsNone(canvas.selected_object)

        third = animated_object(object_id="third")
        canvas.image_objects.append(third)
        canvas.selected_object = third
        canvas._step_selected_animation(1)
        canvas.on_settings_changed()
        self.assertEqual(third.animation.requested_index, 0)
        canvas._step_selected_animation(1)
        self.assertTrue(canvas.shutdown_preloading())
        self.assertTrue(canvas.file_navigator.is_shutdown)
        self.assertEqual(third.animation.requested_index, 0)

    def test_source_and_document_replacement_reject_dispatched_results(self):
        obj = animated_object()
        canvas = AnimationCanvasHarness((obj,))
        canvas._step_selected_animation(1)
        request = canvas.file_navigator.requests[0]
        pixels = load_animation_frame(
            request[0], request[1], expected_source_identity=request[5])
        result = AnimationDecodeResult(
            request[0], request[1], request[3], pixels=pixels)
        replacement = Image.new("RGB", (6, 4), "white")
        obj.change_source_path("replacement.png")
        obj._original_image = replacement
        self.assertFalse(canvas._on_animation_frame_decoded(result))
        self.assertIsNone(result.pixels)
        self.assertEqual(obj.source_path, "replacement.png")

        old = animated_object()
        canvas = AnimationCanvasHarness((old,))
        canvas._step_selected_animation(1)
        request = canvas.file_navigator.requests[0]
        pixels = load_animation_frame(
            request[0], request[1], expected_source_identity=request[5])
        result = AnimationDecodeResult(
            request[0], request[1], request[3], pixels=pixels)
        canvas.image_objects = ImageObjectList((animated_object(RANDOM_GIF),))
        canvas.selected_object = canvas.image_objects[0]
        self.assertFalse(canvas._on_animation_frame_decoded(result))
        self.assertIsNone(result.pixels)

    def test_production_duplication_starts_from_copied_displayed_frame(self):
        source = animated_object()
        source.advance_animation_intent(1)
        frame = load_animation_frame(
            source.source_path, 1,
            expected_source_identity=source.animation.source_identity)
        source.commit_animation_candidate(
            frame, 1, source.animation.source_identity,
            source.animation.request_generation)
        canvas = DuplicateCanvasHarness((source,))
        canvas.selected_object = source
        self.assertTrue(canvas.begin_duplicate(source))
        _, _, context, callback, task = canvas.file_navigator.requests[0]
        copied = task.run()
        callback(DuplicationDecodeResult(
            source.source_path, context, pixels=copied))
        duplicate = canvas.image_objects[1]
        self.assertEqual(duplicate.animation.displayed_index, 1)
        self.assertEqual(duplicate.animation.requested_index, 1)
        duplicate.advance_animation_intent(1)
        self.assertEqual(duplicate.animation.requested_index, 2)
        self.assertEqual(source.animation.requested_index, 1)

    def test_scene_json_contract_persists_selected_frame_and_paused_state(self):
        obj = animated_object()
        obj.advance_animation_intent(1)
        frame = load_animation_frame(
            obj.source_path, 1,
            expected_source_identity=obj.animation.source_identity)
        obj.commit_animation_candidate(
            frame, 1, obj.animation.source_identity,
            obj.animation.request_generation)
        canvas = NamingHarness()
        canvas.image_objects = [obj]
        root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        self.addCleanup(lambda: shutil.rmtree(root))
        state_path = root / "scene.json"
        canvas.save_canvas_state(state_path)
        records = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("frame_index", records[0])
        self.assertEqual(records[0]["animation"], {
            "type": "gif", "frame_index": 1, "paused": True})

    def test_focus_and_modifiers_preserve_punctuation(self):
        obj = animated_object()
        canvas = AnimationCanvasHarness((obj,))
        cases = (
            (KeyEvent(ord('.')), False),
            (KeyEvent(ord(','), control=True), True),
            (KeyEvent(ord('.'), alt=True), True),
        )
        for event, focus_allowed in cases:
            with mock.patch.object(
                    CanvasPanel, "_frame_shortcut_has_canvas_focus",
                    return_value=focus_allowed):
                canvas.on_key_down(event)
            self.assertEqual(event.skipped, 1)
        self.assertEqual(canvas.file_navigator.requests, [])

    def test_current_frame_export_and_duplicate_are_independent(self):
        obj = animated_object()
        canvas = AnimationCanvasHarness((obj,))
        canvas._step_selected_animation(1)
        complete_request(canvas, 0)
        lease = obj.lease_source_pixels()
        record = ExportObjectSnapshot(
            obj.object_id, obj.source_path, obj._source_revision,
            0, 0, 6, 4, 1.0, (0, 0), lease)
        snapshot = ExportSnapshot(6, 4, "#ffffff", (record,))
        composite, result = render_export_snapshot(
            snapshot, ExportCancellation())
        try:
            self.assertEqual(result.rendered, 1)
            self.assertEqual(composite.getpixel((1, 1))[:3], (20, 210, 30))
        finally:
            composite.close()
            snapshot.release()

        duplicate = ImageObject(obj.source_path)
        copied = obj._original_image.copy()
        duplicate._original_image = copied
        self.assertIsNot(duplicate.animation, obj.animation)
        self.assertEqual(duplicate.animation.displayed_index, 1)
        duplicate.advance_animation_intent(1)
        self.assertEqual(duplicate.animation.requested_index, 2)
        self.assertEqual(obj.animation.requested_index, 1)


class BlockingAnimationLoader:
    def __init__(self):
        self.condition = threading.Condition()
        self.calls = []
        self.releases = {}
        self.returned = []

    def block(self, path, frame_index):
        self.releases[(str(path), frame_index)] = threading.Event()

    def release_all(self):
        for event in self.releases.values():
            event.set()

    def __call__(self, path, frame_index, *, expected_source_identity):
        key = (str(path), frame_index)
        with self.condition:
            self.calls.append(key)
            self.condition.notify_all()
        release = self.releases.get(key)
        if release is not None and not release.wait(5.0):
            raise TimeoutError("test did not release animation decode")
        pixels = load_animation_frame(
            path, frame_index,
            expected_source_identity=expected_source_identity)
        self.returned.append(pixels)
        return pixels

    def wait_for_calls(self, count, timeout=2.0):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.calls) >= count, timeout)


class TestAnimationScheduler(unittest.TestCase):
    def test_shared_active_pending_bounds_and_latest_per_object_coalescing(self):
        source = load_source_pixels(DISPOSAL_GIF)
        identity = get_animation_metadata(source).source_identity
        source.close()
        loader = BlockingAnimationLoader()
        for index in (1, 2, 3):
            loader.block(DISPOSAL_GIF, index)
        results = []
        navigator = FileNavigator(
            SettingsStub("0"), animation_loader=loader,
            result_dispatch=lambda callback, result: callback(result))
        self.addCleanup(loader.release_all)
        self.addCleanup(navigator.shutdown)
        for index, key in ((1, "a"), (2, "b"), (3, "c")):
            self.assertTrue(navigator.request_animation_frame(
                str(DISPOSAL_GIF), index, key, (key,), results.append,
                expected_source_identity=identity))
        self.assertTrue(loader.wait_for_calls(3))
        self.assertEqual(navigator.preload_state()["active"], 3)

        for index in range(20):
            self.assertTrue(navigator.request_animation_frame(
                str(DISPOSAL_GIF), 1 + index % 3, "latest", (index,),
                results.append, expected_source_identity=identity))
        navigation_results = []
        self.assertTrue(navigator.request_navigation_decode(
            str(SINGLE_GIF), "navigation", ("navigation",),
            navigation_results.append))
        state = navigator.preload_state()
        self.assertLessEqual(state["active"], navigator.MAX_ACTIVE_DECODES)
        self.assertLessEqual(state["pending"], navigator.MAX_PENDING_PRELOADS)
        self.assertEqual(state["animation"], 4)
        loader.release_all()
        self.assertTrue(navigator.wait_for_workers(5.0))
        self.assertEqual(len(navigation_results), 1)
        self.assertIsNone(navigation_results[0].error)
        navigation_results[0].close()
        contexts = [result.context for result in results]
        self.assertCountEqual(
            contexts, [("a",), ("b",), ("c",), (19,)])
        for result in results:
            result.close()

    def test_clear_and_shutdown_are_nonblocking_and_discard_running_frames(self):
        source = load_source_pixels(DISPOSAL_GIF)
        identity = get_animation_metadata(source).source_identity
        source.close()
        for action in ("clear", "shutdown"):
            with self.subTest(action=action):
                loader = BlockingAnimationLoader()
                loader.block(DISPOSAL_GIF, 1)
                results = []
                navigator = FileNavigator(
                    SettingsStub("0"), animation_loader=loader,
                    result_dispatch=lambda callback, result: callback(result))
                navigator.request_animation_frame(
                    str(DISPOSAL_GIF), 1, "object", (), results.append,
                    expected_source_identity=identity)
                self.assertTrue(loader.wait_for_calls(1))
                started = time.monotonic()
                getattr(navigator, "clear_cache" if action == "clear" else "shutdown")()
                self.assertLess(time.monotonic() - started, 0.25)
                loader.release_all()
                self.assertTrue(navigator.wait_for_workers(5.0))
                self.assertEqual(results, [])


@unittest.skipUnless(
    os.environ.get("NAGUMIX_GUI_TESTS") == "1",
    "set NAGUMIX_GUI_TESTS=1 to run visible GIF paint verification",
)
class TestVisibleGifFramePaint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_old_pixels_remain_during_seek_and_commit_refresh_paints(self):
        frame = wx.Frame(None, title="Regression 27 GIF paint", size=(180, 120))
        canvas = CanvasPanel(frame, SettingsStub("0"))
        frame.Show()
        try:
            obj = animated_object()
            canvas.add_image_object(obj)
            canvas.set_selected_object(obj)
            old_revision = obj._source_revision
            with mock.patch.object(canvas.file_navigator, "request_animation_frame",
                                   return_value=True):
                self.assertTrue(canvas._step_selected_animation(1))
            self.assertEqual(obj.animation.displayed_index, 0)
            self.assertEqual(obj._source_revision, old_revision)
            context = (
                obj, obj.source_path, obj.animation.source_identity,
                obj.animation.request_generation, 1)
            pixels = load_animation_frame(
                obj.source_path, 1,
                expected_source_identity=obj.animation.source_identity)
            canvas._on_animation_frame_decoded(AnimationDecodeResult(
                obj.source_path, 1, context, pixels=pixels))
            canvas.Update()
            wx.Yield()
            self.assertEqual(obj.animation.displayed_index, 1)
            self.assertGreater(obj._source_revision, old_revision)
            self.assertIsNotNone(obj._prepared_bitmap)
        finally:
            canvas.shutdown_preloading()
            frame.Destroy()
            wx.Yield()

    def test_text_entry_and_dialog_focus_keep_punctuation(self):
        frame = wx.Frame(None, title="Regression 27 focus", size=(180, 120))
        canvas = CanvasPanel(frame, SettingsStub("0"))
        obj = animated_object()
        canvas.add_image_object(obj)
        canvas.set_selected_object(obj)
        text = wx.TextCtrl(frame)
        dialog = wx.Dialog(frame, title="Modal-style focus")
        dialog_text = wx.TextCtrl(dialog)
        frame.Show()
        wx.Yield()
        try:
            with mock.patch.object(
                    canvas.file_navigator, "request_animation_frame") as request:
                for control in (text, dialog_text):
                    if control is dialog_text:
                        dialog.Show()
                    control.SetFocus()
                    wx.Yield()
                    self.assertIs(wx.Window.FindFocus(), control)
                    event = KeyEvent(ord('.'))
                    canvas.on_key_down(event)
                    self.assertEqual(event.skipped, 1)
                request.assert_not_called()
        finally:
            dialog.Destroy()
            canvas.shutdown_preloading()
            frame.Destroy()
            wx.Yield()
