import json
import os
import tempfile
import unittest

from PIL import Image

from src.canvas_panel import CanvasPanel, ImageObjectList
from src.image_object import ImageObject


class CanvasStub:
    """Headless surface for exercising CanvasPanel's object/state methods."""

    def __init__(self, size=(4, 2)):
        self.image_objects = ImageObjectList()
        self.selected_object = None
        self.marked_object = None
        self.drag_offset = None
        self.canvas_bg = "#FFFFFF"
        self._size = size
        self.refresh_count = 0

    def GetSize(self):
        return self._size

    def Refresh(self):
        self.refresh_count += 1


class TestDuplicateObjectIdentity(unittest.TestCase):
    def setUp(self):
        self.paths_to_remove = []
        image_file = tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=False,
            dir=os.path.dirname(__file__),
        )
        self.image_path = image_file.name
        image_file.close()
        self.paths_to_remove.append(self.image_path)
        image = Image.new("RGB", (4, 2), "red")
        for x in (2, 3):
            for y in (0, 1):
                image.putpixel((x, y), (0, 128, 0))
        image.save(self.image_path)

    def tearDown(self):
        for path in self.paths_to_remove:
            if os.path.exists(path):
                os.unlink(path)

    def _temporary_path(self, suffix):
        temp_file = tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
            dir=os.path.dirname(__file__),
        )
        path = temp_file.name
        temp_file.close()
        self.paths_to_remove.append(path)
        return path

    def _make_duplicate(self, x, viewport_offset):
        obj = ImageObject(self.image_path, canvas_width=4, canvas_height=2)
        obj.x = x
        obj.y = 0
        obj.width = 2
        obj.height = 2
        obj.zoom_factor = 1.0
        obj.viewport_offset = viewport_offset
        return obj

    def test_same_source_objects_have_distinct_stable_identity(self):
        first = self._make_duplicate(0, (0, 0))
        second = self._make_duplicate(2, (2, 0))

        self.assertNotEqual(first.object_id, second.object_id)
        self.assertNotEqual(first, second)

        original_id = first.object_id
        first.change_source_path(os.path.join(os.path.dirname(self.image_path), "other.png"))
        self.assertEqual(first.object_id, original_id)

    def test_navigating_two_objects_to_same_path_preserves_independence(self):
        first = ImageObject(os.path.join(os.path.dirname(self.image_path), "first.png"))
        second = ImageObject(os.path.join(os.path.dirname(self.image_path), "second.png"))
        first.x, first.viewport_offset = 10, (0, 0)
        second.x, second.viewport_offset = 20, (2, 0)
        identities = (first.object_id, second.object_id)

        with Image.open(self.image_path) as source:
            first.change_source_path(self.image_path, source)
            second.change_source_path(self.image_path, source)

        self.assertEqual((first.object_id, second.object_id), identities)
        self.assertIsNot(first._original_image, second._original_image)
        self.assertEqual(first.x, 10)
        self.assertEqual(second.x, 20)
        self.assertEqual(first.viewport_offset, (0, 0))
        self.assertEqual(second.viewport_offset, (2, 0))

    def test_selection_and_reordering_target_the_chosen_duplicate(self):
        first = self._make_duplicate(0, (0, 0))
        second = self._make_duplicate(2, (2, 0))
        third = self._make_duplicate(1, (0, 0))
        canvas = CanvasStub()
        canvas.image_objects.extend([first, second, third])
        canvas.selected_object = second

        self.assertIs(CanvasPanel.get_selected_object(canvas), second)
        canvas.image_objects.move_to_front(second)

        self.assertEqual(canvas.image_objects, [first, third, second])
        self.assertIs(canvas.image_objects[-1], second)
        self.assertEqual(len({id(obj) for obj in canvas.image_objects}), 3)

    def test_same_instance_or_id_cannot_be_added_twice(self):
        obj = self._make_duplicate(0, (0, 0))
        collection = ImageObjectList()
        collection.append(obj)

        with self.assertRaises(ValueError):
            collection.append(obj)

        duplicate_id = ImageObject(self.image_path, object_id=obj.object_id)
        with self.assertRaises(ValueError):
            collection.append(duplicate_id)
        with self.assertRaises(ValueError):
            ImageObjectList([obj, obj])

    def test_delete_clears_selected_and_marked_references_and_cancels_work(self):
        first = self._make_duplicate(0, (0, 0))
        second = self._make_duplicate(2, (2, 0))
        canvas = CanvasStub()
        canvas.image_objects.extend([first, second])
        canvas.selected_object = second
        canvas.marked_object = second
        generation = second._work_generation

        removed = CanvasPanel.remove_image_object(canvas, second)

        self.assertTrue(removed)
        self.assertEqual(canvas.image_objects, [first])
        self.assertIsNone(canvas.selected_object)
        self.assertIsNone(canvas.marked_object)
        self.assertEqual(second._work_generation, generation + 1)

    def test_swap_allows_same_source_but_rejects_stale_mark(self):
        first = self._make_duplicate(0, (0, 0))
        second = self._make_duplicate(2, (2, 0))
        canvas = CanvasStub()
        canvas.image_objects.extend([first, second])

        self.assertTrue(CanvasPanel.swap_image_objects(canvas, first, second))
        self.assertEqual((first.x, first.viewport_offset), (2, (2, 0)))
        self.assertEqual((second.x, second.viewport_offset), (0, (0, 0)))

        stale_mark = self._make_duplicate(3, (0, 0))
        self.assertFalse(CanvasPanel.swap_image_objects(canvas, first, stale_mark))

    def test_duplicate_transforms_round_trip_and_export_colored_landmarks(self):
        first = self._make_duplicate(0, (0, 0))
        second = self._make_duplicate(2, (2, 0))
        canvas = CanvasStub()
        canvas.image_objects.extend([first, second])
        state_path = self._temporary_path(".json")
        export_path = self._temporary_path(".png")

        CanvasPanel.save_canvas_state(canvas, state_path)
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)

        expected_keys = {
            "source_path", "x", "y", "width", "height",
            "zoom_factor", "viewport_offset", "source_pixel_normalization",
        }
        self.assertEqual(len(state), 2)
        self.assertEqual(set(state[0]), expected_keys)
        self.assertEqual(set(state[1]), expected_keys)
        self.assertEqual(state[0]["source_pixel_normalization"], 1)
        self.assertEqual(state[0]["source_path"], state[1]["source_path"])

        loaded_canvas = CanvasStub()
        CanvasPanel.load_canvas_state(loaded_canvas, state_path)
        self.assertEqual(len(loaded_canvas.image_objects), 2)
        self.assertNotEqual(
            loaded_canvas.image_objects[0].object_id,
            loaded_canvas.image_objects[1].object_id,
        )
        self.assertEqual(
            [obj.viewport_offset for obj in loaded_canvas.image_objects],
            [(0, 0), (2, 0)],
        )

        CanvasPanel.export_to_file(loaded_canvas, export_path)
        with Image.open(export_path) as exported:
            self.assertEqual(exported.getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(exported.getpixel((3, 0)), (0, 128, 0, 255))


if __name__ == "__main__":
    unittest.main()
