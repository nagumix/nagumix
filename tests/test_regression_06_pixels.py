import gc
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import wx
from PIL import Image

from src.canvas_panel import CanvasPanel
from src.file_navigator import FileNavigator
from src.image_object import ImageObject
from src.image_pixels import load_source_pixels, normalize_source_pixels
from tests.test_duplicate_identity import CanvasStub


class SettingsStub:
    def get_setting(self, section, key, default=None):
        return default


class TestSourcePixelNormalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def save(self, image, name, **kwargs):
        path = self.root / name
        image.save(path, **kwargs)
        return path

    def test_supported_modes_preserve_or_normalize_alpha(self):
        palette = Image.new("P", (2, 1))
        palette.putpalette([255, 0, 0, 0, 255, 0] + [0, 0, 0] * 254)
        palette.putdata([0, 1])
        palette.info["transparency"] = bytes([0, 128] + [255] * 254)
        cases = [
            (self.save(palette, "palette.png"), "RGBA", (255, 0, 0, 0), (0, 255, 0, 128)),
            (self.save(Image.new("LA", (2, 1), (80, 96)), "la.png"), "RGBA", (80, 80, 80, 96), None),
            (self.save(Image.new("RGBA", (2, 1), (1, 2, 3, 64)), "rgba.png"), "RGBA", (1, 2, 3, 64), None),
            (self.save(Image.new("L", (2, 1), 70), "gray.png"), "RGB", (70, 70, 70), None),
            (self.save(Image.new("CMYK", (2, 1), (0, 255, 255, 0)), "cmyk.tiff"), "RGB", (255, 0, 0), None),
        ]
        for path, mode, first, second in cases:
            with self.subTest(path=path.name):
                pixels = load_source_pixels(path)
                self.assertEqual(pixels.mode, mode)
                self.assertEqual(pixels.getpixel((0, 0)), first)
                if second is not None:
                    self.assertEqual(pixels.getpixel((1, 0)), second)

    def _oriented_jpeg(self, orientation, name):
        image = Image.new("RGB", (60, 40), "white")
        image.paste("red", (0, 0, 20, 20))
        image.paste("green", (40, 0, 60, 20))
        image.paste("blue", (0, 20, 20, 40))
        image.paste("yellow", (40, 20, 60, 40))
        exif = Image.Exif()
        exif[274] = orientation
        return self.save(image, name, format="JPEG", quality=100, subsampling=0, exif=exif)

    def assert_color_near(self, actual, expected, tolerance=20):
        self.assertTrue(all(abs(a - b) <= tolerance for a, b in zip(actual[:3], expected)),
                        (actual, expected))

    def test_exif_rotation_mirror_and_second_normalization(self):
        rotated = load_source_pixels(self._oriented_jpeg(6, "rotate.jpg"))
        self.assertEqual(rotated.size, (40, 60))
        self.assert_color_near(rotated.getpixel((30, 10)), (255, 0, 0))
        self.assert_color_near(rotated.getpixel((10, 50)), (255, 255, 0))
        self.assertEqual(normalize_source_pixels(rotated).size, (40, 60))
        self.assertEqual(normalize_source_pixels(rotated).tobytes(), rotated.tobytes())

        mirrored = load_source_pixels(self._oriented_jpeg(2, "mirror.jpg"))
        self.assertEqual(mirrored.size, (60, 40))
        self.assert_color_near(mirrored.getpixel((10, 10)), (0, 128, 0))
        self.assert_color_near(mirrored.getpixel((50, 10)), (255, 0, 0))

        transverse = load_source_pixels(self._oriented_jpeg(5, "transpose.jpg"))
        self.assertEqual(transverse.size, (40, 60))
        self.assert_color_near(transverse.getpixel((10, 50)), (0, 128, 0))

        fitted = ImageObject(str(self.root / "rotate.jpg"))
        self.assertTrue(fitted.fit_to_bounds(20, 20))
        self.assertEqual((fitted.width, fitted.height), (13, 20))

    def test_direct_and_preloaded_pixels_match_and_are_independent(self):
        path = self._oriented_jpeg(6, "shared.jpg")
        navigator = FileNavigator(SettingsStub())
        self.addCleanup(navigator.shutdown)
        self.assertTrue(navigator.request_preloads([str(path)]))
        self.assertTrue(navigator.wait_for_workers(2.0))
        cached = navigator.get_preloaded_image(str(path))
        self.assertIsNotNone(cached)
        self.addCleanup(cached.close)
        direct = ImageObject(str(path))
        preloaded = ImageObject("old.png")
        preloaded.change_source_path(str(path), cached)
        direct.load_image()
        self.assertEqual(direct._original_image.tobytes(), preloaded._original_image.tobytes())
        cached.putpixel((0, 0), (1, 2, 3))
        self.assertNotEqual(cached.getpixel((0, 0)), preloaded._original_image.getpixel((0, 0)))
        direct.x, preloaded.x = 3, 9
        direct.zoom_factor, preloaded.zoom_factor = 0.5, 1.0
        self.assertNotEqual((direct.x, direct.zoom_factor), (preloaded.x, preloaded.zoom_factor))

    def test_wx_bitmap_alpha_and_export_composite_match(self):
        source = Image.new("RGBA", (2, 1))
        source.putdata([(255, 0, 0, 128), (0, 0, 255, 0)])
        path = self.save(source, "alpha.png")
        obj = ImageObject(str(path))
        obj.width, obj.height = 2, 1
        dc = mock.Mock()
        obj.draw(dc)
        wx_image = dc.DrawBitmap.call_args.args[0].ConvertToImage()
        self.assertTrue(wx_image.HasAlpha())
        self.assertEqual(wx_image.GetAlpha(0, 0), 128)
        self.assertEqual(wx_image.GetAlpha(1, 0), 0)
        self.assertEqual((wx_image.GetRed(0, 0), wx_image.GetGreen(0, 0), wx_image.GetBlue(0, 0)),
                         (255, 0, 0))

        canvas = CanvasStub((2, 1))
        canvas.canvas_bg = "#00FF00"
        canvas.image_objects.append(obj)
        export = self.root / "composite.png"
        CanvasPanel.export_to_file(canvas, export)
        with Image.open(export) as result:
            self.assert_color_near(result.getpixel((0, 0)), (128, 127, 0), tolerance=1)
            self.assertEqual(result.getpixel((1, 0)), (0, 255, 0, 255))

    def test_first_frame_is_static_and_source_handle_is_released(self):
        first = Image.new("RGBA", (3, 2), "red")
        second = Image.new("RGBA", (3, 2), "blue")
        gif = self.save(first, "animated.gif", save_all=True, append_images=[second], loop=0)
        tiff = self.save(first, "pages.tiff", save_all=True, append_images=[second])
        for path in (gif, tiff):
            with self.subTest(path=path.name):
                pixels = load_source_pixels(path)
                self.assertEqual(pixels.getpixel((1, 1))[:3], (255, 0, 0))
                self.assertIsNone(getattr(pixels, "fp", None))
                gc.collect()
                renamed = path.with_suffix(path.suffix + ".moved")
                os.replace(path, renamed)
                os.replace(renamed, path)

    def test_legacy_exif_crop_keeps_raw_semantics_and_new_state_round_trips(self):
        path = self._oriented_jpeg(6, "legacy.jpg")
        record = {"source_path": str(path), "x": 0, "y": 0, "width": 20,
                  "height": 20, "zoom_factor": 1.0, "viewport_offset": [40, 0]}
        legacy_path = self.root / "legacy.json"
        legacy_path.write_text(json.dumps([record]), encoding="utf-8")
        legacy_canvas = CanvasStub((100, 100))
        CanvasPanel.load_canvas_state(legacy_canvas, legacy_path)
        legacy = legacy_canvas.image_objects[0]
        self.assertFalse(legacy.normalize_orientation)
        self.assertEqual(legacy._original_image, None)
        self.assert_color_near(legacy.get_pil_cropped().getpixel((10, 10)), (0, 128, 0))
        oriented_preload = load_source_pixels(path)
        legacy.change_source_path(str(self._oriented_jpeg(6, "legacy-next.jpg")),
                                  oriented_preload)
        self.assertIsNone(legacy._original_image)
        self.assertEqual(legacy.get_pil_cropped().size, (20, 20))

        new_canvas = CanvasStub((100, 100))
        new = ImageObject(str(path))
        new.width, new.height = 20, 20
        new.viewport_offset = (0, 40)
        new_canvas.image_objects.append(new)
        state = self.root / "new.json"
        CanvasPanel.save_canvas_state(new_canvas, state)
        saved = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["source_pixel_normalization"], 1)
        loaded_canvas = CanvasStub((100, 100))
        CanvasPanel.load_canvas_state(loaded_canvas, state)
        loaded = loaded_canvas.image_objects[0]
        self.assertTrue(loaded.normalize_orientation)
        self.assertEqual(loaded.viewport_offset, (0, 40))
        self.assert_color_near(loaded.get_pil_cropped().getpixel((10, 10)), (255, 255, 0))

        for unsupported in (0, 2, True, "1"):
            bad = dict(record, source_pixel_normalization=unsupported)
            state.write_text(json.dumps([bad]), encoding="utf-8")
            with self.subTest(unsupported=unsupported):
                with self.assertRaisesRegex(ValueError, "source_pixel_normalization"):
                    CanvasPanel.load_canvas_state(CanvasStub((100, 100)), state)


if __name__ == "__main__":
    unittest.main()
