"""Focused U15 source fingerprint, save lifecycle and settings coverage."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image
import wx

from src.canvas_panel import CanvasPanel, SaveOperation
from src.canvas_saving import (
    CanvasSaveCancellation,
    CanvasSaveCanceled,
    CanvasSaveObjectSnapshot,
    CanvasSaveResult,
    CanvasSaveSnapshot,
    CanvasSaveTask,
    FingerprintWarning,
    fingerprint_source,
)
from src.canvas_state import CanvasStateError, read_state, validate_state
from src.file_navigator import FileNavigator
from src.image_object import ImageObject
from src.image_pixels import get_source_identity, load_source_pixels, source_file_identity
from src.main_frame import MainFrame
from src.settings_dialog import SettingsDialog
from src.settings_manager import SettingsManager


def saved_object(path, identity=None, *, x=1, frame=None):
    return CanvasSaveObjectSnapshot(
        os.fspath(path), identity, x, 2, 3, 4, 1.0, (0, 0), True, frame)


def valid_record(path="missing.png"):
    return {
        "source_path": os.fspath(path), "x": 0, "y": 0,
        "width": 10, "height": 10, "zoom_factor": 1.0,
        "viewport_offset": [0, 0],
    }


class TempCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.directory)

    def source(self, name="source.bin", data=b"abc"):
        path = self.directory / name
        path.write_bytes(data)
        return path


class TestFingerprintWorker(TempCase):
    def test_known_bytes_renamed_copy_and_full_gif_file(self):
        first = self.source(data=b"abc")
        renamed = self.source("renamed.bin", b"abc")
        cancellation = CanvasSaveCancellation()
        for path in (first, renamed):
            fingerprint, identity = fingerprint_source(path, cancellation)
            self.assertEqual(identity, source_file_identity(path))
            self.assertEqual(fingerprint, {
                "size_bytes": 3,
                "md5": "900150983cd24fb0d6963f7d28e17f72",
            })

        gif = Path(__file__).parent / "fixtures" / "regression_27_disposal.gif"
        fingerprint, _ = fingerprint_source(gif, cancellation)
        self.assertEqual(fingerprint["size_bytes"], gif.stat().st_size)
        self.assertEqual(fingerprint["md5"], hashlib.md5(gif.read_bytes()).hexdigest())

    def test_duplicate_references_hash_once_and_disabled_reads_nothing(self):
        path = self.source(data=b"same bytes")
        identity = source_file_identity(path)
        calls = []
        writes = []

        def fingerprinter(candidate, cancellation):
            calls.append(candidate)
            return ({"size_bytes": 10, "md5": "a" * 32}, identity)

        def writer(destination, records, cancellation):
            writes.append(records)
            cancellation.replace("unused", destination, lambda *_: None)

        snapshot = CanvasSaveSnapshot(
            (saved_object(path, identity), saved_object(path, identity, x=9)), True)
        result = CanvasSaveTask(
            snapshot, self.directory / "state.json", CanvasSaveCancellation(),
            fingerprinter=fingerprinter, writer=writer).run()
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.committed)
        self.assertEqual(writes[0][0]["source_file"], writes[0][1]["source_file"])

        calls.clear()
        writes.clear()
        disabled = CanvasSaveSnapshot(snapshot.objects, False)
        result = CanvasSaveTask(
            disabled, self.directory / "off.json", CanvasSaveCancellation(),
            fingerprinter=fingerprinter, writer=writer).run()
        self.assertEqual(calls, [])
        self.assertNotIn("source_file", writes[0][0])
        self.assertTrue(result.committed)

    def test_missing_unreadable_unknown_and_changed_sources_warn_but_save(self):
        good = self.source()
        missing = self.directory / "missing.bin"
        unknown = self.directory / "unknown.bin"
        changed = self.source("changed.bin", b"old")
        unreadable = self.source("unreadable.bin", b"secret")
        identities = {
            good: source_file_identity(good),
            missing: (os.path.abspath(missing), 1, 1, 0, 0),
            changed: source_file_identity(changed),
            unreadable: source_file_identity(unreadable),
        }

        def controlled(path, cancellation):
            candidate = Path(path)
            if candidate == unreadable:
                raise PermissionError("access denied")
            if candidate == changed:
                fingerprint, observed = fingerprint_source(candidate, cancellation)
                observed = (observed[0], observed[1], observed[2] + 1, *observed[3:])
                return fingerprint, observed
            return fingerprint_source(candidate, cancellation)

        snapshot = CanvasSaveSnapshot(tuple(
            saved_object(path, identities.get(path))
            for path in (good, missing, unreadable, changed, unknown)), True)
        destination = self.directory / "warnings.json"
        result = CanvasSaveTask(
            snapshot, destination, CanvasSaveCancellation(),
            fingerprinter=controlled).run()
        self.assertEqual(result.status, "success")
        self.assertTrue(result.committed)
        self.assertEqual(len(result.warnings), 4)
        records = json.loads(destination.read_text(encoding="utf-8"))
        self.assertIn("source_file", records[0])
        for record in records[1:]:
            self.assertNotIn("source_file", record)

    def test_identity_change_during_hash_is_rejected(self):
        path = self.source(data=b"stable bytes")
        actual = source_file_identity(path)
        calls = 0

        def changing_identity(candidate):
            nonlocal calls
            calls += 1
            if calls == 1:
                return actual
            return (actual[0], actual[1], actual[2] + 1, *actual[3:])

        with self.assertRaisesRegex(OSError, "changed while"):
            fingerprint_source(
                path, CanvasSaveCancellation(),
                identity_reader=changing_identity)

    def test_streamed_cancel_checks_between_bounded_reads(self):
        path = self.source(data=b"x" * 64)
        started = threading.Event()
        release = threading.Event()

        class BlockingStream:
            def __init__(self, candidate, mode):
                self.stream = open(candidate, mode)
                self.first = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, size):
                if self.first:
                    self.first = False
                    started.set()
                    release.wait(2)
                return self.stream.read(size)

        cancellation = CanvasSaveCancellation()
        outcome = []

        def run():
            try:
                fingerprint_source(
                    path, cancellation, open_file=BlockingStream, chunk_size=8)
            except Exception as exc:
                outcome.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(1))
        self.assertTrue(cancellation.cancel())
        release.set()
        worker.join(2)
        self.assertIsInstance(outcome[0], CanvasSaveCanceled)

    def test_atomic_replace_failure_preserves_old_destination(self):
        source = self.source()
        destination = self.source("state.json", b"last good")
        snapshot = CanvasSaveSnapshot(
            (saved_object(source, source_file_identity(source)),), True)
        with mock.patch("src.canvas_state.os.replace", side_effect=OSError("disk full")):
            result = CanvasSaveTask(
                snapshot, destination, CanvasSaveCancellation()).run()
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.committed)
        self.assertEqual(destination.read_bytes(), b"last good")
        self.assertEqual(list(self.directory.glob(".nagumix-state-*.tmp")), [])


class TestMetadataValidation(TempCase):
    def test_legacy_present_unknown_and_malformed_metadata(self):
        legacy = valid_record()
        self.assertEqual(validate_state([legacy]), [legacy])
        metadata = {"size_bytes": 0, "md5": "0" * 32, "future": "ignored"}
        validated = validate_state([{**legacy, "source_file": metadata,
                                     "future_record": True}])
        self.assertEqual(validated[0]["source_file"], {
            "size_bytes": 0, "md5": "0" * 32})
        path = self.directory / "metadata.json"
        path.write_text(json.dumps([{**legacy, "source_file": metadata}]), encoding="utf-8")
        self.assertEqual(read_state(path)[0]["source_file"]["md5"], "0" * 32)

        invalid = (
            [], {"size_bytes": True, "md5": "0" * 32},
            {"size_bytes": -1, "md5": "0" * 32},
            {"size_bytes": 1, "md5": "A" * 32},
            {"size_bytes": 1, "md5": "0" * 31},
        )
        for source_file in invalid:
            with self.subTest(source_file=source_file), self.assertRaises(CanvasStateError):
                validate_state([{**legacy, "source_file": source_file}])


class TestSettingsAndSnapshot(TempCase):
    def test_default_on_invalid_safe_default_off_and_round_trip(self):
        previous = os.getcwd()
        os.chdir(self.directory)
        try:
            manager = SettingsManager()
            self.assertTrue(manager.get_include_file_identification())
            manager.set_setting("Saving", "include_file_identification", "malformed")
            self.assertTrue(manager.get_include_file_identification())
            draft = manager.get_dialog_draft()
            draft["include_file_identification"] = False
            manager.save_dialog_draft(draft)
            loaded = SettingsManager()
            self.assertFalse(loaded.get_include_file_identification())
            with self.assertRaises(ValueError):
                loaded.save_dialog_draft({
                    **loaded.get_dialog_draft(), "include_file_identification": 1})
        finally:
            os.chdir(previous)

    def test_snapshot_keeps_invocation_transform_path_identity_and_gif_frame(self):
        source = self.source("animated.gif", b"encoded")
        identity = source_file_identity(source)
        obj = ImageObject(source)
        obj.x = 17
        obj.source_identity = identity
        obj.animation = SimpleNamespace(
            frame_count=4, displayed_index=2, source_identity=identity)
        canvas = SimpleNamespace(image_objects=[obj], settings_manager=SimpleNamespace(
            get_include_file_identification=lambda: False))
        snapshot = CanvasPanel._capture_canvas_save_snapshot(canvas)
        obj.x = 99
        obj.source_path = "later.gif"
        obj.animation.displayed_index = 3
        records = []

        def writer(destination, data, cancellation):
            records.extend(data)
            cancellation.replace("unused", destination, lambda *_: None)

        result = CanvasSaveTask(
            snapshot, self.directory / "snapshot.json", CanvasSaveCancellation(),
            writer=writer).run()
        self.assertTrue(result.committed)
        self.assertEqual(records[0]["source_path"], os.fspath(source))
        self.assertEqual(records[0]["x"], 17)
        self.assertEqual(records[0]["animation"]["frame_index"], 2)

    def test_decode_retains_identity_used_by_saves(self):
        path = self.directory / "source.png"
        Image.new("RGB", (2, 2), "red").save(path)
        pixels = load_source_pixels(path)
        try:
            self.assertEqual(get_source_identity(pixels), source_file_identity(path))
            obj = ImageObject(path)
            obj._original_image = pixels
            self.assertEqual(obj.source_identity, source_file_identity(path))
        finally:
            obj.dispose_source_pixels()

    def test_begin_save_captures_toggle_before_returning(self):
        enabled = [True]
        captured = []

        class Navigator:
            is_shutdown = False

            @staticmethod
            def request_canvas_save(task, request_key, callback):
                captured.append(task)
                return True

        canvas = SimpleNamespace(
            image_objects=[], save_operation=None, _save_generation=0,
            _document_identity=0, _naming_request=0, _last_naming_success=0,
            _suggested_base=None, file_navigator=Navigator(),
            settings_manager=SimpleNamespace(
                get_include_file_identification=lambda: enabled[0]),
            Refresh=lambda: None)
        self.assertTrue(CanvasPanel.begin_save_canvas_state(
            canvas, self.directory / "state.json"))
        enabled[0] = False
        self.assertTrue(captured[0].snapshot.include_file_identification)


@unittest.skipUnless(os.environ.get("NAGUMIX_GUI_TESTS") == "1",
                     "Set NAGUMIX_GUI_TESTS=1")
class TestNativeToggle(TempCase):
    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def test_advanced_toggle_is_visible_checked_and_has_approved_help(self):
        previous = os.getcwd()
        os.chdir(self.directory)
        try:
            dialog = SettingsDialog(None, SettingsManager())
            self.addCleanup(dialog.Destroy)
            dialog.select_page(3)
            control = dialog.controls["include_file_identification"]
            self.assertTrue(control.IsShownOnScreen() or control.IsShown())
            self.assertTrue(control.GetValue())
            def descendant_labels(window):
                found = []
                for child in window.GetChildren():
                    if hasattr(child, "GetLabel"):
                        found.append(child.GetLabel())
                    found.extend(descendant_labels(child))
                return found

            labels = descendant_labels(dialog.pages[3][0])
            self.assertTrue(any("Save source file size and MD5" in label
                                for label in labels))
        finally:
            os.chdir(previous)


class TestMainFrameDispatch(unittest.TestCase):
    def test_save_dialog_starts_async_canvas_save(self):
        dialog = mock.Mock()
        dialog.ShowModal.return_value = wx.ID_OK
        dialog.GetPath.return_value = "chosen.json"
        panel = mock.Mock()
        panel.get_scene_save_suggestion.return_value = "suggested.json"
        frame = SimpleNamespace(canvas_panel=panel)
        frame._run_state_dialog = lambda chooser, operation, action: (
            MainFrame._run_state_dialog(frame, chooser, operation, action))
        with mock.patch("src.main_frame.wx.FileDialog", return_value=dialog):
            MainFrame.on_save_canvas_state(frame, None)
        panel.begin_save_canvas_state.assert_called_once_with("chosen.json")
        panel.save_canvas_state.assert_not_called()


class TestSchedulerLifecycle(TempCase):
    @staticmethod
    def writer(destination, records, cancellation):
        cancellation.replace("unused", destination, lambda *_: None)

    def navigator(self, dispatch=None):
        settings = SimpleNamespace(get_setting=lambda *args, **kwargs: kwargs.get("fallback"))
        return FileNavigator(settings, result_dispatch=dispatch)

    def test_save_runs_off_caller_thread_and_clear_cache_does_not_cancel_it(self):
        source = self.source()
        identity = source_file_identity(source)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        worker_threads = []

        def delayed(path, cancellation):
            worker_threads.append(threading.current_thread())
            entered.set()
            release.wait(2)
            cancellation.check()
            return ({"size_bytes": 3, "md5": "a" * 32}, identity)

        navigator = self.navigator(lambda callback, result: callback(result))
        self.addCleanup(navigator.shutdown)
        task = CanvasSaveTask(
            CanvasSaveSnapshot((saved_object(source, identity),), True),
            self.directory / "state.json", CanvasSaveCancellation(),
            fingerprinter=delayed, writer=self.writer)
        self.assertTrue(navigator.request_canvas_save(
            task, ("save", 1), lambda result: finished.set()))
        self.assertTrue(entered.wait(1))
        self.assertIsNot(worker_threads[0], threading.current_thread())
        self.assertTrue(navigator.clear_cache())
        self.assertFalse(task.cancellation.canceled)
        release.set()
        self.assertTrue(finished.wait(2))

    def test_newer_save_supersedes_old_and_only_current_callback_publishes(self):
        source = self.source()
        identity = source_file_identity(source)
        entered = threading.Event()
        release = threading.Event()
        callbacks = []

        def delayed(path, cancellation):
            entered.set()
            release.wait(2)
            cancellation.check()
            return ({"size_bytes": 3, "md5": "a" * 32}, identity)

        navigator = self.navigator(lambda callback, result: callback(result))
        self.addCleanup(navigator.shutdown)
        old = CanvasSaveTask(
            CanvasSaveSnapshot((saved_object(source, identity),), True),
            self.directory / "same.json", CanvasSaveCancellation(),
            fingerprinter=delayed, writer=self.writer)
        new = CanvasSaveTask(
            CanvasSaveSnapshot((saved_object(source, identity),), False),
            self.directory / "same.json", CanvasSaveCancellation(),
            writer=self.writer)
        self.assertTrue(navigator.request_canvas_save(
            old, ("save", 1), lambda result: callbacks.append("old")))
        self.assertTrue(entered.wait(1))
        self.assertTrue(navigator.request_canvas_save(
            new, ("save", 2), lambda result: callbacks.append("new")))
        self.assertTrue(old.cancellation.canceled)
        release.set()
        for _ in range(200):
            if callbacks:
                break
            threading.Event().wait(0.005)
        self.assertEqual(callbacks, ["new"])
        self.assertEqual(navigator.preload_state()["save"], 0)

    def test_shutdown_cancels_running_save_without_callback(self):
        source = self.source()
        identity = source_file_identity(source)
        entered = threading.Event()
        release = threading.Event()
        callbacks = []

        def delayed(path, cancellation):
            entered.set()
            release.wait(2)
            cancellation.check()

        navigator = self.navigator(lambda callback, result: callback(result))
        task = CanvasSaveTask(
            CanvasSaveSnapshot((saved_object(source, identity),), True),
            self.directory / "state.json", CanvasSaveCancellation(),
            fingerprinter=delayed, writer=self.writer)
        navigator.request_canvas_save(
            task, ("save", 1), lambda result: callbacks.append(result))
        self.assertTrue(entered.wait(1))
        self.assertTrue(navigator.shutdown())
        self.assertTrue(task.cancellation.canceled)
        release.set()
        for thread in tuple(navigator._owned_threads):
            thread.join(2)
        self.assertEqual(callbacks, [])


class SaveHarness:
    _capture_naming_request = CanvasPanel._capture_naming_request
    _record_naming_success = CanvasPanel._record_naming_success
    _on_save_finished = CanvasPanel._on_save_finished
    _save_status_lines = CanvasPanel._save_status_lines
    cancel_save_operation = CanvasPanel.cancel_save_operation

    def __init__(self):
        self._document_identity = 4
        self._naming_request = 0
        self._last_naming_success = 0
        self._suggested_base = "kept"
        self.refreshes = 0
        self.canceled = []
        self.file_navigator = SimpleNamespace(
            cancel_canvas_save_work=lambda key: self.canceled.append(key))
        cancellation = CanvasSaveCancellation()
        self.save_operation = SaveOperation(
            1, os.path.abspath("new.json"), 1, cancellation, ("save", 1),
            4, 1, True)

    def Refresh(self):
        self.refreshes += 1


class TestCompletionPolicy(unittest.TestCase):
    def test_success_only_naming_and_failed_warning_never_claims_saved(self):
        canvas = SaveHarness()
        failed = CanvasSaveResult(
            canvas.save_operation.destination, "failed", 1, 1,
            (FingerprintWarning("bad.png", "unreadable"),), "disk full", False)
        self.assertTrue(canvas._on_save_finished((1, failed)))
        self.assertEqual(canvas._suggested_base, "kept")
        self.assertEqual(canvas._save_status_lines(canvas.save_operation)[0],
                         "Canvas save failed")

        canvas = SaveHarness()
        success = CanvasSaveResult(
            canvas.save_operation.destination, "success", 1, 1,
            (FingerprintWarning("bad.png", "unreadable"),), committed=True)
        self.assertTrue(canvas._on_save_finished((1, success)))
        self.assertEqual(canvas._suggested_base, "new")
        self.assertEqual(canvas._save_status_lines(canvas.save_operation)[0],
                         "Canvas saved with fingerprint warnings")

    def test_document_replacement_cancel_and_commit_boundary(self):
        canvas = SaveHarness()
        self.assertTrue(canvas.cancel_save_operation(
            clear=False, reason="document replacement started"))
        self.assertTrue(canvas.save_operation.terminal)
        self.assertEqual(canvas.save_operation.stage, "canceled")
        self.assertEqual(canvas.canceled, [("save", 1)])

        canvas = SaveHarness()
        canvas.save_operation.cancellation.replace(
            "unused", "unused", lambda *_: None)
        self.assertTrue(canvas.cancel_save_operation(clear=False))
        self.assertEqual(canvas.save_operation.stage, "success")
        self.assertTrue(canvas.save_operation.committed)


if __name__ == "__main__":
    unittest.main()
