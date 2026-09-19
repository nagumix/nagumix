# src/utils.py

def snap_to_nearby_edges(x, y, w, h, image_objects, canvas_size, threshold=10):
    """
    If the (x,y) is within 'threshold' of another object's edge or the canvas edge, snap to it.
    """
    canvas_w, canvas_h = canvas_size

    # Snap to canvas edges
    if abs(x) < threshold:
        x = 0
    if abs((x + w) - canvas_w) < threshold:
        x = canvas_w - w
    if abs(y) < threshold:
        y = 0
    if abs((y + h) - canvas_h) < threshold:
        y = canvas_h - h

    # Snap to other objects
    for obj in image_objects:
        # We skip the object that is currently moving itself
        # but let's assume we handle that logic outside or skip if w/h match
        # Snap left edge to obj's right edge
        if abs(x - (obj.x + obj.width)) < threshold:
            x = obj.x + obj.width
        # Snap right edge to obj's left edge
        if abs((x + w) - obj.x) < threshold:
            x = obj.x - w
        # Snap top edge to obj's bottom edge
        if abs(y - (obj.y + obj.height)) < threshold:
            y = obj.y + obj.height
        # Snap bottom edge to obj's top edge
        if abs((y + h) - obj.y) < threshold:
            y = obj.y - h

    return x, y


def bytes_to_human_readable(num_bytes):
    """Convert a byte count into a human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} PB"
