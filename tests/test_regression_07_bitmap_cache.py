import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import wx
from PIL import Image

from src.arrangement import arrange_with_resize
from src.canvas_panel import CanvasPanel
from src.image_object import ImageObject
from tests.test_duplicate_identity import CanvasStub


class CountingImageObject(ImageObject):
    """Instrument the two expensive preparation operations around real work."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resize_count = 0
        self.bitmap_preparation_count = 0

    def _resize_source(self, size):
        self.resize_count += 1
        return super()._resize_source(size)

    def _bitmap_from_pil(self, image):
        self.bitmap_preparation_count += 1
        return super()._bitmap_from_pil(image)


class TestPreparedBitmapCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    @staticmethod
    def landmark_source(size=(8, 4)):
        image = Image.new("RGBA", size, (255, 0, 0, 255))
        image.paste((0, 255, 0, 128),
                    (size[0] // 2, 0, size[0], size[1]))
        return image

    def object_with_pixels(self, source=None, path="shared.png", **kwargs):
        obj = CountingImageObject(path, **kwargs)
        obj._replace_source_pixels((source or self.landmark_source()).copy())
        return obj

    @staticmethod
    def draw(obj):
        dc = mock.Mock()
        dc.GetTextExtent.return_value = wx.Size(24, 12)
        obj.draw(dc)
        return dc, dc.DrawBitmap.call_args.args[0] if dc.DrawBitmap.called else None

    @staticmethod
    def bitmap_pixel(bitmap, x, y):
        image = bitmap.ConvertToImage()
        rgb = (image.GetRed(x, y), image.GetGreen(x, y), image.GetBlue(x, y))
        alpha = image.GetAlpha(x, y) if image.HasAlpha() else 255
        return rgb + (alpha,)

    def test_warm_draw_move_selection_z_order_and_overlay_reuse_pixels(self):
        first = self.object_with_pixels()
        second = self.object_with_pixels(normalize_orientation=False)
        first.width, first.height, first.viewport_offset = 4, 4, (0, 0)
        second.width, second.height, second.viewport_offset = 4, 4, (4, 0)
        canvas = CanvasStub((8, 4))
        canvas.image_objects.extend((first, second))

        first_dc, _ = self.draw(first)
        second_dc, _ = self.draw(second)
        self.assertEqual((first.resize_count, second.resize_count), (1, 1))
        self.assertEqual((first.bitmap_preparation_count,
                          second.bitmap_preparation_count), (1, 1))

        first.x, first.y = 11, 13
        canvas.selected_object = second
        canvas.image_objects.move_to_front(first)
        first.set_status_overlay("Moved", "info")
        overlay_dc, overlay_bitmap = self.draw(first)
        self.assertTrue(overlay_dc.DrawText.called)
        self.assertIs(overlay_bitmap, first._prepared_bitmap)
        CanvasPanel.on_overlay_timer(canvas, None)
        cleared_dc, cleared_bitmap = self.draw(first)
        second_redraw_dc, _ = self.draw(second)

        self.assertEqual((first.resize_count, second.resize_count), (1, 1))
        self.assertEqual((first.bitmap_preparation_count,
                          second.bitmap_preparation_count), (1, 1))
        self.assertFalse(cleared_dc.DrawText.called)
        self.assertIs(cleared_bitmap, overlay_bitmap)
        self.assertEqual(cleared_dc.DrawBitmap.call_count, 1)
        self.assertEqual(second_redraw_dc.DrawBitmap.call_count, 1)
        self.assertTrue(first_dc.DrawBitmap.called)
        self.assertTrue(second_dc.DrawBitmap.called)
        self.assertIsNot(first._prepared_bitmap, second._prepared_bitmap)

    def test_direct_transform_assignments_replace_the_current_bitmap(self):
        obj = self.object_with_pixels()
        obj.width, obj.height = 4, 4
        _, initial = self.draw(obj)
        self.assertEqual(self.bitmap_pixel(initial, 0, 0), (255, 0, 0, 255))

        mutations = [
            ("zoom", lambda: setattr(obj, "zoom_factor", 0.5)),
            ("viewport", lambda: setattr(obj, "viewport_offset", (2, 0))),
            ("frame width", lambda: setattr(obj, "width", 3)),
            ("frame height", lambda: setattr(obj, "height", 1)),
        ]
        prior = initial
        for expected_count, (label, mutate) in enumerate(mutations, start=2):
            with self.subTest(mutation=label):
                mutate()
                dc, current = self.draw(obj)
                self.assertEqual(obj.resize_count, expected_count)
                self.assertEqual(obj.bitmap_preparation_count, expected_count)
                self.assertIsNot(current, prior)
                self.assertTrue(dc.DrawBitmap.called)
                prior = current

        # Only the current representation is retained; position is not keyed.
        self.assertIs(obj._prepared_bitmap, prior)
        current_key = obj._prepared_bitmap_key
        obj.x += 20
        obj.y += 30
        _, moved = self.draw(obj)
        self.assertIs(moved, prior)
        self.assertEqual(obj._prepared_bitmap_key, current_key)

    def test_direct_and_preloaded_source_replacement_invalidate_content(self):
        red = self.root / "red.png"
        blue = self.root / "blue.png"
        green = self.root / "green.png"
        Image.new("RGB", (3, 2), "red").save(red)
        Image.new("RGB", (3, 2), "blue").save(blue)
        Image.new("RGB", (3, 2), "green").save(green)
        obj = CountingImageObject(str(red))
        obj.width, obj.height = 3, 2

        _, red_bitmap = self.draw(obj)
        red_revision = obj._source_revision
        obj.change_source_path(str(blue), None)
        _, blue_bitmap = self.draw(obj)
        self.assertGreater(obj._source_revision, red_revision)
        self.assertEqual(self.bitmap_pixel(blue_bitmap, 1, 1)[:3], (0, 0, 255))

        with Image.open(green) as preload:
            obj.change_source_path(str(green), preload)
        _, green_bitmap = self.draw(obj)
        self.assertEqual(self.bitmap_pixel(green_bitmap, 1, 1)[:3], (0, 128, 0))

        # A directly committed loader result is detected by pixel identity even
        # if an integration path assigns the decoded object itself.
        obj._original_image = Image.new("RGB", (3, 2), "yellow")
        _, yellow_bitmap = self.draw(obj)
        self.assertEqual(self.bitmap_pixel(yellow_bitmap, 1, 1)[:3], (255, 255, 0))
        self.assertEqual(obj.bitmap_preparation_count, 4)
        self.assertIsNot(red_bitmap, blue_bitmap)
        self.assertIsNot(blue_bitmap, green_bitmap)
        self.assertIsNot(green_bitmap, yellow_bitmap)

    def test_fit_reset_arrangement_and_swap_refresh_correct_pixels(self):
        landscape = self.object_with_pixels(self.landmark_source((8, 4)), "same.png")
        portrait_source = Image.new("RGB", (4, 8), "blue")
        portrait_source.paste("yellow", (0, 4, 4, 8))
        portrait = self.object_with_pixels(portrait_source, "same.png")
        landscape.width, landscape.height = 3, 2
        portrait.width, portrait.height = 2, 3
        landscape.zoom_factor = portrait.zoom_factor = 1.0
        landscape.viewport_offset = portrait.viewport_offset = (0, 0)
        self.draw(landscape)
        self.draw(portrait)

        # Zero explicitly retains the prior no-gutter geometry used by this
        # cache-invalidation regression on its deliberately tiny canvas.
        self.assertTrue(arrange_with_resize(
            [landscape, portrait], (8, 4), spacing=0, outer_margin=0))
        _, arranged_landscape = self.draw(landscape)
        _, arranged_portrait = self.draw(portrait)
        self.assertEqual((arranged_landscape.GetWidth(), arranged_landscape.GetHeight()),
                         (4, 2))
        self.assertEqual((arranged_portrait.GetWidth(), arranged_portrait.GetHeight()),
                         (2, 4))
        self.assertEqual(self.bitmap_pixel(arranged_landscape, 3, 1)[:3], (0, 255, 0))
        self.assertEqual(self.bitmap_pixel(arranged_portrait, 1, 3)[:3], (255, 255, 0))

        landscape.viewport_offset = (1, 0)
        self.draw(landscape)
        landscape.reset_viewport_offset()
        _, reset_offset = self.draw(landscape)
        self.assertEqual(self.bitmap_pixel(reset_offset, 0, 0)[:3], (255, 0, 0))
        landscape.zoom_factor = 0.5
        landscape.reset_zoom()
        _, reset_zoom = self.draw(landscape)
        self.assertEqual(reset_zoom.GetWidth(), landscape.width)
        landscape.width = 1
        landscape.height = 1
        landscape.reset_size(fit_to_canvas=False)
        _, reset_size = self.draw(landscape)
        self.assertEqual((reset_size.GetWidth(), reset_size.GetHeight()), (8, 4))

        landscape.width, landscape.height = 3, 2
        portrait.width, portrait.height = 2, 3
        landscape.zoom_factor = portrait.zoom_factor = 1.0
        landscape.viewport_offset = portrait.viewport_offset = (0, 0)
        self.draw(landscape)
        self.draw(portrait)
        canvas = CanvasStub((8, 8))
        canvas.image_objects.extend((landscape, portrait))
        self.assertTrue(CanvasPanel.swap_image_objects(canvas, landscape, portrait))
        _, swapped_landscape = self.draw(landscape)
        _, swapped_portrait = self.draw(portrait)
        self.assertEqual((swapped_landscape.GetWidth(), swapped_landscape.GetHeight()),
                         (2, 3))
        self.assertEqual((swapped_portrait.GetWidth(), swapped_portrait.GetHeight()),
                         (3, 2))
        self.assertEqual(self.bitmap_pixel(swapped_landscape, 0, 0)[:3], (255, 0, 0))
        self.assertEqual(self.bitmap_pixel(swapped_portrait, 0, 0)[:3], (0, 0, 255))

    def _oriented_jpeg(self):
        image = Image.new("RGB", (60, 40), "white")
        image.paste("red", (0, 0, 20, 20))
        image.paste("green", (40, 0, 60, 20))
        image.paste("blue", (0, 20, 20, 40))
        image.paste("yellow", (40, 20, 60, 40))
        exif = Image.Exif()
        exif[274] = 6
        path = self.root / "oriented.jpg"
        image.save(path, quality=100, subsampling=0, exif=exif)
        return path

    def test_same_path_legacy_and_normalized_interpretations_stay_independent(self):
        path = self._oriented_jpeg()
        normalized = CountingImageObject(str(path), normalize_orientation=True)
        legacy = CountingImageObject(str(path), normalize_orientation=False)
        normalized.load_image()
        legacy.load_image()
        self.assertEqual(normalized._original_image.size, (40, 60))
        self.assertEqual(legacy._original_image.size, (60, 40))
        normalized.width, normalized.height = 20, 20
        normalized.viewport_offset = (0, 40)
        legacy.width, legacy.height = 20, 20
        legacy.viewport_offset = (40, 0)

        _, normalized_bitmap = self.draw(normalized)
        _, legacy_bitmap = self.draw(legacy)
        self.assertIsNot(normalized._prepared_bitmap, legacy._prepared_bitmap)
        normalized_rgb = self.bitmap_pixel(normalized_bitmap, 10, 10)[:3]
        legacy_rgb = self.bitmap_pixel(legacy_bitmap, 10, 10)[:3]
        self.assertTrue(all(abs(a - b) <= 20
                            for a, b in zip(normalized_rgb, (255, 255, 0))))
        self.assertTrue(all(abs(a - b) <= 20
                            for a, b in zip(legacy_rgb, (0, 128, 0))))

    def test_export_before_paint_and_after_cached_transform_is_independent(self):
        source = Image.new("RGBA", (4, 2))
        source.putdata([
            (255, 0, 0, 128), (255, 0, 0, 255),
            (0, 0, 255, 0), (0, 0, 255, 255),
        ] * 2)
        obj = self.object_with_pixels(source)
        obj.width, obj.height = 4, 2
        canvas = CanvasStub((4, 2))
        canvas.canvas_bg = "#00FF00"
        canvas.image_objects.append(obj)

        before_path = self.root / "before.png"
        CanvasPanel.export_to_file(canvas, before_path)
        self.assertIsNone(obj._prepared_bitmap)
        with Image.open(before_path) as before:
            before_pixels = before.convert("RGBA").tobytes()

        _, bitmap = self.draw(obj)
        self.assertTrue(bitmap.ConvertToImage().HasAlpha())
        self.assertEqual(self.bitmap_pixel(bitmap, 0, 0), (255, 0, 0, 128))
        prepared_key = obj._prepared_bitmap_key
        prepared_bitmap = obj._prepared_bitmap
        preparation_count = obj.bitmap_preparation_count

        after_path = self.root / "after.png"
        CanvasPanel.export_to_file(canvas, after_path)
        with Image.open(after_path) as after:
            self.assertEqual(after.convert("RGBA").tobytes(), before_pixels)
            self.assertEqual(after.getpixel((0, 0)), (128, 127, 0, 255))
            self.assertEqual(after.getpixel((2, 0)), (0, 255, 0, 255))
        self.assertIs(obj._prepared_bitmap, prepared_bitmap)
        self.assertEqual(obj._prepared_bitmap_key, prepared_key)
        self.assertEqual(obj.bitmap_preparation_count, preparation_count)

        obj.viewport_offset = (1, 0)
        obj.width = 3
        transformed_path = self.root / "transformed.png"
        CanvasPanel.export_to_file(canvas, transformed_path)
        self.assertIs(obj._prepared_bitmap, prepared_bitmap)
        self.assertEqual(obj._prepared_bitmap_key, prepared_key)
        _, transformed_bitmap = self.draw(obj)
        self.assertIsNot(transformed_bitmap, prepared_bitmap)
        with Image.open(transformed_path) as transformed:
            self.assertEqual(self.bitmap_pixel(transformed_bitmap, 0, 0)[:3],
                             transformed.getpixel((0, 0))[:3])

    def test_repr_reports_only_current_bitmap_memory(self):
        obj = self.object_with_pixels()
        obj.width, obj.height = 4, 4
        self.draw(obj)
        representation = repr(obj)
        self.assertIn("mem_bitmap=64.00 B", representation)
        self.assertNotIn("mem_vis", representation)


if __name__ == "__main__":
    unittest.main()
