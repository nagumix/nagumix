"""Focused frozen-path and packaging-definition checks."""

from pathlib import Path
import logging
import os
import tempfile
import unittest
from unittest import mock

from src import runtime_paths
from src.app import configure_logging
from src.settings_manager import SettingsManager
from src.package_diagnostics import write_package_diagnostics


ROOT = Path(__file__).resolve().parents[1]


class FrozenRuntimePathTests(unittest.TestCase):
    def test_source_settings_path_preserves_cwd_behavior(self):
        with mock.patch.object(runtime_paths.sys, "frozen", False, create=True):
            self.assertEqual(runtime_paths.settings_path(), Path("nagumix_settings.ini"))

    def test_frozen_settings_are_isolated_from_cwd_and_round_trip(self):
        with tempfile.TemporaryDirectory() as user_root, tempfile.TemporaryDirectory() as cwd:
            old = os.getcwd()
            try:
                os.chdir(cwd)
                with (mock.patch.object(runtime_paths.sys, "frozen", True, create=True),
                      mock.patch.dict(os.environ, {"LOCALAPPDATA": user_root})):
                    manager = SettingsManager()
                    manager.set_setting("Test", "key", "packaged")
                    manager.save()
                    expected = Path(user_root) / "NaguMIX" / "nagumix_settings.ini"
                    self.assertEqual(Path(manager.loaded_path), expected)
                    self.assertTrue(expected.is_file())
                    self.assertFalse((Path(cwd) / "nagumix_settings.ini").exists())
                    self.assertEqual(SettingsManager().get_setting("Test", "key"), "packaged")
            finally:
                os.chdir(old)

    def test_frozen_logging_works_without_console_streams(self):
        with tempfile.TemporaryDirectory() as user_root:
            with (mock.patch.object(runtime_paths.sys, "frozen", True, create=True),
                  mock.patch.dict(os.environ, {"LOCALAPPDATA": user_root}),
                  mock.patch.object(runtime_paths.sys, "stdout", None),
                  mock.patch.object(runtime_paths.sys, "stderr", None)):
                path = configure_logging()
                logging.getLogger().warning("packaged diagnostic")
                logging.shutdown()
                self.assertEqual(path, Path(user_root) / "NaguMIX" / "nagumix.log")
                self.assertIn("packaged diagnostic", path.read_text(encoding="utf-8"))
        configure_logging()


class PackagingDefinitionTests(unittest.TestCase):
    def test_spec_collects_required_assets_and_is_windowed_one_folder(self):
        spec = (ROOT / "packaging" / "nagumix-windows.spec").read_text(encoding="utf-8")
        for name in ("branding", "icons", "legal"):
            self.assertIn(f'assets / "{name}"', spec)
        self.assertIn("console=False", spec)
        self.assertIn("COLLECT(", spec)

    def test_package_diagnostics_decode_pixels_and_find_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            from PIL import Image
            source = Path(temporary) / "alpha.png"
            report = Path(temporary) / "report.json"
            Image.new("RGBA", (3, 2), (10, 20, 30, 40)).save(source)
            gif = ROOT / "tests" / "fixtures" / "regression_27_random_seek.gif"
            self.assertEqual(write_package_diagnostics(report, [source, gif]), 0)
            import json
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["images"][0]["size"], [3, 2])
            self.assertEqual(payload["images"][0]["mode"], "RGBA")
            self.assertGreater(payload["images"][1]["frame_count"], 1)
            self.assertTrue(payload["images"][1]["frame_2_rgba_sha256"])
            self.assertTrue(all(payload["resources"].values()))


if __name__ == "__main__":
    unittest.main()
