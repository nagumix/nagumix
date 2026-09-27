"""Focused paint, binding and native-dialog boundary coverage."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import wx
from PIL import Image

from src import canvas_bindings as bindings
from src.canvas_panel import CanvasPanel, FileDropTarget
from src.empty_canvas import draw_empty_canvas
from src.file_navigator import FileNavigator
from src.main_frame import MainFrame
from tests.test_regression_14_async_drops import SettingsStub, BlockingLoader
from tests.test_regression_15_async_scene_loading import record


class OnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = self.root / "image.png"
        Image.new("RGB", (32, 24), "red").save(self.image)
        self.frame = MainFrame(None, "Canvas guidance", SettingsStub(), debug_mode=True)
        self.frame.SetClientSize((800, 560))
        self.frame.Show()
        self.canvas = self.frame.canvas_panel
        self.canvas.SetFocus()
        wx.Yield()

    def tearDown(self):
        navigator = self.canvas.file_navigator
        self.canvas.shutdown_preloading()
        self.assertTrue(navigator.wait_for_workers(5))
        self.frame.Destroy()
        wx.Yield()
        self.temp.cleanup()

    def wait_terminal(self, name):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            wx.Yield()
            operation = getattr(self.canvas, name)
            if operation is not None and operation.terminal:
                return operation
            time.sleep(.005)
        self.fail(f"{name} did not finish")

    def assert_guide(self, visible):
        calls = []
        def observe(dc, *args, **kwargs):
            calls.append(True)
            draw_empty_canvas(dc, *args, **kwargs)
        with mock.patch("src.canvas_panel.draw_empty_canvas", new=observe):
            self.canvas.Refresh()
            self.canvas.Update()
            wx.Yield()
        self.assertEqual(bool(calls), visible)

    @staticmethod
    def key(code, *, control=False, shift=False):
        event = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
        event.SetKeyCode(code)
        event.SetControlDown(control)
        event.SetShiftDown(shift)
        return event

    def test_drop_failure_cancel_success_remove_and_paint_only_state(self):
        self.assert_guide(True)
        self.canvas.accept_drop(0, 0, [str(self.root / "missing.png")])
        self.assertEqual(self.wait_terminal("drop_operation").failed, 1)
        self.assert_guide(True)
        loader = BlockingLoader()
        loader.block(str(self.image))
        original = self.canvas.file_navigator
        original.shutdown()
        self.assertTrue(original.wait_for_workers(5))
        self.canvas.file_navigator = FileNavigator(SettingsStub(), image_loader=loader)
        try:
            self.canvas.accept_drop(0, 0, [str(self.image)])
            self.assertTrue(loader.wait_for_calls(1))
            self.assert_guide(True)
            self.canvas.cancel_drop_operation()
            self.assert_guide(True)
        finally:
            loader.release_all()
        target = FileDropTarget(self.canvas)
        target.OnDropFiles(5, 6, [str(self.image)])
        self.assertEqual(self.wait_terminal("drop_operation").succeeded, 1)
        self.assert_guide(False)
        obj = self.canvas.image_objects[0]
        obj.x = -10000
        self.assert_guide(False)
        self.canvas.remove_image_object(obj)
        self.assert_guide(True)
        state = self.root / "empty.json"
        self.canvas.save_canvas_state(str(state))
        self.assertEqual(json.loads(state.read_text()), [])
        exported = self.root / "empty.png"
        self.canvas.export_to_file(str(exported))
        with Image.open(exported) as image:
            self.assertEqual(image.convert("RGB").getextrema(), ((48, 48),) * 3)

    def test_picker_cancel_multifile_and_scene_commit(self):
        dialog = mock.Mock()
        dialog.ShowModal.return_value = wx.ID_CANCEL
        with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog) as factory:
            self.assertFalse(self.frame.on_add_images(None))
        dialog.Destroy.assert_called_once()
        self.assertIsNone(self.canvas.drop_operation)
        self.assertIsNone(self.canvas.get_selected_object())
        self.assert_guide(True)
        self.assertIs(wx.Window.FindFocus(), self.canvas)
        arguments = factory.call_args.kwargs
        self.assertEqual(arguments["style"], wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        for extension in FileNavigator.SUPPORTED_EXTENSIONS:
            self.assertIn("*" + extension, arguments["wildcard"])
        dialog.reset_mock()
        dialog.ShowModal.return_value = wx.ID_OK
        dialog.GetPaths.return_value = [str(self.image), str(self.image)]
        with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog):
            self.assertTrue(self.frame.on_add_images(None))
        operation = self.wait_terminal("drop_operation")
        self.assertEqual(operation.anchor, (400, 280))
        self.assertEqual(operation.succeeded, 2)
        self.assert_guide(False)
        selection = self.canvas.get_selected_object()
        dialog.ShowModal.return_value = wx.ID_CANCEL
        with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog):
            self.frame.on_add_images(None)
        self.assertIs(self.canvas.get_selected_object(), selection)
        self.assertIs(self.canvas.drop_operation, operation)
        for obj in tuple(self.canvas.image_objects):
            self.canvas.remove_image_object(obj)
        scene = self.root / "scene.json"
        scene.write_text(json.dumps([record(self.image)]))
        self.canvas.begin_load_canvas_state(str(scene))
        self.assertTrue(self.wait_terminal("scene_operation").committed)
        self.assert_guide(False)

    def test_shortcut_menu_once_and_focus_modal_repeat_guards(self):
        event = lambda: self.key(ord("O"), control=True)
        with mock.patch.object(self.frame, "on_add_images") as add:
            self.canvas.GetEventHandler().ProcessEvent(event())
            add.assert_called_once_with(None)
        text = wx.TextCtrl(self.frame)
        text.SetFocus()
        wx.Yield()
        with mock.patch.object(self.frame, "on_add_images") as add:
            self.canvas.on_key_down(event())
            add.assert_not_called()
        text.Destroy()
        self.canvas.SetFocus()
        repeated = mock.Mock(wraps=event())
        repeated.IsAutoRepeat.return_value = True
        with mock.patch.object(self.frame, "on_add_images") as add:
            self.canvas.on_key_down(repeated)
            add.assert_not_called()
        with mock.patch("src.main_frame.wx.FileDialog") as factory:
            self.frame._add_images_dialog_open = True
            self.assertFalse(self.frame.on_add_images(None))
            self.frame._add_images_dialog_open = False
            self.frame.Enable(False)
            self.assertFalse(self.frame.on_add_images(None))
            self.frame.Enable(True)
            modal = mock.Mock(spec=wx.Dialog)
            modal.IsModal.return_value = True
            with mock.patch("src.main_frame.wx.GetTopLevelWindows", return_value=[modal]):
                self.assertFalse(self.frame.on_add_images(None))
            factory.assert_not_called()
        labels = []
        def capture(menu):
            labels.append(menu.FindItemById(int(MainFrame.ADD_IMAGES_ID)).GetItemLabel())
        with mock.patch.object(self.frame, "PopupMenu", side_effect=capture):
            self.frame.on_right_click(None)
            self.frame.on_right_click(None)
        self.assertEqual(labels, [bindings.add_images_menu_label()] * 2)
        dialog = mock.Mock()
        dialog.ShowModal.return_value = wx.ID_CANCEL
        with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog) as factory:
            self.frame.ProcessEvent(wx.CommandEvent(wx.wxEVT_MENU, int(MainFrame.ADD_IMAGES_ID)))
            factory.assert_called_once()

    def test_changed_binding_changes_tokens_menu_and_routing(self):
        alternate = bindings.Binding("F2", ("Ctrl", "Shift"))
        with mock.patch.object(bindings, "ADD_IMAGES", alternate):
            self.assertEqual(bindings.hint_rows()[0][:5],
                             [("Ctrl", True), (" + ", False), ("Shift", True),
                              (" + ", False), ("F2", True)])
            self.assertEqual(bindings.add_images_menu_label(), "Add Images...\tCtrl+Shift+F2")
            with mock.patch.object(self.frame, "on_add_images") as add:
                self.canvas.on_key_down(self.key(ord("O"), control=True))
                add.assert_not_called()
                self.canvas.on_key_down(self.key(wx.WXK_F2, control=True, shift=True))
                add.assert_called_once_with(None)
        alternate_quit = bindings.Binding("F3")
        with mock.patch.object(bindings, "QUIT", (bindings.QUIT[0], alternate_quit)):
            self.assertIn(("F3", True), bindings.hint_rows()[1])
            with mock.patch.object(self.frame, "on_quit") as quit_handler:
                self.canvas.on_key_down(self.key(ord("X")))
                quit_handler.assert_not_called()
                self.canvas.on_key_down(self.key(wx.WXK_F3))
                quit_handler.assert_called_once_with(None)

    def test_platform_labels_and_behavior_and_native_drawing(self):
        for platform, modifier, method in (("win32", "Ctrl", "ControlDown"),
                                           ("linux", "Ctrl", "ControlDown"),
                                           ("darwin", "Command", "MetaDown")):
            with self.subTest(platform=platform):
                event = mock.Mock()
                event.GetKeyCode.return_value = ord("O")
                for name in ("ControlDown", "RawControlDown", "MetaDown", "AltDown", "ShiftDown"):
                    getattr(event, name).return_value = name == method
                self.assertTrue(bindings.ADD_IMAGES.matches(event, platform))
                self.assertEqual(bindings.ADD_IMAGES.tokens(platform), (modifier, "O"))
                self.assertEqual(bindings.ADD_IMAGES.accelerator(platform),
                                 "Cmd+O" if platform == "darwin" else "Ctrl+O")
                event.GetKeyCode.return_value = ord("X")
                self.assertFalse(bindings.QUIT[1].matches(event, platform))
                getattr(event, method).return_value = False
                self.assertTrue(bindings.QUIT[1].matches(event, platform))
                event.GetKeyCode.return_value = ord("O")
                self.assertFalse(bindings.ADD_IMAGES.matches(event, platform))
        bitmap = wx.Bitmap(800, 560)
        dc = wx.MemoryDC(bitmap)
        try:
            for size in ((800, 560), (360, 260)):
                for background in ("#303030", "#FFFFFF"):
                    for feedback in (False, True):
                        with mock.patch.object(bindings, "ADD_IMAGES",
                                               bindings.Binding("PageDown", ("Ctrl", "Shift", "Alt"))):
                            before = dc.GetFont()
                            draw_empty_canvas(dc, size, wx.Colour(background), feedback=feedback)
                            self.assertEqual(dc.GetFont(), before)
        finally:
            dc.SelectObject(wx.NullBitmap)
