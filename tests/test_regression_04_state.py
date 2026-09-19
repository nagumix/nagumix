import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import wx

from src.canvas_panel import CanvasPanel
from src.canvas_state import CanvasStateError, read_state, validate_state, write_state
from src.image_object import ImageObject
from src.main_frame import MainFrame
from tests.test_duplicate_identity import CanvasStub


RECORD = dict(source_path="missing-image.png", x=-3, y=4, width=8, height=4,
              zoom_factor=0.125, viewport_offset=[0, 1])


class TestStatePersistence(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state.json"

    def test_invalid_documents_preserve_live_canvas_and_work_tokens(self):
        invalid = [None, {}, {"schema_version": 99}, [None], [{}]]
        for field, values in {
            "source_path": [None, "", "\0bad", 12],
            "x": [True, 1.5, float("inf"), 2**32],
            "width": [0, -1, "8"],
            "zoom_factor": [0, -1, True, float("nan"), float("inf"), 6, 10**500],
            "viewport_offset": [None, [], [0], [0, 1, 2], [-1, 0], [0.5, 0]],
        }.items():
            for value in values:
                invalid.append([RECORD, dict(RECORD, **{field: value})])
        for document in invalid:
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document), encoding="utf-8")
                canvas = CanvasStub()
                old = ImageObject("old.png")
                canvas.image_objects.append(old)
                canvas.selected_object = canvas.marked_object = old
                canvas.drag_offset = (2, 3)
                collection = canvas.image_objects
                with self.assertRaises(ValueError):
                    CanvasPanel.load_canvas_state(canvas, self.path)
                self.assertIs(canvas.image_objects, collection)
                self.assertIs(canvas.selected_object, old)
                self.assertIs(canvas.marked_object, old)
                self.assertEqual(canvas.drag_offset, (2, 3))
                self.assertEqual(old._work_generation, 0)
                self.assertEqual(canvas.refresh_count, 0)

    def test_parse_and_file_errors_do_not_replace_canvas(self):
        canvas = CanvasStub()
        collection = canvas.image_objects
        for contents in ("[", "\ufeffbroken"):
            self.path.write_text(contents, encoding="utf-8")
            with self.assertRaises(ValueError):
                CanvasPanel.load_canvas_state(canvas, self.path)
            self.assertIs(canvas.image_objects, collection)
        with self.assertRaises(OSError):
            CanvasPanel.load_canvas_state(canvas, self.path.with_name("absent.json"))

    def test_legacy_round_trip_keeps_order_duplicates_and_unavailable_sources(self):
        records = [RECORD, dict(RECORD, x=12)]
        write_state(self.path, records)
        canvas = CanvasStub()
        old = ImageObject("old.png")
        canvas.image_objects.append(old)
        canvas.drag_offset = (1, 1)
        CanvasPanel.load_canvas_state(canvas, self.path)
        self.assertEqual([obj.x for obj in canvas.image_objects], [-3, 12])
        self.assertEqual(len({obj.object_id for obj in canvas.image_objects}), 2)
        self.assertTrue(all(obj.source_path == RECORD["source_path"]
                            for obj in canvas.image_objects))
        self.assertIsNone(canvas.drag_offset)
        self.assertEqual(old._work_generation, 1)
        CanvasPanel.save_canvas_state(canvas, self.path)
        self.assertEqual(read_state(self.path), records)
        self.assertEqual(validate_state([]), [])

    def test_failed_save_keeps_previous_bytes_and_removes_temporary_file(self):
        self.path.write_bytes(b"previous saved document")
        for operation in ("json.dump", "os.fsync", "os.replace"):
            with self.subTest(operation=operation):
                with mock.patch("src.canvas_state." + operation,
                                side_effect=OSError("injected write failure")):
                    with self.assertRaises(OSError):
                        write_state(self.path, [RECORD])
                self.assertEqual(self.path.read_bytes(), b"previous saved document")
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_partial_write_failure_keeps_destination(self):
        self.path.write_bytes(b"old")

        def fail_after_write(records, stream, **kwargs):
            stream.write('[{"source_path":')
            raise OSError("disk full")

        with mock.patch("src.canvas_state.json.dump", side_effect=fail_after_write):
            with self.assertRaises(OSError):
                write_state(self.path, [RECORD])
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_save_creates_no_file_and_never_attempts_replace(self):
        with mock.patch("src.canvas_state.os.replace") as replace:
            with self.assertRaises(CanvasStateError):
                write_state(self.path, [dict(RECORD, width=0)])
            replace.assert_not_called()
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_optional_fields_are_ignored_without_mutating_input(self):
        document = [dict(RECORD, optional_future_field="extra")]
        original = copy.deepcopy(document)
        self.assertEqual(validate_state(document), [RECORD])
        self.assertEqual(document, original)


class TestStateDialog(unittest.TestCase):
    def test_cancel_destroys_dialog_without_operation(self):
        dialog = mock.Mock()
        dialog.ShowModal.return_value = wx.ID_CANCEL
        operation = mock.Mock()
        MainFrame._run_state_dialog(mock.sentinel.frame, dialog, operation, "load")
        dialog.Destroy.assert_called_once()
        operation.assert_not_called()

    def test_load_save_errors_are_reported_after_dialog_destruction(self):
        for action in ("load", "save"):
            dialog = mock.Mock()
            dialog.ShowModal.return_value = wx.ID_OK
            dialog.GetPath.return_value = "state.json"

            def fail(path):
                dialog.Destroy.assert_called_once()
                raise CanvasStateError("Image 2: invalid width")

            with mock.patch("src.main_frame.wx.MessageBox") as message:
                MainFrame._run_state_dialog(mock.sentinel.frame, dialog, fail, action)
                self.assertIn("Image 2: invalid width", message.call_args.args[0])
