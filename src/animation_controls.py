"""Canvas-only layout and fade state for compact GIF controls."""

from dataclasses import dataclass


ACTIVATION_PADDING_DIP = 32
FADE_DURATION_MS = 1000
FADE_TICK_MS = 16
PANEL_INSET_DIP = 8
PANEL_GAP_DIP = 6
PANEL_HEIGHT_DIP = 36
COMPACT_PANEL_HEIGHT_DIP = 66
BUTTON_SIZE_DIP = 28
BUTTON_GAP_DIP = 3
PANEL_PADDING_DIP = 5
POSITION_WIDTH_DIP = 52
TIMELINE_PREFERRED_WIDTH_DIP = 180
TIMELINE_MIN_WIDTH_DIP = 96
TIMELINE_PREVIEW_DELAY_MS = 100

CONTROL_NAMES = ("previous", "play_pause", "next", "hide")
FOCUS_NAMES = ("previous", "play_pause", "next", "timeline", "hide")
CONTROL_LABELS = {
    "previous": "Previous frame (,)",
    "play_pause": "Play or pause (Space)",
    "next": "Next frame (.)",
    "timeline": "Choose an exact GIF frame",
    "hide": "Hide animation controls",
}


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def contains(self, x, y):
        return self.x <= x < self.right and self.y <= y < self.bottom


@dataclass(frozen=True)
class ControlButton:
    name: str
    rect: Rect


@dataclass(frozen=True)
class AnimationControlLayout:
    object_id: str
    panel: Rect
    activation: Rect
    buttons: tuple
    position_rect: object = None
    timeline_rect: object = None
    timeline_track: object = None
    compact: bool = False

    def button_at(self, x, y):
        return next((button for button in self.buttons
                     if button.rect.contains(x, y)), None)

    def timeline_at(self, x, y):
        return (self.timeline_rect is not None
                and self.timeline_rect.contains(x, y))


@dataclass(frozen=True)
class AnimationControlPresentation:
    frame_label: str
    pending_label: object
    playing: bool
    previous_enabled: bool
    next_enabled: bool
    displayed_index: int
    requested_index: int


def animation_control_presentation(descriptor):
    """Keep committed display and pending seek intent visibly distinct."""
    displayed = int(descriptor.displayed_index)
    requested = int(descriptor.requested_index)
    count = int(descriptor.frame_count)
    return AnimationControlPresentation(
        f"{displayed + 1} / {count}",
        (f"Seeking {requested + 1}" if requested != displayed else None),
        bool(descriptor.playing), requested > 0, requested < count - 1,
        displayed, requested)


def timeline_frame_at(track, pointer_x, frame_count):
    """Map a track pixel to a clamped frame using deterministic half-up rounding."""
    count = max(1, int(frame_count))
    if count == 1:
        return 0
    span = max(1, int(track.width) - 1)
    offset = min(span, max(0, int(pointer_x) - int(track.x)))
    return min(count - 1, (offset * (count - 1) + span // 2) // span)


def timeline_thumb_x(track, frame_index, frame_count):
    """Return the clamped track pixel for a zero-based frame index."""
    count = max(1, int(frame_count))
    span = max(1, int(track.width) - 1)
    index = min(count - 1, max(0, int(frame_index)))
    if count == 1:
        return int(track.x)
    return int(track.x) + (index * span + (count - 1) // 2) // (count - 1)


def _scaled(value, scale):
    return max(1, int(round(value * max(0.25, float(scale)))))


def layout_animation_controls(object_id, object_rect, canvas_size, scale=1.0):
    """Return a clamped panel near the visible bottom of an object."""
    canvas_w, canvas_h = (max(0, int(canvas_size[0])),
                          max(0, int(canvas_size[1])))
    ox, oy, ow, oh = object_rect
    visible_left = max(0, int(ox))
    visible_top = max(0, int(oy))
    visible_right = min(canvas_w, int(ox + ow))
    visible_bottom = min(canvas_h, int(oy + oh))
    if (canvas_w <= 0 or canvas_h <= 0
            or visible_right <= visible_left or visible_bottom <= visible_top):
        return None

    inset = min(_scaled(PANEL_INSET_DIP, scale), canvas_w // 2, canvas_h // 2)
    max_panel_width = max(1, canvas_w - 2 * inset)
    padding = min(_scaled(PANEL_PADDING_DIP, scale),
                  max(0, (max_panel_width - 4) // 10))
    gap = min(_scaled(BUTTON_GAP_DIP, scale),
              max(0, (max_panel_width - 2 * padding - 4) // 3))
    preferred_button = _scaled(BUTTON_SIZE_DIP, scale)
    normal_height = _scaled(PANEL_HEIGHT_DIP, scale)
    compact_height = _scaled(COMPACT_PANEL_HEIGHT_DIP, scale)
    timeline_min = _scaled(TIMELINE_MIN_WIDTH_DIP, scale)
    timeline_preferred = _scaled(TIMELINE_PREFERRED_WIDTH_DIP, scale)
    preferred_essential = 2 * padding + 4 * preferred_button + 4 * gap
    use_compact = max_panel_width < preferred_essential + timeline_min
    requested_height = compact_height if use_compact else normal_height
    height = min(requested_height, max(1, canvas_h - 2 * inset))

    if use_compact:
        action_available = max(4, max_panel_width - 2 * padding - 3 * gap)
        button_size = min(
            preferred_button, max(1, action_available // 4),
            max(1, (height - 3 * padding) // 2))
        panel_w = min(max_panel_width, max(
            2 * padding + 4 * button_size + 3 * gap,
            2 * padding + min(timeline_min, max_panel_width - 2 * padding)))
    else:
        button_size = min(preferred_button, max(1, height - 2 * padding))
        fixed_width = 2 * padding + 4 * button_size + 4 * gap
        timeline_width = min(timeline_preferred,
                             max(timeline_min, max_panel_width - fixed_width))
        panel_w = min(max_panel_width, fixed_width + timeline_width)

    center_x = (visible_left + visible_right) // 2
    panel_x = min(max(inset, center_x - panel_w // 2),
                  max(inset, canvas_w - inset - panel_w))
    outside_y = visible_bottom + _scaled(PANEL_GAP_DIP, scale)
    if outside_y + height <= canvas_h - inset:
        panel_y = outside_y
    else:
        panel_y = visible_bottom - height - _scaled(PANEL_GAP_DIP, scale)
    panel_y = min(max(inset, panel_y), max(inset, canvas_h - inset - height))
    panel = Rect(panel_x, panel_y, panel_w, height)

    cursor = panel_x + padding
    buttons = []
    position_rect = None
    if use_compact:
        control_y = panel_y + padding
        for name in CONTROL_NAMES[:3]:
            buttons.append(ControlButton(
                name, Rect(cursor, control_y, button_size, button_size)))
            cursor += button_size + gap
        buttons.append(ControlButton(
            "hide", Rect(panel.right - padding - button_size, control_y,
                         button_size, button_size)))
        timeline_y = control_y + button_size + padding
        timeline_height = max(1, panel.bottom - padding - timeline_y)
        timeline_rect = Rect(panel.x + padding, timeline_y,
                             max(2, panel.width - 2 * padding), timeline_height)
    else:
        control_y = panel_y + max(0, (height - button_size) // 2)
        for name in CONTROL_NAMES[:3]:
            buttons.append(ControlButton(
                name, Rect(cursor, control_y, button_size, button_size)))
            cursor += button_size + gap
        hide_x = panel.right - padding - button_size
        timeline_rect = Rect(cursor, control_y, max(2, hide_x - gap - cursor),
                             button_size)
        buttons.append(ControlButton(
            "hide", Rect(hide_x, control_y, button_size, button_size)))

    label_height = max(1, min(_scaled(13, scale), timeline_rect.height - 2))
    track_y = timeline_rect.y + label_height
    track_height = max(2, timeline_rect.bottom - track_y)
    track_inset = min(_scaled(5, scale), max(0, (timeline_rect.width - 2) // 4))
    timeline_track = Rect(
        timeline_rect.x + track_inset,
        track_y,
        max(2, timeline_rect.width - 2 * track_inset),
        track_height,
    )

    activation_padding = _scaled(ACTIVATION_PADDING_DIP, scale)
    ax = max(0, panel.x - activation_padding)
    ay = max(0, panel.y - activation_padding)
    ar = min(canvas_w, panel.right + activation_padding)
    ab = min(canvas_h, panel.bottom + activation_padding)
    return AnimationControlLayout(
        str(object_id), panel, Rect(ax, ay, ar - ax, ab - ay),
        tuple(buttons), position_rect, timeline_rect, timeline_track, use_compact)


class AnimationControlState:
    """Runtime-only target, focus, suppression and reversible fade state."""

    def __init__(self, clock):
        self._clock = clock
        self.target_id = None
        self.opacity = 0.0
        self.visible_goal = False
        self.last_update = float(clock())
        self.focus_index = None
        self.hover_name = None
        self.pressed_name = None
        self.suppressed_id = None
        self.timeline_dragging = False
        self.timeline_layout = None
        self.timeline_desired_index = None

    @property
    def transitioning(self):
        goal = 1.0 if self.visible_goal else 0.0
        return abs(self.opacity - goal) > 1e-9

    @property
    def retaining(self):
        return (self.focus_index is not None or self.pressed_name is not None
                or self.timeline_dragging)

    def clear_timeline_drag(self):
        self.timeline_dragging = False
        self.timeline_layout = None
        self.timeline_desired_index = None

    def advance(self, now=None):
        now = float(self._clock() if now is None else now)
        elapsed = max(0.0, now - self.last_update)
        self.last_update = now
        amount = elapsed / (FADE_DURATION_MS / 1000.0)
        previous = self.opacity
        if self.visible_goal:
            self.opacity = min(1.0, self.opacity + amount)
        else:
            self.opacity = max(0.0, self.opacity - amount)
        return self.opacity != previous
