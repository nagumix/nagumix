"""Regression 38 frame geometry and canvas ownership checks."""

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import wx
from PIL import Image

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.image_object import ImageObject
from src.image_pixels import load_animation_frame, load_source_pixels
from src.file_navigator import AnimationDecodeResult
from src.exporting import ExportCancellation, render_export_snapshot
from src.canvas_state import write_state, read_state
from src.main_frame import MainFrame
from src.viewport_geometry import (
    GeometryError, Rect, ViewGeometry, apply_to_object, handle_centers,
    hit_handle, reposition_content, reset_frame_size, resize_frame,
)
from tests.test_regression_31_animation_controls import ControlHarness


START = ViewGeometry(Rect(180, 130, 320, 220), (600, 400), (80, 55), 1.0)


class TestViewportGeometry(unittest.TestCase):
    def test_all_handles_keep_content_and_opposite_anchor(self):
        for name, center in handle_centers(START.frame):
            with self.subTest(name=name):
                dx = -20 if "w" in name else 20 if "e" in name else 0
                dy = -15 if "n" in name else 15 if "s" in name else 0
                new = resize_frame(START, name, (center[0] + dx, center[1] + dy))
                self.assertEqual(new.content_rect, START.content_rect)
                self.assertEqual(new.zoom, START.zoom)
                if "w" in name:
                    self.assertEqual(new.frame.right, START.frame.right)
                if "e" in name:
                    self.assertEqual(new.frame.x, START.frame.x)
                if "n" in name:
                    self.assertEqual(new.frame.bottom, START.frame.bottom)
                if "s" in name:
                    self.assertEqual(new.frame.y, START.frame.y)
                if name in ("n", "s"):
                    self.assertEqual(new.frame.width, START.frame.width)
                if name in ("e", "w"):
                    self.assertEqual(new.frame.height, START.frame.height)

    def test_reviewed_landmarks_and_reset_after_pan(self):
        left = resize_frame(START, "w", (140, 230))
        self.assertEqual((left.frame, left.offset),
                         (Rect(140, 130, 360, 220), (40, 55)))
        top = resize_frame(START, "n", (300, 95))
        self.assertEqual((top.frame, top.offset),
                         (Rect(180, 95, 320, 255), (80, 20)))
        corner = resize_frame(START, "nw", (120, 90))
        self.assertEqual((corner.frame, corner.offset),
                         (Rect(120, 90, 380, 260), (20, 15)))
        for geometry in (START, left, top, corner):
            self.assertEqual((geometry.content_rect.x + 240,
                              geometry.content_rect.y + 160), (340, 235))
        panned = reposition_content(START, (-35, 20))
        self.assertEqual((panned.frame, panned.offset),
                         (START.frame, (115, 35)))
        self.assertEqual(reset_frame_size(panned).frame, Rect(65, 95, 600, 400))

    def test_bounds_minima_and_reverse_drag_have_no_drift(self):
        for name in ("nw", "n", "ne", "e", "se", "s", "sw", "w"):
            with self.subTest(name=name):
                for point in ((-10000, -10000), (10000, 10000),
                              (180, 130), (210, 150)):
                    value = resize_frame(START, name, point)
                    value.validate()
                    self.assertGreaterEqual(value.frame.width, 32)
                    self.assertGreaterEqual(value.frame.height, 32)
        tiny = ViewGeometry(Rect(0, 0, 12, 8), (12, 8), (0, 0), 1.0)
        self.assertEqual(resize_frame(tiny, "se", (0, 0)).frame,
                         Rect(0, 0, 12, 8))
        for _ in range(20):
            resize_frame(START, "nw", (1000, 1000))
            self.assertEqual(resize_frame(START, "nw", (180, 130)), START)
        self.assertEqual(reposition_content(START, (10000, -10000)).offset,
                         (0, 180))

    def test_invalid_legacy_refuses_edit_but_reset_repairs(self):
        legacy = ViewGeometry(Rect(180, 130, 650, 220), (600, 400), (80, 55), 1.0)
        with self.assertRaisesRegex(GeometryError, "Reset Frame Size"):
            resize_frame(legacy, "e", (300, 200))
        with self.assertRaisesRegex(GeometryError, "Reset Frame Size"):
            reposition_content(legacy, (5, 5))
        self.assertEqual(reset_frame_size(legacy).frame, Rect(100, 75, 600, 400))

    def test_hit_overlap_is_nearest_with_stable_ties(self):
        tiny = Rect(0, 0, 8, 8)
        self.assertEqual(hit_handle(tiny, (0, 0), 12), "nw")
        self.assertEqual(hit_handle(tiny, (4, 0), 12), "n")


class Event:
    def __init__(self, point=(0, 0), *, key=None, drag=False):
        self.point, self.key, self.drag = point, key, drag
        self.skipped = False

    def GetPosition(self):
        return self.point

    def GetKeyCode(self):
        return self.key

    def Dragging(self):
        return self.drag

    def LeftIsDown(self):
        return self.drag

    def Skip(self):
        self.skipped = True


class Harness(ControlHarness):
    bring_image_object_to_front = CanvasPanel.bring_image_object_to_front
    _reset_zoom_wheel_remainder = CanvasPanel._reset_zoom_wheel_remainder
    reset_selected_frame = CanvasPanel.reset_selected_frame
    _wheel_is_vertical = staticmethod(CanvasPanel._wheel_is_vertical)
    _zoom_selected_image = CanvasPanel._zoom_selected_image

    def __init__(self, objects):
        super().__init__(objects, size=(500, 350))
        self._viewport_gesture = None
        self._viewport_pointer = None
        self._viewport_hover_handle = None
        self.scene_operation = None
        self.save_operation = None
        self.export_operation = None
        self.drop_operation = None
        self._document_identity = 0
        self.canvas_bg = "#ffffff"
        self.cursors = []

    def SetCursor(self, cursor):
        self.cursors.append(cursor)

    def start_preloading_for_object(self, obj):
        pass

    def _schedule_overlay_clear(self, delay_ms=None, image_object=None):
        pass


def obj(object_id="one", path="same.png"):
    image = ImageObject(path, object_id=object_id)
    pixels = Image.new("RGB", (200, 120), "red")
    pixels.putpixel((40, 30), (0, 255, 0))
    image._original_image = pixels
    image.x, image.y, image.width, image.height = 30, 25, 100, 80
    image.viewport_offset = (20, 10)
    return image


class TestViewportCanvas(unittest.TestCase):
    def test_handle_capture_escape_and_commit_own_runtime_id(self):
        first, peer = obj(), obj("two")
        self.addCleanup(first.dispose_source_pixels)
        self.addCleanup(peer.dispose_source_pixels)
        canvas = Harness([first, peer])
        canvas.selected_object = first
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        self.assertTrue(canvas.captured)
        CanvasPanel.on_mouse_move(canvas, Event((40, 35), drag=True))
        self.assertEqual((first.x, first.y), (40, 35))
        self.assertEqual((peer.x, peer.y), (30, 25))
        CanvasPanel.on_key_down(canvas, Event(key=wx.WXK_ESCAPE))
        self.assertEqual((first.x, first.y), (30, 25))
        self.assertFalse(canvas.captured)
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        CanvasPanel.on_mouse_move(canvas, Event((40, 35), drag=True))
        CanvasPanel.on_left_up(canvas, Event((40, 35)))
        self.assertEqual((first.x, first.y), (40, 35))

    def test_grip_is_hover_only_and_repositions_without_peer_cache_loss(self):
        first, peer = obj(), obj("two")
        self.addCleanup(first.dispose_source_pixels)
        self.addCleanup(peer.dispose_source_pixels)
        canvas = Harness([first, peer])
        canvas.selected_object = first
        peer._prepared_bitmap = object()
        self.assertIsNone(CanvasPanel._viewport_hit(canvas, 85, 40)[0])
        CanvasPanel.on_mouse_move(canvas, Event((75, 50)))
        grip = CanvasPanel._viewport_grip_rect(canvas,
                                                ViewGeometry(Rect(30, 25, 100, 80),
                                                             (200, 120), (20, 10), 1.0))
        point = (grip.x + grip.width // 2, grip.y + grip.height // 2)
        self.assertEqual(CanvasPanel._viewport_hit(canvas, *point)[0], "reposition")
        CanvasPanel.on_left_down(canvas, Event(point))
        CanvasPanel.on_mouse_move(canvas, Event((point[0] - 15, point[1] + 5),
                                                drag=True))
        self.assertEqual((first.x, first.y, first.width, first.height),
                         (30, 25, 100, 80))
        self.assertEqual(first.viewport_offset, (35, 5))
        self.assertIsNotNone(peer._prepared_bitmap)
        CanvasPanel.on_left_up(canvas, Event(point))
        CanvasPanel.on_mouse_leave(canvas, Event())
        self.assertIsNone(canvas._viewport_pointer)

    def test_save_and_export_during_drag_capture_committed_geometry(self):
        image = obj()
        self.addCleanup(image.dispose_source_pixels)
        canvas = Harness([image])
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        CanvasPanel.on_mouse_move(canvas, Event((50, 45), drag=True))
        snapshot = CanvasPanel._capture_canvas_save_snapshot(canvas, False)
        self.assertEqual((snapshot.objects[0].x, snapshot.objects[0].y), (30, 25))
        self.assertIsNone(canvas._viewport_gesture)
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        CanvasPanel.on_mouse_move(canvas, Event((50, 45), drag=True))
        export = CanvasPanel._capture_export_snapshot(canvas)
        try:
            self.assertEqual((export.objects[0].x, export.objects[0].y), (30, 25))
        finally:
            export.objects[0].pixel_lease.release()

    def test_reset_and_zoom_do_not_change_peer_or_rejected_crop(self):
        image, peer = obj(), obj("two")
        self.addCleanup(image.dispose_source_pixels)
        self.addCleanup(peer.dispose_source_pixels)
        canvas = Harness([image, peer])
        canvas.selected_object = image
        self.assertTrue(CanvasPanel.reset_selected_frame(canvas, image.object_id))
        self.assertEqual((image.x, image.y, image.width, image.height,
                          image.viewport_offset), (10, 15, 200, 120, (0, 0)))
        image.x, image.y, image.width, image.height = 30, 25, 100, 80
        image.viewport_offset = (20, 10)
        image.zoom_factor = 5.0
        image.width, image.height = 100, 80
        with mock.patch.object(CanvasPanel, "_note_user_interaction"), \
             mock.patch("src.canvas_panel._cancel_object_work"):
            CanvasPanel._zoom_selected_image(canvas, True)
        self.assertEqual((image.width, image.height, image.viewport_offset),
                         (100, 80, (20, 10)))
        image.zoom_factor = 1.0
        with mock.patch.object(CanvasPanel, "_note_user_interaction"), \
             mock.patch("src.canvas_panel._cancel_object_work"):
            CanvasPanel._zoom_selected_image(canvas, True)
        self.assertEqual((image.width, image.height, image.viewport_offset),
                         (250, 150, (0, 0)))
        self.assertEqual((peer.width, peer.height, peer.viewport_offset),
                         (100, 80, (20, 10)))

    def test_context_reset_targets_id_and_source_change_abandons_capture(self):
        first, second = obj("first"), obj("second")
        self.addCleanup(first.dispose_source_pixels)
        self.addCleanup(second.dispose_source_pixels)
        canvas = Harness([first, second])
        canvas.selected_object = first
        canvas._context_object_id = second.object_id
        MainFrame.on_reset_frame(SimpleNamespace(canvas_panel=canvas), None)
        self.assertEqual((first.width, first.height), (100, 80))
        self.assertEqual((second.width, second.height), (200, 120))
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        CanvasPanel.on_mouse_move(canvas, Event((45, 40), drag=True))
        first._original_image = Image.new("RGB", (220, 130), "blue")
        CanvasPanel.on_mouse_move(canvas, Event((50, 45), drag=True))
        self.assertIsNone(canvas._viewport_gesture)
        self.assertFalse(canvas.captured)
        self.assertEqual((first.x, first.y), (45, 40))

    def test_capture_loss_restores_and_duplicate_snapshot_is_committed(self):
        image = obj()
        self.addCleanup(image.dispose_source_pixels)
        canvas = Harness([image])
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        CanvasPanel.on_mouse_move(canvas, Event((45, 40), drag=True))
        canvas.captured = False
        CanvasPanel.on_mouse_capture_lost(canvas, Event())
        self.assertEqual((image.x, image.y), (30, 25))
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        CanvasPanel.on_mouse_move(canvas, Event((45, 40), drag=True))
        snapshot = CanvasPanel._capture_duplication_snapshot(canvas, image)
        try:
            self.assertEqual((snapshot.x, snapshot.y), (30, 25))
        finally:
            snapshot.release()

    def test_still_crop_landmark_and_target_cache(self):
        image, peer = obj(), obj("peer")
        self.addCleanup(image.dispose_source_pixels)
        self.addCleanup(peer.dispose_source_pixels)
        peer._prepared_bitmap = object()
        before = image._render_pil_crop()
        self.assertEqual(before.getpixel((20, 20)), (0, 255, 0))
        changed = resize_frame(ViewGeometry(Rect(30, 25, 100, 80),
                                            (200, 120), (20, 10), 1.0),
                               "nw", (40, 35))
        apply_to_object(image, changed)
        after = image._render_pil_crop()
        self.assertEqual(after.getpixel((10, 10)), (0, 255, 0))
        self.assertIsNotNone(peer._prepared_bitmap)

    def test_save_load_and_export_keep_the_same_landmark(self):
        image = obj()
        self.addCleanup(image.dispose_source_pixels)
        canvas = Harness([image])
        start = ViewGeometry(Rect(30, 25, 100, 80),
                             (200, 120), (20, 10), 1.0)
        apply_to_object(image, resize_frame(start, "nw", (40, 35)))
        save = CanvasPanel._capture_canvas_save_snapshot(canvas, False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.json"
            write_state(path, [save.objects[0].record()])
            loaded = read_state(path)[0]
            self.assertEqual((loaded["x"], loaded["y"], loaded["width"],
                              loaded["height"], loaded["viewport_offset"]),
                             (40, 35, 90, 70, [30, 20]))
        export = CanvasPanel._capture_export_snapshot(canvas)
        composite, result = render_export_snapshot(export, ExportCancellation())
        try:
            self.assertFalse(result.failures)
            self.assertEqual(composite.getpixel((50, 45))[:3], (0, 255, 0))
        finally:
            composite.close()
            export.release()

    def test_fractional_ctrl_wheel_keeps_crop_until_a_full_step(self):
        image = obj()
        self.addCleanup(image.dispose_source_pixels)
        canvas = Harness([image])
        class Wheel:
            def GetWheelAxis(self):
                return wx.MOUSE_WHEEL_VERTICAL
            def GetWheelRotation(self):
                return 60
            def GetWheelDelta(self):
                return 120
        CanvasPanel._handle_ctrl_wheel_zoom(canvas, Wheel())
        self.assertEqual((image.width, image.height, image.viewport_offset),
                         (100, 80, (20, 10)))
        with mock.patch.object(CanvasPanel, "_note_user_interaction"), \
             mock.patch("src.canvas_panel._cancel_object_work"):
            CanvasPanel._handle_ctrl_wheel_zoom(canvas, Wheel())
        self.assertEqual((image.width, image.height, image.viewport_offset),
                         (250, 150, (0, 0)))

    def test_gif_seek_adopts_frame_during_resize_and_cancel_keeps_new_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "two.gif"
            red = Image.new("RGB", (200, 120), "red")
            blue = Image.new("RGB", (200, 120), "blue")
            red.save(path, save_all=True, append_images=[blue], duration=100,
                     loop=0)
            image = ImageObject(str(path), object_id="gif")
            image._original_image = load_source_pixels(path)
            self.addCleanup(image.dispose_source_pixels)
            image.x, image.y, image.width, image.height = 30, 25, 100, 80
            image.viewport_offset = (20, 10)
            canvas = Harness([image])
            CanvasPanel.on_left_down(canvas, Event((30, 25)))
            CanvasPanel.on_mouse_move(canvas, Event((40, 35), drag=True))
            descriptor = image.animation
            descriptor.request_generation += 1
            descriptor.requested_index = 1
            identity = tuple(descriptor.source_identity)
            pixels = load_animation_frame(path, 1,
                                          expected_source_identity=identity)
            result = AnimationDecodeResult(
                str(path), 1,
                (image, str(path), identity, descriptor.request_generation, 1),
                pixels=pixels)
            self.assertTrue(CanvasPanel._on_animation_frame_decoded(canvas, result))
            self.assertEqual((image.x, image.y, image.width, image.height,
                              image.viewport_offset),
                             (40, 35, 90, 70, (30, 20)))
            self.assertIsNotNone(canvas._viewport_gesture)
            CanvasPanel.on_key_down(canvas, Event(key=wx.WXK_ESCAPE))
            self.assertEqual((image.x, image.y, image.viewport_offset),
                             (30, 25, (20, 10)))
            self.assertEqual(image.animation.displayed_index, 1)

    def test_actual_animation_button_wins_over_handle_and_padding_does_not(self):
        image = obj()
        self.addCleanup(image.dispose_source_pixels)
        image.animation = SimpleNamespace(frame_count=2, source_identity=("gif",),
                                          playback_buffer=[])
        canvas = Harness([image])
        state = canvas._animation_controls
        state.target_id = image.object_id
        state.visible_goal = True
        state.opacity = 1.0
        layout = CanvasPanel._animation_control_layout(canvas)
        button = layout.buttons[0]
        point = (button.rect.x + 1, button.rect.y + 1)
        with mock.patch.object(CanvasPanel, "_activate_animation_control") as activate:
            CanvasPanel.on_left_down(canvas, Event(point))
        activate.assert_called_once()
        self.assertIsNone(canvas._viewport_gesture)
        CanvasPanel.on_left_up(canvas, Event(point))
        state.visible_goal = False
        state.opacity = 0.0
        CanvasPanel.on_left_down(canvas, Event((30, 25)))
        self.assertEqual(canvas._viewport_gesture["kind"], "resize")
        CanvasPanel.on_left_up(canvas, Event((30, 25)))

    def test_grip_clamps_to_visible_frame_at_canvas_edges(self):
        canvas = Harness([])
        edge = ViewGeometry(Rect(-20, -10, 100, 80),
                            (200, 120), (20, 10), 1.0)
        rect = CanvasPanel._viewport_grip_rect(canvas, edge)
        self.assertGreaterEqual(rect.x, 0)
        self.assertGreaterEqual(rect.y, 0)
        self.assertLessEqual(rect.right, 80)
        self.assertLessEqual(rect.bottom, 70)
        tiny_visible = ViewGeometry(Rect(-95, 10, 100, 80),
                                    (200, 120), (20, 10), 1.0)
        rect = CanvasPanel._viewport_grip_rect(canvas, tiny_visible)
        self.assertEqual(rect.width, 5)
        self.assertEqual(rect.x, 0)
