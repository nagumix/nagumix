import gc
import os
import tempfile
import unittest
import weakref
from unittest import mock

import wx
from PIL import Image

from src.canvas_panel import CanvasPanel, FileDropTarget
from src.image_object import ImageObject
from src.main_frame import MainFrame
from tests.test_regression_02_resets import FIXTURE_IMAGE, LEGACY_STATE, SettingsStub


class TestPaintLifetime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_image_does_not_retain_callers_drawing_context(self):
        obj = ImageObject(FIXTURE_IMAGE, canvas_width=80, canvas_height=40)
        obj.reset_size()
        dc = mock.Mock()
        reference = weakref.ref(dc)
        obj.draw(dc)
        dc.DrawBitmap.assert_called_once()
        del dc
        gc.collect()
        self.assertIsNone(reference(), "Image retains the paint context after draw")


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1 to run visible paint tests")
class TestVisiblePaint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)
        stream = tempfile.NamedTemporaryFile(
            suffix=".png", delete=False, dir=os.path.dirname(__file__))
        cls.drop_fixture = stream.name
        stream.close()
        Image.new("RGB", (8, 4), "red").save(cls.drop_fixture)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.drop_fixture)

    def check_visible_content(self, action, fullscreen=False, transition=None):
        frame = MainFrame(None, "Regression 03 paint verification", SettingsStub(),
                          debug_mode=not fullscreen)
        frame.SetClientSize((160, 100))
        canvas = frame.canvas_panel
        results = []
        errors = []
        paint_count = []
        click_count = []
        original_draw = ImageObject.draw

        def counted_draw(obj, dc, *args, **kwargs):
            original_draw(obj, dc, *args, **kwargs)
            paint_count.append(obj.object_id)

        def clicked(event):
            click_count.append(event)
            event.Skip()

        canvas.Bind(wx.EVT_LEFT_DOWN, clicked)
        loop = wx.GUIEventLoop()
        activator = wx.EventLoopActivator(loop)

        def inspect_pixels():
            try:
                # Read the actual client surface, not a cached PIL crop.
                dc = wx.ClientDC(canvas)
                results.extend(tuple(dc.GetPixel(*point).Get()[:3])
                               for point in sample_points)
            except Exception as exc:
                errors.append(exc)
            finally:
                loop.Exit()

        sample_points = [(11, 11)] if action == "single" else [(11, 11), (31, 31)]
        if action == "load":
            sample_points = [(4, 5)]

        def commit():
            try:
                if action == "load":
                    canvas.load_canvas_state(LEGACY_STATE)
                    wx.CallLater(300, inspect_pixels)
                else:
                    paths = [self.drop_fixture] * (1 if action == "single" else 2)
                    FileDropTarget(canvas).OnDropFiles(10, 10, paths)

                    def inspect_after_drop():
                        operation = canvas.drop_operation
                        if operation is not None and operation.terminal:
                            # The Regression 14 terminal summary intentionally sits
                            # over the canvas. Dismiss it before sampling the
                            # committed image pixels this Regression 03 test owns.
                            canvas.drop_operation = None
                            expected_paints = len(paint_count) + len(canvas.image_objects)
                            canvas.Refresh()

                            def inspect_after_paint():
                                if len(paint_count) >= expected_paints:
                                    inspect_pixels()
                                else:
                                    wx.CallLater(10, inspect_after_paint)

                            wx.CallLater(10, inspect_after_paint)
                        else:
                            wx.CallLater(10, inspect_after_drop)

                    wx.CallLater(10, inspect_after_drop)
            except Exception as exc:
                errors.append(exc)
                loop.Exit()

        def prepare():
            if transition == "resize":
                frame.SetClientSize((180, 120))
            elif transition == "restore":
                frame.Iconize(True)
                wx.CallLater(100, frame.Iconize, False)
            wx.CallLater(200, commit)

        try:
            frame.Show()
            if fullscreen:
                frame.ShowFullScreen(True)
            wx.CallLater(100, prepare)
            timeout = wx.CallLater(3000, loop.Exit)
            with mock.patch.object(ImageObject, "draw", counted_draw):
                loop.Run()
            timeout.Stop()
            self.assertFalse(errors, errors)
            self.assertEqual(results, [(255, 0, 0)] * len(sample_points))
            self.assertEqual(click_count, [])
            self.assertTrue(paint_count, "No successful object draw was observed")
            self.assertEqual(set(paint_count),
                             {obj.object_id for obj in canvas.image_objects})
        finally:
            canvas.overlay_clear_timer.Stop()
            frame.Destroy()
            del activator
            wx.Yield()

    def test_drops_and_legacy_load_present_without_clicks(self):
        for fullscreen in (False, True):
            for action in ("single", "multiple", "load"):
                with self.subTest(fullscreen=fullscreen, action=action):
                    self.check_visible_content(action, fullscreen)

    def test_content_after_resize_and_restore(self):
        for transition in ("resize", "restore"):
            for action in ("multiple", "load"):
                with self.subTest(transition=transition, action=action):
                    self.check_visible_content(action, transition=transition)
