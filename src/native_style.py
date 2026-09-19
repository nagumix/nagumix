"""Shared native dialog palette, typography, painting and appearance lifecycle.

Application-owned dialogs opt in with AppearanceController.attach(). Canvas
painting is deliberately outside this module. OS-owned dialogs stay native.
"""
import logging
import weakref

import wx

from .branding import APP_NAME
from .brand_resources import header_bitmap


def system_uses_dark():
    """Prefer OS app preference, not the currently forced application palette."""
    try:
        appearance = wx.SystemSettings.GetAppearance()
        query = getattr(appearance, "AreAppsDark", appearance.IsDark)
        return bool(query())
    except (AttributeError, NotImplementedError):
        # Platforms without an appearance query use the light dialog palette.
        return False


class AppearanceController:
    """One saved preference; weak subscriptions to owned top-level windows.

    Native appearance is requested at startup, before windows exist, and on
    preference changes. System notifications only resolve/repaint; they never
    write settings or call SetAppearance recursively.
    """
    def __init__(self, app, settings, system_query=system_uses_dark):
        self.app = app
        self.settings = settings
        self.system_query = system_query
        self.mode = None
        self.effective = None
        self.native_result = None
        self.pending_mode = None
        self._windows = weakref.WeakKeyDictionary()
        self._pending = False
        self._closed = False
        self.apply_saved()

    @property
    def palette(self):
        return DARK_COLORS if self.effective == "dark" else LIGHT_COLORS

    def apply_saved(self):
        if self._closed:
            return
        mode = self.settings.get_appearance_mode()
        if mode == self.mode:
            self.pending_mode = None
        elif mode != self.pending_mode:
            try:
                self.native_result = self.app.SetAppearance(
                    getattr(wx.App.Appearance, mode.title()))
            except (AttributeError, NotImplementedError):
                self.native_result = None
            if self.native_result == wx.App.AppearanceResult.Ok:
                self.mode = mode
                self.pending_mode = None
            else:
                # Never combine a new custom palette with old native chrome.
                # Keep the active mode (including System following OS changes)
                # until the saved request can be applied on the next startup.
                self.pending_mode = mode
                logging.warning("Native appearance request %s returned %s; keeping active appearance until restart",
                                mode, self.native_result)
                if self.mode is None:
                    self.mode = "system"
        self._resolve()

    def _resolve(self):
        effective = ("dark" if self.system_query() else "light") if self.mode == "system" else self.mode
        if effective == self.effective:
            return
        self.effective = effective
        for window in tuple(self._windows):
            if window and not window.IsBeingDeleted():
                apply = getattr(window, "apply_dialog_appearance", None)
                if apply:
                    apply(self.palette)

    def attach(self, window):
        """Subscribe a frame for OS signals, or a dialog for signals and styling."""
        if window in self._windows or self._closed:
            return
        reference = weakref.ref(window)

        def destroyed(event):
            target = reference()
            if target is not None and event.GetEventObject() is target:
                self.detach(target)
            event.Skip()

        window.Bind(wx.EVT_SYS_COLOUR_CHANGED, self.on_system_changed)
        window.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        self._windows[window] = destroyed
        apply = getattr(window, "apply_dialog_appearance", None)
        if apply:
            apply(self.palette)

    def detach(self, window):
        destroyed = self._windows.pop(window, None)
        if destroyed:
            window.Unbind(wx.EVT_SYS_COLOUR_CHANGED, handler=self.on_system_changed)
            window.Unbind(wx.EVT_WINDOW_DESTROY, handler=destroyed)

    def on_system_changed(self, event):
        event.Skip()  # wx must propagate native color updates to children.
        if self._closed or self.mode != "system" or self._pending:
            return
        self._pending = True
        wx.CallAfter(self._flush_system_change)

    def _flush_system_change(self):
        self._pending = False
        if not self._closed and self.mode == "system":
            self._resolve()

    def close(self):
        self._closed = True
        for window in tuple(self._windows):
            if window:
                self.detach(window)


def colors(window):
    while window is not None:
        palette = getattr(window, "_dialog_colors", None)
        if palette is not None:
            return palette
        window = window.GetParent()
    return LIGHT_COLORS


def restyle(window):
    palette = colors(window)
    background = getattr(window, "_native_background_role", None)
    foreground = getattr(window, "_native_foreground_role", None)
    if background:
        window.SetBackgroundColour(palette[background])
    if foreground:
        window.SetForegroundColour(palette[foreground])
    for child in window.GetChildren():
        restyle(child)
    window.Refresh()


DARK_COLORS = dict(surface="#212121", side="#181818", control="#2e2e2e",
              ink="#ededed", muted="#ababab", line="#3a3a3a")
LIGHT_COLORS = dict(surface="#ffffff", side="#f4f4f4", control="#ffffff",
                    ink="#202020", muted="#606060", line="#d5d5d5")
SPACING = dict(page=28, group=20, section=32, title=10, sidebar=24,
               gap=12, actions=72)


def dip(win, value):
    return win.FromDIP(value)


def style(win, background="surface"):
    win._native_background_role = background
    win._native_foreground_role = "ink"
    win.SetBackgroundColour(colors(win)[background])
    win.SetForegroundColour(colors(win)["ink"])
    return win


def label(parent, text, size=14, bold=False, muted=False):
    item = wx.StaticText(parent, label=text)
    font = wx.Font(wx.FontInfo(10).FaceName("Segoe UI"))
    font.SetFractionalPointSize(size * .75)
    if bold:
        font.SetWeight(wx.FONTWEIGHT_SEMIBOLD)
    item.SetFont(font)
    item._native_foreground_role = "muted" if muted else "ink"
    item.SetForegroundColour(colors(parent)[item._native_foreground_role])
    return item


class RoundedPanel(wx.Panel):
    """Only the group border is painted; editors are real wx controls."""
    def __init__(self, parent):
        super().__init__(parent)
        style(self)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.paint)

    def paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(colors(self)["side"]))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        gc.SetPen(wx.Pen(colors(self)["line"]))
        gc.SetBrush(wx.Brush(colors(self)["surface"]))
        w, h = self.GetClientSize()
        gc.DrawRoundedRectangle(.5, .5, w - 1, h - 1, dip(self, 10))


class Brand(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, size=dip(parent, (155, 22)))
        style(self)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.paint)

    def paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(colors(self)["surface"]))
        dc.Clear()
        mark_size = dip(self, 20)
        variant = "dark" if colors(self) == DARK_COLORS else "light"
        bitmap = header_bitmap(variant, mark_size)
        font = wx.Font(wx.FontInfo(9).FaceName("Segoe UI"))
        dc.SetFont(font)
        dc.SetTextForeground(colors(self)["ink"])
        if bitmap is not None:
            dc.DrawBitmap(bitmap, 0, 0, True)
        else:
            # A missing bundle asset must not make the brand header disappear.
            dc.SetPen(wx.Pen(colors(self)["muted"]))
            dc.SetBrush(wx.TRANSPARENT_BRUSH)
            dc.DrawRoundedRectangle(0, 0, mark_size, mark_size, dip(self, 5))
            mark_width, mark_height = dc.GetTextExtent("N")
            dc.DrawText("N", (mark_size-mark_width)//2,
                        (mark_size-mark_height)//2)
        _, h = dc.GetTextExtent("N")
        dc.SetTextForeground(colors(self)["muted"])
        dc.DrawText(APP_NAME, dip(self, 28), (dip(self, 20)-h)//2)


class PositionPreview(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, size=dip(parent, (184, 106)))
        self.position = 1
        style(self, "side")
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.paint)

    def paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(colors(self)["side"]))
        dc.Clear()
        w, h = self.GetClientSize()
        dc.SetPen(wx.Pen(colors(self)["line"]))
        dc.SetBrush(wx.Brush(colors(self)["side"]))
        dc.DrawRoundedRectangle(0, 0, w, h, dip(self, 6))
        dc.SetBrush(wx.Brush(colors(self)["control"]))
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.DrawPolygon([(dip(self, 12), h-dip(self, 14)), (w//3, h//3),
                        (w*3//5, h*2//3), (w*4//5, h//2), (w-dip(self, 12), h-dip(self, 14))])
        dc.SetFont(wx.Font(wx.FontInfo(7).FaceName("Segoe UI")))
        dc.SetTextForeground(colors(self)["ink"])
        tw, th = dc.GetTextExtent("Frame 1 / 24")
        bw, bh = tw+dip(self, 14), th+dip(self, 6)
        row, col = divmod(self.position, 3)
        x = [dip(self, 8), (w-bw)//2, w-bw-dip(self, 8)][col]
        y = [dip(self, 8), (h-bh)//2, h-bh-dip(self, 8)][row]
        dc.DrawRoundedRectangle(x, y, bw, bh, dip(self, 4))
        dc.DrawText("Frame 1 / 24", x+dip(self, 7), y+dip(self, 3))
