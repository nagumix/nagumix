from pathlib import Path
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from PIL import Image
import wx

from src.canvas_panel import CanvasPanel
from src.file_navigator import DirectorySnapshot, FileNavigator, path_comparison_key
from src.image_object import ImageObject
from src.image_pixels import load_source_pixels
from tests.test_regression_13_async_navigation import (
    CanvasHarness as NavigationCanvasHarness,
    Dispatcher as NavigationDispatcher,
    SettingsStub as NavigationSettings,
)
from tests.test_regression_14_async_drops import (
    Dispatcher as DropDispatcher,
    DropCanvasHarness,
    SettingsStub as DropSettings,
)
from tests.test_regression_15_async_scene_loading import SceneCanvasHarness, record


FIXTURES = Path(__file__).parent / "fixtures"
LANDMARKS = FIXTURES / "regression_24_landmarks.heic"
ALPHA = FIXTURES / "regression_24_alpha.heif"
UPPER_ALPHA = FIXTURES / "regression_24_alpha_upper.HEIF"
ORIENTED = FIXTURES / "regression_24_oriented.heic"
MIRRORED = FIXTURES / "regression_24_mirrored.heic"
PRIMARY = FIXTURES / "regression_24_primary.heif"
MALFORMED = FIXTURES / "regression_24_malformed.heic"


class HEIFRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def assert_color_near(self, actual, expected, tolerance=18):
        self.assertTrue(
            all(abs(actual[index] - expected[index]) <= tolerance
                for index in range(3)),
            (actual, expected),
        )

    def make_navigator(self, settings, dispatcher=None, **kwargs):
        navigator = FileNavigator(settings, result_dispatch=dispatcher, **kwargs)
        self.addCleanup(navigator.shutdown)
        self.addCleanup(lambda: navigator.wait_for_workers(5.0))
        return navigator

    def test_real_landmarks_alpha_uppercase_primary_and_detachment(self):
        landmarks = load_source_pixels(LANDMARKS)
        alpha = load_source_pixels(ALPHA)
        upper = load_source_pixels(UPPER_ALPHA)
        primary = load_source_pixels(PRIMARY)
        for image in (landmarks, alpha, upper, primary):
            self.addCleanup(image.close)

        self.assertEqual((landmarks.mode, landmarks.size), ("RGB", (16, 12)))
        self.assert_color_near(landmarks.getpixel((2, 2)), (255, 0, 0))
        self.assert_color_near(landmarks.getpixel((12, 2)), (0, 255, 0))
        self.assertEqual((alpha.mode, upper.mode), ("RGBA", "RGBA"))
        self.assertEqual(alpha.tobytes(), upper.tobytes())
        self.assertGreaterEqual(alpha.getpixel((1, 1))[3], 245)
        self.assertLessEqual(alpha.getpixel((4, 1))[3], 10)

        # The fixture's second image is primary and red; frame zero is blue.
        self.assertEqual((primary.mode, primary.size), ("RGB", (7, 3)))
        self.assert_color_near(primary.getpixel((0, 0)), (255, 0, 0))
        self.assertIsNone(getattr(landmarks, "fp", None))

    def test_orientation_is_applied_once_and_raw_mode_is_available(self):
        normalized = load_source_pixels(ORIENTED)
        raw = load_source_pixels(ORIENTED, apply_orientation=False)
        mirrored = load_source_pixels(MIRRORED)
        self.addCleanup(normalized.close)
        self.addCleanup(raw.close)
        self.addCleanup(mirrored.close)

        self.assertEqual(normalized.size, (2, 3))
        self.assertEqual(raw.size, (3, 2))
        self.assert_color_near(normalized.getpixel((0, 0)), (255, 255, 0))
        self.assert_color_near(normalized.getpixel((1, 0)), (255, 0, 0))
        self.assert_color_near(normalized.getpixel((0, 2)), (0, 255, 255))
        self.assertEqual(mirrored.size, (3, 2))
        self.assert_color_near(mirrored.getpixel((0, 0)), (0, 0, 255))
        self.assert_color_near(mirrored.getpixel((2, 0)), (255, 0, 0))
        self.assertIsNone(getattr(normalized, "fp", None))

    def test_registration_is_once_and_safe_for_simultaneous_decodes(self):
        import pillow_heif
        import src.image_pixels as image_pixels

        with image_pixels._HEIF_REGISTRATION_LOCK:
            previous = image_pixels._heif_registration_result
            image_pixels._heif_registration_result = None
        try:
            with mock.patch.object(
                    pillow_heif, "register_heif_opener",
                    wraps=pillow_heif.register_heif_opener) as register:
                barrier = threading.Barrier(8)

                def probe():
                    barrier.wait()
                    return image_pixels._heif_decoder_available()

                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda _unused: probe(), range(8)))
                self.assertEqual(results, [True] * 8)
                self.assertEqual(register.call_count, 1)
        finally:
            with image_pixels._HEIF_REGISTRATION_LOCK:
                image_pixels._heif_registration_result = previous

    def test_missing_codec_and_corrupt_input_have_readable_errors(self):
        import src.image_pixels as image_pixels

        with mock.patch.object(image_pixels, "_heif_decoder_available",
                               return_value=False):
            with self.assertRaisesRegex(OSError, "HEIC/HEIF decoding is unavailable"):
                load_source_pixels(LANDMARKS)

        with self.assertRaises(Exception) as context:
            load_source_pixels(MALFORMED)
        self.assertTrue(str(context.exception))

    def test_discovery_and_preload_use_the_shared_loader(self):
        navigator = self.make_navigator(DropSettings("0"))
        discovery = navigator.discover_directory(LANDMARKS)
        self.assertTrue(discovery.succeeded)
        names = {Path(path).name for path in discovery.snapshot.files}
        self.assertIn(ALPHA.name, names)
        self.assertIn(UPPER_ALPHA.name, names)
        self.assertIn(PRIMARY.name, names)

        self.assertTrue(navigator.request_preloads([str(UPPER_ALPHA)]))
        self.assertTrue(navigator.wait_for_workers(5.0))
        cached = navigator.get_preloaded_image(str(UPPER_ALPHA))
        self.assertIsNotNone(cached)
        self.addCleanup(cached.close)
        self.assertEqual(cached.mode, "RGBA")

    def test_async_drop_mixed_failure_fit_and_duplicate_independence(self):
        dispatcher = DropDispatcher()
        navigator = FileNavigator(DropSettings("0"), result_dispatch=dispatcher)
        self.addCleanup(navigator.shutdown)
        self.addCleanup(lambda: navigator.wait_for_workers(5.0))
        canvas = DropCanvasHarness(navigator, bounds=(40, 30))
        self.assertTrue(canvas.accept_drop(
            3, 4, [str(LANDMARKS), str(MALFORMED), str(LANDMARKS)]))
        self.assertTrue(dispatcher.drain_until_terminal(canvas, navigator))

        operation = canvas.drop_operation
        self.assertEqual((operation.succeeded, operation.failed), (2, 1))
        self.assertEqual(len(canvas.image_objects), 2)
        self.assertIsNot(canvas.image_objects[0]._original_image,
                         canvas.image_objects[1]._original_image)
        self.assertLessEqual(canvas.image_objects[0].x + canvas.image_objects[0].width, 40)
        self.assertLessEqual(canvas.image_objects[0].y + canvas.image_objects[0].height, 30)
        canvas.image_objects[0]._original_image.putpixel((0, 0), (1, 2, 3))
        self.assertNotEqual(canvas.image_objects[0]._original_image.getpixel((0, 0)),
                            canvas.image_objects[1]._original_image.getpixel((0, 0)))
        self.assertIn("regression_24_malformed.heic",
                      "\n".join(canvas._drop_status_lines(operation)))

    def test_navigation_retains_last_good_object_until_heif_commit(self):
        dispatcher = NavigationDispatcher()
        navigator = self.make_navigator(NavigationSettings("0"), dispatcher)
        snapshot = DirectorySnapshot(
            str(FIXTURES), (str(LANDMARKS), str(ALPHA)),
            (path_comparison_key(LANDMARKS), path_comparison_key(ALPHA)), True)
        with navigator._lock:
            navigator._file_cache[navigator._directory_key_for(str(LANDMARKS))] = snapshot

        obj = ImageObject(str(LANDMARKS), canvas_width=40, canvas_height=30)
        obj.load_image()
        canvas = NavigationCanvasHarness([obj], navigator, bounds=(40, 30))
        canvas._navigate_to_adjacent_file()
        self.assertTrue(dispatcher.drain_until_idle(navigator))
        self.assertEqual(obj.source_path, str(ALPHA))
        self.assertEqual(obj._original_image.mode, "RGBA")
        self.assertLessEqual(obj.width, 40)
        self.assertLessEqual(obj.height, 30)

    def test_scene_geometry_alpha_and_existing_export_remain_unchanged(self):
        dispatcher = DropDispatcher()
        navigator = self.make_navigator(
            DropSettings("0"), dispatcher,
            state_reader=lambda _path: [record(
                ALPHA, x=-4, y=7, width=23, height=18,
                zoom_factor=1.75, viewport_offset=[2, 1])])
        old = ImageObject("old.png")
        old.commit_scene_candidate(
            Image.new("RGB", (4, 4), "purple"),
            record("old.png", x=9, y=8, width=4, height=4), (80, 60))
        canvas = SceneCanvasHarness(navigator, objects=(old,), bounds=(80, 60))
        self.assertTrue(canvas.begin_load_canvas_state("heif-scene.json"))
        self.assertTrue(self._drain_scene(canvas, dispatcher))

        self.assertTrue(canvas.scene_operation.committed)
        loaded = canvas.image_objects[0]
        self.assertEqual(loaded.source_path, str(ALPHA))
        self.assertEqual((loaded.x, loaded.y, loaded.width, loaded.height,
                          loaded.zoom_factor, loaded.viewport_offset),
                         (-4, 7, 23, 18, 1.75, (2, 1)))
        self.assertEqual(loaded._original_image.mode, "RGBA")

        from tests.test_duplicate_identity import CanvasStub
        export_canvas = CanvasStub((8, 4))
        export_object = ImageObject(str(ALPHA))
        export_object.load_image()
        export_object.width, export_object.height = 8, 4
        export_canvas.image_objects.append(export_object)
        self.addCleanup(export_object.dispose_source_pixels)
        output = Path(__file__).with_name(".regression_24-heif-alpha.png")
        try:
            CanvasPanel.export_to_file(export_canvas, output)
            with Image.open(output) as exported:
                self.assertEqual((exported.format, exported.mode, exported.size),
                                 ("PNG", "RGBA", (8, 4)))
                self.assertGreater(exported.getpixel((1, 1))[0], 200)
                self.assertEqual(exported.getpixel((1, 1))[3], 255)
        finally:
            output.unlink(missing_ok=True)

    @staticmethod
    def _drain_scene(canvas, dispatcher, timeout=5.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with dispatcher.condition:
                item = dispatcher.items.pop(0) if dispatcher.items else None
            if item is not None:
                item[0](item[1])
                continue
            if canvas.scene_operation is not None and canvas.scene_operation.terminal:
                return True
            with dispatcher.condition:
                dispatcher.condition.wait(0.01)
        return False


if __name__ == "__main__":
    unittest.main()
