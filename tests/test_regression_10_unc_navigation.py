import os
import ntpath
import threading
import unittest
from unittest import mock

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.file_navigator import (
    DirectoryDiscovery,
    DirectoryFailure,
    DirectorySnapshot,
    FileNavigator,
    NavigationResult,
    path_comparison_key,
)
from src.image_object import ImageObject


class SettingsStub:
    def __init__(self, sort_method="name_asc", preload_count="0"):
        self.sort_method = sort_method
        self.preload_count = preload_count

    def get_setting(self, section, key, fallback=None):
        if (section, key) == ("Navigation", "sort_method"):
            return self.sort_method
        if (section, key) == ("Navigation", "preload_count"):
            return self.preload_count
        if (section, key) == ("Navigation", "enable_wheel_navigation"):
            return "true"
        return fallback


def snapshot(current_path, names, *, path_module=os.path):
    directory = path_module.dirname(current_path)
    paths = tuple(path_module.join(directory, name) for name in names)
    return DirectoryDiscovery(snapshot=DirectorySnapshot(
        directory,
        paths,
        tuple(path_comparison_key(path) for path in paths),
        True,
    ))


class FakeEntry:
    def __init__(self, directory, name, *, is_file=True, modified=1,
                 path_module=os.path):
        self.name = name
        self.path = path_module.join(directory, name)
        self._is_file = is_file
        self._modified = modified

    def is_file(self):
        return self._is_file

    def stat(self):
        if isinstance(self._modified, Exception):
            raise self._modified
        return mock.Mock(st_mtime=self._modified)


class FakeScandir:
    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        return iter(self.entries)

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class TestDirectoryBoundary(unittest.TestCase):
    def test_empty_directory_is_distinct_from_recorded_enumeration_error(self):
        current = os.path.join("share", "folder", "current.PNG")
        navigator = FileNavigator(
            SettingsStub(), directory_reader=lambda path: snapshot(path, []))
        try:
            empty = navigator.discover_directory(current)
            self.assertTrue(empty.succeeded)
            self.assertEqual(empty.snapshot.files, ())

            navigator.clear_cache()
            error = OSError("[WinError -2146893818] Invalid Signature")
            error.winerror = -2146893818
            navigator._directory_reader = lambda path: DirectoryDiscovery(
                failure=DirectoryFailure.from_exception("enumeration", error))
            failed = navigator.discover_directory(current)
            self.assertFalse(failed.succeeded)
            self.assertEqual(failed.failure.error_code, -2146893818)
            self.assertIn("Invalid Signature", failed.failure.user_message())
            self.assertIn("Scroll to retry", failed.failure.user_message())

            for message, code in (("The network path was not found", 53),
                                  ("Access is denied", 5)):
                navigator._directory_reader = lambda path, m=message, c=code: (
                    DirectoryDiscovery(failure=DirectoryFailure(
                        "enumeration", m, c)))
                failure = navigator.discover_directory(current).failure
                self.assertEqual((failure.message, failure.error_code),
                                 (message, code))
        finally:
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(1.0))

    def test_ordinary_and_extended_unc_aliases_compare_without_rewriting(self):
        ordinary = r"\\Server\Share\Photos\Current.PNG"
        extended = r"\\?\UNC\server\share\photos\current.png"
        self.assertEqual(path_comparison_key(ordinary), path_comparison_key(extended))

        result = snapshot(
            ordinary, ["Alpha.jpg", "CURRENT.png", "zeta.GIF"],
            path_module=ntpath)
        snap = result.snapshot
        self.assertEqual(snap.index_for(extended), 1)
        self.assertEqual(snap.files[1], r"\\Server\Share\Photos\CURRENT.png")

    def test_read_boundary_handles_unicode_uppercase_and_metadata_race(self):
        current = os.path.join("folder with spaces", "été.PNG")
        directory = os.path.dirname(current)
        entries = [
            FakeEntry(directory, "zeta.txt"),
            FakeEntry(directory, "été.PNG", modified=20),
            FakeEntry(directory, "ALPHA.JPG", modified=10),
            FakeEntry(directory, "gone.gif", modified=FileNotFoundError("gone")),
            FakeEntry(directory, "scan.TIF", modified=30),
        ]
        navigator = FileNavigator(SettingsStub(sort_method="date_asc"))

        try:
            with (mock.patch("src.file_navigator.os.path.exists", return_value=True),
                  mock.patch("src.file_navigator.os.scandir",
                             return_value=FakeScandir(entries))):
                result = navigator.discover_directory(current)
            self.assertTrue(result.succeeded)
            self.assertEqual(
                [os.path.basename(path) for path in result.snapshot.files],
                ["ALPHA.JPG", "été.PNG", "scan.TIF", "gone.gif"],
            )
            self.assertEqual(result.snapshot.index_for(current), 1)
        finally:
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(1.0))

    def test_current_file_may_disappear_while_neighbors_remain_navigable(self):
        current = os.path.join("folder", "missing.png")
        navigator = FileNavigator(SettingsStub())
        directory = os.path.dirname(current)
        entries = [FakeEntry(directory, name) for name in
                   ["alpha.JPG", "missing.png", "omega.PNG"]]
        try:
            with (mock.patch("src.file_navigator.os.path.exists", return_value=False),
                  mock.patch("src.file_navigator.os.scandir",
                             return_value=FakeScandir(entries))):
                result = navigator.discover_directory(current)
            self.assertTrue(result.succeeded)
            self.assertFalse(result.snapshot.source_exists)
            self.assertEqual(result.snapshot.index_for(current), 1)
        finally:
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(1.0))

    def test_scandir_metadata_avoids_provider_isfile_misclassification(self):
        current = os.path.join("share", "photos", "current.jpg")
        directory = os.path.dirname(current)
        entries = [FakeEntry(directory, name) for name in (
            "first.jpg", "current.jpg", "third.jpg")]
        navigator = FileNavigator(SettingsStub())
        try:
            with (mock.patch("src.file_navigator.os.path.exists", return_value=True),
                  mock.patch("src.file_navigator.os.scandir",
                             return_value=FakeScandir(entries)),
                  mock.patch("src.file_navigator.os.path.isfile", return_value=False),
                  mock.patch("src.file_navigator.os.stat", return_value=mock.Mock(
                      st_mode=0o40777, st_file_attributes=16))):
                result = navigator.discover_directory(current)
            self.assertTrue(result.succeeded)
            self.assertEqual(len(result.snapshot.files), 3)
            self.assertEqual(result.snapshot.index_for(current), 0)
            self.assertEqual(
                [os.path.basename(path) for path in result.snapshot.files],
                ["current.jpg", "first.jpg", "third.jpg"],
            )
            self.assertTrue(result.snapshot.files[2].endswith("third.jpg"))
        finally:
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(1.0))


class TestAsyncDiscovery(unittest.TestCase):
    def tearDown(self):
        for navigator, releases in getattr(self, "_owned", []):
            for release in releases:
                release.set()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(2.0))

    def own(self, navigator, *releases):
        if not hasattr(self, "_owned"):
            self._owned = []
        self._owned.append((navigator, releases))
        return navigator

    def test_failure_preserves_code_and_explicit_retry_recovers(self):
        current = os.path.join("share", "two.png")
        completed = threading.Event()
        results = []
        attempts = 0

        def reader(path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return DirectoryDiscovery(failure=DirectoryFailure(
                    "enumeration", "Invalid Signature", -2146893818))
            return snapshot(path, ["one.png", "two.png", "three.png"])

        navigator = self.own(FileNavigator(SettingsStub(), directory_reader=reader))

        def receive(result):
            results.append(result)
            completed.set()

        self.assertTrue(navigator.request_navigation(current, 1, "first", receive))
        self.assertTrue(completed.wait(1.0))
        self.assertEqual(attempts, 1)
        self.assertEqual(results[-1].failure.error_code, -2146893818)

        completed.clear()
        self.assertTrue(navigator.request_navigation(current, 1, "retry", receive))
        self.assertTrue(completed.wait(1.0))
        self.assertEqual(attempts, 2)
        self.assertIsNone(results[-1].failure)
        self.assertTrue(results[-1].target_path.endswith("three.png"))

    def test_delayed_requests_are_coalesced_and_preserve_logical_steps(self):
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        calls = 0
        current = os.path.join("share", "one.png")

        def reader(path):
            nonlocal calls
            calls += 1
            entered.set()
            self.assertTrue(release.wait(2.0))
            return snapshot(path, ["one.png", "two.png", "three.png", "four.png"])

        navigator = self.own(
            FileNavigator(SettingsStub(), directory_reader=reader), release)
        results = []
        self.assertTrue(navigator.request_navigation(
            current, 1, "same-request", lambda result: (results.append(result), completed.set())))
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(navigator.request_navigation(
            current, 1, "same-request", lambda result: (results.append(result), completed.set())))
        self.assertTrue(navigator.request_navigation(
            current, 1, "same-request", lambda result: (results.append(result), completed.set())))
        state = navigator.preload_state()
        self.assertTrue(state["discovery_active"])
        self.assertFalse(state["discovery_pending"])

        release.set()
        self.assertTrue(completed.wait(1.0))
        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].target_path.endswith("four.png"))

    def test_clear_and_shutdown_reject_late_results_without_waiting(self):
        for action in ("clear", "shutdown"):
            with self.subTest(action=action):
                entered = threading.Event()
                release = threading.Event()
                delivered = threading.Event()
                current = os.path.join("share", "one.png")

                def reader(path):
                    entered.set()
                    self.assertTrue(release.wait(2.0))
                    return snapshot(path, ["one.png", "two.png"])

                navigator = self.own(
                    FileNavigator(SettingsStub(), directory_reader=reader), release)
                navigator.request_navigation(
                    current, 1, action, lambda result: delivered.set())
                self.assertTrue(entered.wait(1.0))
                if action == "clear":
                    self.assertTrue(navigator.clear_cache())
                else:
                    self.assertTrue(navigator.shutdown())
                self.assertFalse(navigator.wait_for_workers(0.0))
                self.assertFalse(delivered.is_set())
                release.set()
                self.assertTrue(navigator.wait_for_workers(1.0))
                self.assertFalse(delivered.is_set())

    def test_only_one_discovery_runs_and_only_latest_other_folder_is_pending(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def reader(path):
            calls.append(path)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2.0))
            return snapshot(path, [os.path.basename(path), "neighbor.png"])

        navigator = self.own(
            FileNavigator(SettingsStub(), directory_reader=reader), release)
        callback = lambda result: None
        navigator.request_navigation(os.path.join("one", "a.png"), 1, "one", callback)
        self.assertTrue(entered.wait(1.0))
        navigator.request_navigation(os.path.join("two", "b.png"), 1, "two", callback)
        navigator.request_navigation(os.path.join("three", "c.png"), 1, "three", callback)
        state = navigator.preload_state()
        self.assertTrue(state["discovery_active"])
        self.assertTrue(state["discovery_pending"])

        release.set()
        self.assertTrue(navigator.wait_for_workers(1.0))
        self.assertEqual([os.path.dirname(path) for path in calls], ["one", "three"])


class CanvasStub:
    _on_navigation_discovered = CanvasPanel._on_navigation_discovered

    def __init__(self, image_object):
        self.image_objects = ImageObjectList([image_object])
        self.selected_object = image_object
        self.refresh_count = 0
        self.overlay_delays = []

    def Refresh(self):
        self.refresh_count += 1

    def _schedule_overlay_clear(self, delay):
        self.overlay_delays.append(delay)


class TestCanvasDiscoveryResult(unittest.TestCase):
    def test_failure_and_stale_results_never_change_image_or_transforms(self):
        image_object = ImageObject(r"\\server\share\current.png")
        image_object.x, image_object.y = 12, 34
        image_object.width, image_object.height = 320, 240
        image_object.zoom_factor = 1.75
        image_object.viewport_offset = (7, 9)
        original = (
            image_object.source_path, image_object.x, image_object.y,
            image_object.width, image_object.height, image_object.zoom_factor,
            image_object.viewport_offset,
        )
        canvas = CanvasStub(image_object)
        context = (image_object, image_object._work_generation, image_object.source_path)
        failure = NavigationResult(
            None, False, context,
            DirectoryFailure("enumeration", "Access is denied", 5),
        )

        canvas._on_navigation_discovered(failure)
        self.assertEqual(original, (
            image_object.source_path, image_object.x, image_object.y,
            image_object.width, image_object.height, image_object.zoom_factor,
            image_object.viewport_offset,
        ))
        self.assertEqual(image_object.status_type, "warning")
        self.assertIn("Scroll to retry", image_object.status_message)

        image_object.change_source_path(r"\\server\share\elsewhere.png")
        image_object.clear_status_overlay()
        canvas._on_navigation_discovered(failure)
        self.assertFalse(image_object.show_status_overlay)


if __name__ == "__main__":
    unittest.main()
