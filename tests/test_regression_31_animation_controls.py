import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import wx

from src.animation_controls import (
    ACTIVATION_PADDING_DIP,
    FADE_DURATION_MS,
    AnimationControlState,
    animation_control_presentation,
    layout_animation_controls,
)
from src.canvas_panel import CanvasPanel, ImageObjectList
from tests.test_regression_27_gif_frames import SettingsStub, animated_object


class FakeClock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class FakeTimer:
    def __init__(self):
        self.running = False
        self.delay = None

    def IsRunning(self):
        return self.running

    def Start(self, delay, *_args):
        self.running = True
        self.delay = delay

    def Stop(self):
        self.running = False


class NavigatorStub:
    is_shutdown = False

    def __init__(self):
        self.preloads = []

    def request_preloading(self, path):
        self.preloads.append(path)
        return True

    def cancel_navigation_decode(self, _object_id):
        return True

    def cancel_animation_frame(self, _object_id):
        return True

    def cancel_gif_playback(self, _object_id):
        return True


class Event:
    def __init__(self, position=(0, 0), key=None, *, shift=False):
        self.position = position
        self.key = key
        self.shift = shift
        self.skipped = False

    def GetPosition(self):
        return self.position

    def GetKeyCode(self):
        return self.key

    def ShiftDown(self):
        return self.shift

    def ControlDown(self):
        return False

    def AltDown(self):
        return False

    def IsAutoRepeat(self):
        return False

    def Dragging(self):
        return False

    def LeftIsDown(self):
        return False

    def Skip(self):
        self.skipped = True


class ControlHarness:
    get_selected_object = CanvasPanel.get_selected_object
    set_selected_object = CanvasPanel.set_selected_object
    start_preloading_for_object = CanvasPanel.start_preloading_for_object

    def __init__(self, objects, *, size=(360, 240), clock=None):
        self.image_objects = ImageObjectList(objects)
        self.selected_object = objects[0] if objects else None
        self.marked_object = None
        self.drag_offset = None
        self.resizing = False
        self._interaction_revision = 0
        self._zoom_wheel_remainders = {}
        self._context_object_id = None
        self._size = size
        self._monotonic = clock or FakeClock()
        self._animation_controls = AnimationControlState(self._monotonic)
        self._animation_control_pointer = None
        self.animation_control_timer = FakeTimer()
        self.file_navigator = NavigatorStub()
        self.refreshes = 0
        self.tooltip = None
        self.focused = False
        self.captured = False

    def GetClientSize(self):
        return self._size

    def GetSize(self):
        return self._size

    def GetDPIScaleFactor(self):
        return 1.0

    def Refresh(self, *_args):
        self.refreshes += 1

    def SetToolTip(self, value):
        self.tooltip = value

    def SetFocus(self):
        self.focused = True

    def HasCapture(self):
        return self.captured

    def CaptureMouse(self):
        self.captured = True

    def ReleaseMouse(self):
        self.captured = False

    def _reschedule_overlay_timer(self, *_args, **_kwargs):
        return False


def positioned_object(object_id, x=80, y=50):
    obj = animated_object(object_id=object_id)
    obj.x, obj.y, obj.width, obj.height = x, y, 160, 100
    return obj


class TestGeometryAndFade(unittest.TestCase):
    def test_hidden_activation_is_padded_clipped_and_invisible_objects_skip(self):
        layout = layout_animation_controls("visible", (5, 10, 80, 60),
                                           (180, 120), 1.0)
        self.assertIsNotNone(layout)
        self.assertLessEqual(layout.activation.x, layout.panel.x)
        self.assertGreaterEqual(layout.activation.right, layout.panel.right)
        self.assertLessEqual(layout.panel.right, 180)
        self.assertLessEqual(layout.activation.width,
                             layout.panel.width + 2 * ACTIVATION_PADDING_DIP)
        self.assertIsNone(layout_animation_controls(
            "outside", (-100, -100, 20, 20), (180, 120), 1.0))

    def test_small_edge_layout_keeps_essential_buttons_and_scales(self):
        small = layout_animation_controls("edge", (145, 80, 70, 60),
                                          (150, 95), 1.0)
        scaled = layout_animation_controls("edge", (20, 20, 100, 80),
                                           (420, 260), 2.0)
        self.assertEqual([button.name for button in small.buttons],
                         ["previous", "play_pause", "next", "hide"])
        self.assertIsNone(small.position_rect)
        self.assertGreater(scaled.panel.height, small.panel.height)
        self.assertLessEqual(small.panel.right, 150)
        self.assertLessEqual(small.panel.bottom, 95)

    def test_committed_label_play_state_and_boundaries(self):
        descriptor = SimpleNamespace(
            displayed_index=0, requested_index=2, frame_count=3, playing=False)
        view = animation_control_presentation(descriptor)
        self.assertEqual(view.frame_label, "1 / 3")
        self.assertEqual(view.pending_label, "Seeking 3")
        self.assertFalse(view.playing)
        self.assertTrue(view.previous_enabled)
        self.assertFalse(view.next_enabled)
        descriptor.displayed_index = 2
        descriptor.playing = True
        view = animation_control_presentation(descriptor)
        self.assertEqual(view.frame_label, "3 / 3")
        self.assertIsNone(view.pending_label)
        self.assertTrue(view.playing)
        self.assertFalse(view.next_enabled)

    def test_fade_reverses_from_current_opacity(self):
        clock = FakeClock()
        state = AnimationControlState(clock)
        state.target_id = "gif"
        state.visible_goal = True
        clock.value = 0.4
        state.advance()
        self.assertAlmostEqual(state.opacity, 0.4)
        state.visible_goal = False
        clock.value = 0.6
        state.advance()
        self.assertAlmostEqual(state.opacity, 0.2)
        state.visible_goal = True
        clock.value = 0.7
        state.advance()
        self.assertAlmostEqual(state.opacity, 0.3)
        self.assertEqual(FADE_DURATION_MS, 1000)


class TestTargetingAndInput(unittest.TestCase):
    def test_topmost_duplicate_path_wins_without_hover_selection(self):
        bottom = positioned_object("bottom")
        top = positioned_object("top")
        self.assertEqual(bottom.source_path, top.source_path)
        canvas = ControlHarness([bottom, top])
        canvas.selected_object = bottom
        layout = CanvasPanel._animation_control_layout_for(canvas, top)
        point = (layout.panel.x + 2, layout.panel.y + 2)
        CanvasPanel._update_animation_control_hover(canvas, *point)
        self.assertEqual(canvas._animation_controls.target_id, "top")
        self.assertIs(canvas.selected_object, bottom)
        self.assertTrue(canvas.animation_control_timer.IsRunning())

    def test_padding_passes_through_and_panel_click_selects_dispatches_once(self):
        bottom = positioned_object("bottom")
        top = positioned_object("top")
        canvas = ControlHarness([bottom, top])
        layout = CanvasPanel._animation_control_layout_for(canvas, top)
        padding_point = (layout.activation.x, layout.activation.y)
        CanvasPanel._update_animation_control_hover(canvas, *padding_point)
        self.assertIsNone(CanvasPanel._animation_control_button_at(
            canvas, *padding_point))
        padding_event = Event(padding_point)
        CanvasPanel.on_left_down(canvas, padding_event)
        self.assertIsNone(canvas.selected_object)
        self.assertTrue(padding_event.skipped)

        play = next(button for button in layout.buttons
                    if button.name == "play_pause")
        point = (play.rect.x + 1, play.rect.y + 1)
        CanvasPanel._update_animation_control_hover(canvas, *point)
        with mock.patch.object(CanvasPanel, "_toggle_animation_playback",
                               return_value=True) as action:
            CanvasPanel.on_left_down(canvas, Event(point))
        self.assertIs(canvas.selected_object, top)
        action.assert_called_once_with(canvas, top.object_id)
        self.assertTrue(canvas.captured)

    def test_wheel_is_consumed_only_over_actual_visible_panel(self):
        obj = positioned_object("wheel")
        canvas = ControlHarness([obj])
        layout = CanvasPanel._animation_control_layout_for(canvas, obj)
        point = (layout.panel.x + 2, layout.panel.y + 2)
        CanvasPanel._update_animation_control_hover(canvas, *point)
        event = Event(point)
        CanvasPanel.on_mouse_wheel(canvas, event)
        self.assertFalse(event.skipped)

    def test_focused_space_activates_once_and_tab_releases_without_trap(self):
        obj = positioned_object("focus")
        canvas = ControlHarness([obj])
        self.assertTrue(CanvasPanel.show_animation_controls(canvas, obj.object_id))
        canvas._animation_controls.focus_index = 1
        event = Event(key=wx.WXK_SPACE)
        with mock.patch.object(CanvasPanel, "_activate_animation_control",
                               return_value=True) as action:
            self.assertTrue(CanvasPanel._handle_animation_control_key(
                canvas, event))
        action.assert_called_once_with(canvas, "play_pause")
        canvas._animation_controls.focus_index = 4
        tab = Event(key=wx.WXK_TAB)
        self.assertTrue(CanvasPanel._handle_animation_control_key(canvas, tab))
        self.assertIsNone(canvas._animation_controls.focus_index)

    def test_focused_control_does_not_swallow_frame_step_shortcuts(self):
        obj = positioned_object("focus-step")
        canvas = ControlHarness([obj])
        canvas.selected_object = obj
        canvas._animation_controls.focus_index = 0
        event = Event(key=ord('.'))
        with mock.patch.object(CanvasPanel, "_step_selected_animation",
                               return_value=True) as step, \
                mock.patch.object(CanvasPanel, "_frame_shortcut_has_canvas_focus",
                                  return_value=True):
            CanvasPanel.on_key_down(canvas, event)
        step.assert_called_once_with(canvas, 1)

    def test_explicit_hide_requires_leave_then_reentry_and_context_reopens(self):
        obj = positioned_object("hide")
        canvas = ControlHarness([obj])
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        self.assertTrue(CanvasPanel._activate_animation_control(canvas, "hide"))
        self.assertEqual(canvas._animation_controls.suppressed_id, "hide")
        layout = CanvasPanel._animation_control_layout_for(canvas, obj)
        point = (layout.panel.x + 1, layout.panel.y + 1)
        CanvasPanel._update_animation_control_hover(canvas, *point)
        self.assertFalse(canvas._animation_controls.visible_goal)
        CanvasPanel._update_animation_control_hover(canvas, 0, 0)
        self.assertIsNone(canvas._animation_controls.suppressed_id)
        CanvasPanel._update_animation_control_hover(canvas, *point)
        self.assertTrue(canvas._animation_controls.visible_goal)
        CanvasPanel._activate_animation_control(canvas, "hide")
        self.assertTrue(CanvasPanel.show_animation_controls(canvas, obj.object_id))
        self.assertIsNone(canvas._animation_controls.suppressed_id)

    def test_deleted_target_clears_hits_timer_focus_and_decoration_keeps_cache(self):
        obj = positioned_object("deleted")
        cache = object()
        obj._prepared_bitmap = cache
        obj._prepared_bitmap_key = ("sentinel",)
        canvas = ControlHarness([obj])
        CanvasPanel.show_animation_controls(canvas, obj.object_id)
        CanvasPanel._update_animation_control_hover(canvas, 100, 100)
        self.assertIs(obj._prepared_bitmap, cache)
        canvas.image_objects.remove(obj)
        CanvasPanel.on_animation_control_timer(canvas, None)
        self.assertIsNone(canvas._animation_controls.target_id)
        self.assertFalse(canvas.animation_control_timer.IsRunning())
        self.assertIsNone(CanvasPanel._animation_control_button_at(
            canvas, 100, 100))


@unittest.skipUnless(
    os.environ.get("NAGUMIX_GUI_TESTS") == "1",
    "Set NAGUMIX_GUI_TESTS=1 to run visible production animation controls",
)
class TestVisibleProductionControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_overlapping_hover_fade_actions_edge_hide_and_peer_playback(self):
        frame = wx.Frame(None, title="Regression 31 GIF controls", size=(360, 240))
        canvas = CanvasPanel(frame, SettingsStub("0"))
        first = positioned_object("visible-first", 180, 120)
        second = positioned_object("visible-second", 190, 125)
        canvas.add_image_object(first)
        canvas.add_image_object(second)
        frame.Show()
        wx.Yield()
        try:
            canvas.set_selected_object(first)
            self.assertTrue(canvas._toggle_selected_animation_playback())
            layout = canvas._animation_control_layout_for(second)
            point = (layout.panel.x + 2, layout.panel.y + 2)
            canvas._update_animation_control_hover(*point)
            canvas._animation_controls.advance(canvas._monotonic() + 0.35)
            faded_in = canvas._animation_controls.opacity
            canvas._set_animation_control_target(second.object_id, False)
            canvas._animation_controls.advance(canvas._monotonic() + 0.1)
            canvas._set_animation_control_target(second.object_id, True)
            self.assertGreater(faded_in, 0.0)
            self.assertTrue(first.animation.playing)
            self.assertTrue(canvas.show_animation_controls(second.object_id))
            self.assertTrue(canvas._activate_animation_control("play_pause"))
            self.assertTrue(second.animation.playing)
            self.assertTrue(canvas._activate_animation_control("hide"))
            self.assertTrue(first.animation.playing)
            self.assertTrue(canvas.show_animation_controls(second.object_id))
            edge_layout = canvas._animation_control_layout_for(second)
            width, height = canvas.get_client_dimensions()
            self.assertLessEqual(edge_layout.panel.right, width)
            self.assertLessEqual(edge_layout.panel.bottom, height)
            print({
                "target": canvas._animation_controls.target_id,
                "fade_reversed_from": round(faded_in, 2),
                "edge_panel": edge_layout.panel,
                "peer_playing": first.animation.playing,
                "target_playing": second.animation.playing,
            })
        finally:
            canvas.shutdown_preloading()
            canvas.file_navigator.wait_for_workers(3)
            frame.Destroy()
            wx.Yield()


if __name__ == "__main__":
    unittest.main()
