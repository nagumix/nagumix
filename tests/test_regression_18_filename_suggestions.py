import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from src.canvas_panel import CanvasPanel, ExportOperation
from src.exporting import ExportCancellation, ExportResult
from src.filename_suggestions import filename_base, suggested_filename, timestamp_base
from src.main_frame import MainFrame


class NamingHarness:
    _capture_naming_request = CanvasPanel._capture_naming_request
    _suggested_name = CanvasPanel._suggested_name
    _record_naming_success = CanvasPanel._record_naming_success
    get_scene_save_suggestion = CanvasPanel.get_scene_save_suggestion
    get_export_suggestion = CanvasPanel.get_export_suggestion
    _on_export_finished = CanvasPanel._on_export_finished
    save_canvas_state = CanvasPanel.save_canvas_state

    def __init__(self):
        self._wall_clock = lambda: datetime(2026, 9, 8, 7, 6)
        self._document_identity = 0
        self._naming_request = 0
        self._last_naming_success = 0
        self._suggested_base = None
        self.export_operation = None
        self.refreshes = 0
        self.image_objects = []

    def Refresh(self):
        self.refreshes += 1


class TestFilenameSuggestions(unittest.TestCase):
    def test_stable_timestamp_and_single_relevant_suffix(self):
        clock = lambda: datetime(2026, 11, 30, 23, 59)
        self.assertEqual(timestamp_base(clock), "nagumix-2026-11-30-2359")
        self.assertEqual(suggested_filename(None, ".json", clock),
                         "nagumix-2026-11-30-2359.json")
        for name in ("My collage.png", "a.b.jpeg", "über name.JSON"):
            self.assertEqual(filename_base(name), name.rsplit(".", 1)[0])
        self.assertEqual(filename_base("name.custom"), "name.custom")

    def test_shared_base_and_all_export_formats(self):
        canvas = NamingHarness()
        self.assertEqual(canvas.get_scene_save_suggestion(),
                         "nagumix-2026-09-08-0706.json")
        self.assertEqual(canvas.get_scene_save_suggestion(),
                         "nagumix-2026-09-08-0706.json")
        canvas._record_naming_success(0, 1, filename_base("My collage.png"))
        self.assertEqual(canvas.get_scene_save_suggestion(), "My collage.json")
        self.assertEqual(canvas.get_export_suggestion("PNG"), "My collage.png")
        self.assertEqual(canvas.get_export_suggestion("JPEG"), "My collage.jpg")
        self.assertEqual(canvas.get_export_suggestion("WEBP"), "My collage.webp")
        self.assertEqual(canvas.get_export_suggestion("BMP"), "My collage.bmp")

    def test_success_order_identity_and_failures(self):
        canvas = NamingHarness()
        identity, old_request = canvas._capture_naming_request()
        _, newer_request = canvas._capture_naming_request()
        canvas._record_naming_success(identity, newer_request, "newer")
        canvas._record_naming_success(identity, old_request, "older")
        self.assertEqual(canvas._suggested_base, "newer")
        canvas._record_naming_success(identity, newer_request + 1, "failed-is-not-called")
        canvas._document_identity += 1
        canvas._record_naming_success(identity, newer_request + 2, "old-document")
        self.assertEqual(canvas._suggested_base, "failed-is-not-called")

    def test_export_completion_updates_only_committed_success(self):
        canvas = NamingHarness()
        operation = ExportOperation(
            1, "My collage.png", "PNG", 10, 10, 0,
            ExportCancellation(), ("export", 1), 0, 1)
        canvas.export_operation = operation
        failed = ExportResult("My collage.png", 10, 10, "PNG", "failed", 0, 0,
                             error="no")
        canvas._on_export_finished((1, failed))
        self.assertIsNone(canvas._suggested_base)
        success = ExportResult("My collage.png", 10, 10, "PNG", "success", 0, 0,
                               committed=True)
        canvas._on_export_finished((1, success))
        self.assertEqual(canvas._suggested_base, "My collage")

    def test_save_updates_only_after_atomic_write_success(self):
        canvas = NamingHarness()
        with mock.patch("src.canvas_panel.write_state") as write:
            canvas.save_canvas_state("My collage.json")
        self.assertEqual(canvas._suggested_base, "My collage")
        canvas._suggested_base = "kept"
        with mock.patch("src.canvas_panel.write_state",
                        side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                canvas.save_canvas_state("failed.json")
        self.assertEqual(canvas._suggested_base, "kept")

    def test_export_dialog_prefills_and_replaces_only_untouched_generated_suffix(self):
        class Dialog:
            def __init__(self):
                self.filename = None
                self.destroyed = False

            def SetFilename(self, value):
                self.filename = value

            def ShowModal(self):
                return 5100  # wx.ID_OK

            def GetPath(self):
                return "D:/exports/" + self.filename

            def GetFilterIndex(self):
                return 1  # JPEG

            def Destroy(self):
                self.destroyed = True

        dialog = Dialog()
        panel = SimpleNamespace(
            get_export_suggestion=lambda fmt: {
                "PNG": "base.png", "JPEG": "base.jpg"}[fmt],
            begin_export=mock.Mock())
        frame = SimpleNamespace(canvas_panel=panel)
        with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog):
            MainFrame.on_export_canvas(frame, None)
        self.assertEqual(dialog.filename, "base.png")
        self.assertTrue(dialog.destroyed)
        self.assertEqual(panel.begin_export.call_args.args[0].replace("\\", "/"),
                         "D:/exports/base.jpg")
        self.assertEqual(panel.begin_export.call_args.args[1], "JPEG")
