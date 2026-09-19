import unittest
import os
import tempfile
import sys
from PIL import Image

# Add parent directory to path to import src modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.image_object import ImageObject  # noqa: E402
from src.utils import snap_to_nearby_edges, bytes_to_human_readable  # noqa: E402


class TestImageObject(unittest.TestCase):
    def setUp(self):
        """Create a temporary test image for each test."""
        self.test_image = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        # Create a simple test image
        img = Image.new('RGB', (100, 50), color='red')
        img.save(self.test_image.name)
        self.test_image.close()

    def tearDown(self):
        """Clean up temporary test image."""
        if os.path.exists(self.test_image.name):
            os.unlink(self.test_image.name)

    def test_image_object_creation(self):
        """Test that ImageObject can be created and has expected default values."""
        obj = ImageObject(self.test_image.name, canvas_width=800, canvas_height=600)
        self.assertEqual(obj.source_path, self.test_image.name)
        self.assertEqual(obj.x, 0)
        self.assertEqual(obj.y, 0)
        self.assertEqual(obj.zoom_factor, 1.0)
        self.assertEqual(obj.viewport_offset, (0, 0))
        self.assertEqual(obj.canvas_w, 800)
        self.assertEqual(obj.canvas_h, 600)

    def test_image_loading(self):
        """Test that images are loaded correctly."""
        obj = ImageObject(self.test_image.name)
        obj.load_image()
        self.assertIsNotNone(obj._original_image)
        self.assertEqual(obj._original_image.size, (100, 50))
        self.assertEqual(obj._aspect_ratio, 2.0)  # 100/50

    def test_reset_size(self):
        """Test that reset_size sets dimensions to original image size."""
        obj = ImageObject(self.test_image.name, canvas_width=800, canvas_height=600)
        obj.width = 50  # Set to non-default
        obj.height = 25
        obj.reset_size(fit_to_canvas=False)
        self.assertEqual(obj.width, 100)  # Original image width
        self.assertEqual(obj.height, 50)  # Original image height

    def test_reset_size_with_canvas_fitting(self):
        """Test reset_size with canvas fitting for oversized images."""
        obj = ImageObject(self.test_image.name, canvas_width=80, canvas_height=40)
        obj.reset_size(fit_to_canvas=True)
        # Image (100x50) should be constrained to canvas (80x40)
        self.assertEqual(obj.width, 80)  # Constrained to canvas width
        self.assertEqual(obj.height, 40)  # Constrained to canvas height
        self.assertEqual(obj.x, 0)  # Positioned at edge
        self.assertEqual(obj.y, 0)

    def test_zoom_in_out(self):
        """Test zoom functionality."""
        obj = ImageObject(self.test_image.name)
        original_zoom = obj.zoom_factor

        obj.zoom_in()
        self.assertGreater(obj.zoom_factor, original_zoom)

        obj.zoom_out()
        self.assertAlmostEqual(obj.zoom_factor, original_zoom, places=6)

    def test_zoom_limits(self):
        """Test zoom limits are respected."""
        obj = ImageObject(self.test_image.name)

        # Test zoom in limit
        for _ in range(20):  # Should hit 5.0 limit
            obj.zoom_in()
        self.assertLessEqual(obj.zoom_factor, 5.0)

        # Test zoom out limit
        obj.zoom_factor = 1.0  # Reset
        for _ in range(20):  # Should hit 0.25 limit
            obj.zoom_out()
        self.assertGreaterEqual(obj.zoom_factor, 0.25)

    def test_contains(self):
        """Test hit detection for mouse clicks."""
        obj = ImageObject(self.test_image.name)
        obj.x, obj.y = 10, 20
        obj.width, obj.height = 100, 50

        # Point inside
        self.assertTrue(obj.contains(50, 40))
        # Point outside
        self.assertFalse(obj.contains(5, 15))
        self.assertFalse(obj.contains(150, 80))

    def test_reset_zoom_and_viewport(self):
        """Test resetting zoom and viewport offset."""
        obj = ImageObject(self.test_image.name)
        obj.zoom_factor = 2.0
        obj.viewport_offset = (10, 5)

        obj.reset_zoom()
        self.assertEqual(obj.zoom_factor, 1.0)

        obj.reset_viewport_offset()
        self.assertEqual(obj.viewport_offset, (0, 0))


class TestUtils(unittest.TestCase):
    def test_bytes_to_human_readable(self):
        """Test byte conversion to human readable format."""
        self.assertEqual(bytes_to_human_readable(512), "512.00 B")
        self.assertEqual(bytes_to_human_readable(1024), "1.00 KB")
        self.assertEqual(bytes_to_human_readable(1024 * 1024), "1.00 MB")
        self.assertEqual(bytes_to_human_readable(1024 * 1024 * 1024), "1.00 GB")

    def test_snap_to_nearby_edges(self):
        """Test edge snapping functionality."""
        # Create mock image objects
        class MockObj:
            def __init__(self, x, y, w, h):
                self.x, self.y, self.width, self.height = x, y, w, h

        objects = [MockObj(100, 100, 50, 50)]
        canvas_size = (800, 600)

        # Test snapping to canvas edge (within threshold)
        x, y = snap_to_nearby_edges(5, 9, 100, 100, objects, canvas_size, threshold=10)
        self.assertEqual(x, 0)  # Should snap to left edge
        self.assertEqual(y, 0)  # Should snap to top edge

        # Test snapping to object edge
        x, y = snap_to_nearby_edges(145, 100, 50, 50, objects, canvas_size, threshold=10)
        self.assertEqual(x, 150)  # Should snap to right edge of object at x=100, width=50


if __name__ == '__main__':
    unittest.main()
