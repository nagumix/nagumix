"""Approved native settings layout, with draft-only lazy pages."""
import configparser

import wx
from wx.lib.colourselect import ColourSelect, EVT_COLOURSELECT

from .settings_manager import APPEARANCE_MODES, OBJECT_INFO_POSITIONS, SORT_METHODS, CANVAS_BACKGROUND_DEFAULT
from .brand_resources import set_window_icon
from .legal_resources import (
    LEGAL_DOCUMENTS, LICENSE_IDENTIFIER, PUBLIC_SOURCE_ORGANIZATION_URL,
    read_legal_resource,
)
from .native_style import (
    DARK_COLORS, LIGHT_COLORS, SPACING, Brand, PositionPreview, RoundedPanel,
    colors, dip, label, restyle, style, system_uses_dark,
)

OBJECT_INFO_POSITION_LABELS = (
    "Top left", "Top center", "Top right",
    "Middle left", "Center", "Middle right",
    "Bottom left", "Bottom center", "Bottom right",
)


SORT_METHOD_LABELS = (
    "Natural Order (ascending)",
    "Natural Order (descending)",
    "Alphabetical (A-Z)",
    "Alphabetical (Z-A)",
    "Modification Date (oldest first)",
    "Modification Date (newest first)",
    "File Size (smallest first)",
    "File Size (largest first)",
)


CHOICE_VALUES = {"mode": APPEARANCE_MODES, "sort": SORT_METHODS,
                 "position": OBJECT_INFO_POSITIONS}
POSITIONS = OBJECT_INFO_POSITION_LABELS
SORTS = SORT_METHOD_LABELS


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, settings_manager):
        super().__init__(parent, title="Settings",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        set_window_icon(self)
        self.settings_manager = settings_manager
        self.saved = settings_manager.get_dialog_draft()
        self.draft = dict(self.saved)
        controller = getattr(wx.GetApp(), "dialog_appearance", None)
        self.appearance_controller = controller
        mode = settings_manager.get_appearance_mode()
        self._dialog_colors = (controller.palette if controller else
                               DARK_COLORS if mode == "dark" or
                               (mode == "system" and system_uses_dark()) else LIGHT_COLORS)
        style(self)
        self.SetClientSize(dip(self, (920, 760)))
        display_index = wx.Display.GetFromWindow(parent) if parent else wx.NOT_FOUND
        work = wx.Display(0 if display_index == wx.NOT_FOUND else display_index).GetClientArea()
        self.SetSize((min(self.GetSize().width, work.width),
                      min(self.GetSize().height, work.height)))
        self.SetMinClientSize((min(dip(self, 640), self.GetClientSize().width),
                               min(dip(self, 520), self.GetClientSize().height)))
        self.CentreOnParent() if parent else self.CentreOnScreen()
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetAffirmativeId(wx.ID_OK)
        self.controls = {}
        self.built_pages = set()
        self.reflow_pending = False
        self.responsive_rows = []
        outer = wx.BoxSizer(wx.HORIZONTAL)
        self.sidebar = style(wx.Panel(self))
        self.sidebar.SetMinSize(dip(self, (200, -1)))
        side = wx.BoxSizer(wx.VERTICAL)
        side.Add(Brand(self.sidebar), 0, wx.LEFT | wx.TOP, dip(self, SPACING["sidebar"]))
        side.Add(label(self.sidebar, "Settings", 20, True), 0, wx.ALL, dip(self, SPACING["sidebar"]))
        self.nav = []
        self.book = wx.Simplebook(self)
        for index, name in enumerate([
                "Appearance", "Navigation", "Arrangement", "Advanced", "Licensing"]):
            button = wx.Button(self.sidebar, label=name, style=wx.BORDER_NONE | wx.BU_LEFT)
            style(button)
            button.SetMinSize(dip(self, (-1, 40)))
            button.Bind(wx.EVT_BUTTON, lambda e, i=index: self.select_page(i))
            side.Add(button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, dip(self, 12))
            self.nav.append(button)
        self.sidebar.SetSizer(side)
        outer.Add(self.sidebar, 0, wx.EXPAND)
        right = wx.BoxSizer(wx.VERTICAL)
        self.pages = []
        self.wrapped = []
        specs = [("Appearance", "Choose dialog appearance, the backdrop and on-image information."),
                 ("Navigation", "Choose how you move through images in the same folder."),
                 ("Arrangement", "Set the space around images when you use Arrange All."),
                 ("Advanced", "Fine-tune image loading and saved canvas files."),
                 ("Licensing", "Read, select and copy NaguMIX licensing information offline.")]
        for name, description in specs:
            page = style(wx.ScrolledWindow(self.book), "side")
            page.SetScrollRate(0, dip(self, 16))
            content = wx.BoxSizer(wx.VERTICAL)
            content.Add(label(page, name, 23, True), 0, wx.BOTTOM, dip(self, 6))
            self.help(page, content, description, 13)
            content.AddSpacer(dip(self, 28))
            self.pages.append((page, content))
            self.book.AddPage(page, name)
        for page, content in self.pages:
            padded = wx.BoxSizer(wx.VERTICAL)
            padded.Add(content, 1, wx.EXPAND | wx.ALL, dip(self, SPACING["page"]))
            page.SetSizer(padded)
        right.Add(self.book, 1, wx.EXPAND)
        footer = style(wx.Panel(self))
        footer.SetMinSize(dip(self, (-1, SPACING["actions"])))
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.note = label(footer, "Changes take effect when you save.", 11, muted=True)
        actions.Add(self.note, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, dip(self, 24))
        cancel = style(wx.Button(footer, wx.ID_CANCEL, "Cancel"))
        save = wx.Button(footer, wx.ID_OK, "Save")
        save.SetDefault()
        for button in (cancel, save):
            button.SetMinSize(dip(self, (73, 36)))
            actions.Add(button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, dip(self, 8))
        actions.AddSpacer(dip(self, 16))
        self.cancel_button, self.save_button = cancel, save
        self.Bind(wx.EVT_BUTTON, self.on_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)
        footer.SetSizer(actions)
        right.Add(footer, 0, wx.EXPAND)
        outer.Add(right, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.Bind(wx.EVT_SIZE, self.resize)
        self.select_page(0)
        self.Layout()
        if controller:
            controller.attach(self)

    def help(self, parent, sizer, text, size=12):
        item = label(parent, text, size, muted=True)
        # A long unwrapped best width must not become the scroller's minimum
        # width. Height is recomputed from the actual available row width.
        item.SetMinSize((1, -1))
        sizer.Add(item, 0, wx.EXPAND | wx.TOP, dip(self, 5))
        self.wrapped.append((item, text))
        return item

    def group(self, index, title):
        page, content = self.pages[index]
        content.Add(label(page, title, 13, True), 0, wx.BOTTOM, dip(self, SPACING["title"]))
        panel = RoundedPanel(page)
        body = wx.BoxSizer(wx.VERTICAL)
        inset = wx.BoxSizer(wx.VERTICAL)
        inset.Add(body, 1, wx.EXPAND | wx.ALL, dip(self, SPACING["group"]))
        panel.SetSizer(inset)
        content.Add(panel, 0, wx.EXPAND | wx.BOTTOM, dip(self, SPACING["section"]))
        return panel, body

    def field(self, panel, body, key, title, help_text, kind="number", choices=None,
              minimum=0, maximum=1000, unit=None):
        body.Add(label(panel, title, 14, True), 0)
        self.help(panel, body, help_text)
        body.AddSpacer(dip(self, 12))
        if kind == "choice":
            control = wx.Choice(panel, choices=choices)
            control.SetSelection(CHOICE_VALUES[key].index(self.draft[key]))
        elif kind == "text":
            control = wx.TextCtrl(panel, value=self.draft[key], size=dip(self, (125, 36)))
        elif kind == "bool":
            control = wx.CheckBox(panel, label="Enabled")
            control.SetValue(self.draft[key])
        else:
            control = wx.SpinCtrl(panel, min=minimum, max=maximum, initial=self.draft[key],
                                  size=dip(self, (86, -1)))
        # Native checkboxes blend into their group; their label and indicator
        # must share the platform's natural height rather than a field's height.
        style(control, "surface" if kind == "bool" else "control")
        control.SetName(title)
        if kind in ("number", "bool"):
            control.SetMinSize(control.GetBestSize())
        else:
            control.SetMinSize(dip(self, (-1, 36)))
        self.controls[key] = control
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(control, 0, wx.ALIGN_CENTER_VERTICAL)
        if unit:
            row.Add(label(panel, unit, 12, muted=True), 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, dip(self, 9))
        body.Add(row, 0, wx.EXPAND)
        return control

    def appearance(self):
        panel, body = self.group(0, "Theme")
        self.field(panel, body, "mode", "Appearance mode",
                   "Use light, dark or your system appearance for dialogs.",
                   "choice", ["Light", "Dark", "System"])
        self.help(panel, body, "Your canvas background stays independent.", 11)
        if wx.Platform == "__WXMSW__":
            self.help(panel, body, "On Windows, save your mode and restart the app to change appearance.", 11)
        panel, body = self.group(0, "Canvas")
        color_row = wx.BoxSizer(wx.HORIZONTAL)
        color_copy = wx.BoxSizer(wx.VERTICAL)
        color_copy.Add(label(panel, "Background color", 14, True))
        self.help(panel, color_copy, "The area behind your images.")
        color_row.Add(color_copy, 1, wx.ALIGN_CENTER_VERTICAL)
        color_edit = wx.BoxSizer(wx.HORIZONTAL)
        # wxPython 4.3.1's macOS ColourPickerCtrl calls SetPickerCtrl,
        # which is missing from its bindings. ColourSelect uses ColourDialog
        # directly and retains the same GetColour/SetColour interface.
        picker_class = ColourSelect if wx.Platform == "__WXMAC__" else wx.ColourPickerCtrl
        picker = picker_class(panel, colour=CANVAS_BACKGROUND_DEFAULT, size=dip(self, (34, 36)))
        control = style(wx.TextCtrl(panel, value=self.draft["background"], size=dip(self, (103, -1))), "control")
        control.SetName("Background color")
        self.controls["background"] = control
        self.color_picker = picker
        self.sync_picker()
        control.Bind(wx.EVT_TEXT, self.sync_picker)
        picker_event = EVT_COLOURSELECT if wx.Platform == "__WXMAC__" else wx.EVT_COLOURPICKER_CHANGED
        picker.Bind(picker_event, lambda e: control.SetValue(picker.GetColour().GetAsString(wx.C2S_HTML_SYNTAX)))
        color_edit.Add(picker, 0, wx.RIGHT, dip(self, 8))
        color_edit.Add(control, 0, wx.ALIGN_CENTER_VERTICAL)
        color_row.Add(color_edit, 0, wx.ALIGN_CENTER_VERTICAL)
        body.Add(color_row, 0, wx.EXPAND)
        body.AddSpacer(dip(self, 15))
        self.help(panel, body, "Choose a color or enter its hex code.", 11)
        self.responsive_rows.append(color_row)
        panel, body = self.group(0, "On-image information")
        self.field(panel, body, "timeout", "Status duration", "How long zoom and wraparound messages stay visible.", minimum=100, maximum=10000, unit="ms")
        self.help(panel, body, "Running operations stay visible until they finish or are canceled.", 11)
        body.AddSpacer(dip(self, 20))
        position_row = wx.BoxSizer(wx.HORIZONTAL)
        position_copy = wx.BoxSizer(wx.VERTICAL)
        position_copy.Add(label(panel, "Object info position", 14, True))
        self.help(panel, position_copy, "Place status and frame information on each image.")
        self.help(panel, position_copy, "Animation controls keep their own placement.", 11)
        position_row.Add(position_copy, 1, wx.RIGHT, dip(self, 20))
        position_edit = wx.BoxSizer(wx.VERTICAL)
        choice = style(wx.Choice(panel, choices=POSITIONS, size=dip(self, (184, 36))), "control")
        choice.SetSelection(OBJECT_INFO_POSITIONS.index(self.draft["position"]))
        choice.SetName("Object info position")
        self.controls["position"] = choice
        position_edit.Add(choice, 0, wx.EXPAND)
        self.preview = PositionPreview(panel)
        self.preview.position = choice.GetSelection()
        position_edit.Add(self.preview, 0, wx.TOP, dip(self, 10))
        position_row.Add(position_edit)
        body.Add(position_row, 0, wx.EXPAND)
        self.responsive_rows.append(position_row)
        choice.Bind(wx.EVT_CHOICE, self.position_changed)

    def navigation(self):
        panel, body = self.group(1, "Browsing")
        self.field(panel, body, "wheel", "Mouse wheel navigation", "Select an image, then scroll to the next or previous file.", "bool")
        panel, body = self.group(1, "File order")
        self.field(panel, body, "sort", "Cycling order", "Use the same order each time you browse a folder.", "choice", SORTS)
        self.help(panel, body, "Natural Order: image 2, image 9, image 10. Preloading is in Advanced.", 11)

    def arrangement(self):
        panel, body = self.group(2, "Spacing")
        self.field(panel, body, "spacing", "Spacing between images", "The reserved gap between neighboring images.", unit="px")
        body.AddSpacer(dip(self, 20))
        self.field(panel, body, "margin", "Outer margin", "The reserved space around the arrangement.", unit="px")
        self.help(panel, body, "0–1000 px. Takes effect on the next Arrange All. Images are never stretched, cropped or enlarged to fill space.", 11)

    def advanced(self):
        panel, body = self.group(3, "Image loading")
        self.field(panel, body, "preload", "Images to preload", "Load nearby images ahead of time for smoother navigation.", maximum=5, unit="each way")
        self.help(panel, body, "0–5 in each direction. Set to 0 to turn preloading off.", 11)
        panel, body = self.group(3, "Saved canvas files")
        self.field(
            panel, body, "include_file_identification",
            "Include file identification metadata",
            "Save source file size and MD5 to help tools find moved or renamed files.",
            "bool")

    def licensing(self):
        panel, body = self.group(4, "Project licensing")
        body.Add(label(panel, LICENSE_IDENTIFIER, 15, True), 0)
        self.help(
            panel, body,
            "Application code is free software under the GNU Affero General Public\n"
            "License version 3 or later. Commercial redistribution is permitted;\n"
            "the license includes source-sharing and warranty terms.")
        self.help(
            panel, body,
            "Original documentation prose uses CC BY-SA 4.0. Branding artwork and\n"
            "third-party components have separate terms. User photos and exported\n"
            "collages are not licensed merely because NaguMIX processes them.",
            11)
        body.AddSpacer(dip(self, 14))
        body.Add(label(panel, "Planned public source organization", 12, True), 0)
        source = style(wx.TextCtrl(
            panel, value=PUBLIC_SOURCE_ORGANIZATION_URL,
            style=wx.TE_READONLY), "control")
        source.SetName("Planned public source organization")
        source.SetMinSize(dip(self, (-1, 32)))
        body.Add(source, 0, wx.EXPAND | wx.TOP, dip(self, 6))
        self.help(
            panel, body,
            "This GitHub organization exists, but the NaguMIX application repository\n"
            "has not been created yet. A packaged release must provide its version,\n"
            "exact commit and durable matching source URL; this is not release metadata.", 11)
        self.public_source_organization = source

        panel, body = self.group(4, "Offline legal documents")
        self.help(
            panel, body,
            "Choose a bundled document. The complete text remains available offline\n"
            "and can be selected with standard keyboard shortcuts.")
        choice = style(wx.Choice(
            panel, choices=[title for title, _ in LEGAL_DOCUMENTS]), "control")
        choice.SetName("Legal document")
        choice.SetSelection(0)
        choice.SetMinSize(dip(self, (-1, 36)))
        body.Add(choice, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, dip(self, 12))
        text = style(wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL),
            "control")
        text.SetName("Legal document text")
        text.SetMinSize(dip(self, (-1, 300)))
        text.SetFont(wx.Font(wx.FontInfo(9).FaceName("Consolas")))
        body.Add(text, 0, wx.EXPAND)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        select_all = style(wx.Button(panel, label="Select all"))
        copy = style(wx.Button(panel, label="Copy selection or document"))
        for button in (select_all, copy):
            button.SetMinSize(dip(self, (-1, 36)))
            buttons.Add(button, 0, wx.RIGHT, dip(self, 8))
        body.Add(buttons, 0, wx.TOP, dip(self, 12))
        choice.Bind(wx.EVT_CHOICE, self.legal_document_changed)
        select_all.Bind(wx.EVT_BUTTON, self.select_all_legal_text)
        copy.Bind(wx.EVT_BUTTON, self.copy_legal_text)
        self.legal_choice = choice
        self.legal_text = text
        self.legal_select_all_button = select_all
        self.legal_copy_button = copy
        self.legal_document_changed()

    def select_page(self, index):
        if index not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement,
             self.advanced, self.licensing][index]()
            self.built_pages.add(index)
        self.book.SetSelection(index)
        self.note.SetLabel(
            "Licensing information is read-only." if index == 4
            else "Changes take effect when you save.")
        for i, button in enumerate(self.nav):
            # Keep identical text/insets in both states. A prefix glyph shifts
            # text unpredictably with native font metrics.
            font = button.GetFont()
            font.SetWeight(wx.FONTWEIGHT_BOLD if i == index else wx.FONTWEIGHT_NORMAL)
            button.SetFont(font)
            button._native_foreground_role = "ink" if i == index else "muted"
            button.SetForegroundColour(colors(self)[button._native_foreground_role])
        self.schedule_reflow()

    def legal_document_changed(self, event=None):
        index = self.legal_choice.GetSelection()
        if index < 0:
            return
        _, relative = LEGAL_DOCUMENTS[index]
        try:
            value = read_legal_resource(relative)
        except (OSError, UnicodeError, ValueError) as exc:
            value = f"This bundled legal document could not be loaded.\n\n{exc}"
        self.legal_text.ChangeValue(value)
        self.legal_text.SetInsertionPoint(0)

    def select_all_legal_text(self, event=None):
        self.legal_text.SetFocus()
        self.legal_text.SelectAll()

    def copy_legal_text(self, event=None):
        selected = self.legal_text.GetStringSelection()
        value = selected if selected else self.legal_text.GetValue()
        if not value:
            return
        data = wx.TextDataObject(value)
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(data)
                wx.TheClipboard.Flush()
                self.note.SetLabel("Licensing text copied to the clipboard.")
            finally:
                wx.TheClipboard.Close()

    def position_changed(self, event=None):
        self.preview.position = self.controls["position"].GetSelection()
        self.preview.Refresh()

    def sync_picker(self, event=None):
        try:
            color = wx.Colour(self.controls["background"].GetValue().strip())
            if color.IsOk():
                self.color_picker.SetColour(color)
        except (ValueError, TypeError):
            pass  # Legacy free text is retained; validation is a separate U03 task.
        if event:
            event.Skip()

    def snapshot(self):
        values = dict(self.draft)
        for key, control in self.controls.items():
            if key in CHOICE_VALUES:
                index = control.GetSelection()
                values[key] = CHOICE_VALUES[key][index] if index >= 0 else None
            elif isinstance(control, wx.SpinCtrl):
                # GetValue can clamp uncommitted editor text. Validate the text
                # itself so invalid drafts cannot be silently accepted.
                text = control.GetTextValue().strip()
                try:
                    values[key] = int(text)
                except ValueError:
                    values[key] = text
            else:
                values[key] = control.GetValue()
        return values

    def on_ok(self, event):
        try:
            self.settings_manager.save_dialog_draft(self.snapshot())
        except (OSError, ValueError, configparser.Error) as exc:
            self.note.SetLabel("Could not save. Your changes are still here.")
            self.note.SetToolTip(str(exc))
            wx.MessageBox(str(exc), "Settings could not be saved", wx.OK | wx.ICON_ERROR, self)
            return
        self.EndModal(wx.ID_OK)

    def on_cancel(self, event=None):
        if self.appearance_controller:
            self.appearance_controller.detach(self)
        self.EndModal(wx.ID_CANCEL) if self.IsModal() else self.Close()

    def on_close(self, event):
        if self.appearance_controller:
            self.appearance_controller.detach(self)
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            event.Skip()

    def apply_dialog_appearance(self, palette):
        self._dialog_colors = palette
        restyle(self)

    def reflow(self):
        self.reflow_pending = False
        if not self or self.IsBeingDeleted():
            return
        small = self.ToDIP(self.GetClientSize()).width < 800
        self.sidebar.SetMinSize(dip(self, (150 if small else 200, -1)))
        for row in self.responsive_rows:
            if not hasattr(row, "normal_items"):
                row.normal_items = [(item.GetFlag(), item.GetBorder(), item.GetProportion())
                                    for item in row.GetChildren()]
            row.SetOrientation(wx.VERTICAL if small else wx.HORIZONTAL)
            for index, (item, normal) in enumerate(zip(row.GetChildren(), row.normal_items)):
                if small:
                    # The copy fills the row's width, with its full natural
                    # wrapped height. Horizontal proportions must not become
                    # vertical height allocation when stacking these rows.
                    item.SetFlag(wx.EXPAND | wx.BOTTOM if index == 0 else 0)
                    item.SetBorder(dip(self, 12) if index == 0 else 0)
                    item.SetProportion(0)
                else:
                    flags, border, proportion = normal
                    item.SetFlag(flags)
                    item.SetBorder(border)
                    item.SetProportion(proportion)
        self.Layout()
        for page, _ in self.pages:
            page.Layout()
        for item, original in self.wrapped:
            item.SetLabel(original)
            available = max(dip(self, 100), item.GetSize().width)
            item.Wrap(available)
        for page, _ in self.pages:
            page.Layout()
            page.FitInside()

    def resize(self, event):
        event.Skip()
        self.schedule_reflow()

    def schedule_reflow(self):
        if not self.reflow_pending:
            self.reflow_pending = True
            wx.CallAfter(self.reflow)

    @property
    def bg_color_text(self):
        if 0 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][0]()
            self.built_pages.add(0)
        return self.controls["background"]

    @property
    def enable_wheel_nav(self):
        if 1 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][1]()
            self.built_pages.add(1)
        return self.controls["wheel"]

    @property
    def sort_choice(self):
        if 1 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][1]()
            self.built_pages.add(1)
        return self.controls["sort"]

    @property
    def preload_spin(self):
        if 3 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][3]()
            self.built_pages.add(3)
        return self.controls["preload"]

    @property
    def overlay_timeout_spin(self):
        if 0 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][0]()
            self.built_pages.add(0)
        return self.controls["timeout"]

    @property
    def object_info_choice(self):
        if 0 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][0]()
            self.built_pages.add(0)
        return self.controls["position"]

    @property
    def arrangement_spacing_spin(self):
        if 2 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][2]()
            self.built_pages.add(2)
        return self.controls["spacing"]

    @property
    def arrangement_margin_spin(self):
        if 2 not in self.built_pages:
            [self.appearance, self.navigation, self.arrangement, self.advanced][2]()
            self.built_pages.add(2)
        return self.controls["margin"]
