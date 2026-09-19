import unittest
import os
import tempfile
from unittest import mock

import wx
from PIL import Image

from src.arrangement import arrange_with_resize
from src.canvas_panel import CanvasPanel, FileDropTarget
from src.image_object import ImageObject
from src.image_geometry import fit_geometry
from src.image_pixels import load_source_pixels
from tests.test_duplicate_identity import CanvasStub
from tests.test_regression_02_resets import FIXTURE_IMAGE


def image_object(size=(400, 200)):
    obj = ImageObject("in-memory")
    obj._original_image = Image.new("RGB", size, "red")
    obj._original_image.paste("blue", (size[0] // 2, 0, size[0], size[1]))
    return obj


class DropCanvas(CanvasStub):
    get_client_dimensions = CanvasPanel.get_client_dimensions
    add_image_object = CanvasPanel.add_image_object

    def start_preloading_for_object(self, obj):
        pass


class TestWholeImageFit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def assert_full_source(self, obj):
        pixels = obj.get_pil_cropped()
        self.assertEqual(pixels.size, (obj.width, obj.height))
        self.assertEqual(pixels.getpixel((0, obj.height // 2)), (255, 0, 0, 255))
        self.assertEqual(pixels.getpixel((obj.width - 1, obj.height // 2)), (0, 0, 255, 255))

    def test_fit_keeps_both_halves_and_preserves_or_clamps_position(self):
        obj = image_object()
        obj.x, obj.y = 25, 15
        obj.viewport_offset = (40, 20)
        obj.set_status_overlay("Wrapped to first image")
        self.assertTrue(obj.fit_to_bounds(100, 100))
        self.assertEqual((obj.width, obj.height, obj.zoom_factor), (100, 50, 0.25))
        self.assertEqual((obj.x, obj.y), (0, 15))
        self.assertEqual(obj.viewport_offset, (0, 0))
        self.assertEqual(obj.status_message, "Wrapped to first image")
        self.assert_full_source(obj)

    def test_portrait_landscape_and_panorama_fit(self):
        for size in ((400, 200), (200, 400), (2000, 100)):
            with self.subTest(size=size):
                obj = image_object(size)
                obj.fit_to_bounds(100, 100)
                self.assertLessEqual(obj.width, 100)
                self.assertLessEqual(obj.height, 100)
                self.assert_full_source(obj)

    def test_tiny_thin_and_invalid_bounds(self):
        for size in ((1, 1), (1, 1000), (1000, 1), (2, 3)):
            with self.subTest(size=size):
                obj = image_object(size)
                obj.fit_to_bounds(10, 10)
                self.assertTrue(1 <= obj.width <= 10)
                self.assertTrue(1 <= obj.height <= 10)
                self.assertEqual(obj.get_pil_cropped().size, (obj.width, obj.height))
        self.assertEqual(fit_geometry((2, 3), (100, 100)), (1.0, 2, 3))
        obj = image_object()
        old = (obj.width, obj.height, obj.zoom_factor, obj.viewport_offset)
        for bounds in ((0, 10), (-1, 10), (None, None)):
            self.assertFalse(obj.fit_to_bounds(*bounds))
            self.assertEqual((obj.width, obj.height, obj.zoom_factor, obj.viewport_offset), old)

    def test_zoom_can_return_to_fit_below_twenty_five_percent(self):
        obj = image_object((1000, 500))
        obj.fit_to_bounds(100, 100)
        self.assertEqual(obj.zoom_factor, 0.1)
        obj.zoom_in()
        self.assertAlmostEqual(obj.zoom_factor, 0.125)
        obj.zoom_out()
        self.assertAlmostEqual(obj.zoom_factor, 0.1)
        obj.zoom_out()
        self.assertAlmostEqual(obj.zoom_factor, 0.1)
        self.assert_full_source(obj)

    def test_navigation_uses_fit_without_overwriting_status(self):
        obj = image_object()
        obj.zoom_factor = 2
        obj.viewport_offset = (80, 10)
        obj.x, obj.y = 90, 90
        obj.set_status_overlay("Loading...", "processing")
        self.assertTrue(CanvasPanel._reset_navigated_image_properties(CanvasStub((100, 100)), obj))
        self.assertEqual((obj.x, obj.y), (0, 50))
        self.assertEqual(obj.zoom_factor, 0.25)
        self.assertEqual(obj.status_message, "Loading...")
        self.assert_full_source(obj)

    def test_drop_fits_at_canvas_edge_and_keeps_each_object_inside(self):
        canvas = DropCanvas((4, 4))
        geometry = fit_geometry((8, 4), (4, 4))
        for intended in ((3, 3), (23, 23)):
            obj = ImageObject(FIXTURE_IMAGE, canvas_width=4, canvas_height=4)
            obj.commit_drop_candidate(
                load_source_pixels(FIXTURE_IMAGE), geometry, (4, 4), intended)
            canvas.add_image_object(obj)
        self.assertEqual(len(canvas.image_objects), 2)
        for obj in canvas.image_objects:
            self.assertEqual((obj.width, obj.height, obj.zoom_factor), (4, 2, 0.5))
            self.assertEqual((obj.x, obj.y), (0, 2))
        pixels = load_source_pixels(FIXTURE_IMAGE)
        try:
            with self.assertRaises(ValueError):
                ImageObject(FIXTURE_IMAGE).commit_drop_candidate(
                    pixels, geometry, (0, 0), (0, 0))
        finally:
            pixels.close()

    def test_arrangement_fits_real_pixels_and_uses_distinct_cells(self):
        objects = [image_object(), image_object()]
        self.assertTrue(arrange_with_resize(objects, (200, 100)))
        self.assertEqual([(obj.x, obj.y) for obj in objects], [(10, 29), (105, 29)])
        self.assertEqual(objects[1].x - (objects[0].x + objects[0].width), 10)
        for obj in objects:
            self.assert_full_source(obj)
        old = [(obj.x, obj.y, obj.width, obj.height) for obj in objects]
        self.assertFalse(arrange_with_resize(objects, (1, 1)))
        self.assertEqual([(obj.x, obj.y, obj.width, obj.height) for obj in objects], old)

    def test_preview_bitmap_matches_export_dimensions_and_landmarks(self):
        obj = image_object()
        obj.fit_to_bounds(100, 100)
        dc = mock.Mock()
        obj.draw(dc)
        bitmap = dc.DrawBitmap.call_args.args[0].ConvertToImage()
        self.assertEqual((bitmap.GetWidth(), bitmap.GetHeight()), (100, 50))
        self.assertEqual((bitmap.GetRed(99, 25), bitmap.GetBlue(99, 25)), (0, 255))
        self.assert_full_source(obj)

    def test_fitted_legacy_state_round_trips_and_zoom_works_before_first_paint(self):
        canvas = DropCanvas((1, 1))
        obj = ImageObject(FIXTURE_IMAGE, canvas_width=1, canvas_height=1)
        obj.commit_drop_candidate(
            load_source_pixels(FIXTURE_IMAGE), fit_geometry((8, 4), (1, 1)),
            (1, 1), (0, 0))
        canvas.add_image_object(obj)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                         dir=os.path.dirname(__file__)) as stream:
            path = stream.name
        try:
            CanvasPanel.save_canvas_state(canvas, path)
            loaded = CanvasStub((100, 100))
            CanvasPanel.load_canvas_state(loaded, path)
            obj = loaded.image_objects[0]
            self.assertEqual((obj.width, obj.height, obj.zoom_factor), (1, 1, 0.125))
            obj.zoom_in()
            obj.zoom_out()
            self.assertAlmostEqual(obj.zoom_factor, 0.125)
            self.assertEqual(obj.get_pil_cropped().size, (1, 1))
        finally:
            os.unlink(path)

    def test_missing_arrangement_source_leaves_all_transforms_unchanged(self):
        first = image_object()
        first.x = 31
        missing = ImageObject("regression_05-source-that-does-not-exist.png")
        with self.assertLogs(level="ERROR"):
            self.assertFalse(arrange_with_resize([first, missing], (100, 100)))
        self.assertEqual((first.x, first.width, first.height), (31, 200, 150))
