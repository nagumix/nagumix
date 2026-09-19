import os
import threading
import unittest
from unittest import mock

import wx

from src.file_navigator import DirectoryDiscovery, DirectorySnapshot, FileNavigator
from src.settings_dialog import SORT_METHOD_LABELS, SettingsDialog
from src.settings_manager import SORT_METHOD_DEFAULT, SORT_METHODS, SettingsManager


class SettingsStub:
    def __init__(self, sort_method=SORT_METHOD_DEFAULT, preload_count="0"):
        self.sort_method = sort_method
        self.preload_count = preload_count

    def get_setting(self, section, key, fallback=None):
        values = {
            ("Navigation", "sort_method"): self.sort_method,
            ("Navigation", "preload_count"): self.preload_count,
            ("Navigation", "enable_wheel_navigation"): "true",
        }
        return values.get((section, key), fallback)


class FakeEntry:
    def __init__(self, directory, name, modified=1, size=1, stat_error=None):
        self.name = name
        self.path = os.path.join(directory, name)
        self.modified = modified
        self.size = size
        self.stat_error = stat_error
        self.stat_calls = 0

    def is_file(self):
        return True

    def stat(self):
        self.stat_calls += 1
        if self.stat_error is not None:
            raise self.stat_error
        return mock.Mock(st_mtime=self.modified, st_size=self.size)


class FakeScandir:
    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return iter(self.entries)

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class NaturalSortRegressionTests(unittest.TestCase):
    def test_settings_default_legacy_round_trip_and_dialog_labels(self):
        with mock.patch("src.settings_manager.os.path.exists", return_value=False):
            settings = SettingsManager()

        self.assertEqual(settings.get_sort_method(), SORT_METHOD_DEFAULT)
        self.assertEqual(settings.config.get("Navigation", "sort_method"),
                         SORT_METHOD_DEFAULT)
        self.assertEqual(len(SORT_METHOD_LABELS), len(SORT_METHODS))

        for method in SORT_METHODS:
            settings.set_sort_method(method)
            self.assertEqual(settings.get_sort_method(), method)
        settings.set_setting("Navigation", "sort_method", "name_asc")
        self.assertEqual(settings.get_sort_method(), "name_asc")
        settings.set_setting("Navigation", "sort_method", "not-a-sort")
        self.assertEqual(settings.get_sort_method(), SORT_METHOD_DEFAULT)
        with self.assertRaises(ValueError):
            settings.set_sort_method("capture_date_asc")


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1 to run dialog checks")
class SortingSettingsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_cancel_does_not_persist_a_new_sort_choice(self):
        with mock.patch("src.settings_manager.os.path.exists", return_value=False):
            settings = SettingsManager()
        settings.set_sort_method("date_desc")
        parent = wx.Frame(None)
        dialog = SettingsDialog(parent, settings)
        try:
            self.assertEqual(dialog.sort_choice.GetCount(), len(SORT_METHODS))
            self.assertEqual(dialog.sort_choice.GetSelection(),
                             SORT_METHODS.index("date_desc"))
            dialog.sort_choice.SetSelection(SORT_METHODS.index("size_asc"))
        finally:
            dialog.Destroy()
            parent.Destroy()
        self.assertEqual(settings.get_sort_method(), "date_desc")


class DirectorySortingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = os.path.join("folder", "photos")
        self.current = os.path.join(self.directory, "image2.png")
        self.entries = [
            FakeEntry(self.directory, "image10.png", modified=30, size=40),
            FakeEntry(self.directory, "image2.png", modified=20, size=20),
            FakeEntry(self.directory, "image02.png", modified=20, size=20),
            FakeEntry(self.directory, "ALPHA.png", modified=10, size=30),
            FakeEntry(self.directory, "été.png", modified=40, size=10),
            FakeEntry(self.directory, "unknown.png", modified=OSError("gone"),
                      size=OSError("gone"), stat_error=OSError("gone")),
        ]
        self.navigator = FileNavigator(SettingsStub())

    def tearDown(self):
        self.navigator.shutdown()
        self.assertTrue(self.navigator.wait_for_workers(2.0))

    def discover_names(self, method):
        self.navigator.settings_manager.sort_method = method
        self.navigator.clear_cache()
        with (mock.patch("src.file_navigator.os.path.exists", return_value=True),
              mock.patch("src.file_navigator.os.scandir",
                         return_value=FakeScandir(self.entries))):
            result = self.navigator.discover_directory(self.current)
        self.assertTrue(result.succeeded)
        return [os.path.basename(path) for path in result.snapshot.files]

    def test_each_mode_and_direction_has_explicit_order(self):
        self.assertEqual(self.discover_names("natural_asc"), [
            "ALPHA.png", "image2.png", "image02.png", "image10.png",
            "unknown.png", "été.png",
        ])
        self.assertEqual(self.discover_names("natural_desc"), [
            "été.png", "unknown.png", "image10.png", "image02.png",
            "image2.png", "ALPHA.png",
        ])
        self.assertEqual(self.discover_names("name_asc"), [
            "ALPHA.png", "image02.png", "image10.png", "image2.png",
            "unknown.png", "été.png",
        ])
        self.assertEqual(self.discover_names("name_desc"), [
            "été.png", "unknown.png", "image2.png", "image10.png",
            "image02.png", "ALPHA.png",
        ])
        self.assertEqual(self.discover_names("date_asc"), [
            "ALPHA.png", "image2.png", "image02.png", "image10.png",
            "été.png", "unknown.png",
        ])
        self.assertEqual(self.discover_names("date_desc"), [
            "été.png", "image10.png", "image2.png", "image02.png",
            "ALPHA.png", "unknown.png",
        ])
        self.assertEqual(self.discover_names("size_asc"), [
            "été.png", "image2.png", "image02.png", "ALPHA.png",
            "image10.png", "unknown.png",
        ])
        self.assertEqual(self.discover_names("size_desc"), [
            "image10.png", "ALPHA.png", "image2.png", "image02.png",
            "été.png", "unknown.png",
        ])

    def test_stat_is_reused_once_and_metadata_failures_do_not_drop_files(self):
        self.navigator.settings_manager.sort_method = "size_asc"
        with (mock.patch("src.file_navigator.os.path.exists", return_value=True),
              mock.patch("src.file_navigator.os.scandir",
                         return_value=FakeScandir(self.entries))):
            result = self.navigator.discover_directory(self.current)
        self.assertEqual(len(result.snapshot.files), len(self.entries))
        self.assertTrue(result.snapshot.files[-1].endswith("unknown.png"))
        self.assertTrue(all(entry.stat_calls == 1 for entry in self.entries))

    def test_unknown_setting_uses_natural_default(self):
        self.assertEqual(self.discover_names("not-a-sort")[:4], [
            "ALPHA.png", "image2.png", "image02.png", "image10.png",
        ])

    def test_sort_change_clears_snapshot_without_reordering_canvas_state(self):
        self.navigator.settings_manager.sort_method = "natural_asc"
        first = self.discover_names("natural_asc")
        generation = self.navigator.preload_state()["generation"]
        self.navigator.settings_manager.sort_method = "size_desc"
        self.assertTrue(self.navigator.clear_cache())
        second = self.discover_names("size_desc")
        self.assertNotEqual(first, second)
        self.assertGreater(self.navigator.preload_state()["generation"], generation)


class SortingInvalidationRegressionTests(unittest.TestCase):
    def test_clear_discards_delayed_old_discovery_result(self):
        entered = threading.Event()
        release = threading.Event()
        delivered = []

        def reader(path):
            entered.set()
            self.assertTrue(release.wait(2.0))
            return DirectoryDiscovery(snapshot=DirectorySnapshot(
                os.path.dirname(path), (path, path + "-neighbor.png"),
                (path, path + "-neighbor.png"), True))

        navigator = FileNavigator(SettingsStub(), directory_reader=reader)
        try:
            self.assertTrue(navigator.request_navigation(
                "folder/current.png", 1, "old", delivered.append))
            self.assertTrue(entered.wait(1.0))
            self.assertTrue(navigator.clear_cache())
            release.set()
            self.assertTrue(navigator.wait_for_workers(2.0))
            self.assertEqual(delivered, [])
        finally:
            release.set()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(2.0))


if __name__ == "__main__":
    unittest.main()
