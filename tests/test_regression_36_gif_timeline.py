import os
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import wx

from src.animation_controls import (
    FOCUS_NAMES,
    TIMELINE_PREVIEW_DELAY_MS,
    animation_control_presentation,
    layout_animation_controls,
    timeline_frame_at,
    timeline_thumb_x,
)
from src.canvas_panel import CanvasPanel
from src.file_navigator import AnimationDecodeResult, FileNavigator
from src.image_pixels import (
    AnimationSourceChangedError,
    get_animation_metadata,
    load_animation_frame,
    load_source_pixels,
    source_file_identity,
)
from tests.test_regression_27_gif_frames import (
    DISPOSAL_GIF,
    BlockingAnimationLoader,
    SettingsStub,
    animated_object,
)
from tests.test_regression_31_animation_controls import (
    ControlHarness,
    Event,
    FakeTimer,
    positioned_object,
)


class TimelineNavigatorStub:
    is_shutdown = False

    def __init__(self):
        self.requests = []
        self.animation_cancels = []
        self.playback_cancels = []

    def request_animation_frame(self, path, frame_index, request_key, context,
                                callback, *, expected_source_identity):
        self.requests.append((
            path, frame_index, request_key, context, callback,
            tuple(expected_source_identity)))
        return True

    def cancel_animation_frame(self, request_key):
        self.animation_cancels.append(request_key)
        return True

    def cancel_gif_playback(self, request_key):
        self.playback_cancels.append(request_key)
        return True

    def cancel_navigation_decode(self, _request_key):
        return True

    def request_preloading(self, _path):
        return True


class TimelineHarness(ControlHarness):
    def __init__(self, objects, *, size=(520, 280)):
        super().__init__(objects, size=size)
        self.file_navigator = TimelineNavigatorStub()
        self.timeline_preview_timer = FakeTimer()
        self.canvas_bg = "#ffffff"
        self.settings_manager = SettingsStub("0")
        self.drop_operation = None
        self.scene_operation = None
        self.export_operation = None
        self.save_operation = None
        self._duplication_operations = {}

    def _schedule_overlay_clear(self, delay_ms=None, image_object=None):
        return True


class DragEvent(Event):
    def Dragging(self):
        return True

    def LeftIsDown(self):
        return True


class TestTimelineGeometryAndPresentation(unittest.TestCase):
    def test_frame_mapping_reaches_endpoints_and_rounds_half_up(self):
        layout = layout_animation_controls(
            "gif", (80, 40, 240, 150), (520, 280), 1.0)
        self.assertFalse(layout.compact)
        track = layout.timeline_track
        self.assertEqual(timeline_frame_at(track, track.x - 100, 5), 0)
        self.assertEqual(timeline_frame_at(track, track.right + 100, 5), 4)
        for index in range(5):
            x = timeline_thumb_x(track, index, 5)
            self.assertEqual(timeline_frame_at(track, x, 5), index)

        three_pixel_track = SimpleNamespace(x=10, width=3)
        self.assertEqual(timeline_frame_at(three_pixel_track, 11, 2), 1)

    def test_narrow_layout_wraps_track_without_losing_two_frame_endpoints(self):
        layout = layout_animation_controls(
            "gif", (30, 20, 100, 80), (180, 140), 1.0)
        self.assertTrue(layout.compact)
        self.assertGreater(layout.panel.height, 36)
        self.assertLessEqual(layout.panel.right, 180)
        self.assertGreaterEqual(layout.timeline_track.width, 2)
        self.assertEqual(
            timeline_frame_at(layout.timeline_track, layout.timeline_track.x, 2), 0)
        self.assertEqual(timeline_frame_at(
            layout.timeline_track, layout.timeline_track.right - 1, 2), 1)

    def test_pending_thumb_does_not_change_committed_label(self):
        descriptor = SimpleNamespace(
            displayed_index=2, requested_index=8, frame_count=12, playing=False)
        presentation = animation_control_presentation(descriptor)
        self.assertEqual(presentation.frame_label, "3 / 12")
        self.assertEqual(presentation.pending_label, "Seeking 9")
        self.assertEqual(presentation.displayed_index, 2)
        self.assertEqual(presentation.requested_index, 8)

    def test_absolute_intent_validates_clamps_and_uses_latest_target(self):
        obj = animated_object()
        self.addCleanup(obj.dispose_source_pixels)
        self.assertIsNone(obj.set_animation_intent(True))
        self.assertIsNone(obj.set_animation_intent(1.5))
        descriptor, changed = obj.set_animation_intent(99)
        self.assertTrue(changed)
        self.assertEqual(descriptor.requested_index, descriptor.frame_count - 1)
        descriptor, changed = obj.advance_animation_intent(-1)
        self.assertTrue(changed)
        self.assertEqual(descriptor.requested_index, descriptor.frame_count - 2)


class TestTimelineInteraction(unittest.TestCase):
    def test_click_selects_pauses_and_seeks_immediately_without_peer_changes(self):
        bottom = positioned_object("bottom")
        top = positioned_object("top")
        self.addCleanup(bottom.dispose_source_pixels)
        self.addCleanup(top.dispose_source_pixels)
        top.animation.playing = True
        bottom.animation.playing = True
        canvas = TimelineHarness([bottom, top])
        canvas.selected_object = bottom
        CanvasPanel.show_animation_controls(canvas, top.object_id)
        layout = CanvasPanel._animation_control_layout(canvas)
        point = (layout.timeline_track.right - 1, layout.timeline_track.y)

        CanvasPanel.on_left_down(canvas, Event(point))

        self.assertIs(canvas.selected_object, top)
        self.assertFalse(top.animation.playing)
        self.assertTrue(bottom.animation.playing)
        self.assertEqual(bottom.animation.displayed_index, 0)
        self.assertEqual(canvas.file_navigator.requests[-1][1], 3)
        self.assertEqual(
            canvas._animation_controls.focus_index,
            FOCUS_NAMES.index("timeline"))
        self.assertTrue(canvas.captured)

    def test_many_motion_events_wait_for_standstill_then_release_exactly(self):
        obj = positioned_object("drag")
        self.addCleanup(obj.dispose_source_pixels)
        canvas = TimelineHarness([obj])
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        layout = CanvasPanel._animation_control_layout(canvas)
        track = layout.timeline_track
        CanvasPanel.on_left_down(canvas, Event((track.x, track.y)))
        canvas.file_navigator.requests.clear()

        for offset in range(1, track.width * 2):
            x = track.x + (offset % track.width)
            CanvasPanel.on_mouse_move(canvas, DragEvent((x, track.y)))
        self.assertEqual(canvas.file_navigator.requests, [])
        self.assertTrue(canvas.timeline_preview_timer.IsRunning())
        self.assertEqual(canvas.timeline_preview_timer.delay,
                         TIMELINE_PREVIEW_DELAY_MS)

        CanvasPanel.on_timeline_preview_timer(canvas, None)
        self.assertEqual(len(canvas.file_navigator.requests), 1)
        preview_target = canvas.file_navigator.requests[-1][1]

        release_x = timeline_thumb_x(track, 2, obj.animation.frame_count)
        CanvasPanel.on_left_up(canvas, Event((release_x, track.y)))
        self.assertEqual(canvas.file_navigator.requests[-1][1], 2)
        self.assertFalse(canvas.timeline_preview_timer.IsRunning())
        self.assertFalse(canvas._animation_controls.timeline_dragging)
        self.assertFalse(canvas.captured)
        self.assertFalse(obj.animation.playing)
        self.assertEqual(obj.animation.displayed_index, 0)
        self.assertEqual(obj.animation.requested_index, 2)
        self.assertIn(preview_target, range(obj.animation.frame_count))

    def test_capture_loss_abandons_preview_and_keeps_last_committed_frame(self):
        obj = positioned_object("capture-loss")
        self.addCleanup(obj.dispose_source_pixels)
        canvas = TimelineHarness([obj])
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        layout = CanvasPanel._animation_control_layout(canvas)
        CanvasPanel.on_left_down(canvas, Event((
            layout.timeline_track.right - 1, layout.timeline_track.y)))
        self.assertNotEqual(obj.animation.requested_index,
                            obj.animation.displayed_index)
        canvas.captured = False
        event = Event()
        CanvasPanel.on_mouse_capture_lost(canvas, event)
        self.assertTrue(event.skipped)
        self.assertFalse(canvas._animation_controls.timeline_dragging)
        self.assertFalse(canvas.timeline_preview_timer.IsRunning())
        self.assertEqual(obj.animation.requested_index,
                         obj.animation.displayed_index)
        self.assertIn(obj.object_id, canvas.file_navigator.animation_cancels)

    def test_release_retries_same_target_after_delayed_preview_failure(self):
        obj = positioned_object("retry-release")
        self.addCleanup(obj.dispose_source_pixels)
        canvas = TimelineHarness([obj])
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        layout = CanvasPanel._animation_control_layout(canvas)
        target_x = timeline_thumb_x(
            layout.timeline_track, 2, obj.animation.frame_count)
        point = (target_x, layout.timeline_track.y)
        CanvasPanel.on_left_down(canvas, Event(point))
        request = canvas.file_navigator.requests[-1]
        self.assertFalse(CanvasPanel._on_animation_frame_decoded(
            canvas, AnimationDecodeResult(
                request[0], request[1], request[3], error="delayed failure")))
        self.assertEqual(obj.animation.requested_index,
                         obj.animation.displayed_index)

        CanvasPanel.on_left_up(canvas, Event(point))
        self.assertEqual(len(canvas.file_navigator.requests), 2)
        self.assertEqual(canvas.file_navigator.requests[-1][1], 2)
        self.assertEqual(obj.animation.requested_index, 2)
        self.assertIn("Seeking frame 3/4", obj.status_message)

    def test_timeline_keys_use_latest_intent_and_space_routes_once(self):
        obj = positioned_object("keys")
        self.addCleanup(obj.dispose_source_pixels)
        canvas = TimelineHarness([obj])
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        canvas._animation_controls.focus_index = FOCUS_NAMES.index("timeline")
        obj.animation.requested_index = 1

        self.assertTrue(CanvasPanel._handle_animation_control_key(
            canvas, Event(key=wx.WXK_RIGHT)))
        self.assertEqual(canvas.file_navigator.requests[-1][1], 2)
        self.assertTrue(CanvasPanel._handle_animation_control_key(
            canvas, Event(key=wx.WXK_END)))
        self.assertEqual(canvas.file_navigator.requests[-1][1], 3)
        self.assertTrue(CanvasPanel._handle_animation_control_key(
            canvas, Event(key=wx.WXK_HOME)))
        self.assertEqual(obj.animation.requested_index, 0)

        with mock.patch.object(CanvasPanel, "_toggle_animation_playback",
                               return_value=True) as toggle:
            self.assertTrue(CanvasPanel._handle_animation_control_key(
                canvas, Event(key=wx.WXK_SPACE)))
        toggle.assert_called_once_with(canvas, obj.object_id)

    def test_hide_and_snapshots_preserve_committed_frame_while_pending(self):
        obj = positioned_object("snapshot")
        self.addCleanup(obj.dispose_source_pixels)
        canvas = TimelineHarness([obj])
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        CanvasPanel._seek_animation_frame(
            canvas, obj.object_id, 2, prepare=True, submit=False)
        committed_pixels = obj._original_image
        committed_revision = obj._source_revision

        self.assertTrue(CanvasPanel._activate_animation_control(canvas, "hide"))
        self.assertEqual(obj.animation.requested_index, 2)
        self.assertIs(obj._original_image, committed_pixels)
        save = CanvasPanel._capture_canvas_save_snapshot(
            canvas, include_file_identification=False)
        self.assertEqual(save.objects[0].animation_frame_index,
                         obj.animation.displayed_index)
        export = CanvasPanel._capture_export_snapshot(canvas)
        try:
            self.assertEqual(export.objects[0].source_revision,
                             committed_revision)
        finally:
            export.release()


class TestTimelineScheduler(unittest.TestCase):
    def test_unc_identity_ignores_unstable_remote_file_ids(self):
        first = SimpleNamespace(
            st_size=225, st_mtime_ns=123456789, st_dev=11, st_ino=22)
        second = SimpleNamespace(
            st_size=225, st_mtime_ns=123456789, st_dev=33, st_ino=44)
        path = r"\\macbook\share\animation.gif"
        self.assertEqual(
            source_file_identity(path, first),
            source_file_identity(path, second),
        )
        self.assertEqual(source_file_identity(path, first)[-2:], (0, 0))

    def test_change_during_decode_is_not_retried(self):
        calls = []

        def changing_loader(path, frame_index, *, expected_source_identity):
            calls.append((path, frame_index, expected_source_identity))
            raise AnimationSourceChangedError(
                "the GIF source changed while its frame was decoded",
                phase="during",
            )

        results = []
        navigator = FileNavigator(
            SettingsStub("0"), animation_loader=changing_loader,
            result_dispatch=lambda callback, result: callback(result),
        )
        self.addCleanup(navigator.shutdown)
        identity = ("source.gif", 1, 2, 3, 4)
        self.assertTrue(navigator.request_animation_frame(
            "source.gif", 1, "object", ("current",), results.append,
            expected_source_identity=identity))
        self.assertTrue(navigator.wait_for_workers(5.0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 1)
        self.assertIn("changed while", results[0].error)
        self.assertFalse(results[0].source_refreshed)

    def test_running_decode_keeps_only_latest_waiting_target(self):
        source = load_source_pixels(DISPOSAL_GIF)
        identity = get_animation_metadata(source).source_identity
        source.close()
        loader = BlockingAnimationLoader()
        loader.block(DISPOSAL_GIF, 1)
        results = []
        navigator = FileNavigator(
            SettingsStub("0"), animation_loader=loader,
            result_dispatch=lambda callback, result: callback(result))
        self.addCleanup(loader.release_all)
        self.addCleanup(navigator.shutdown)

        self.assertTrue(navigator.request_animation_frame(
            str(DISPOSAL_GIF), 1, "object", ("first",), results.append,
            expected_source_identity=identity))
        self.assertTrue(loader.wait_for_calls(1))
        for index in range(20):
            target = 2 if index % 2 == 0 else 3
            self.assertTrue(navigator.request_animation_frame(
                str(DISPOSAL_GIF), target, "object", ("latest", index),
                results.append, expected_source_identity=identity))

        state = navigator.preload_state()
        self.assertEqual(state["animation"], 1)
        self.assertEqual(state["animation_deferred"], 1)
        self.assertLessEqual(state["active"], navigator.MAX_ACTIVE_DECODES)
        self.assertLessEqual(state["pending"], navigator.MAX_PENDING_PRELOADS)

        loader.release_all()
        self.assertTrue(navigator.wait_for_workers(5.0))
        self.assertEqual(len(loader.calls), 2)
        self.assertEqual([result.context for result in results], [("latest", 19)])
        self.assertEqual(results[0].frame_index, 3)
        results[0].close()

    def test_right_edge_seek_recovers_after_source_identity_refresh(self):
        root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        self.addCleanup(lambda: shutil.rmtree(root))
        source = root / "replaced.gif"
        shutil.copy2(DISPOSAL_GIF, source)
        obj = animated_object(source, object_id="refreshed-source")
        self.addCleanup(obj.dispose_source_pixels)
        old_identity = obj.animation.source_identity
        stat_result = source.stat()
        os.utime(source, ns=(
            stat_result.st_atime_ns,
            stat_result.st_mtime_ns + 1_000_000_000,
        ))
        self.assertNotEqual(source_file_identity(source), old_identity)

        dispatched = []
        navigator = FileNavigator(
            SettingsStub("0"),
            result_dispatch=lambda callback, result: dispatched.append(
                (callback, result)),
        )
        self.addCleanup(navigator.shutdown)
        canvas = TimelineHarness([obj])
        canvas.file_navigator = navigator
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        layout = CanvasPanel._animation_control_layout(canvas)

        self.assertTrue(CanvasPanel._begin_timeline_drag(
            canvas, layout, layout.timeline_track.right + 500))
        self.assertTrue(navigator.wait_for_workers(5.0))
        self.assertEqual(len(dispatched), 1)
        callback, result = dispatched.pop()
        self.assertTrue(result.source_refreshed)
        self.assertTrue(callback(result))
        self.assertEqual(obj.animation.displayed_index,
                         obj.animation.frame_count - 1)
        self.assertEqual(obj.animation.source_identity,
                         source_file_identity(source))
        self.assertNotIn("source changed", obj.status_message.lower())

        self.assertTrue(CanvasPanel._finish_timeline_drag(
            canvas, layout.timeline_track.right + 500))
        self.assertFalse(canvas.captured)


@unittest.skipUnless(
    os.environ.get("NAGUMIX_GUI_TESTS") == "1",
    "Set NAGUMIX_GUI_TESTS=1 to run visible production timeline checks",
)
class TestVisibleProductionTimeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_overlapping_drag_pending_commit_keyboard_and_narrow_layout(self):
        frame = wx.Frame(None, title="Regression 36 GIF timeline", size=(520, 300))
        canvas = CanvasPanel(frame, SettingsStub("0"))
        bottom = positioned_object("visible-bottom", 120, 70)
        top = positioned_object("visible-top", 130, 75)
        canvas.add_image_object(bottom)
        canvas.add_image_object(top)
        frame.Show()
        wx.Yield()
        requests = []
        try:
            canvas.set_selected_object(bottom)
            canvas.show_animation_controls(top.object_id)
            layout = canvas._animation_control_layout()
            with mock.patch.object(
                    canvas.file_navigator, "request_animation_frame",
                    side_effect=lambda *args, **kwargs: requests.append(
                        (args, kwargs)) or True):
                start = (layout.timeline_track.x, layout.timeline_track.y)
                canvas.on_left_down(Event(start))
                for index in (1, 3, 2, 3):
                    x = timeline_thumb_x(
                        layout.timeline_track, index, top.animation.frame_count)
                    canvas.on_mouse_move(DragEvent((x, layout.timeline_track.y)))
                canvas.on_timeline_preview_timer(None)
                pending = animation_control_presentation(top.animation)
                self.assertEqual(pending.frame_label, "1 / 4")
                self.assertEqual(pending.pending_label, "Seeking 4")

                final_x = timeline_thumb_x(
                    layout.timeline_track, 2, top.animation.frame_count)
                canvas.on_left_up(Event((final_x, layout.timeline_track.y)))
                self.assertFalse(top.animation.playing)
                self.assertEqual(bottom.animation.displayed_index, 0)
                self.assertEqual(top.animation.requested_index, 2)

                final_args, _final_kwargs = requests[-1]
                context = final_args[3]
                pixels = load_animation_frame(
                    top.source_path, 2,
                    expected_source_identity=top.animation.source_identity)
                self.assertTrue(canvas._on_animation_frame_decoded(
                    AnimationDecodeResult(
                        top.source_path, 2, context, pixels=pixels)))
                self.assertEqual(top.animation.displayed_index, 2)

                canvas._animation_controls.focus_index = FOCUS_NAMES.index(
                    "timeline")
                canvas._handle_animation_control_key(Event(key=wx.WXK_LEFT))
                self.assertEqual(top.animation.requested_index, 1)

            frame.SetSize((210, 190))
            wx.Yield()
            narrow = canvas._animation_control_layout_for(top)
            self.assertTrue(narrow.compact)
            self.assertLessEqual(narrow.panel.right,
                                 canvas.get_client_dimensions()[0])
            print({
                "preview_delay_ms": TIMELINE_PREVIEW_DELAY_MS,
                "requests_after_rapid_drag": len(requests),
                "committed_frame": top.animation.displayed_index + 1,
                "peer_frame": bottom.animation.displayed_index + 1,
                "narrow_panel": narrow.panel,
                "narrow_track": narrow.timeline_track,
            })
        finally:
            canvas.shutdown_preloading()
            canvas.file_navigator.wait_for_workers(3)
            frame.Destroy()
            wx.Yield()


if __name__ == "__main__":
    unittest.main()
