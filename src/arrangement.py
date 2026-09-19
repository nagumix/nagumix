"""Transactional canvas arrangement algorithms."""

import math

from .image_geometry import fit_geometry


def _valid_layout_inputs(canvas_size, spacing, outer_margin):
    if not isinstance(canvas_size, (tuple, list)) or len(canvas_size) != 2:
        return False
    values = (*canvas_size, spacing, outer_margin)
    return (
        all(type(value) is int for value in values)
        and canvas_size[0] > 0
        and canvas_size[1] > 0
        and spacing >= 0
        and outer_margin >= 0
    )


def arrange_no_resize(image_objects, canvas_size, spacing=10, outer_margin=10):
    """Flow images without resizing, or leave every transform unchanged."""
    if not _valid_layout_inputs(canvas_size, spacing, outer_margin):
        return False
    if not image_objects:
        return True

    canvas_w, canvas_h = canvas_size
    usable_w = canvas_w - (2 * outer_margin)
    usable_h = canvas_h - (2 * outer_margin)
    if usable_w < 1 or usable_h < 1:
        return False

    proposed = []
    current_x = outer_margin
    current_y = outer_margin
    row_height = 0

    for obj in image_objects:
        width, height = obj.width, obj.height
        if (type(width) is not int or type(height) is not int
                or width <= 0 or height <= 0
                or width > usable_w or height > usable_h):
            return False

        if current_x != outer_margin and current_x + width > canvas_w - outer_margin:
            current_x = outer_margin
            current_y += row_height + spacing
            row_height = 0

        if current_y + height > canvas_h - outer_margin:
            return False

        proposed.append((obj, current_x, current_y))
        current_x += width + spacing
        row_height = max(row_height, height)

    for obj, x, y in proposed:
        obj.x = x
        obj.y = y
    return True


def _integer_cells(origin, usable_length, count, spacing):
    """Allocate remainder pixels from the leading cells deterministically."""
    base, remainder = divmod(usable_length, count)
    sizes = [base + (index < remainder) for index in range(count)]
    starts = []
    position = origin
    for size in sizes:
        starts.append(position)
        position += size + spacing
    return starts, sizes


def arrange_with_resize(image_objects, canvas_size, spacing=10, outer_margin=10):
    """Fit whole images into the existing grid strategy without enlargement.

    Margins and inter-cell gaps are reserved before cell sizes are calculated.
    Invalid geometry or an unreadable source returns False without changing any
    position, size, zoom, or crop transform.
    """
    if not _valid_layout_inputs(canvas_size, spacing, outer_margin):
        return False
    if not image_objects:
        return True

    canvas_w, canvas_h = canvas_size
    count = len(image_objects)
    grid_cols = int(math.ceil(math.sqrt(count)))
    grid_rows = int(math.ceil(count / grid_cols))
    usable_w = canvas_w - (2 * outer_margin) - ((grid_cols - 1) * spacing)
    usable_h = canvas_h - (2 * outer_margin) - ((grid_rows - 1) * spacing)
    if usable_w < grid_cols or usable_h < grid_rows:
        return False

    column_starts, column_widths = _integer_cells(
        outer_margin, usable_w, grid_cols, spacing)
    row_starts, row_heights = _integer_cells(
        outer_margin, usable_h, grid_rows, spacing)

    proposed = []
    for index, obj in enumerate(image_objects):
        obj.load_image()
        if obj._original_image is None:
            return False
        row, column = divmod(index, grid_cols)
        geometry = fit_geometry(
            obj._original_image.size,
            (column_widths[column], row_heights[row]),
        )
        if geometry is None:
            return False
        zoom, width, height = geometry
        x = column_starts[column] + (column_widths[column] - width) // 2
        y = row_starts[row] + (row_heights[row] - height) // 2
        proposed.append((obj, x, y, width, height, zoom))

    for obj, x, y, width, height, zoom in proposed:
        obj.set_canvas_size(canvas_w, canvas_h)
        obj.x, obj.y = x, y
        obj.width, obj.height = width, height
        obj.zoom_factor = zoom
        obj._minimum_zoom = min(0.25, zoom)
        obj.viewport_offset = (0, 0)
        obj._clear_image_caches()
    return True
