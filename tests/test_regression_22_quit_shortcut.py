import unittest
from unittest import mock

import wx

from src.canvas_panel import CanvasPanel
from src.image_object import ImageObject
from src.main_frame import MainFrame
from tests.test_regression_02_resets import FIXTURE_IMAGE, SettingsStub


class QuitShortcutRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.frame = MainFrame(None, "Regression 22 keyboard routing", SettingsStub(),
                               debug_mode=True)
        self.frame.SetClientSize((160, 100))
        self.frame.Layout()
        self.frame.Show()
        wx.Yield()

    def tearDown(self):
        self.frame.canvas_panel.overlay_clear_timer.Stop()
        self.frame.canvas_panel.file_navigator.clear_cache()
        self.frame.Destroy()
        wx.Yield()

    @staticmethod
    def key_event(keycode, *, control=False):
        event = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
        event.SetKeyCode(keycode)
        event.SetControlDown(control)
        return event

    def add_object(self):
        obj = ImageObject(FIXTURE_IMAGE, canvas_width=160, canvas_height=100)
        self.frame.canvas_panel.add_image_object(obj)
        return obj

    def test_canvas_focus_x_dispatches_once_without_selection(self):
        canvas = self.frame.canvas_panel
        canvas.SetFocus()
        wx.Yield()
        with mock.patch.object(self.frame, "on_quit") as quit_handler:
            handled = canvas.GetEventHandler().ProcessEvent(
                self.key_event(ord("x")))
        self.assertTrue(handled)
        quit_handler.assert_called_once_with(None)

    def test_canvas_focus_x_is_selection_independent_and_not_duplicated(self):
        canvas = self.frame.canvas_panel
        cases = (("empty", False, False),
                 ("populated-cleared", True, False),
                 ("selected", True, True))
        for name, populated, selected in cases:
            with self.subTest(selection=name):
                obj = self.add_object() if populated else None
                if not selected:
                    obj = None
                canvas.set_selected_object(obj)
                canvas.SetFocus()
                wx.Yield()
                with mock.patch.object(self.frame, "on_quit") as quit_handler:
                    handled = canvas.GetEventHandler().ProcessEvent(
                        self.key_event(ord("X")))
                self.assertTrue(handled)
                quit_handler.assert_called_once_with(None)

    def test_ctrl_x_does_not_quit_and_zoom_still_requires_selection(self):
        canvas = self.frame.canvas_panel
        canvas.SetFocus()
        with mock.patch.object(self.frame, "on_quit") as quit_handler:
            handled = canvas.GetEventHandler().ProcessEvent(
                self.key_event(ord("x"), control=True))
        self.assertFalse(handled)
        quit_handler.assert_not_called()

        with mock.patch.object(CanvasPanel, "_zoom_selected_image",
                               autospec=True) as zoom:
            canvas.on_key_down(self.key_event(wx.WXK_ADD))
        zoom.assert_not_called()

        obj = self.add_object()
        canvas.set_selected_object(obj)
        with mock.patch.object(CanvasPanel, "_zoom_selected_image",
                               autospec=True) as zoom:
            canvas.on_key_down(self.key_event(wx.WXK_ADD))
        zoom.assert_called_once_with(canvas, zoom_in=True)

    def test_frame_x_protects_text_entry_dialog_and_ctrl_x(self):
        text = wx.TextCtrl(self.frame, value="filename")
        text.SetFocus()
        wx.Yield()
        with mock.patch.object(self.frame, "on_quit") as quit_handler:
            self.frame.on_key_down(self.key_event(ord("x")))
            self.frame.on_key_down(self.key_event(ord("x"), control=True))
        quit_handler.assert_not_called()

        dialog = wx.Dialog(self.frame, title="Settings")
        dialog_text = wx.TextCtrl(dialog, value="setting")
        dialog.SetSizer(wx.BoxSizer(wx.VERTICAL))
        dialog.GetSizer().Add(dialog_text)
        dialog.Show()
        dialog_text.SetFocus()
        wx.Yield()
        try:
            with mock.patch.object(self.frame, "on_quit") as quit_handler:
                self.frame.on_key_down(self.key_event(ord("x")))
            quit_handler.assert_not_called()
        finally:
            dialog.Destroy()
            wx.Yield()


if __name__ == "__main__":
    unittest.main()
