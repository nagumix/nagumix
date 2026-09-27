"""Native, paint-only empty-canvas guidance; never part of document state."""
import wx

from . import canvas_bindings


def draw_empty_canvas(dc, size, background, *, feedback=False):
    width, height = size
    if width < 40 or height < 40:
        return
    old_font, old_color = dc.GetFont(), dc.GetTextForeground()
    try:
        _draw(dc, width, height, background, feedback)
    finally:
        # Status cards following the guide keep their normal native font.
        dc.SetFont(old_font)
        dc.SetTextForeground(old_color)


def _draw(dc, width, height, background, feedback):
    luminance = (background.Red() * .299 + background.Green() * .587
                 + background.Blue() * .114)
    target = 255 if luminance < 145 else 0

    def color(amount):
        return wx.Colour(*(round(value + (target - value) * amount)
                           for value in background.Get()[:3]))

    def font(pixels, bold=False):
        result = wx.Font(wx.FontInfo(12).Family(wx.FONTFAMILY_SWISS))
        result.SetPixelSize(wx.Size(0, max(1, round(pixels))))
        if bold:
            result.SetWeight(wx.FONTWEIGHT_BOLD)
        dc.SetFont(result)

    heading = "Drop images here"
    subtitle = "or right-click → " + canvas_bindings.ADD_IMAGES_LABEL
    rows = canvas_bindings.hint_rows()
    # Measure actual binding names, including multi-modifier/long-name chords.
    font(64, True)
    natural_width = dc.GetTextExtent(heading).width
    font(30)
    natural_width = max(natural_width, dc.GetTextExtent(subtitle).width,
                        *(sum(dc.GetTextExtent(text).width + (20 if key else 0)
                              for text, key in row) for row in rows))
    scale = min(1.6, (width * .84 - 32) / natural_width, height * .78 / 430)
    scale = max(.03, scale)
    box_width = min(width * .88, (natural_width + 96) * scale)
    box_height = 430 * scale
    left, top = (width - box_width) / 2, (height - box_height) / 2
    dc.SetBrush(wx.TRANSPARENT_BRUSH)
    dc.SetPen(wx.Pen(color(.30), max(1, round(scale)), wx.PENSTYLE_SHORT_DASH))
    dc.DrawRoundedRectangle(round(left), round(top), round(box_width),
                            round(box_height), max(1, round(16 * scale)))

    def line(points):
        dc.DrawLines([wx.Point(round(width / 2 + x * scale),
                              round(top + y * scale)) for x, y in points])

    dc.SetPen(wx.Pen(color(.55), max(1, round(3 * scale))))
    line([(-67, 143), (-88, 53), (18, 28), (28, 61)])
    line([(-55, 63), (59, 63), (59, 151), (-55, 151), (-55, 63)])
    line([(-55, 139), (-18, 104), (8, 131), (20, 119), (30, 129)])
    dc.DrawCircle(round(width / 2 + 25 * scale), round(top + 88 * scale),
                  max(1, round(8 * scale)))
    dc.SetBrush(wx.Brush(background))
    dc.DrawCircle(round(width / 2 + 60 * scale), round(top + 143 * scale),
                  max(1, round(25 * scale)))
    line([(60, 130), (60, 156)])
    line([(47, 143), (73, 143)])

    def centered(text, y, pixels, bold=False):
        font(pixels * scale, bold)
        dc.SetTextForeground(color(.90 if bold else .75))
        dc.DrawText(text, round((width - dc.GetTextExtent(text).width) / 2),
                    round(top + y * scale))

    centered(heading, 188, 64, True)
    if feedback:
        # Operation cards are drawn afterwards, above the decorative guidance.
        return
    centered(subtitle, 273, 30)
    font(27 * scale)
    dc.SetTextForeground(color(.80))
    dc.SetPen(wx.Pen(color(.48), max(1, round(2 * scale))))
    dc.SetBrush(wx.TRANSPARENT_BRUSH)
    for row, y in zip(rows, (332, 379)):
        extents = [dc.GetTextExtent(text) for text, _ in row]
        pad = max(1, round(10 * scale))
        widths = [extent.width + (2 * pad if key else 0)
                  for extent, (_, key) in zip(extents, row)]
        x = (width - sum(widths)) / 2
        for (text, key), extent, item_width in zip(row, extents, widths):
            text_y = round(top + y * scale)
            if key:
                dc.DrawRoundedRectangle(round(x), text_y - round(4 * scale),
                                        item_width, extent.height + round(8 * scale),
                                        max(1, round(6 * scale)))
            dc.DrawText(text, round(x + (pad if key else 0)), text_y)
            x += item_width
