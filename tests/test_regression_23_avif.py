from pathlib import Path
import unittest
from unittest import mock

from PIL import Image, features
import wx

from src.canvas_panel import CanvasPanel
from src.file_navigator import (
    DirectorySnapshot,
    FileNavigator,
    path_comparison_key,
)
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
from tests.test_regression_15_async_scene_loading import (
    SceneCanvasHarness,
    record,
)


FIXTURES = Path(__file__).parent / "fixtures"
LANDMARKS = FIXTURES / "regression_23_landmarks.avif"
ALPHA = FIXTURES / "regression_23_alpha.avif"
UPPER_ALPHA = FIXTURES / "regression_23_alpha_upper.AVIF"
ORIENTED = FIXTURES / "regression_23_oriented.avif"
SEQUENCE = FIXTURES / "regression_23_sequence.avif"
MALFORMED = FIXTURES / "regression_23_malformed.avif"


class AVIFRegressionTests(unittest.TestCase):
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

    def test_real_rgb_rgba_uppercase_and_first_frame_decode(self):
        self.assertTrue(features.check("avif"))

        rgb = load_source_pixels(LANDMARKS)
        self.addCleanup(rgb.close)
        self.assertEqual((rgb.mode, rgb.size), ("RGB", (16, 12)))
        self.assert_color_near(rgb.getpixel((2, 2)), (255, 0, 0))
        self.assert_color_near(rgb.getpixel((12, 2)), (0, 255, 0))
        self.assertIsNone(getattr(rgb, "fp", None))

        alpha = load_source_pixels(ALPHA)
        upper = load_source_pixels(UPPER_ALPHA)
        self.addCleanup(alpha.close)
        self.addCleanup(upper.close)
        self.assertEqual((alpha.mode, upper.mode), ("RGBA", "RGBA"))
        self.assertEqual(alpha.size, upper.size)
        self.assertGreaterEqual(alpha.getpixel((1, 1))[3], 245)
        self.assertLessEqual(alpha.getpixel((5, 1))[3], 10)
        self.assertEqual(alpha.tobytes(), upper.tobytes())

        sequence = load_source_pixels(SEQUENCE)
        self.addCleanup(sequence.close)
        self.assert_color_near(sequence.getpixel((0, 0)), (255, 0, 0))

    def test_orientation_and_source_handle_release(self):
        normalized = load_source_pixels(ORIENTED)
        self.addCleanup(normalized.close)
        self.assertEqual(normalized.size, (3, 2))

        raw = load_source_pixels(ORIENTED, apply_orientation=False)
        self.addCleanup(raw.close)
        self.assertEqual(raw.size, (2, 3))

        self.assertIsNone(getattr(normalized, "fp", None))

    def test_missing_codec_and_malformed_input_have_useful_errors(self):
        with mock.patch("src.image_pixels._avif_decoder_available", return_value=False):
            with self.assertRaisesRegex(OSError, "AVIF decoding is unavailable"):
                load_source_pixels(LANDMARKS)

        with self.assertRaises(Exception) as context:
            load_source_pixels(MALFORMED)
        self.assertTrue(str(context.exception))

    def test_extension_discovery_and_worker_preload_use_shared_loader(self):
        navigator = self.make_navigator(DropSettings("0"))
        discovery = navigator.discover_directory(LANDMARKS)
        self.assertTrue(discovery.succeeded)
        names = {Path(path).name for path in discovery.snapshot.files}
        self.assertIn(ALPHA.name, names)
        self.assertIn(UPPER_ALPHA.name, names)

        self.assertTrue(navigator.request_preloads([str(UPPER_ALPHA)]))
        self.assertTrue(navigator.wait_for_workers(5.0))
        cached = navigator.get_preloaded_image(str(UPPER_ALPHA))
        self.assertIsNotNone(cached)
        self.addCleanup(cached.close)
        self.assertEqual(cached.mode, "RGBA")
        self.assertLessEqual(cached.getpixel((5, 1))[3], 10)

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
        self.assertIn("regression_23_malformed.avif",
                      "\n".join(canvas._drop_status_lines(operation)))

    def test_navigation_keeps_existing_object_until_avif_commit(self):
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

    def test_scene_load_preserves_saved_geometry_and_alpha(self):
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
        self.assertTrue(canvas.begin_load_canvas_state("avif-scene.json"))
        self.assertTrue(self._drain_scene(canvas, dispatcher))

        self.assertTrue(canvas.scene_operation.committed)
        loaded = canvas.image_objects[0]
        self.assertEqual(loaded.source_path, str(ALPHA))
        self.assertEqual((loaded.x, loaded.y, loaded.width, loaded.height,
                          loaded.zoom_factor, loaded.viewport_offset),
                         (-4, 7, 23, 18, 1.75, (2, 1)))
        self.assertEqual(loaded._original_image.mode, "RGBA")

    def test_duplicate_pixels_and_existing_png_export(self):
        first = ImageObject(str(ALPHA))
        second = ImageObject(str(ALPHA))
        first.load_image()
        second.load_image()
        self.addCleanup(first.dispose_source_pixels)
        self.addCleanup(second.dispose_source_pixels)
        self.assertIsNot(first._original_image, second._original_image)
        first._original_image.putpixel((0, 0), (1, 2, 3, 4))
        self.assertNotEqual(first._original_image.getpixel((0, 0)),
                            second._original_image.getpixel((0, 0)))

        from tests.test_duplicate_identity import CanvasStub
        canvas = CanvasStub((8, 4))
        first.width, first.height = 8, 4
        canvas.image_objects.append(first)
        output = Path(__file__).with_name(".regression_23-avif-alpha.png")
        try:
            CanvasPanel.export_to_file(canvas, output)
            with Image.open(output) as exported:
                self.assertEqual(exported.format, "PNG")
                self.assertEqual(exported.size, (8, 4))
                self.assertGreater(exported.getpixel((1, 1))[1],
                                   exported.getpixel((1, 1))[0])
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
