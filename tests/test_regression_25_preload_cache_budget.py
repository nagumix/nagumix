import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from src.canvas_panel import DuplicationCancellation, DuplicationTask
from src.file_navigator import FileNavigator
from src.image_object import ImageObject
from src.settings_manager import SettingsManager


MIB = 1024 * 1024


class SettingsStub:
    def __init__(self, cache_mb=1, preload_count="5"):
        self.cache_mb = cache_mb
        self.preload_count = preload_count

    def get_preload_cache_mb(self):
        return self.cache_mb

    def get_setting(self, section, key, fallback=None):
        values = {
            ("Navigation", "preload_cache_mb"): str(self.cache_mb),
            ("Navigation", "preload_count"): self.preload_count,
            ("Navigation", "enable_wheel_navigation"): "true",
            ("Navigation", "sort_method"): "name_asc",
        }
        return values.get((section, key), fallback)


class TrackedImage:
    def __init__(self, mode, size, color=0):
        self.image = Image.new(mode, size, color)
        self.mode = mode
        self.size = size
        self.closed = False

    def copy(self):
        return self.image.copy()

    def close(self):
        self.closed = True
        self.image.close()


class BlockingCopyImage(TrackedImage):
    def __init__(self, mode, size, color=0):
        super().__init__(mode, size, color)
        self.copy_entered = threading.Event()
        self.copy_release = threading.Event()

    def copy(self):
        self.copy_entered.set()
        if not self.copy_release.wait(5.0):
            raise TimeoutError("compatibility copy was not released")
        return super().copy()


class BlockingLoader:
    def __init__(self, factory):
        self.factory = factory
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returned = []

    def __call__(self, path):
        self.entered.set()
        if not self.release.wait(5.0):
            raise TimeoutError("decode was not released")
        image = self.factory(path)
        self.returned.append(image)
        return image


class NavigatorTestMixin:
    def setUp(self):
        self.navigators = []

    def tearDown(self):
        for navigator in self.navigators:
            navigator.shutdown()
            self.assertTrue(navigator.wait_for_workers(5.0))

    def navigator(self, settings=None, loader=None):
        navigator = FileNavigator(
            settings or SettingsStub(), image_loader=loader,
            result_dispatch=lambda callback, result: callback(result))
        self.navigators.append(navigator)
        return navigator

    @staticmethod
    def admit(navigator, path, image):
        with navigator._condition:
            return navigator._admit_cache_image_locked(path, image)


class TestDecodedCacheAccounting(NavigatorTestMixin, unittest.TestCase):

    def test_exact_rgb_rgba_accounting_access_refresh_and_replacement(self):
        navigator = self.navigator(SettingsStub(1))
        rgb = TrackedImage("RGB", (256, 256), "red")
        rgba = TrackedImage("RGBA", (256, 256), "blue")
        self.assertTrue(self.admit(navigator, "rgb", rgb))
        self.assertTrue(self.admit(navigator, "rgba", rgba))

        state = navigator.preload_state()
        self.assertEqual(state["cache_retained_bytes"], 256 * 256 * 7)
        self.assertEqual(state["cache_retired_bytes"], 0)
        self.assertEqual(state["cache_lru"], ("rgb", "rgba"))

        copied = navigator.get_preloaded_image("rgb")
        self.assertEqual(copied.getpixel((0, 0)), (255, 0, 0))
        copied.close()
        self.assertEqual(navigator.preload_state()["cache_lru"], ("rgba", "rgb"))

        replacement = TrackedImage("RGB", (128, 128), "green")
        self.assertTrue(self.admit(navigator, "rgba", replacement))
        self.assertTrue(rgba.closed)
        state = navigator.preload_state()
        self.assertEqual(state["cache_retained_bytes"],
                         256 * 256 * 3 + 128 * 128 * 3)
        self.assertEqual(state["cache_admissions"], 3)
        self.assertEqual(state["cache_rejections"], 0)

    def test_lru_eviction_tie_break_and_worker_plateau(self):
        created = {}

        def loader(path):
            image = TrackedImage("RGBA", (256, 256), len(created))
            created[path] = image
            return image

        navigator = self.navigator(SettingsStub(1), loader)
        for index in range(8):
            path = f"image-{index}"
            self.assertTrue(navigator.request_preloads([path]))
            self.assertTrue(navigator.wait_for_workers(2.0))
            state = navigator.preload_state()
            self.assertLessEqual(state["cache_total_bytes"], MIB)
            self.assertLessEqual(state["cache_retained_entries"], 4)

        state = navigator.preload_state()
        self.assertEqual(state["cache_total_bytes"], MIB)
        self.assertEqual(state["cached"],
                         ("image-4", "image-5", "image-6", "image-7"))
        recent = navigator.get_preloaded_image("image-4")
        self.assertIsNotNone(recent)
        recent.close()
        self.assertTrue(navigator.request_preloads(["image-8"]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(navigator.preload_state()["cached"],
                         ("image-4", "image-6", "image-7", "image-8"))

        with navigator._condition:
            navigator._preload_cache["image-4"].access_order = 1
            navigator._preload_cache["image-6"].access_order = 1
        self.assertTrue(navigator.request_preloads(["image-9"]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        state = navigator.preload_state()
        self.assertNotIn("image-4", state["cached"])
        self.assertIn("image-6", state["cached"])
        self.assertEqual(state["cache_evictions"], 6)

    def test_zero_oversize_and_invalid_setting_policy(self):
        with mock.patch("src.settings_manager.os.path.exists", return_value=False):
            settings = SettingsManager()
        self.assertEqual(settings.get_preload_cache_mb(), 256)
        for invalid in ("bad", "1.5", -1, 4097):
            settings.set_setting("Navigation", "preload_cache_mb", invalid)
            self.assertEqual(settings.get_preload_cache_mb(), 256)
        for valid in (0, 4096):
            settings.set_setting("Navigation", "preload_cache_mb", valid)
            self.assertEqual(settings.get_preload_cache_mb(), valid)

        zero_settings = SettingsStub(0)
        zero = self.navigator(zero_settings,
                              lambda path: TrackedImage("RGB", (1, 1), "red"))
        self.assertTrue(zero.request_preloads(["tiny"]))
        self.assertTrue(zero.wait_for_workers(2.0))
        state = zero.preload_state()
        self.assertEqual(state["cache_total_bytes"], 0)
        self.assertEqual(state["cache_last_admission"]["outcome"],
                         "retention_disabled")

        oversize = TrackedImage("RGBA", (513, 512), "blue")
        one_mib = self.navigator(SettingsStub(1))
        self.assertFalse(self.admit(one_mib, "oversize", oversize))
        self.assertTrue(oversize.closed)
        state = one_mib.preload_state()
        self.assertEqual(state["cache_oversize_rejections"], 1)
        self.assertEqual(state["cache_total_bytes"], 0)

    def test_compatibility_lease_remains_charged_across_lower_and_clear(self):
        settings = SettingsStub(2)
        navigator = self.navigator(settings,
                                   lambda path: TrackedImage("RGB", (1, 1), "green"))
        pinned = BlockingCopyImage("RGBA", (768, 512), "red")
        self.assertTrue(self.admit(navigator, "pinned", pinned))
        copied = []
        reader = threading.Thread(
            target=lambda: copied.append(navigator.get_preloaded_image("pinned")))
        reader.start()
        self.assertTrue(pinned.copy_entered.wait(2.0))

        settings.cache_mb = 1
        self.assertTrue(navigator.clear_cache())
        state = navigator.preload_state()
        self.assertEqual(state["cache_retained_bytes"], 0)
        self.assertEqual(state["cache_retired_bytes"], 768 * 512 * 4)
        self.assertGreater(state["cache_total_bytes"], state["cache_budget_bytes"])
        self.assertFalse(pinned.closed)

        self.assertTrue(navigator.request_preloads(["new-small"]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        state = navigator.preload_state()
        self.assertEqual(state["cache_last_admission"]["outcome"],
                         "pinned_rejection")
        self.assertNotIn("new-small", state["cached"])

        pinned.copy_release.set()
        reader.join(2.0)
        self.assertFalse(reader.is_alive())
        self.assertTrue(pinned.closed)
        self.assertEqual(copied[0].getpixel((0, 0)), (255, 0, 0, 255))
        copied[0].close()
        self.assertEqual(navigator.preload_state()["cache_total_bytes"], 0)

        self.assertTrue(navigator.request_preloads(["new-small"]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertIn("new-small", navigator.preload_state()["cached"])


class TestDecodedCacheTransfersAndWorkers(NavigatorTestMixin, unittest.TestCase):
    def test_navigation_drop_and_scene_transfer_remove_charge_once(self):
        for purpose in ("navigation", "drop", "scene"):
            with self.subTest(purpose=purpose):
                navigator = self.navigator(SettingsStub(1))
                image = TrackedImage("RGBA", (64, 32), "blue")
                self.assertTrue(self.admit(navigator, purpose, image))
                results = []
                callback = results.append
                if purpose == "navigation":
                    accepted = navigator.request_navigation_decode(
                        purpose, (purpose, 1), None, callback)
                elif purpose == "drop":
                    accepted = navigator.request_drop_decode(
                        purpose, (purpose, 1), None, callback)
                else:
                    accepted = navigator.request_scene_decode(
                        purpose, (purpose, 1), None, callback)
                self.assertTrue(accepted)
                self.assertTrue(navigator.wait_for_workers(2.0))
                self.assertEqual(navigator.preload_state()["cache_total_bytes"], 0)
                self.assertIs(results[0].pixels, image)
                results[0].close()
                self.assertTrue(image.closed)

    def test_pinned_entry_is_not_transferred_or_closed_during_reader(self):
        decoded = []
        navigator = self.navigator(
            SettingsStub(2), lambda path: decoded.append(
                TrackedImage("RGBA", (2, 2), "green")) or decoded[-1])
        pinned = BlockingCopyImage("RGBA", (32, 32), "red")
        self.assertTrue(self.admit(navigator, "same", pinned))
        copies = []
        reader = threading.Thread(
            target=lambda: copies.append(navigator.get_preloaded_image("same")))
        reader.start()
        self.assertTrue(pinned.copy_entered.wait(2.0))

        results = []
        self.assertTrue(navigator.request_navigation_decode(
            "same", ("navigation", 1), None, results.append))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(len(decoded), 1)
        self.assertIs(results[0].pixels, decoded[0])
        self.assertFalse(pinned.closed)
        results[0].close()
        pinned.copy_release.set()
        reader.join(2.0)
        copies[0].close()

    def test_oversized_foreground_demand_still_decodes_and_returns_pixels(self):
        returned = []

        def loader(path):
            image = TrackedImage("RGBA", (513, 512), "purple")
            returned.append(image)
            return image

        navigator = self.navigator(SettingsStub(1), loader)
        self.assertTrue(navigator.request_preloads(["large"]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertTrue(returned[0].closed)
        self.assertEqual(navigator.preload_state()["cache_total_bytes"], 0)

        results = []
        self.assertTrue(navigator.request_navigation_decode(
            "large", ("navigation", 1), None, results.append))
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertIs(results[0].pixels, returned[1])
        self.assertFalse(returned[1].closed)
        results[0].close()

    def test_worker_publication_uses_latest_budget_and_stale_results_close(self):
        settings = SettingsStub(2)
        loader = BlockingLoader(
            lambda path: TrackedImage("RGBA", (512, 512), "blue"))
        navigator = self.navigator(settings, loader)
        self.assertTrue(navigator.request_preloads(["budget-change"]))
        self.assertTrue(loader.entered.wait(2.0))
        settings.cache_mb = 0
        loader.release.set()
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertTrue(loader.returned[0].closed)
        self.assertEqual(navigator.preload_state()["cache_total_bytes"], 0)

        clear_loader = BlockingLoader(
            lambda path: TrackedImage("RGB", (10, 10), "red"))
        clear_nav = self.navigator(SettingsStub(1), clear_loader)
        self.assertTrue(clear_nav.request_preloads(["clear-stale"]))
        self.assertTrue(clear_loader.entered.wait(2.0))
        self.assertTrue(clear_nav.clear_cache())
        clear_loader.release.set()
        self.assertTrue(clear_nav.wait_for_workers(2.0))
        self.assertTrue(clear_loader.returned[0].closed)

        stop_loader = BlockingLoader(
            lambda path: TrackedImage("RGB", (10, 10), "red"))
        stop_nav = self.navigator(SettingsStub(1), stop_loader)
        self.assertTrue(stop_nav.request_preloads(["shutdown-stale"]))
        self.assertTrue(stop_loader.entered.wait(2.0))
        self.assertTrue(stop_nav.shutdown())
        stop_loader.release.set()
        self.assertTrue(stop_nav.wait_for_workers(2.0))
        self.assertTrue(stop_loader.returned[0].closed)

    def test_worker_start_failure_releases_transferred_cache_owner(self):
        navigator = self.navigator(SettingsStub(1))
        cached = TrackedImage("RGBA", (64, 32), "blue")
        self.assertTrue(self.admit(navigator, "cached", cached))

        with mock.patch("src.file_navigator.threading.Thread.start",
                        side_effect=RuntimeError("deliberate start failure")), \
                mock.patch("src.file_navigator.logging.exception") as logged:
            self.assertFalse(navigator.request_navigation_decode(
                "cached", ("navigation", 1), None, lambda result: None))
        logged.assert_called_once()

        state = navigator.preload_state()
        self.assertTrue(cached.closed)
        self.assertEqual(state["cache_total_bytes"], 0)
        self.assertEqual(state["jobs"], 0)
        self.assertEqual(state["foreground"], 0)

    def test_matching_preload_promotion_bypasses_retention_and_duplicate_is_independent(self):
        loader = BlockingLoader(
            lambda path: TrackedImage("RGBA", (64, 64), "blue"))
        navigator = self.navigator(SettingsStub(0), loader)
        self.assertTrue(navigator.request_preloads(["promoted"]))
        self.assertTrue(loader.entered.wait(2.0))
        results = []
        self.assertTrue(navigator.request_navigation_decode(
            "promoted", ("navigation", 1), None, results.append))
        loader.release.set()
        self.assertTrue(navigator.wait_for_workers(2.0))
        self.assertEqual(len(loader.returned), 1)
        self.assertIs(results[0].pixels, loader.returned[0])
        self.assertEqual(navigator.preload_state()["cache_total_bytes"], 0)
        results[0].close()

        source = ImageObject("source.png")
        source._original_image = Image.new("RGBA", (3, 2), "red")
        lease = source.lease_source_pixels()
        snapshot = SimpleNamespace(pixels=lease.pixels, release=lease.release)
        task = DuplicationTask(snapshot, cancellation=DuplicationCancellation())
        duplicate_results = []
        self.assertTrue(navigator.request_duplication(
            source.source_path, ("duplicate", 1), None,
            duplicate_results.append, task))
        self.assertTrue(navigator.wait_for_workers(2.0))
        duplicate = duplicate_results[0].pixels
        self.assertIsNot(duplicate, source._original_image)
        duplicate.putpixel((0, 0), (1, 2, 3, 4))
        self.assertEqual(source._original_image.getpixel((0, 0)),
                         (255, 0, 0, 255))
        duplicate_results[0].close()
        source.dispose_source_pixels()


if __name__ == "__main__":
    unittest.main()
