import unittest
from types import SimpleNamespace

import wx

from src.canvas_panel import (
    CanvasPanel,
    DropEntry,
    ImageObjectList,
    DropOperation,
    ExportOperation,
    SceneLoadOperation,
)
from src.exporting import ExportCancellation
from src.image_object import ImageObject


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

    def IsRunning(self):
        return self.running

    def Start(self, delay_ms, mode):
        self.running = True
        self.starts.append((delay_ms, mode))

    def Stop(self):
        self.running = False


class FakeDC:
    def GetTextExtent(self, text):
        return SimpleNamespace(width=max(20, len(text) * 7), height=14)

    def SetPen(self, *args):
        pass

    def SetBrush(self, *args):
        pass

    def SetTextForeground(self, *args):
        pass

    def DrawRoundedRectangle(self, *args):
        pass

    def DrawText(self, *args):
        pass


class Settings:
    def get_setting(self, section, key, fallback=None):
        return fallback


class CanvasHarness:
    _draw_drop_status = CanvasPanel._draw_drop_status
    _drop_status_lines = CanvasPanel._drop_status_lines
    _draw_scene_status = CanvasPanel._draw_scene_status
    _scene_status_lines = CanvasPanel._scene_status_lines
    _draw_export_status = CanvasPanel._draw_export_status
    _finalize_drop_operation = CanvasPanel._finalize_drop_operation
    _commit_scene_operation = CanvasPanel._commit_scene_operation
    _on_export_finished = CanvasPanel._on_export_finished
    _schedule_overlay_clear = CanvasPanel._schedule_overlay_clear
    _reschedule_overlay_timer = CanvasPanel._reschedule_overlay_timer
    _stop_overlay_timer = CanvasPanel._stop_overlay_timer
    on_overlay_timer = CanvasPanel.on_overlay_timer
    cancel_export_operation = CanvasPanel.cancel_export_operation
    _handle_drop_card_click = CanvasPanel._handle_drop_card_click
    _handle_export_card_click = CanvasPanel._handle_export_card_click
    _point_in_rect = staticmethod(CanvasPanel._point_in_rect)
    _retire_drop_card = CanvasPanel._retire_drop_card
    _retire_scene_card = CanvasPanel._retire_scene_card
    _retire_export_card = CanvasPanel._retire_export_card

    def __init__(self, objects=()):
        self.image_objects = ImageObjectList(objects)
        self.selected_object = None
        self.marked_object = None
        self.settings_manager = Settings()
        self._monotonic = FakeClock()
        self._zoom_wheel_remainders = {}
        self.overlay_clear_timer = FakeTimer()
        self.drop_operation = None
        self.scene_operation = None
        self.export_operation = None
        self._drop_card_rect = None
        self._drop_cancel_rect = None
        self._scene_card_rect = None
        self._scene_cancel_rect = None
        self._export_card_rect = None
        self._export_cancel_rect = None
        self.refresh_count = 0
        self.file_navigator = SimpleNamespace(is_shutdown=False)

    def Refresh(self):
        self.refresh_count += 1

    def get_client_dimensions(self):
        return 500, 400

    def GetSize(self):
        return 500, 400

    def get_selected_object(self):
        return self.selected_object

    def start_preloading_for_object(self, obj):
        pass


def successful_drop(generation=1):
    obj = ImageObject("dropped.png")
    operation = DropOperation(generation, (20, 20), 0)
    operation.entries.append(
        DropEntry(1, "dropped.png", (20, 20), "succeeded", image_object=obj))
    return operation, obj


def successful_export(generation=1, **overrides):
    values = dict(
        generation=generation, destination="canvas.png", format_name="PNG",
        width=500, height=400, total=0,
        cancellation=ExportCancellation(), request_key=("export", generation),
    )
    values.update(overrides)
    return ExportOperation(**values)


class TestNotificationCardLifetimes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_successful_drop_starts_on_first_paint_and_expires_due(self):
        canvas = CanvasHarness()
        operation, obj = successful_drop()
        obj._prepared_bitmap = object()
        canvas.drop_operation = operation

        self.assertTrue(canvas._finalize_drop_operation())
        self.assertIsNone(operation.card_deadline)
        canvas._draw_drop_status(FakeDC())
        self.assertAlmostEqual(operation.card_deadline, 102.5)
        bitmap = obj._prepared_bitmap

        canvas._monotonic.advance(2.499)
        canvas.on_overlay_timer(None)
        self.assertIs(canvas.drop_operation, operation)
        canvas._monotonic.advance(0.001)
        canvas.on_overlay_timer(None)
        self.assertIsNone(canvas.drop_operation)
        self.assertIsNone(canvas._drop_card_rect)
        self.assertIsNone(canvas._drop_cancel_rect)
        self.assertIs(obj._prepared_bitmap, bitmap)

    def test_empty_scene_completion_is_a_routine_success(self):
        canvas = CanvasHarness()
        operation = SceneLoadOperation(1, "empty.json", stage="decoding")
        canvas.scene_operation = operation

        self.assertTrue(canvas._commit_scene_operation(operation))
        canvas._draw_scene_status(FakeDC())
        self.assertAlmostEqual(operation.card_deadline, 102.5)
        canvas._monotonic.advance(2.5)
        canvas.on_overlay_timer(None)
        self.assertIsNone(canvas.scene_operation)
        self.assertIsNone(canvas._scene_card_rect)

    def test_background_only_export_completion_is_a_routine_success(self):
        canvas = CanvasHarness()
        operation = successful_export()
        canvas.export_operation = operation
        result = SimpleNamespace(
            destination="canvas.png", rendered=0, visible=0, clipped=0,
            outside=0, failures=(), error=None, committed=True, status="success")

        self.assertTrue(canvas._on_export_finished((1, result)))
        canvas._draw_export_status(FakeDC())
        self.assertAlmostEqual(operation.card_deadline, 102.5)
        canvas._monotonic.advance(2.5)
        canvas.on_overlay_timer(None)
        self.assertIsNone(canvas.export_operation)
        self.assertIsNone(canvas._export_card_rect)

    def test_multiple_cards_use_independent_presentation_deadlines(self):
        canvas = CanvasHarness()
        drop, _ = successful_drop()
        drop.terminal = True
        scene = SceneLoadOperation(1, "scene.json", stage="complete",
                                    committed=True, terminal=True)
        canvas.drop_operation = drop
        canvas.scene_operation = scene
        canvas._draw_drop_status(FakeDC())
        canvas._monotonic.advance(1.0)
        canvas._draw_scene_status(FakeDC())
        self.assertEqual((drop.card_deadline, scene.card_deadline), (102.5, 103.5))

        canvas._monotonic.advance(1.5)
        canvas.on_overlay_timer(None)
        self.assertIsNone(canvas.drop_operation)
        self.assertIs(canvas.scene_operation, scene)
        self.assertTrue(canvas.overlay_clear_timer.IsRunning())
        self.assertEqual(canvas.overlay_clear_timer.starts[-1][0], 1000)

    def test_repeated_paints_do_not_extend_deadline_and_delayed_paint_gets_full_interval(self):
        canvas = CanvasHarness()
        operation, _ = successful_drop()
        operation.terminal = True
        canvas.drop_operation = operation
        canvas._monotonic.advance(10.0)
        canvas._draw_drop_status(FakeDC())
        first_deadline = operation.card_deadline
        canvas._monotonic.advance(1.0)
        canvas._draw_drop_status(FakeDC())
        self.assertEqual(operation.card_deadline, first_deadline)
        self.assertEqual(first_deadline, 112.5)

    def test_failures_partial_success_canceled_and_clipping_warnings_persist(self):
        canvas = CanvasHarness()
        failed_drop, _ = successful_drop()
        failed_drop.entries[0].state = "failed"
        failed_drop.entries[0].image_object = None
        failed_drop.entries[0].error = "decode failed"
        failed_drop.terminal = True
        canvas.drop_operation = failed_drop
        canvas._draw_drop_status(FakeDC())
        self.assertIsNone(failed_drop.card_deadline)

        scene = SceneLoadOperation(1, "failed.json", stage="failed",
                                   terminal=True)
        canvas.scene_operation = scene
        canvas._draw_scene_status(FakeDC())
        self.assertIsNone(scene.card_deadline)

        export = successful_export(clipped=1, total=1, rendered=1,
                                   visible=1, committed=True, stage="success",
                                   terminal=True)
        canvas.export_operation = export
        canvas._draw_export_status(FakeDC())
        self.assertIsNone(export.card_deadline)

    def test_active_export_keeps_cancel_and_has_no_success_deadline(self):
        canvas = CanvasHarness()
        operation = successful_export(total=2, stage="rendering", rendered=1)
        canvas.export_operation = operation
        canvas._draw_export_status(FakeDC())
        self.assertIsNone(operation.card_deadline)
        self.assertIsNotNone(canvas._export_cancel_rect)

    def test_manual_dismissal_retires_card_and_hit_regions(self):
        canvas = CanvasHarness()
        operation, _ = successful_drop()
        operation.terminal = True
        canvas.drop_operation = operation
        canvas._draw_drop_status(FakeDC())
        left, top, _, _ = canvas._drop_card_rect
        self.assertTrue(canvas._handle_drop_card_click(left + 1, top + 1))
        self.assertIsNone(canvas.drop_operation)
        self.assertIsNone(canvas._drop_card_rect)
        self.assertIsNone(canvas._drop_cancel_rect)
        self.assertFalse(canvas.overlay_clear_timer.IsRunning())

    def test_replacing_card_makes_old_timer_delivery_harmless(self):
        canvas = CanvasHarness()
        old, _ = successful_drop(1)
        canvas.drop_operation = old
        canvas._draw_drop_status(FakeDC())
        canvas._retire_drop_card()
        new = DropOperation(2, (30, 30), 0)
        canvas.drop_operation = new
        canvas._monotonic.advance(2.5)
        canvas.on_overlay_timer(None)
        self.assertIs(canvas.drop_operation, new)

    def test_object_status_and_card_expire_independently(self):
        obj = ImageObject("peer.png")
        canvas = CanvasHarness([obj])
        obj.set_status_overlay("peer")
        canvas._schedule_overlay_clear(5000, obj)
        operation, _ = successful_drop()
        operation.terminal = True
        canvas.drop_operation = operation
        canvas._draw_drop_status(FakeDC())
        canvas._monotonic.advance(2.5)
        canvas.on_overlay_timer(None)
        self.assertIsNone(canvas.drop_operation)
        self.assertTrue(obj.show_status_overlay)
        self.assertEqual(canvas.overlay_clear_timer.starts[-1][0], 2500)

    def test_cancel_keeps_summary_persistent_and_removes_cancel_target(self):
        canvas = CanvasHarness()
        operation = successful_export(total=1, stage="rendering")
        canvas.export_operation = operation
        canvas._draw_export_status(FakeDC())
        left, top, _, _ = canvas._export_cancel_rect
        self.assertTrue(canvas.cancel_export_operation())
        self.assertEqual(operation.stage, "canceled")
        self.assertIsNone(operation.card_deadline)
        canvas._draw_export_status(FakeDC())
        self.assertIsNone(canvas._export_cancel_rect)
        self.assertTrue(canvas._handle_export_card_click(left + 1, top + 1)
                        if canvas._export_card_rect else False)


if __name__ == "__main__":
    unittest.main()
