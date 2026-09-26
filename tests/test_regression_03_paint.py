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
        # Leave a real-pixel landmark between the selected object's 8-DIP
        # viewport handles.  The former 8x4 fixture was completely covered by
        # those controls, so its white handle pixels were mistaken for a
        # missing first presentation.
        Image.new("RGB", (32, 24), "red").save(cls.drop_fixture)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.drop_fixture)

    def check_visible_content(self, action, fullscreen=False, transition=None):
        paint_entries = []
        paint_completions = []
        original_paint = CanvasPanel.on_paint

        def observed_paint(panel, event):
            paint_entries.append(tuple(obj.object_id for obj in panel.image_objects))
            original_paint(panel, event)
            operation = panel.drop_operation
            paint_completions.append({
                "object_ids": tuple(obj.object_id for obj in panel.image_objects),
                "drop_terminal": bool(operation is not None and operation.terminal),
                "drop_card": (tuple(panel._drop_card_rect)
                              if panel._drop_card_rect is not None else None),
            })

        # CanvasPanel binds this method while the frame is constructed.  The
        # bound wrapper remains test-owned after the class patch is restored.
        with mock.patch.object(CanvasPanel, "on_paint", observed_paint):
            frame = MainFrame(None, "Regression 03 paint verification",
                              SettingsStub(), debug_mode=not fullscreen)
        frame.SetClientSize((160, 100))
        canvas = frame.canvas_panel
        results = []
        errors = []
        paint_count = []
        click_count = []
        presentation_checks = []
        scheduled = []
        committed = False
        dismissal_paint = None
        workers_stopped = False
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

        def schedule(delay, callback, *args):
            timer = wx.CallLater(delay, callback, *args)
            scheduled.append(timer)
            return timer

        def read_pixels(points):
            dc = wx.ClientDC(canvas)
            return [tuple(dc.GetPixel(*point).Get()[:3]) for point in points]

        def terminal_card_is_present(rect):
            x, y, width, height = rect
            dc = wx.ClientDC(canvas)
            return any(
                (lambda red, green, blue: (
                    green >= red + 20 and green >= blue + 20))(
                        *dc.GetPixel(px, py).Get()[:3])
                for py in range(y, y + height)
                for px in range(x, x + width)
            )

        def inspect_pixels():
            try:
                # Read fixture-derived landmarks from the actual client
                # surface, not a cached PIL crop or a mocked drawing call.
                points = ([(obj.x, obj.y) for obj in canvas.image_objects]
                          if action == "load" else
                          [(obj.x + obj.width // 2, obj.y + obj.height // 2)
                           for obj in canvas.image_objects])
                results.extend(read_pixels(points))
            except Exception as exc:
                errors.append(exc)
            finally:
                loop.Exit()

        def inspect_when_present():
            nonlocal dismissal_paint
            try:
                object_ids = tuple(obj.object_id for obj in canvas.image_objects)
                completed = paint_completions[-1] if paint_completions else None
                if (not committed or not object_ids or completed is None
                        or completed["object_ids"] != object_ids):
                    schedule(10, inspect_when_present)
                    return

                if action == "load":
                    inspect_pixels()
                    return

                if dismissal_paint is None:
                    if not completed["drop_terminal"] or completed["drop_card"] is None:
                        schedule(10, inspect_when_present)
                        return
                    # This is the application's first completed terminal paint,
                    # before any test-owned dismissal or replacement Refresh.
                    presentation_checks.append(
                        terminal_card_is_present(completed["drop_card"]))
                    dismissal_paint = len(paint_completions)
                    CanvasPanel.cancel_drop_operation(
                        canvas, clear=True, reason="test-owned card dismissal")
                    schedule(10, inspect_when_present)
                    return

                if (len(paint_completions) <= dismissal_paint
                        or canvas.drop_operation is not None
                        or completed["drop_card"] is not None):
                    schedule(10, inspect_when_present)
                    return
                inspect_pixels()
            except Exception as exc:
                errors.append(exc)
                loop.Exit()

        def commit():
            nonlocal committed
            try:
                committed = True
                if action == "load":
                    canvas.load_canvas_state(LEGACY_STATE)
                else:
                    paths = [self.drop_fixture] * (1 if action == "single" else 2)
                    FileDropTarget(canvas).OnDropFiles(10, 10, paths)
                schedule(10, inspect_when_present)
            except Exception as exc:
                errors.append(exc)
                loop.Exit()

        def prepare():
            if transition == "resize":
                frame.SetClientSize((180, 120))
            elif transition == "restore":
                frame.Iconize(True)
                schedule(100, frame.Iconize, False)
            schedule(200, commit)

        try:
            frame.Show()
            if fullscreen:
                frame.ShowFullScreen(True)
            schedule(100, prepare)
            schedule(3000, loop.Exit)
            with mock.patch.object(ImageObject, "draw", counted_draw):
                loop.Run()
        finally:
            for timer in scheduled:
                timer.Stop()
            canvas.overlay_clear_timer.Stop()
            canvas.shutdown_preloading()
            workers_stopped = canvas.file_navigator.wait_for_workers(2.0)
            committed_ids = {obj.object_id for obj in canvas.image_objects}
            frame.Destroy()
            del activator
            wx.Yield()

        self.assertFalse(errors, errors)
        self.assertTrue(paint_entries, "EVT_PAINT was never entered")
        self.assertTrue(paint_completions, "EVT_PAINT never completed")
        if action != "load":
            self.assertEqual(presentation_checks, [True],
                             "The first terminal paint was not presented")
        self.assertEqual(
            results, [(255, 0, 0)] * len(committed_ids),
            "Presented pixels do not match the fixture landmarks")
        self.assertEqual(click_count, [])
        self.assertTrue(paint_count, "No successful object draw was observed")
        self.assertEqual(set(paint_count), committed_ids)
        self.assertTrue(workers_stopped, "Canvas workers did not stop during cleanup")

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

    def test_real_pixel_check_rejects_wrong_presented_pixels(self):
        Image.new("RGB", (32, 24), "blue").save(self.drop_fixture)
        try:
            with self.assertRaisesRegex(
                    AssertionError,
                    "Presented pixels do not match the fixture landmarks"):
                self.check_visible_content("single")
        finally:
            Image.new("RGB", (32, 24), "red").save(self.drop_fixture)
