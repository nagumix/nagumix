"""Focused checks for collected branding resources and native icon loading."""

import os
from pathlib import Path
import unittest
from unittest import mock

from PIL import Image
import wx

from src import brand_resources


ROOT = Path(__file__).resolve().parents[1]


class TestBrandFiles(unittest.TestCase):
    def test_source_checkout_lookup_ignores_working_directory_and_missing_asset(self):
        old = Path.cwd()
        try:
            os.chdir(ROOT / "docs")
            path = brand_resources.resource_path(
                "branding/nagumix-logo-dark-80.png")
            self.assertEqual(path, ROOT / "assets/branding/nagumix-logo-dark-80.png")
            self.assertIsNone(brand_resources.resource_path("missing.png"))
        finally:
            os.chdir(old)

    def test_frozen_bundle_lookup_uses_collected_asset_root(self):
        fixture = ROOT / "tests/brand-resource-fixture"
        path = fixture / "assets/icons/nagumix-app-256.png"
        path.parent.mkdir(parents=True, exist_ok=False)
        try:
            path.write_bytes(b"test")
            with mock.patch.object(brand_resources.sys, "_MEIPASS", str(fixture),
                                   create=True):
                self.assertEqual(brand_resources.resource_path(
                    "icons/nagumix-app-256.png"), path)
        finally:
            path.unlink()
            path.parent.rmdir()
            path.parent.parent.rmdir()
            fixture.rmdir()

    def test_platform_files_have_alpha_and_expected_embedded_sizes(self):
        icons = ROOT / "assets/icons"
        with Image.open(icons / "nagumix-app.ico") as image:
            self.assertEqual(sorted(image.ico.sizes()),
                             [(16, 16), (24, 24), (32, 32), (48, 48),
                              (64, 64), (128, 128), (256, 256)])
            self.assertEqual(image.convert("RGBA").getpixel((0, 0))[3], 0)
        with Image.open(icons / "nagumix-app.icns") as image:
            self.assertIn((512, 512, 2), image.icns.itersizes())
            self.assertEqual(image.size, (1024, 1024))
        for size in (128, 256, 512):
            with Image.open(icons / f"nagumix-app-{size}.png") as image:
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.mode, "RGBA")


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1 for native resource checks")
class TestNativeBrandResources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def _clear_resource_caches(self):
        brand_resources.header_bitmap.cache_clear()
        brand_resources.app_icon_bundle.cache_clear()

    def setUp(self):
        self._clear_resource_caches()
        self.addCleanup(self._clear_resource_caches)

    def test_cached_header_bitmap_and_missing_resource(self):
        brand_resources.header_bitmap.cache_clear()
        with mock.patch.object(brand_resources, "resource_path",
                               wraps=brand_resources.resource_path) as lookup:
            first = brand_resources.header_bitmap("dark", 40)
            self.assertTrue(first.IsOk())
            self.assertIs(first, brand_resources.header_bitmap("dark", 40))
            self.assertEqual(lookup.call_count, 1)
        brand_resources.header_bitmap.cache_clear()
        brand_resources.app_icon_bundle.cache_clear()
        with mock.patch.object(brand_resources, "resource_path", return_value=None):
            self.assertIsNone(brand_resources.header_bitmap("light", 20))
            self.assertIsNone(brand_resources.app_icon_bundle())

    def test_missing_resource_isolated_after_icon_cache_warmup(self):
        self.assertIsNotNone(brand_resources.app_icon_bundle())
        self._clear_resource_caches()
        with mock.patch.object(brand_resources, "resource_path", return_value=None) as lookup:
            self.assertIsNone(brand_resources.header_bitmap("dark", 20))
            self.assertIsNone(brand_resources.app_icon_bundle())
        self.assertEqual(lookup.call_count, 2)

    def test_icon_bundle_contains_native_small_and_large_sizes(self):
        bundle = brand_resources.app_icon_bundle()
        self.assertIsNotNone(bundle)
        self.assertTrue(bundle.GetIcon((16, 16)).IsOk())
        self.assertTrue(bundle.GetIcon((256, 256)).IsOk())


if __name__ == "__main__":
    unittest.main()
