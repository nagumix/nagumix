import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.file_navigator import FileNavigator, PlaybackSliceResult
from src.gif_playback import (
    DEFAULT_FRAME_DURATION_MS,
    GifDecoderSession,
    PLAYBACK_FRAME_CAP,
    PLAYBACK_SLICE_FRAMES,
    PlaybackSession,
    PlaybackTransferBudget,
    effective_frame_duration_ms,
    gif_loop_count,
)
from src.image_object import ImageObject
from src.image_pixels import (
    AnimationFrameMetadata,
    _attach_animation_metadata,
    get_animation_metadata,
    load_animation_frame,
    load_source_pixels,
)
from tests.test_regression_27_gif_frames import (
    DISPOSAL_GIF,
    RANDOM_GIF,
    SettingsStub,
    animated_object,
)


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = float(value)


class FakeTimer:
    def __init__(self):
        self.delay = None
        self.running = False

    def IsRunning(self):
        return self.running

    def Start(self, delay, _mode):
        self.delay = delay
        self.running = True

    def Stop(self):
        self.running = False


def decoder_factory(durations, *, loop_count=None, size=(4, 3), mode="RGBA"):
    class Decoder:
        instances = []

        def __init__(self, path, source_identity):
            self.path = path
            self.source_identity = tuple(source_identity)
            self.closed = False
            self.calls = []
            type(self).instances.append(self)

        def decode(self, index):
            self.calls.append(index)
            pixels = Image.new(mode, size, (index * 40, 20, 200, 180))
            metadata = AnimationFrameMetadata(
                len(durations), index, effective_frame_duration_ms(durations[index]),
                size, self.source_identity, loop_count)
            return _attach_animation_metadata(pixels, metadata), metadata

        def close(self):
            self.closed = True

    return Decoder


def playback_session(*, durations=(100, 40, 70), loop_count=None,
                     displayed_index=0, repeats_completed=0, started_at=10.0,
                     restart=False, budget=None, size=(4, 3)):
    factory = decoder_factory(durations, loop_count=loop_count, size=size)
    session = PlaybackSession(
        "object", "ignored.gif", ("ignored.gif", 1, 2), 7,
        len(durations), size, loop_count, displayed_index,
        durations[displayed_index], repeats_completed, started_at,
        restart=restart, decoder_factory=factory)
    return session, budget or PlaybackTransferBudget(), factory


class KeyEvent:
    def __init__(self, key, *, repeat=False, control=False, alt=False):
        self.key = key
        self.repeat = repeat
        self.control = control
        self.alt = alt
        self.skipped = False

    def GetKeyCode(self):
        return self.key

    def IsAutoRepeat(self):
        return self.repeat

    def ControlDown(self):
        return self.control

    def AltDown(self):
        return self.alt

    def Skip(self):
        self.skipped = True


class DeferredPlaybackNavigator:
    def __init__(self):
        self.is_shutdown = False
        self.starts = []
        self.cancels = []
        self.slice_requests = []
        self.retirements = []
        self.animation_cancels = []

    def start_gif_playback(self, **kwargs):
        self.starts.append(kwargs)
        return True

    def cancel_gif_playback(self, object_id):
        self.cancels.append(object_id)
        return True

    def request_playback_slice(self, object_id):
        self.slice_requests.append(object_id)
        return True

    def retire_gif_playback(self, object_id, generation):
        self.retirements.append((object_id, generation))
        return True

    def cancel_animation_frame(self, object_id):
        self.animation_cancels.append(object_id)
        return True

    def cancel_navigation_decode(self, _object_id):
        return True

    def request_preloading(self, _path):
        return True


class PlaybackCanvasHarness:
    get_selected_object = CanvasPanel.get_selected_object
    set_selected_object = CanvasPanel.set_selected_object
    _toggle_selected_animation_playback = (
        CanvasPanel._toggle_selected_animation_playback)
    _on_playback_slice = CanvasPanel._on_playback_slice
    _present_due_playback = CanvasPanel._present_due_playback
    _reschedule_playback_timer = CanvasPanel._reschedule_playback_timer
    _stop_playback_timer = CanvasPanel._stop_playback_timer
    on_key_down = CanvasPanel.on_key_down

    def __init__(self, objects, clock=None):
        self.image_objects = ImageObjectList(objects)
        self.selected_object = objects[0] if objects else None
        self.file_navigator = DeferredPlaybackNavigator()
        self._monotonic = clock or FakeClock()
        self.playback_timer = FakeTimer()
        self._interaction_revision = 0
        self._zoom_wheel_remainders = {}
        self.refreshes = 0

    def Refresh(self, *_args):
        self.refreshes += 1

    def _schedule_overlay_clear(self, *args, **kwargs):
        return None

    def _reschedule_overlay_timer(self, *args, **kwargs):
        return None

    def start_preloading_for_object(self, image_object):
        return self.file_navigator.request_preloading(image_object.source_path)


class TestDecoderAndPolicies(unittest.TestCase):
    def test_confirmed_duration_and_loop_policies(self):
        self.assertEqual(effective_frame_duration_ms(None), 100)
        self.assertEqual(effective_frame_duration_ms(0), 100)
        self.assertEqual(effective_frame_duration_ms(1), 1)
        self.assertIsNone(gif_loop_count(None))
        self.assertEqual(gif_loop_count(0), 0)
        self.assertEqual(gif_loop_count(3), 3)

    def test_persistent_decoder_matches_disposal_palette_alpha_and_exact_seek(self):
        first = load_source_pixels(DISPOSAL_GIF)
        identity = get_animation_metadata(first).source_identity
        first.close()
        session = GifDecoderSession(DISPOSAL_GIF, identity)
        try:
            for index in (0, 1, 2, 3, 1):
                candidate, metadata = session.decode(index)
                exact = load_animation_frame(
                    DISPOSAL_GIF, index, expected_source_identity=identity)
                try:
                    self.assertEqual(candidate.mode, exact.mode)
                    self.assertEqual(candidate.size, exact.size)
                    self.assertEqual(candidate.tobytes(), exact.tobytes())
                    self.assertEqual(metadata.frame_index, index)
                    self.assertEqual(metadata.loop_count, 0)
                finally:
                    candidate.close()
                    exact.close()
        finally:
            session.close()
        self.assertTrue(session.closed)


class TestPlaybackSession(unittest.TestCase):
    def test_variable_deadlines_slice_and_frame_caps(self):
        clock = FakeClock(1.0)
        session, budget, _ = playback_session(
            durations=(0, 30, 70, 90), started_at=10.0)
        first = session.decode_slice(budget, clock=clock)
        self.assertEqual(len(first.packets), PLAYBACK_SLICE_FRAMES)
        self.assertEqual([packet.frame_index for packet in first.packets], [1, 2])
        self.assertAlmostEqual(first.packets[0].due_time, 10.1)
        self.assertAlmostEqual(first.packets[1].due_time, 10.13)
        second = session.decode_slice(budget, clock=clock)
        self.assertEqual([packet.frame_index for packet in second.packets], [3])
        self.assertTrue(second.packets[0].terminal)
        self.assertLessEqual(session.max_retained_frames, PLAYBACK_FRAME_CAP)
        for outcome in (first, second):
            outcome.close()
        self.assertEqual(budget.snapshot()["retained_bytes"], 0)

    def test_positive_and_infinite_loops_follow_repeat_metadata(self):
        finite, budget, _ = playback_session(
            durations=(10, 10), loop_count=2, started_at=0)
        indexes = []
        terminals = []
        while not finite.finished:
            outcome = finite.decode_slice(budget, clock=FakeClock())
            indexes.extend(packet.frame_index for packet in outcome.packets)
            terminals.extend(packet.terminal for packet in outcome.packets)
            outcome.close()
        self.assertEqual(indexes, [1, 0, 1, 0, 1])
        self.assertEqual(terminals, [False, False, False, False, True])

        infinite, budget, _ = playback_session(
            durations=(10, 10), loop_count=0, started_at=0)
        indexes = []
        for _ in range(3):
            outcome = infinite.decode_slice(budget, clock=FakeClock())
            indexes.extend(packet.frame_index for packet in outcome.packets)
            outcome.close()
        self.assertFalse(infinite.finished)
        self.assertEqual(indexes, [1, 0, 1, 0, 1, 0])
        infinite.cancel()
        infinite.close_decoder()

    def test_resume_full_duration_and_completed_restart_from_zero(self):
        resumed, budget, _ = playback_session(
            durations=(25, 60, 90), displayed_index=1, started_at=5.0)
        outcome = resumed.decode_slice(budget, clock=FakeClock())
        self.assertEqual(outcome.packets[0].frame_index, 2)
        self.assertEqual(outcome.packets[0].due_time, 5.06)
        outcome.close()

        restarted, budget, _ = playback_session(
            durations=(25, 60, 90), displayed_index=2, started_at=8.0,
            restart=True)
        outcome = restarted.decode_slice(budget, clock=FakeClock())
        self.assertEqual(outcome.packets[0].frame_index, 0)
        self.assertEqual(outcome.packets[0].due_time, 8.0)
        outcome.close()

    def test_global_budget_block_and_oversize_do_not_spin(self):
        budget = PlaybackTransferBudget(limit_bytes=60)
        first, _, _ = playback_session(
            durations=(10, 10, 10), budget=budget, size=(4, 3))
        outcome = first.decode_slice(budget, clock=FakeClock())
        self.assertEqual(len(outcome.packets), 1)
        self.assertTrue(outcome.blocked)
        self.assertEqual(first.decoded_frames, 1)
        self.assertFalse(first.wants_work())
        outcome.close()

        oversize_budget = PlaybackTransferBudget(limit_bytes=40)
        large, _, _ = playback_session(
            durations=(10, 10), budget=oversize_budget, size=(4, 3))
        outcome = large.decode_slice(oversize_budget, clock=FakeClock())
        self.assertIn("32 MiB", outcome.error)
        self.assertTrue(outcome.finished)
        self.assertEqual(oversize_budget.snapshot()["retained_bytes"], 0)

    def test_idle_decoder_retires_without_canceling_playback_intent(self):
        first = load_source_pixels(DISPOSAL_GIF)
        metadata = get_animation_metadata(first)
        first.close()
        session = PlaybackSession(
            "idle", str(DISPOSAL_GIF), metadata.source_identity, 1,
            metadata.frame_count, metadata.logical_size, metadata.loop_count,
            0, metadata.duration_ms, 0, 0.0)
        budget = PlaybackTransferBudget()
        outcome = session.decode_slice(budget, clock=FakeClock())
        self.assertTrue(outcome.packets)
        self.assertFalse(session.decoder.closed)
        session.record_decode_time(0.0)
        self.assertTrue(session.retire_decoder_if_idle(6.0))
        self.assertTrue(session.decoder.closed)
        self.assertFalse(session.canceled)
        outcome.close()


class TestCanvasPlayback(unittest.TestCase):
    def make_result(self, image_object, *, started_at=0.0, durations=(100, 40, 70),
                    loop_count=None):
        descriptor = image_object.animation
        factory = decoder_factory(
            durations, loop_count=loop_count, size=descriptor.logical_size)
        session = PlaybackSession(
            image_object.object_id, image_object.source_path,
            descriptor.source_identity, descriptor.playback_generation,
            len(durations), descriptor.logical_size, loop_count,
            descriptor.displayed_index, descriptor.frame_duration_ms,
            descriptor.loop_repeats_completed, started_at,
            decoder_factory=factory)
        budget = PlaybackTransferBudget()
        outcome = session.decode_slice(budget, clock=FakeClock())
        context = (image_object, image_object.source_path,
                   descriptor.source_identity, descriptor.playback_generation)
        return PlaybackSliceResult(
            context, outcome.packets, outcome.error,
            outcome.blocked, outcome.finished), budget

    def test_space_toggle_repeat_suppression_and_still_no_selection(self):
        import wx

        obj = animated_object()
        canvas = PlaybackCanvasHarness((obj,), FakeClock(4.0))
        with mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                               return_value=True):
            event = KeyEvent(wx.WXK_SPACE)
            canvas.on_key_down(event)
            self.assertTrue(obj.animation.playing)
            self.assertEqual(len(canvas.file_navigator.starts), 1)

            repeat = KeyEvent(wx.WXK_SPACE, repeat=True)
            canvas.on_key_down(repeat)
            self.assertTrue(repeat.skipped)
            self.assertTrue(obj.animation.playing)

            canvas.on_key_down(KeyEvent(wx.WXK_SPACE))
            self.assertFalse(obj.animation.playing)
            self.assertEqual(canvas.file_navigator.cancels[-1], obj.object_id)

        still = ImageObject(str(Path(__file__)))
        canvas.image_objects = ImageObjectList((still,))
        canvas.selected_object = still
        skipped = KeyEvent(wx.WXK_SPACE)
        with mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                               return_value=True):
            canvas.on_key_down(skipped)
        self.assertTrue(skipped.skipped)
        canvas.selected_object = None
        canvas.image_objects = ImageObjectList()
        skipped = KeyEvent(wx.WXK_SPACE)
        with mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                               return_value=True):
            canvas.on_key_down(skipped)
        self.assertTrue(skipped.skipped)

    def test_focus_block_and_selection_change_do_not_stop_peer_playback(self):
        import wx

        first = animated_object(object_id="first")
        second = animated_object(RANDOM_GIF, object_id="second")
        canvas = PlaybackCanvasHarness((first, second), FakeClock())
        with mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                               return_value=True):
            canvas.on_key_down(KeyEvent(wx.WXK_SPACE))
        self.assertTrue(first.animation.playing)
        canvas.set_selected_object(second)
        self.assertTrue(first.animation.playing)
        self.assertNotIn("first", canvas.file_navigator.cancels)

        blocked = KeyEvent(wx.WXK_SPACE)
        with mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                               return_value=False):
            canvas.on_key_down(blocked)
        self.assertTrue(blocked.skipped)
        self.assertFalse(second.animation.playing)

    def test_no_early_presentation_late_drop_and_geometry_preservation(self):
        obj = animated_object()
        clock = FakeClock(0.0)
        canvas = PlaybackCanvasHarness((obj,), clock)
        canvas._toggle_selected_animation_playback()
        result, budget = self.make_result(
            obj, started_at=0.0, durations=(100, 40, 70, 90))
        self.assertTrue(canvas._on_playback_slice(result))
        original = (obj.x, obj.y, obj.width, obj.height,
                    obj.zoom_factor, obj.viewport_offset)
        self.assertFalse(canvas._present_due_playback(0.079))
        self.assertEqual(obj.animation.displayed_index, 0)
        self.assertTrue(canvas._present_due_playback(0.2))
        self.assertEqual(obj.animation.displayed_index, 2)
        self.assertEqual(obj.animation.dropped_frames, 1)
        self.assertEqual(obj.animation.late_frames, 1)
        self.assertEqual(
            (obj.x, obj.y, obj.width, obj.height,
             obj.zoom_factor, obj.viewport_offset), original)
        self.assertEqual(budget.snapshot()["retained_bytes"], 0)

    def test_pause_and_stepping_freeze_committed_not_buffered_frame(self):
        obj = animated_object()
        canvas = PlaybackCanvasHarness((obj,), FakeClock())
        canvas._toggle_selected_animation_playback()
        result, budget = self.make_result(
            obj, started_at=0.0, durations=(100, 40, 70, 90))
        canvas._on_playback_slice(result)
        self.assertEqual(obj.animation.displayed_index, 0)
        self.assertTrue(obj.animation.playback_buffer)
        obj.pause_animation_playback()
        self.assertEqual(obj.animation.displayed_index, 0)
        self.assertFalse(obj.animation.playback_buffer)
        self.assertEqual(budget.snapshot()["retained_bytes"], 0)

    def test_finite_completion_stops_and_next_space_restarts_at_frame_zero(self):
        obj = animated_object()
        descriptor = obj.animation
        descriptor.request_generation += 1
        descriptor.requested_index = 2
        pixels = load_animation_frame(
            obj.source_path, 2,
            expected_source_identity=descriptor.source_identity)
        obj.commit_animation_candidate(
            pixels, 2, descriptor.source_identity,
            descriptor.request_generation)
        obj.animation.loop_count = None
        canvas = PlaybackCanvasHarness((obj,), FakeClock(1.0))
        self.assertTrue(canvas._toggle_selected_animation_playback())
        result, budget = self.make_result(
            obj, started_at=1.0, durations=(20, 20, 20, 20),
            loop_count=None)
        self.assertTrue(result.finished)
        canvas._on_playback_slice(result)
        self.assertTrue(canvas._present_due_playback(1.2))
        self.assertEqual(obj.animation.displayed_index, 3)
        self.assertFalse(obj.animation.playing)
        self.assertTrue(obj.animation.completed)
        self.assertEqual(budget.snapshot()["retained_bytes"], 0)

        canvas._monotonic.set(2.0)
        self.assertTrue(canvas._toggle_selected_animation_playback())
        self.assertTrue(canvas.file_navigator.starts[-1]["restart"])
        self.assertTrue(obj.animation.playing)

    def test_save_and_duplicate_keep_committed_paused_snapshots(self):
        obj = animated_object()
        obj.animation.playing = True
        canvas = PlaybackCanvasHarness((obj,))
        canvas._document_identity = 0
        canvas._naming_request = 0
        canvas._last_naming_success = 0
        canvas._suggested_base = None
        canvas._wall_clock = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.json"
            CanvasPanel.save_canvas_state(canvas, path)
            record = json.loads(path.read_text(encoding="utf-8"))[0]
        self.assertTrue(obj.animation.playing)
        self.assertEqual(record["animation"], {
            "type": "gif", "frame_index": 0, "paused": True})

        duplicate = ImageObject(obj.source_path)
        duplicate._original_image = obj._original_image.copy()
        self.assertTrue(duplicate.is_animated)
        self.assertFalse(duplicate.animation.playing)
        self.assertIsNot(duplicate.animation, obj.animation)
        duplicate.dispose_source_pixels()


class TestNavigatorPlaybackLifecycle(unittest.TestCase):
    def test_running_cancel_is_nonblocking_and_late_result_is_released(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        class BlockingDecoder:
            def __init__(self, _path, source_identity):
                self.source_identity = tuple(source_identity)

            def decode(self, index):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release decoder")
                pixels = Image.new("RGBA", (4, 3), (20, 30, 40, 255))
                metadata = AnimationFrameMetadata(
                    3, index, 20, (4, 3), self.source_identity, 0)
                return _attach_animation_metadata(pixels, metadata), metadata

            def close(self):
                closed.set()

        results = []
        navigator = FileNavigator(
            SettingsStub("0"), playback_decoder_factory=BlockingDecoder,
            result_dispatch=lambda callback, result: callback(result))
        self.addCleanup(release.set)
        accepted = navigator.start_gif_playback(
            object_id="blocked", path="ignored.gif",
            source_identity=("ignored.gif", 1, 2), playback_generation=1,
            frame_count=3, logical_size=(4, 3), loop_count=0,
            displayed_index=0, displayed_duration_ms=20,
            repeats_completed=0, started_at=0, restart=False,
            context=(), callback=results.append)
        self.assertTrue(accepted)
        self.assertTrue(entered.wait(2))
        started = time.perf_counter()
        self.assertTrue(navigator.cancel_gif_playback("blocked"))
        self.assertLess(time.perf_counter() - started, 0.1)
        release.set()
        self.assertTrue(navigator.wait_for_workers(2))
        self.assertTrue(closed.wait(1))
        self.assertEqual(results, [])
        self.assertEqual(
            navigator.preload_state()["playback_transfer"]["retained_bytes"], 0)

    def test_three_sessions_share_limits_and_release_all_transfers(self):
        factory = decoder_factory((20, 20, 20, 20), loop_count=0)
        results = []
        navigator = FileNavigator(
            SettingsStub("0"), playback_decoder_factory=factory,
            result_dispatch=lambda callback, result: callback(result))
        for index in range(3):
            self.assertTrue(navigator.start_gif_playback(
                object_id=f"object-{index}", path="ignored.gif",
                source_identity=("ignored.gif", 1, 2), playback_generation=1,
                frame_count=4, logical_size=(4, 3), loop_count=0,
                displayed_index=0, displayed_duration_ms=20,
                repeats_completed=0, started_at=0, restart=False,
                context=(index,), callback=results.append))
        self.assertTrue(navigator.wait_for_workers(2))
        state = navigator.preload_state()
        self.assertLessEqual(state["active"], 3)
        self.assertLessEqual(state["pending"], 10)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(len(result.packets) <= 2 for result in results))
        self.assertTrue(all(value <= 3 for value in
                            state["playback_session_max_frames"].values()))
        for result in results:
            result.close()
        for index in range(3):
            navigator.cancel_gif_playback(f"object-{index}")
        self.assertEqual(
            navigator.preload_state()["playback_transfer"]["retained_bytes"], 0)

    def test_global_capacity_release_wakes_a_blocked_peer(self):
        factory = decoder_factory((20, 20, 20), loop_count=0)
        results = {"first": [], "second": []}
        second_woke = threading.Event()
        navigator = FileNavigator(
            SettingsStub("0"), playback_decoder_factory=factory,
            playback_budget_bytes=60,
            result_dispatch=lambda callback, result: callback(result))

        def start(name):
            def receive(result):
                results[name].append(result)
                if name == "second" and len(results[name]) >= 2:
                    second_woke.set()

            return navigator.start_gif_playback(
                object_id=name, path="ignored.gif",
                source_identity=("ignored.gif", 1, 2), playback_generation=1,
                frame_count=3, logical_size=(4, 3), loop_count=0,
                displayed_index=0, displayed_duration_ms=20,
                repeats_completed=0, started_at=0, restart=False,
                context=(name,), callback=receive)

        self.assertTrue(start("first"))
        self.assertTrue(navigator.wait_for_workers(2))
        self.assertEqual(len(results["first"]), 1)
        self.assertTrue(results["first"][0].blocked)
        self.assertTrue(start("second"))
        self.assertTrue(navigator.wait_for_workers(2))
        self.assertTrue(results["second"][0].blocked)
        self.assertFalse(results["second"][0].packets)

        results["first"][0].close()
        self.assertTrue(second_woke.wait(2))
        self.assertEqual(len(results["second"]), 2)
        self.assertTrue(results["second"][1].packets)
        for group in results.values():
            for result in group:
                result.close()
        navigator.cancel_gif_playback("first")
        navigator.cancel_gif_playback("second")
        self.assertEqual(
            navigator.preload_state()["playback_transfer"]["retained_bytes"], 0)

    def test_full_playback_queue_defers_lookahead_for_foreground_work(self):
        release = threading.Event()
        condition = threading.Condition()
        entered = [0]

        class BlockingDecoder:
            def __init__(self, _path, source_identity):
                self.source_identity = tuple(source_identity)

            def decode(self, index):
                with condition:
                    entered[0] += 1
                    condition.notify_all()
                if not release.wait(5):
                    raise TimeoutError("test did not release playback")
                pixels = Image.new("RGBA", (4, 3), (index, 20, 30, 255))
                metadata = AnimationFrameMetadata(
                    3, index, 20, (4, 3), self.source_identity, 0)
                return _attach_animation_metadata(pixels, metadata), metadata

            def close(self):
                return None

        navigator = FileNavigator(
            SettingsStub("0"), playback_decoder_factory=BlockingDecoder,
            result_dispatch=lambda callback, result: callback(result))
        self.addCleanup(release.set)
        playback_done = []

        def receive_playback(result):
            playback_done.append(result.context[0])
            result.close()

        for index in range(13):
            self.assertTrue(navigator.start_gif_playback(
                object_id=f"queued-{index}", path="ignored.gif",
                source_identity=("ignored.gif", 1, 2), playback_generation=1,
                frame_count=3, logical_size=(4, 3), loop_count=0,
                displayed_index=0, displayed_duration_ms=20,
                repeats_completed=0, started_at=0, restart=False,
                context=(index,), callback=receive_playback))
        with condition:
            self.assertTrue(condition.wait_for(lambda: entered[0] >= 3, timeout=2))
        self.assertEqual(navigator.preload_state()["pending"], 10)

        self.assertTrue(navigator.request_navigation_decode(
            "foreground-missing.png", "foreground", (),
            lambda result: result.close()))
        state = navigator.preload_state()
        self.assertEqual(state["pending"], 10)
        self.assertEqual(state["foreground"], 1)
        self.assertEqual(state["playback_sessions"], 13)
        self.assertEqual(state["queued"][0], "foreground-missing.png")

        release.set()
        self.assertTrue(navigator.wait_for_workers(3))
        self.assertEqual(set(playback_done), set(range(13)))
        for index in range(13):
            navigator.cancel_gif_playback(f"queued-{index}")


@unittest.skipUnless(
    os.environ.get("NAGUMIX_GUI_TESTS") == "1",
    "Set NAGUMIX_GUI_TESTS=1 to run visible production GIF playback",
)
class TestVisibleProductionPlayback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import wx
        cls.app = wx.App.Get() or wx.App(False)

    def test_one_and_three_overlapping_cropped_animations(self):
        import wx

        observations = []
        for object_count in (1, 3):
            frame = wx.Frame(
                None, title=f"Regression 30 playback: {object_count} GIF(s)",
                size=(360, 260))
            canvas = CanvasPanel(frame, SettingsStub("0"))
            loop = wx.GUIEventLoop()
            activator = wx.EventLoopActivator(loop)
            objects = []
            try:
                for index in range(object_count):
                    obj = animated_object(object_id=f"visible-{object_count}-{index}")
                    obj.x = 20 + index * 12
                    obj.y = 20 + index * 10
                    canvas.add_image_object(obj)
                    objects.append(obj)
                frame.Show()
                wx.Yield()
                for obj in objects:
                    canvas.set_selected_object(obj)
                    self.assertTrue(canvas._toggle_selected_animation_playback())

                deadline = time.monotonic() + 4
                while (time.monotonic() < deadline
                       and any(obj.animation.presented_frames < 2
                               for obj in objects)):
                    wx.Yield()
                    wx.MilliSleep(2)
                self.assertTrue(
                    all(obj.animation.presented_frames >= 2 for obj in objects),
                    ([(obj.animation.playing, obj.animation.presented_frames,
                       len(obj.animation.playback_buffer), obj.status_message)
                      for obj in objects],
                     (canvas.playback_timer.IsRunning(),
                      canvas.playback_timer.GetInterval()),
                     canvas.file_navigator.preload_state()),
                )

                selected = objects[0]
                canvas.set_selected_object(selected)
                self.assertTrue(canvas._toggle_selected_animation_playback())
                self.assertFalse(selected.animation.playing)
                committed = selected.animation.displayed_index
                direction = -1 if committed == selected.animation.frame_count - 1 else 1
                self.assertTrue(canvas._step_selected_animation(direction))
                deadline = time.monotonic() + 3
                while (time.monotonic() < deadline
                       and selected.animation.displayed_index == committed):
                    wx.Yield()
                    wx.MilliSleep(2)
                self.assertNotEqual(selected.animation.displayed_index, committed)
                self.assertFalse(selected.animation.playing)
                self.assertTrue(canvas._toggle_selected_animation_playback())
                resumed_count = selected.animation.presented_frames
                deadline = time.monotonic() + 3
                while (time.monotonic() < deadline
                       and selected.animation.presented_frames == resumed_count):
                    wx.Yield()
                    wx.MilliSleep(2)
                self.assertGreater(selected.animation.presented_frames, resumed_count)
                observations.append({
                    "objects": object_count,
                    "presented": [obj.animation.presented_frames for obj in objects],
                    "late": [obj.animation.late_frames for obj in objects],
                    "dropped": [obj.animation.dropped_frames for obj in objects],
                })
            finally:
                canvas.shutdown_preloading()
                canvas.file_navigator.wait_for_workers(3)
                frame.Destroy()
                wx.Yield()
                del activator
        print(f"Regression 30 visible observations: {observations}")

    def test_space_preserves_text_button_and_dialog_focus(self):
        import wx

        frame = wx.Frame(None, title="Regression 30 Space focus", size=(320, 220))
        canvas = CanvasPanel(frame, SettingsStub("0"))
        obj = animated_object(object_id="focus-object")
        canvas.add_image_object(obj)
        canvas.set_selected_object(obj)
        text = wx.TextCtrl(frame)
        button = wx.Button(frame, label="Keep focus")
        dialog = wx.Dialog(frame, title="Dialog focus")
        dialog_text = wx.TextCtrl(dialog)
        frame.Show()
        dialog.Show()
        wx.Yield()
        try:
            for control in (text, button, dialog_text):
                with self.subTest(control=type(control).__name__):
                    control.SetFocus()
                    wx.Yield()
                    self.assertIs(wx.Window.FindFocus(), control)
                    event = KeyEvent(wx.WXK_SPACE)
                    canvas.on_key_down(event)
                    self.assertTrue(event.skipped)
                    self.assertFalse(obj.animation.playing)
        finally:
            dialog.Destroy()
            canvas.shutdown_preloading()
            frame.Destroy()
            wx.Yield()

if __name__ == "__main__":
    unittest.main()
