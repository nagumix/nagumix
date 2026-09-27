"""Small shared binding source for canvas onboarding and its actions."""
from dataclasses import dataclass
import sys

import wx


@dataclass(frozen=True)
class Binding:
    key: str
    modifiers: tuple[str, ...] = ()
    # Legacy quit keys accept extra modifiers, except the primary modifier for X.
    ignored_modifiers: tuple[str, ...] = ()

    def resolved_modifiers(self, platform=None):
        platform = sys.platform if platform is None else platform
        return tuple(("Command" if platform == "darwin" else "Ctrl")
                     if name == "Primary" else name for name in self.modifiers)

    def tokens(self, platform=None):
        return (*self.resolved_modifiers(platform), self.key)

    def accelerator(self, platform=None):
        return "+".join("Cmd" if token == "Command" else token
                        for token in self.tokens(platform))

    def matches(self, event, platform=None):
        platform = sys.platform if platform is None else platform
        code = event.GetKeyCode()
        expected = (ord(self.key.upper()) if len(self.key) == 1 else
                    getattr(wx, "WXK_" + {"Esc": "ESCAPE"}.get(
                        self.key, self.key.upper())))
        if 97 <= code <= 122:
            code = ord(chr(code).upper())
        if code != expected:
            return False
        methods = {"Ctrl": "RawControlDown" if platform == "darwin" else "ControlDown",
                   "Command": "MetaDown", "Alt": "AltDown", "Shift": "ShiftDown"}
        required = self.resolved_modifiers(platform)
        ignored = set(self.ignored_modifiers)
        if "NonPrimary" in ignored:
            ignored.add("Ctrl" if platform == "darwin" else "Command")
        return all(name in ignored or
                   bool(getattr(event, method, lambda: False)()) == (name in required)
                   for name, method in methods.items())


ADD_IMAGES_LABEL = "Add Images..."
ADD_IMAGES = Binding("O", ("Primary",))
QUIT = (Binding("Esc", ignored_modifiers=("Ctrl", "Command", "Alt", "Shift")),
        Binding("X", ignored_modifiers=("NonPrimary", "Alt", "Shift")))


def add_images_menu_label():
    return f"{ADD_IMAGES_LABEL}\t{ADD_IMAGES.accelerator()}"


def hint_rows(platform=None):
    """Pairs of (text, outlined keycap), measured by the renderer."""
    def chord(binding):
        result = []
        for token in binding.tokens(platform):
            if result:
                result.append((" + ", False))
            result.append((token, True))
        return result

    add = chord(ADD_IMAGES) + [(" to add images", False)]
    quit_row = []
    for binding in QUIT:
        if quit_row:
            quit_row.append((" or ", False))
        quit_row.extend(chord(binding))
    return add, quit_row + [(" to exit", False)]
