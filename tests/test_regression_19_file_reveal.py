import ntpath
import posixpath
import unittest
from types import SimpleNamespace
from unittest import mock

from src.file_reveal import reveal_source
from src.image_object import ImageObject
from src.main_frame import MainFrame


class TestFileRevealAdapter(unittest.TestCase):
    def test_windows_uses_literal_select_argument(self):
        popen = mock.Mock()
        path = r"C:\Pictures\über name, 'quoted' $x.png"
        result = reveal_source(path, system="Windows", popen=popen,
                               exists=lambda _: True, isdir=lambda _: True)
        self.assertTrue(result.launched)
        self.assertTrue(result.selected)
        self.assertEqual(popen.call_args.args[0],
                         ["explorer.exe", "/select," + path])

    def test_windows_relative_path_uses_explicit_windows_cwd(self):
        popen = mock.Mock()
        source = r"folder\name with spaces\image[1].png"
        result = reveal_source(
            source, system="Windows", cwd=r"C:\work", popen=popen,
            exists=lambda path: path == r"C:\work\folder\name with spaces\image[1].png",
            isdir=lambda path: path == r"C:\work\folder\name with spaces")
        self.assertTrue(result.launched)
        self.assertEqual(
            popen.call_args.args[0],
            ["explorer.exe", "/select," + ntpath.abspath(
                ntpath.join(r"C:\work", source))])

    def test_macos_uses_open_reveal_and_relative_path_is_absolute(self):
        popen = mock.Mock()
        result = reveal_source("folder/über image.png", system="Darwin",
                               cwd="/work", popen=popen,
                               exists=lambda _: True, isdir=lambda _: True)
        self.assertTrue(result.selected)
        self.assertEqual(popen.call_args.args[0][0:2], ["open", "-R"])
        self.assertTrue(popen.call_args.args[0][-1].endswith(
            posixpath.join("folder", "über image.png")))

    def test_linux_opens_parent_and_reports_folder_only_fallback(self):
        popen = mock.Mock()
        result = reveal_source("/tmp/a folder/image.png", system="Linux",
                               popen=popen, which=lambda _: "/usr/bin/xdg-open",
                               exists=lambda _: True, isdir=lambda _: True)
        self.assertTrue(result.launched)
        self.assertFalse(result.selected)
        self.assertIn("selection", result.message)
        self.assertEqual(popen.call_args.args[0],
                         ["/usr/bin/xdg-open", "/tmp/a folder"])

    def test_linux_keeps_backslash_as_a_filename_character(self):
        popen = mock.Mock()
        path = "/tmp/a folder/name\\with ü [x] $?.png"
        result = reveal_source(
            path, system="Linux", popen=popen,
            which=lambda _: "/usr/bin/xdg-open",
            exists=lambda _: True, isdir=lambda _: True)
        self.assertTrue(result.launched)
        self.assertEqual(popen.call_args.args[0],
                         ["/usr/bin/xdg-open", "/tmp/a folder"])

    def test_missing_file_opens_existing_parent_but_missing_parent_does_not_launch(self):
        popen = mock.Mock()
        result = reveal_source("/tmp/folder/missing.png", system="Linux",
                               popen=popen, exists=lambda _: False,
                               isdir=lambda path: path == "/tmp/folder")
        self.assertTrue(result.launched)
        self.assertTrue(result.missing)
        self.assertEqual(popen.call_args.args[0], ["xdg-open", "/tmp/folder"])

        popen.reset_mock()
        result = reveal_source("/tmp/no-folder/missing.png", system="Linux",
                               popen=popen, exists=lambda _: False,
                               isdir=lambda _: False)
        self.assertFalse(result.launched)
        popen.assert_not_called()

    def test_unc_path_skips_existence_preflight_and_launch_errors_are_readable(self):
        popen = mock.Mock(side_effect=OSError("blocked"))
        exists = mock.Mock(side_effect=AssertionError("UNC preflight"))
        result = reveal_source(r"\\server\share\a folder\image.png",
                               system="Windows", popen=popen, exists=exists,
                               isdir=exists)
        self.assertFalse(result.launched)
        self.assertIn("launch", result.message)
        exists.assert_not_called()

    def test_windows_unc_aliases_remain_literal_and_skip_preflight(self):
        for path in (
                r"\\server\share\folder\image with spaces.png",
                r"\\?\UNC\server\share\folder\image with spaces.png"):
            with self.subTest(path=path):
                popen = mock.Mock()
                exists = mock.Mock(side_effect=AssertionError("UNC preflight"))
                result = reveal_source(
                    path, system="Windows", popen=popen, exists=exists,
                    isdir=exists)
                self.assertTrue(result.launched)
                self.assertTrue(result.selected)
                self.assertEqual(
                    popen.call_args.args[0], ["explorer.exe", "/select," + path])
                exists.assert_not_called()


class MenuCaptureFrame(MainFrame):
    def PopupMenu(self, menu, *args, **kwargs):
        self.menu_labels = [item.GetItemLabel()
                            for item in menu.GetMenuItems()]
        return True


class TestRevealMenuDispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import wx
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        class Settings:
            def get_setting(self, section, key, fallback=None):
                return fallback

            def save(self):
                pass

        self.frame = MenuCaptureFrame(None, "Regression 19", Settings(), debug_mode=True)
        self.frame.SetClientSize((240, 160))
        self.frame.Layout()

    def tearDown(self):
        self.frame.canvas_panel.overlay_clear_timer.Stop()
        self.frame.canvas_panel.file_navigator.shutdown()
        self.frame.Destroy()

    def test_clicked_duplicate_is_targeted_and_deleted_target_is_safe(self):
        first = ImageObject("same source.png")
        second = ImageObject("same source.png")
        self.frame.canvas_panel.add_image_object(first)
        self.frame.canvas_panel.add_image_object(second)
        self.frame.canvas_panel.selected_object = first
        self.frame.canvas_panel._context_object_id = second.object_id
        with mock.patch("src.main_frame.reveal_source") as reveal:
            reveal.return_value = SimpleNamespace(launched=True, missing=False,
                                                  message="opened")
            self.assertTrue(self.frame.on_reveal_source(None))
        self.assertEqual(reveal.call_args.args, (second.source_path,))
        self.assertIs(self.frame.canvas_panel.selected_object, first)

        self.frame.canvas_panel.remove_image_object(second)
        with mock.patch("src.main_frame.reveal_source") as reveal:
            self.assertFalse(self.frame.on_reveal_source(None))
            reveal.assert_not_called()

    def test_menu_has_stable_reveal_item_for_clicked_source_only(self):
        obj = ImageObject("committed.png")
        self.frame.canvas_panel.add_image_object(obj)
        self.frame.canvas_panel._context_object_id = obj.object_id
        self.frame.on_right_click(None)
        self.assertIn(self.frame._reveal_label(), self.frame.menu_labels)

    def test_no_source_has_no_reveal_item_and_pending_path_is_untouched(self):
        obj = ImageObject("")
        obj._navigation_base_path = "pending.png"
        self.frame.canvas_panel.add_image_object(obj)
        self.frame.canvas_panel._context_object_id = obj.object_id
        self.frame.on_right_click(None)
        self.assertNotIn(self.frame._reveal_label(), self.frame.menu_labels)
        self.assertEqual(obj.source_path, "")
        self.assertEqual(obj._navigation_base_path, "pending.png")


if __name__ == "__main__":
    unittest.main()
