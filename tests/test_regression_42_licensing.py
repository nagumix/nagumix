"""Focused checks for offline legal resources and the native Licensing page."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest import mock

import wx

from src import legal_resources
from src.native_style import DARK_COLORS
from src.settings_dialog import SettingsDialog
from src.settings_manager import SettingsManager


ROOT = Path(__file__).resolve().parents[1]


def canonical_digest(path):
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256((text.rstrip("\n") + "\n").encode("utf-8")).hexdigest()


class TestLegalResources(unittest.TestCase):
    def tearDown(self):
        legal_resources.read_legal_resource.cache_clear()

    def test_official_texts_have_verified_content_and_matching_repository_copies(self):
        expected = {
            "assets/legal/AGPL-3.0.txt":
                "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0",
            "assets/legal/CC-BY-SA-4.0.txt":
                "23ee78c8bae49cf08ea2f0c84945c66b987ebe4520881fb51b3dad4fb43d07c2",
        }
        for relative, digest in expected.items():
            self.assertEqual(canonical_digest(ROOT / relative), digest)
        self.assertEqual(canonical_digest(ROOT / "LICENSE"), expected["assets/legal/AGPL-3.0.txt"])
        self.assertEqual(
            canonical_digest(ROOT / "LICENSES/CC-BY-SA-4.0.txt"),
            expected["assets/legal/CC-BY-SA-4.0.txt"])
        self.assertIn("Version 3, 19 November 2007",
                      (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("Attribution-ShareAlike 4.0 International",
                      (ROOT / "LICENSES/CC-BY-SA-4.0.txt").read_text(encoding="utf-8"))

    def test_metadata_notice_scope_and_bundled_document_inventory_are_consistent(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["license"], "AGPL-3.0-or-later")
        notice = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
        for value in ("AGPL-3.0-or-later", "CC-BY-SA-4.0", "2025–2026 Vangual",
                      "collages they export", "Modified builds and forks"):
            self.assertIn(value, notice)
        for _, relative in legal_resources.LEGAL_DOCUMENTS:
            text = legal_resources.read_legal_resource(relative)
            self.assertGreater(len(text), 100, relative)

    def test_lookup_ignores_working_directory_and_supports_collected_bundle(self):
        previous = Path.cwd()
        try:
            os.chdir(ROOT / "docs")
            self.assertIn(
                legal_resources.LICENSE_IDENTIFIER,
                legal_resources.read_legal_resource("legal/PROJECT-NOTICE.txt"))
        finally:
            os.chdir(previous)

        fixture = ROOT / "tests/regression_42-legal-fixture"
        target = fixture / "assets/legal/PROJECT-NOTICE.txt"
        target.parent.mkdir(parents=True, exist_ok=False)
        target.write_text("collected legal resource", encoding="utf-8")
        try:
            legal_resources.read_legal_resource.cache_clear()
            with mock.patch.object(legal_resources.resource_path.__globals__["sys"],
                                   "_MEIPASS", str(fixture), create=True):
                self.assertEqual(
                    legal_resources.read_legal_resource("legal/PROJECT-NOTICE.txt"),
                    "collected legal resource")
        finally:
            shutil.rmtree(fixture)

    def test_unknown_resource_is_rejected(self):
        with self.assertRaises(ValueError):
            legal_resources.read_legal_resource("../LICENSE")


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1 for native Licensing checks")
class TestNativeLicensingPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.directory.name)
        self.manager = SettingsManager()

    def tearDown(self):
        os.chdir(self.previous)
        self.directory.cleanup()

    def dialog(self):
        dialog = SettingsDialog(None, self.manager)
        self.addCleanup(lambda: dialog.Destroy() if dialog else None)
        return dialog

    def test_page_is_after_advanced_lazy_readonly_copyable_and_not_a_preference(self):
        dialog = self.dialog()
        self.assertEqual([button.GetLabel() for button in dialog.nav], [
            "Appearance", "Navigation", "Arrangement", "Advanced", "Licensing"])
        self.assertEqual(dialog.built_pages, {0})
        dialog.bg_color_text.SetValue("#123456")
        dialog.select_page(4)
        self.assertEqual(dialog.built_pages, {0, 4})
        self.assertFalse(dialog.legal_text.IsEditable())
        self.assertEqual(dialog.legal_choice.GetCount(), len(legal_resources.LEGAL_DOCUMENTS))
        self.assertIn("AGPL-3.0-or-later", dialog.legal_text.GetValue())
        self.assertEqual(dialog.public_source_organization.GetValue(),
                         legal_resources.PUBLIC_SOURCE_ORGANIZATION_URL)

        dialog.legal_choice.SetSelection(2)
        dialog.legal_document_changed()
        self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", dialog.legal_text.GetValue())
        dialog.legal_text.SetSelection(0, 3)
        self.assertEqual(dialog.legal_text.GetStringSelection(), "   ")
        self.assertTrue(dialog.legal_choice.AcceptsFocus())
        self.assertTrue(dialog.legal_select_all_button.AcceptsFocus())
        self.assertTrue(dialog.legal_copy_button.AcceptsFocus())

        clipboard = mock.Mock()
        clipboard.Open.return_value = True
        with mock.patch("src.settings_dialog.wx.TheClipboard", clipboard):
            dialog.copy_legal_text()
        clipboard.SetData.assert_called_once()
        self.assertIn("Licensing text copied", dialog.note.GetLabel())
        self.assertNotIn("licens", " ".join(dialog.snapshot()).lower())

        with mock.patch.object(dialog, "EndModal"):
            dialog.on_ok(None)
        self.assertEqual(SettingsManager().get_dialog_draft()["background"], "#123456")

    def test_active_appearance_and_small_window_scrolling(self):
        dialog = self.dialog()
        dialog.select_page(4)
        dialog.apply_dialog_appearance(DARK_COLORS)
        self.assertEqual(dialog.legal_text.GetBackgroundColour(),
                         wx.Colour(DARK_COLORS["control"]))
        self.assertEqual(dialog.legal_text.GetForegroundColour(),
                         wx.Colour(DARK_COLORS["ink"]))
        dialog.SetClientSize(dialog.FromDIP((640, 520)))
        dialog.Show()
        dialog.reflow()
        self.app.Yield()
        page = dialog.pages[4][0]
        self.assertGreater(page.GetVirtualSize().height, page.GetClientSize().height)
        self.assertLessEqual(page.GetVirtualSize().width, page.GetClientSize().width)

    def test_cancel_from_licensing_page_preserves_saved_draft(self):
        before = self.manager.get_dialog_draft()
        dialog = self.dialog()
        dialog.bg_color_text.SetValue("#010203")
        dialog.select_page(4)
        with mock.patch.object(dialog, "Close") as finish:
            dialog.on_cancel()
        finish.assert_called_once_with()
        self.assertEqual(self.manager.get_dialog_draft(), before)


if __name__ == "__main__":
    unittest.main()
