import unittest
import os
import tempfile
import json
import sys
import shutil

# Add parent directory to path to import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.arrangement import arrange_no_resize, arrange_with_resize  # noqa: E402
from src.settings_manager import SettingsManager  # noqa: E402
from src.file_navigator import FileNavigator  # noqa: E402
from src.image_object import ImageObject
from PIL import Image


class TestArrangement(unittest.TestCase):
    def setUp(self):
        """Use real image objects to exercise production fitting."""
        self.objects = []
        for size in ((50, 50), (75, 60), (100, 80)):
            obj = ImageObject("in-memory")
            obj._original_image = Image.new("RGB", size, "red")
            obj.width, obj.height = size
            self.objects.append(obj)
        self.canvas_size = (80, 60)

    def test_arrange_no_resize(self):
        """Test that arrange_no_resize positions objects without changing size."""
        original_sizes = [(obj.width, obj.height) for obj in self.objects]

        self.assertTrue(arrange_no_resize(self.objects, (300, 200)))

        # Check that sizes haven't changed
        for i, obj in enumerate(self.objects):
            self.assertEqual((obj.width, obj.height), original_sizes[i])

        # Check that objects are positioned (not all at 0,0)
        positions = [(obj.x, obj.y) for obj in self.objects]
        unique_positions = set(positions)
        self.assertGreater(len(unique_positions), 1, "Objects should be positioned differently")

    def test_arrange_with_resize(self):
        """Test that arrange_with_resize changes object dimensions."""
        original_sizes = [(obj.width, obj.height) for obj in self.objects]

        arrange_with_resize(self.objects, self.canvas_size)

        # At least some objects should have different sizes
        new_sizes = [(obj.width, obj.height) for obj in self.objects]
        self.assertNotEqual(original_sizes, new_sizes, "Sizes should change with resize arrangement")

        # All objects should fit within canvas bounds
        for obj in self.objects:
            self.assertLessEqual(obj.x + obj.width, self.canvas_size[0])
            self.assertLessEqual(obj.y + obj.height, self.canvas_size[1])

    def test_empty_object_list(self):
        """Test that arrangement functions handle empty object lists gracefully."""
        empty_objects = []

        # Should not raise exceptions
        arrange_no_resize(empty_objects, self.canvas_size)
        arrange_with_resize(empty_objects, self.canvas_size)

        self.assertEqual(len(empty_objects), 0)


class TestSettingsManager(unittest.TestCase):
    def setUp(self):
        """Set up a temporary directory for settings testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        os.chdir(self.original_cwd)
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_settings_manager_defaults(self):
        """Test that SettingsManager provides expected defaults."""
        manager = SettingsManager()

        # Should have default canvas background color
        bg_color = manager.get_setting("Canvas", "background_color", "#000000")
        self.assertEqual(bg_color, "#303030")

    def test_background_default_preserves_explicit_preferences(self):
        for contents, expected in (
                ("[Canvas]\n", "#303030"),
                ("[Canvas]\nbackground_color = #FFFFFF\n", "#FFFFFF"),
                ("[Canvas]\nbackground_color = #123456\n", "#123456")):
            with self.subTest(expected=expected):
                with open("nagumix_settings.ini", "w", encoding="utf-8") as stream:
                    stream.write(contents)
                manager = SettingsManager()
                self.assertEqual(manager.get_dialog_draft()["background"], expected)
                with open("nagumix_settings.ini", encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), contents)

    def test_settings_persistence(self):
        """Test that settings can be saved and loaded."""
        manager = SettingsManager()

        # Set a custom value
        manager.set_setting("Test", "key", "value")
        manager.save()

        # Create new manager instance (simulates restart)
        manager2 = SettingsManager()
        value = manager2.get_setting("Test", "key", "default")

        self.assertEqual(value, "value")

    def test_fallback_values(self):
        """Test that fallback values work when settings don't exist."""
        manager = SettingsManager()

        value = manager.get_setting("NonExistent", "key", "fallback")
        self.assertEqual(value, "fallback")


class TestCanvasStateManagement(unittest.TestCase):
    def setUp(self):
        """Set up temporary files for state testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.temp_dir, "test_state.json")

    def tearDown(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_canvas_state_format(self):
        """Test that canvas state has expected JSON structure."""
        # Create a mock canvas state
        mock_state = [
            {
                "source_path": "/path/to/image1.png",
                "x": 10,
                "y": 20,
                "width": 100,
                "height": 50,
                "zoom_factor": 1.5,
                "viewport_offset": [0, 0]
            },
            {
                "source_path": "/path/to/image2.png",
                "x": 150,
                "y": 80,
                "width": 200,
                "height": 100,
                "zoom_factor": 1.0,
                "viewport_offset": [10, 5]
            }
        ]

        # Save state to file
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(mock_state, f, indent=2)

        # Load and verify structure
        with open(self.state_file, "r", encoding="utf-8") as f:
            loaded_state = json.load(f)

        self.assertEqual(len(loaded_state), 2)

        # Check required fields
        required_fields = ["source_path", "x", "y", "width", "height", "zoom_factor", "viewport_offset"]
        for obj_data in loaded_state:
            for field in required_fields:
                self.assertIn(field, obj_data, f"Field '{field}' should be present in state data")


class TestFileNavigator(unittest.TestCase):
    def setUp(self):
        """Set up test environment for file navigation."""
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)
        self.settings_manager = SettingsManager()
        self.settings_manager.set_setting("Navigation", "sort_method", "name_asc")
        self.file_navigator = FileNavigator(self.settings_manager)

        # Use real, fully decodable image files for navigation/preloading.
        self.test_files = [
            "image_01.jpg",
            "image_02.png",
            "image_03.gif",
            "picture_a.jpg",
            "picture_z.png"
        ]

        for index, filename in enumerate(self.test_files):
            filepath = os.path.join(self.temp_dir, filename)
            image = Image.new("RGBA", (4, 3), (index * 30, 20, 200, 255))
            if filename.endswith(".jpg"):
                image.convert("RGB").save(filepath)
            else:
                image.save(filepath)

    def tearDown(self):
        """Clean up test files."""
        self.file_navigator.shutdown()
        self.assertTrue(self.file_navigator.wait_for_workers(2.0))
        os.chdir(self.original_cwd)
        shutil.rmtree(self.temp_dir)

    def test_get_files_in_directory(self):
        """Test getting files in directory with different sort methods."""
        test_file = os.path.join(self.temp_dir, self.test_files[0])

        # Test name ascending (default)
        self.settings_manager.set_setting("Navigation", "sort_method", "name_asc")
        self.file_navigator.clear_cache()  # Clear cache to pick up new setting
        files, index = self.file_navigator.get_files_in_directory(test_file)

        self.assertEqual(len(files), 5)
        # Should be sorted by name
        basenames = [os.path.basename(f) for f in files]
        self.assertEqual(basenames[0], "image_01.jpg")
        self.assertEqual(basenames[-1], "picture_z.png")

    def test_navigation_methods(self):
        """Test next/previous file navigation."""
        test_file = os.path.join(self.temp_dir, "image_02.png")

        # Test next file
        result = self.file_navigator.get_next_file(test_file)
        self.assertIsNotNone(result)
        next_file, is_wraparound = result
        self.assertIsNotNone(next_file)
        self.assertTrue(os.path.basename(next_file) in self.test_files)
        self.assertFalse(is_wraparound)  # Should not wrap for middle file

        # Test previous file
        result = self.file_navigator.get_previous_file(test_file)
        self.assertIsNotNone(result)
        prev_file, is_wraparound = result
        self.assertIsNotNone(prev_file)
        self.assertTrue(os.path.basename(prev_file) in self.test_files)
        self.assertFalse(is_wraparound)  # Should not wrap for middle file

        # Should wrap around at boundaries
        first_file = os.path.join(self.temp_dir, "image_01.jpg")
        result = self.file_navigator.get_previous_file(first_file)
        self.assertIsNotNone(result)
        prev_of_first, is_wraparound = result
        self.assertIsNotNone(prev_of_first)
        self.assertTrue(is_wraparound)  # Should wrap around at boundary
        # Should be the last file in the sorted list
        self.assertEqual(os.path.basename(prev_of_first), "picture_z.png")

    def test_sort_methods(self):
        """Test different file sorting methods."""
        test_file = os.path.join(self.temp_dir, self.test_files[0])

        # Test name descending
        self.settings_manager.set_setting("Navigation", "sort_method", "name_desc")
        files, _ = self.file_navigator.get_files_in_directory(test_file)
        basenames = [os.path.basename(f) for f in files]
        self.assertEqual(basenames[0], "picture_z.png")
        self.assertEqual(basenames[-1], "image_01.jpg")

    def test_preload_limits(self):
        """Production neighbor preloading completes within its fixed bounds."""
        test_file = os.path.join(self.temp_dir, self.test_files[0])

        # Test with high preload count - should be limited
        self.settings_manager.set_setting("Navigation", "preload_count", "10")

        self.assertTrue(self.file_navigator.start_preloading(test_file))
        self.assertTrue(self.file_navigator.wait_for_workers(2.0))
        state = self.file_navigator.preload_state()
        self.assertLessEqual(state["active"], self.file_navigator.MAX_ACTIVE_DECODES)
        self.assertLessEqual(state["pending"], self.file_navigator.MAX_PENDING_PRELOADS)
        self.assertEqual(state["jobs"], 0)

    def test_preloaded_image_isolation(self):
        """Test that preloaded images are properly isolated between objects."""
        current_file = os.path.join(self.temp_dir, self.test_files[0])
        test_file = os.path.join(self.temp_dir, self.test_files[1])

        # Preload a real neighbor rather than requesting the excluded current file.
        self.assertTrue(self.file_navigator.start_preloading(current_file))
        self.assertTrue(self.file_navigator.wait_for_workers(2.0))

        # Get two copies of the preloaded image
        img1 = self.file_navigator.get_preloaded_image(test_file)
        img2 = self.file_navigator.get_preloaded_image(test_file)

        self.assertIsNotNone(img1)
        self.assertIsNotNone(img2)
        self.assertIsNot(img1, img2, "Preloaded images should be separate instances")
        img1.putpixel((0, 0), (1, 2, 3, 4))
        self.assertNotEqual(img1.getpixel((0, 0)), img2.getpixel((0, 0)))
        img1.close()
        img2.close()

    def test_cache_invalidation(self):
        """Test that cache can be cleared and invalidated."""
        test_file = os.path.join(self.temp_dir, self.test_files[0])

        # Get files to populate cache
        self.file_navigator.get_files_in_directory(test_file)

        # Clear cache
        self.file_navigator.clear_cache()

        # Should work after cache clear
        files, index = self.file_navigator.get_files_in_directory(test_file)
        self.assertEqual(len(files), 5)


if __name__ == '__main__':
    unittest.main()
