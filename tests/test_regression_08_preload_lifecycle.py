import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image
import wx

from src.file_navigator import (
    DirectoryDiscovery,
    DirectorySnapshot,
    FileNavigator,
    path_comparison_key,
)
from src.main_frame import MainFrame


class SettingsStub:
    def __init__(self, preload_count="5"):
        self.preload_count = preload_count
        self.save_calls = 0

    def get_setting(self, section, key, fallback=None):
        if (section, key) == ("Navigation", "preload_count"):
            return self.preload_count
        if (section, key) == ("Navigation", "enable_wheel_navigation"):
            return "true"
        if (section, key) == ("Navigation", "sort_method"):
            return "name_asc"
        if (section, key) == ("Canvas", "background_color"):
            return "#FFFFFF"
        return fallback

    def save(self):
        self.save_calls += 1


class TrackedImage:
    """Pillow-like cache value whose ownership release is observable."""

    def __init__(self, color):
        self.image = Image.new("RGBA", (3, 2), color)
        self.closed = False

    def copy(self):
        return self.image.copy()

    def close(self):
        self.closed = True
        self.image.close()


class BlockingLoader:
    def __init__(self):
        self.release = threading.Event()
        self.condition = threading.Condition()
        self.calls = []
        self.returned = []
        self.active = 0
        self.max_active = 0

    def __call__(self, path):
        with self.condition:
            self.calls.append(path)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
        try:
            if not self.release.wait(5.0):
                raise TimeoutError("test loader was not released")
            image = TrackedImage((len(self.calls), 20, 30, 255))
            self.returned.append(image)
            return image
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def wait_for_calls(self, count, timeout=2.0):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.calls) >= count, timeout)


class TestPreloadLifecycle(unittest.TestCase):
    def tearDown(self):
        for navigator, releases in getattr(self, "_navigators", []):
            for release in releases:
                release.set()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(5.0))

    def own(self, navigator, *releases):
        if not hasattr(self, "_navigators"):
            self._navigators = []
        self._navigators.append((navigator, releases))
        return navigator

    def wait_for_cached(self, navigator, path, timeout=2.0):
        with navigator._condition:
            return navigator._condition.wait_for(
                lambda: path in navigator._preload_cache, timeout)

    def test_clear_during_decode_discards_and_closes_old_result(self):
        loader = BlockingLoader()
        navigator = self.own(FileNavigator(SettingsStub(), image_loader=loader), loader.release)
        self.assertTrue(navigator.request_preloads(["slow.png"]))
        self.assertTrue(loader.wait_for_calls(1))

        self.assertTrue(navigator.clear_cache())
        self.assertEqual(navigator.preload_state()["generation"], 1)
        loader.release.set()
        self.assertTrue(navigator.wait_for_workers(2.0))

        self.assertIsNone(navigator.get_preloaded_image("slow.png"))
        self.assertTrue(loader.returned[0].closed)
        self.assertEqual(navigator.preload_state()["jobs"], 0)

    def test_clear_during_directory_scan_does_not_restore_old_snapshot(self):
        entered = threading.Event()
        release = threading.Event()

        def reader(current_path):
            entered.set()
            self.assertTrue(release.wait(5.0))
            directory = os.path.dirname(current_path)
            files = tuple(os.path.join(directory, name)
                          for name in ("one.png", "two.png"))
            return DirectoryDiscovery(snapshot=DirectorySnapshot(
                directory,
                files,
                tuple(path_comparison_key(path) for path in files),
                True,
            ))

        navigator = self.own(
            FileNavigator(SettingsStub(), directory_reader=reader), release)
        lookup = threading.Thread(
            target=navigator.get_files_in_directory,
            args=(os.path.join("folder", "one.png"),),
        )
        lookup.start()
        self.assertTrue(entered.wait(2.0))
        self.assertTrue(navigator.clear_cache())
        release.set()
        lookup.join(2.0)
        self.assertFalse(lookup.is_alive())

        self.assertEqual(navigator._file_cache, {})

    def test_clear_then_new_same_path_keeps_new_job_and_cache_entry(self):
        first_started = threading.Event()
        first_release = threading.Event()
        second_returned = threading.Event()
        returned = {}
        call_lock = threading.Lock()
        call_count = 0

        def loader(path):
            nonlocal call_count
            with call_lock:
                call_count += 1
                number = call_count
            if number == 1:
                first_started.set()
                self.assertTrue(first_release.wait(5.0))
                image = TrackedImage((255, 0, 0, 255))
            else:
                image = TrackedImage((0, 255, 0, 255))
                second_returned.set()
            returned[number] = image
            return image

        navigator = self.own(FileNavigator(SettingsStub(), image_loader=loader), first_release)
        self.assertTrue(navigator.request_preloads(["same.png"]))
        self.assertTrue(first_started.wait(2.0))
        self.assertTrue(navigator.clear_cache())
        self.assertTrue(navigator.request_preloads(["same.png"]))
        self.assertTrue(second_returned.wait(2.0))
        self.assertTrue(self.wait_for_cached(navigator, "same.png"))

        fresh = navigator.get_preloaded_image("same.png")
        self.assertEqual(fresh.getpixel((0, 0)), (0, 255, 0, 255))
        fresh.close()
        first_release.set()
        self.assertTrue(navigator.wait_for_workers(2.0))

        still_fresh = navigator.get_preloaded_image("same.png")
        self.assertEqual(still_fresh.getpixel((0, 0)), (0, 255, 0, 255))
        still_fresh.close()
        self.assertTrue(returned[1].closed)
        self.assertFalse(returned[2].closed)
        self.assertEqual(navigator.preload_state()["jobs"], 0)

    def test_shutdown_is_terminal_idempotent_and_does_not_wait(self):
        loader = BlockingLoader()
        navigator = self.own(FileNavigator(SettingsStub(), image_loader=loader), loader.release)
        self.assertTrue(navigator.request_preloads(["blocked.png", "queued.png"]))
        self.assertTrue(loader.wait_for_calls(2))

        self.assertTrue(navigator.shutdown())
        self.assertFalse(navigator.shutdown())
        self.assertFalse(navigator.request_preloads(["later.png"]))
        self.assertFalse(navigator.wait_for_workers(0.0))
        state = navigator.preload_state()
        self.assertTrue(state["shutdown"])
        self.assertGreater(state["active"], 0)
        self.assertEqual(state["pending"], 0)

        loader.release.set()
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertTrue(all(image.closed for image in loader.returned))
        self.assertEqual(navigator.preload_state()["jobs"], 0)

    def test_failure_removes_job_and_allows_same_path_retry(self):
        attempts = 0

        def loader(path):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("deliberate decode failure")
            return TrackedImage((9, 8, 7, 255))

        navigator = self.own(FileNavigator(SettingsStub(), image_loader=loader))
        self.assertTrue(navigator.request_preloads(["retry.png"]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(navigator.preload_state()["jobs"], 0)
        self.assertIsNone(navigator.get_preloaded_image("retry.png"))

        self.assertTrue(navigator.request_preloads(["retry.png"]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        retry = navigator.get_preloaded_image("retry.png")
        self.assertIsNotNone(retry)
        retry.close()
        self.assertEqual(attempts, 2)

    def test_concurrent_submissions_are_deduplicated_and_bounded(self):
        loader = BlockingLoader()
        navigator = self.own(FileNavigator(SettingsStub(), image_loader=loader), loader.release)
        paths = [f"image-{index:02}.png" for index in range(20)]
        barrier = threading.Barrier(6)

        def submit():
            barrier.wait()
            navigator.request_preloads(paths + paths[:5])

        submitters = [threading.Thread(target=submit) for _ in range(5)]
        for thread in submitters:
            thread.start()
        barrier.wait()
        for thread in submitters:
            thread.join(2.0)
            self.assertFalse(thread.is_alive())
        self.assertTrue(loader.wait_for_calls(3))

        state = navigator.preload_state()
        self.assertEqual(state["active"], 3)
        self.assertEqual(state["pending"], navigator.MAX_PENDING_PRELOADS)
        self.assertLessEqual(loader.max_active, navigator.MAX_ACTIVE_DECODES)

        loader.release.set()
        self.assertTrue(navigator.wait_for_workers(5.0))
        retained = paths[:navigator.MAX_ACTIVE_DECODES + navigator.MAX_PENDING_PRELOADS]
        self.assertEqual(set(loader.calls), set(retained))
        self.assertEqual(len(loader.calls), len(set(loader.calls)))
        self.assertEqual(navigator.preload_state()["jobs"], 0)
        self.assertLessEqual(loader.max_active, navigator.MAX_ACTIVE_DECODES)

    def test_latest_request_replaces_only_obsolete_queued_work(self):
        loader = BlockingLoader()
        navigator = self.own(FileNavigator(SettingsStub(), image_loader=loader), loader.release)
        old = [f"old-{index}.png" for index in range(13)]
        new = [f"new-{index}.png" for index in range(10)]
        self.assertTrue(navigator.request_preloads(old))
        self.assertTrue(loader.wait_for_calls(3))

        self.assertTrue(navigator.request_preloads(new))
        self.assertEqual(navigator.preload_state()["queued"], tuple(new))
        loader.release.set()
        self.assertTrue(navigator.wait_for_workers(5.0))

        self.assertEqual(set(loader.calls[:3]), set(old[:3]))
        self.assertTrue(set(old[3:]).isdisjoint(loader.calls))
        self.assertTrue(set(new).issubset(loader.calls))

    def test_neighbor_priority_is_next_then_previous_by_distance(self):
        loader = BlockingLoader()
        navigator = self.own(FileNavigator(SettingsStub("3"), image_loader=loader), loader.release)
        files = [f"image-{index}.png" for index in range(7)]
        with mock.patch.object(
                navigator, "get_files_in_directory", return_value=(files, 3)):
            self.assertTrue(navigator.start_preloading(files[3]))
        self.assertTrue(loader.wait_for_calls(3))

        # The first three priorities are already running; the retained queue
        # continues next 2, previous 2, next 3, previous 3 ordering.
        self.assertEqual(
            navigator.preload_state()["queued"],
            (files[1], files[6], files[0]),
        )

    def test_caller_copies_survive_clear_and_are_independent(self):
        navigator = self.own(FileNavigator(SettingsStub()))
        root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        path = root / "copy.png"
        Image.new("RGBA", (3, 2), (10, 20, 30, 255)).save(path)

        self.assertTrue(navigator.request_preloads([str(path)]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        first = navigator.get_preloaded_image(str(path))
        second = navigator.get_preloaded_image(str(path))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        first.putpixel((0, 0), (1, 2, 3, 4))
        self.assertNotEqual(first.getpixel((0, 0)), second.getpixel((0, 0)))

        self.assertTrue(navigator.clear_cache())
        self.assertEqual(first.getpixel((0, 0)), (1, 2, 3, 4))
        self.assertEqual(second.getpixel((0, 0)), (10, 20, 30, 255))
        self.assertTrue(navigator.shutdown())
        self.assertEqual(first.getpixel((0, 0)), (1, 2, 3, 4))
        self.assertEqual(second.getpixel((0, 0)), (10, 20, 30, 255))
        first.close()
        second.close()

    def test_real_gif_failure_and_file_removal_after_wait(self):
        root = Path(tempfile.mkdtemp(dir=Path(__file__).parent))
        gif = root / "valid.gif"
        broken = root / "broken.png"
        first = Image.new("RGBA", (3, 2), "red")
        second = Image.new("RGBA", (3, 2), "blue")
        first.save(gif, save_all=True, append_images=[second])
        broken.write_bytes(b"not an image")
        navigator = FileNavigator(SettingsStub())
        try:
            self.assertTrue(navigator.request_preloads([str(gif), str(broken)]))
            self.assertTrue(navigator.wait_for_workers(3.0))
            decoded = navigator.get_preloaded_image(str(gif))
            self.assertIsNotNone(decoded)
            self.assertEqual(decoded.size, (3, 2))
            decoded.close()
            self.assertIsNone(navigator.get_preloaded_image(str(broken)))
        finally:
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(3.0))
            shutil.rmtree(root)
        self.assertFalse(root.exists())


class TestPreloadCloseIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_frame_close_starts_shutdown_without_waiting_for_decode(self):
        settings = SettingsStub()
        loader = BlockingLoader()
        frame = MainFrame(None, "Regression 08 close", settings, debug_mode=True)
        original = frame.canvas_panel.file_navigator
        original.shutdown()
        self.assertTrue(original.wait_for_workers(1.0))
        navigator = FileNavigator(settings, image_loader=loader)
        frame.canvas_panel.file_navigator = navigator
        try:
            self.assertTrue(navigator.request_preloads(["blocked.png"]))
            self.assertTrue(loader.wait_for_calls(1))
            started = time.monotonic()
            frame.Close(force=True)
            elapsed = time.monotonic() - started

            self.assertTrue(navigator.is_shutdown)
            self.assertFalse(navigator.wait_for_workers(0.0))
            self.assertLess(elapsed, 0.5)
            self.assertEqual(settings.save_calls, 1)
        finally:
            loader.release.set()
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(3.0))
            if frame:
                try:
                    frame.Destroy()
                except RuntimeError:
                    pass


if __name__ == "__main__":
    unittest.main()
