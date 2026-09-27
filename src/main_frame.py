# src/main_frame.py
import os
import wx
from .canvas_panel import CanvasPanel
from .settings_dialog import SettingsDialog
from .brand_resources import set_window_icon
from .arrangement import arrange_no_resize, arrange_with_resize
import logging
from .exporting import resolve_export_path
from .file_reveal import reveal_source
from . import canvas_bindings
from .file_navigator import FileNavigator


class MainFrame(wx.Frame):
    ADD_IMAGES_ID = wx.NewIdRef()
    RESET_FRAME_ID = wx.NewIdRef()
    RESET_SIZE_ID = wx.NewIdRef()
    RESET_ZOOM_ID = wx.NewIdRef()
    RESET_OFFSET_ID = wx.NewIdRef()
    REVEAL_SOURCE_ID = wx.NewIdRef()
    DUPLICATE_ID = wx.NewIdRef()
    SHOW_ANIMATION_CONTROLS_ID = wx.NewIdRef()

    def __init__(self, parent, title, settings_manager, debug_mode=False):
        # Choose style based on debug mode
        if debug_mode:
            # Use default window style for debug mode (with title bar, borders, etc.)
            style = wx.DEFAULT_FRAME_STYLE
        else:
            # Style for borderless fullscreen mode
            style = wx.FRAME_NO_TASKBAR | wx.NO_BORDER

        super().__init__(parent, title=title, style=style)
        set_window_icon(self)

        self.settings_manager = settings_manager
        self.debug_mode = debug_mode
        self._shutdown_started = False
        self._add_images_dialog_open = False

        # Create the main panel (the canvas)
        self.canvas_panel = CanvasPanel(self, settings_manager=self.settings_manager)

        # Bind a context menu event on the frame level
        self.Bind(wx.EVT_CONTEXT_MENU, self.on_right_click)

        # Reset commands use stable IDs and are bound once. Each handler
        # resolves the current selection when the command is dispatched.
        self.Bind(wx.EVT_MENU, self.on_reset_size, id=int(self.RESET_SIZE_ID))
        self.Bind(wx.EVT_MENU, self.on_add_images, id=int(self.ADD_IMAGES_ID))
        self.Bind(wx.EVT_MENU, self.on_reset_frame, id=int(self.RESET_FRAME_ID))
        self.Bind(wx.EVT_MENU, self.on_reset_zoom, id=int(self.RESET_ZOOM_ID))
        self.Bind(wx.EVT_MENU, self.on_reset_offset, id=int(self.RESET_OFFSET_ID))
        self.Bind(wx.EVT_MENU, self.on_reveal_source, id=int(self.REVEAL_SOURCE_ID))
        self.Bind(wx.EVT_MENU, self.on_duplicate_object, id=int(self.DUPLICATE_ID))
        self.Bind(wx.EVT_MENU, self.on_show_animation_controls,
                  id=int(self.SHOW_ANIMATION_CONTROLS_ID))

        # Also allow the user to exit fullscreen with ESC or F11
        self.Bind(wx.EVT_KEY_DOWN, self.on_key_down)

        # A simple sizer so that the canvas fills the entire frame
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.canvas_panel, 1, wx.EXPAND)
        self.SetSizer(sizer)

        # Make sure we can handle sizing events
        self.Bind(wx.EVT_SIZE, self.on_resize)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def on_resize(self, event):
        """Handle resize events to refresh the layout if needed."""
        self.canvas_panel.Refresh()
        event.Skip()

    def on_right_click(self, event):
        """Show a context menu for the entire canvas if user right-clicked outside any image object."""
        menu = wx.Menu()
        menu.Append(int(self.ADD_IMAGES_ID), canvas_bindings.add_images_menu_label())

        # Resolve the object under this context click, not a stale selection.
        target_id = getattr(self.canvas_panel, "_context_object_id", None)
        get_position = getattr(event, "GetPosition", None)
        if get_position is not None:
            position = get_position()
            try:
                keyboard_invoked = position.x < 0 and position.y < 0
            except AttributeError:
                keyboard_invoked = position[0] < 0 and position[1] < 0
            if keyboard_invoked:
                selected = self.canvas_panel.get_selected_object()
                target_id = selected.object_id if selected is not None else None
                self.canvas_panel._context_object_id = target_id
        sel_obj = next((obj for obj in self.canvas_panel.image_objects
                        if obj.object_id == target_id), None)
        if target_id is None and not hasattr(self.canvas_panel, "_context_object_id"):
            sel_obj = self.canvas_panel.get_selected_object()
        if sel_obj:
            menu.AppendSeparator()
            mark_item = menu.Append(wx.ID_ANY, "Mark This Object")
            self.Bind(wx.EVT_MENU, self.on_mark_object, mark_item)

            swap_item = menu.Append(wx.ID_ANY, "Swap With Marked Object")
            self.Bind(wx.EVT_MENU, self.on_swap_objects, swap_item)

            menu.AppendSeparator()

            menu.Append(int(self.RESET_SIZE_ID), "Reset Size to Original")
            menu.Append(int(self.RESET_FRAME_ID), "Reset Frame Size")
            menu.Append(int(self.RESET_ZOOM_ID), "Reset Zoom to Original")
            menu.Append(int(self.RESET_OFFSET_ID), "Reset Offset to Original")

            menu.AppendSeparator()

            menu.Append(int(self.DUPLICATE_ID), "Duplicate")

            if sel_obj.is_animated:
                menu.Append(int(self.SHOW_ANIMATION_CONTROLS_ID),
                            "Show Animation Controls")

            delete_item = menu.Append(wx.ID_ANY, "Remove from Canvas")
            self.Bind(wx.EVT_MENU, self.on_delete_object, delete_item)
            if sel_obj.source_path:
                menu.Append(int(self.REVEAL_SOURCE_ID), self._reveal_label())
        else:
            # No object selected, or user clicked on empty canvas
            pass

        menu.AppendSeparator()

        arrange_item = menu.Append(wx.ID_ANY, "Arrange All (No Resize)")
        self.Bind(wx.EVT_MENU, self.on_arrange_no_resize, arrange_item)

        arrange_resize_item = menu.Append(wx.ID_ANY, "Arrange All (With Resize)")
        self.Bind(wx.EVT_MENU, self.on_arrange_with_resize, arrange_resize_item)

        menu.AppendSeparator()

        export_item = menu.Append(wx.ID_ANY, "Export Canvas...")
        self.Bind(wx.EVT_MENU, self.on_export_canvas, export_item)

        menu.AppendSeparator()

        load_state_item = menu.Append(wx.ID_ANY, "Load Canvas State...")
        self.Bind(wx.EVT_MENU, self.on_load_canvas_state, load_state_item)
        save_state_item = menu.Append(wx.ID_ANY, "Save Canvas State...")
        self.Bind(wx.EVT_MENU, self.on_save_canvas_state, save_state_item)

        menu.AppendSeparator()

        settings_item = menu.Append(wx.ID_ANY, "Settings...")
        self.Bind(wx.EVT_MENU, self.on_open_settings, settings_item)

        quit_item = menu.Append(wx.ID_ANY, "Quit")
        self.Bind(wx.EVT_MENU, self.on_quit, quit_item)

        self.PopupMenu(menu)
        menu.Destroy()

    def on_add_images(self, event):
        """Use the drop pipeline for a native multi-file chooser."""
        if (self._add_images_dialog_open or self._shutdown_started
                or not self.IsEnabled()
                or any(isinstance(window, wx.Dialog) and window.IsModal()
                       for window in wx.GetTopLevelWindows())):
            return False
        self._add_images_dialog_open = True
        paths = []
        try:
            patterns = ";".join("*" + extension for extension in
                                sorted(FileNavigator.SUPPORTED_EXTENSIONS))
            dialog = wx.FileDialog(
                self, canvas_bindings.ADD_IMAGES_LABEL,
                wildcard=f"Supported images|{patterns}",
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
            try:
                if dialog.ShowModal() == wx.ID_OK:
                    paths = dialog.GetPaths()
            finally:
                dialog.Destroy()
        finally:
            self._add_images_dialog_open = False
            if not self._shutdown_started:
                self.canvas_panel.SetFocus()
        if not paths or self._shutdown_started:
            return False
        width, height = self.canvas_panel.get_client_dimensions()
        return self.canvas_panel.accept_drop(width // 2, height // 2, paths)

    @staticmethod
    def _reveal_label():
        import sys
        if sys.platform == "win32":
            return "Show in Explorer"
        if sys.platform == "darwin":
            return "Show in Finder"
        return "Show in File Manager"

    def on_reveal_source(self, event):
        """Reveal the still-live object captured by the context click."""
        target_id = getattr(self.canvas_panel, "_context_object_id", None)
        target = next((obj for obj in self.canvas_panel.image_objects
                       if obj.object_id == target_id), None)
        if target is None:
            return False
        result = reveal_source(target.source_path)
        if result.message:
            target.set_status_overlay(result.message,
                                      "info" if result.launched and not result.missing
                                      else "warning")
            self.canvas_panel.Refresh()
        return result.launched

    def on_duplicate_object(self, event):
        """Duplicate the still-live object captured by the context click."""
        target_id = getattr(self.canvas_panel, "_context_object_id", None)
        target = next((obj for obj in self.canvas_panel.image_objects
                       if obj.object_id == target_id), None)
        if target is None:
            return False
        return self.canvas_panel.begin_duplicate(target)

    def on_show_animation_controls(self, event):
        """Open and focus controls for the still-live context target."""
        target_id = getattr(self.canvas_panel, "_context_object_id", None)
        target = next((obj for obj in self.canvas_panel.image_objects
                       if obj.object_id == target_id), None)
        if target is None or not target.is_animated:
            return False
        return self.canvas_panel.show_animation_controls(target.object_id)

    def _reset_selected_object(self, reset_method, needs_canvas_bounds=False,
                               schedule_overlay_clear=False):
        """Apply one reset to the live selection and invalidate only it."""
        sel_obj = self.canvas_panel.get_selected_object()
        if not sel_obj:
            return False

        CanvasPanel._cancel_viewport_gesture(self.canvas_panel)

        prepare_edit = getattr(self.canvas_panel, "prepare_canvas_edit", None)
        if prepare_edit is not None:
            prepare_edit("image reset")

        if needs_canvas_bounds:
            canvas_w, canvas_h = self.canvas_panel.get_client_dimensions()
            sel_obj.set_canvas_size(canvas_w, canvas_h)

        if not reset_method(sel_obj):
            return False

        self.canvas_panel.Refresh()
        if schedule_overlay_clear:
            self.canvas_panel._schedule_overlay_clear(image_object=sel_obj)
        return True

    def on_reset_size(self, event):
        """Reset the selected object using the current drawable bounds."""
        return self._reset_selected_object(
            lambda obj: obj.reset_size(fit_to_canvas=True),
            needs_canvas_bounds=True,
        )

    def on_reset_frame(self, event):
        """Reveal the current content at the context target's runtime ID."""
        target_id = getattr(self.canvas_panel, "_context_object_id", None)
        if target_id is None:
            return False
        return self.canvas_panel.reset_selected_frame(target_id)

    def on_reset_zoom(self, event):
        """Reset zoom of the selected object and retain timed feedback."""
        return self._reset_selected_object(
            lambda obj: obj.reset_zoom(),
            schedule_overlay_clear=True,
        )

    def on_reset_offset(self, event):
        """Reset the selected object's crop offset."""
        return self._reset_selected_object(
            lambda obj: obj.reset_viewport_offset(),
        )

    def on_mark_object(self, event):
        """Mark the currently selected object."""
        sel_obj = self.canvas_panel.get_selected_object()
        if sel_obj:
            self.canvas_panel.marked_object = sel_obj

    def on_swap_objects(self, event):
        """Swap selected object with previously marked object."""
        sel_obj = self.canvas_panel.get_selected_object()
        marked_obj = self.canvas_panel.marked_object
        self.canvas_panel.swap_image_objects(sel_obj, marked_obj)

    def on_delete_object(self, event):
        sel_obj = self.canvas_panel.get_selected_object()
        if sel_obj:
            self.canvas_panel.remove_image_object(sel_obj)

    def on_arrange_no_resize(self, event):
        if wx.MessageBox("Arrange all images without resizing?\nThis will move them around on the canvas.",
                         "Confirm Arrangement", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            prepare_edit = getattr(self.canvas_panel, "prepare_canvas_edit", None)
            if prepare_edit is not None:
                prepare_edit("images arranged")
            arrangement = self.settings_manager.get_arrangement_settings()
            if arrange_no_resize(
                    self.canvas_panel.image_objects,
                    self.canvas_panel.get_client_dimensions(),
                    arrangement["spacing"], arrangement["outer_margin"]):
                self.canvas_panel.Refresh()
            else:
                wx.MessageBox(
                    "The images do not fit without resizing. Try Arrange All "
                    "(With Resize), reduce spacing or margin, or enlarge the canvas.",
                    "Arrangement unavailable", wx.OK | wx.ICON_WARNING, self,
                )

    def on_arrange_with_resize(self, event):
        if wx.MessageBox("Arrange all images WITH resizing?\nThis will move and resize them to fit.",
                         "Confirm Arrangement", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            prepare_edit = getattr(self.canvas_panel, "prepare_canvas_edit", None)
            if prepare_edit is not None:
                prepare_edit("images arranged")
            arrangement = self.settings_manager.get_arrangement_settings()
            if not arrange_with_resize(
                    self.canvas_panel.image_objects,
                    self.canvas_panel.get_client_dimensions(),
                    arrangement["spacing"], arrangement["outer_margin"]):
                wx.MessageBox(
                    "Could not arrange the images. Check that all source files "
                    "are readable and the canvas has room for the grid.",
                    "Arrangement unavailable", wx.OK | wx.ICON_WARNING, self,
                )
            else:
                self.canvas_panel.Refresh()

    def on_export_canvas(self, event):
        """Export the current canvas as an image file."""
        wildcard = "PNG files (*.png)|*.png|" \
                   "JPEG files (*.jpg)|*.jpg|" \
                   "WebP files (*.webp)|*.webp|" \
                   "BMP files (*.bmp)|*.bmp"
        dialog = wx.FileDialog(self, "Save Exported Image", wildcard=wildcard,
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        initial_name = self.canvas_panel.get_export_suggestion("PNG")
        set_filename = getattr(dialog, "SetFilename", None)
        if set_filename is not None:
            set_filename(initial_name)
        path = None
        format_name = None
        try:
            if dialog.ShowModal() == wx.ID_OK:
                path = dialog.GetPath()
                formats = ("PNG", "JPEG", "WEBP", "BMP")
                index = dialog.GetFilterIndex()
                format_name = formats[index] if 0 <= index < len(formats) else "PNG"
                # Native dialogs differ in whether a filter change rewrites
                # the filename.  Only adjust the untouched generated value;
                # an explicitly edited conflicting suffix remains an error.
                if os.path.basename(path) == initial_name:
                    path = os.path.join(
                        os.path.dirname(path),
                        self.canvas_panel.get_export_suggestion(format_name))
        finally:
            dialog.Destroy()
        if path is None:
            return
        try:
            destination = resolve_export_path(path, format_name)
            self.canvas_panel.begin_export(destination, format_name)
        except (OSError, RuntimeError, ValueError) as exc:
            logging.warning("Could not start canvas export %s: %s", path, exc)
            wx.MessageBox(
                f"Could not export the canvas:\n{path}\n\n{exc}\n\n"
                "Any existing destination file is unchanged.",
                "Canvas export unavailable", wx.OK | wx.ICON_ERROR, self,
            )

    def on_load_canvas_state(self, event):
        """Load canvas state from JSON."""
        dialog = wx.FileDialog(self, "Load Canvas State", wildcard="JSON files (*.json)|*.json",
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        self._run_state_dialog(
            dialog, self.canvas_panel.begin_load_canvas_state, "load")

    def on_save_canvas_state(self, event):
        """Save canvas state to JSON."""
        dialog = wx.FileDialog(self, "Save Canvas State", wildcard="JSON files (*.json)|*.json",
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        set_filename = getattr(dialog, "SetFilename", None)
        if set_filename is not None:
            set_filename(self.canvas_panel.get_scene_save_suggestion())
        self._run_state_dialog(
            dialog, self.canvas_panel.begin_save_canvas_state, "save")

    def _run_state_dialog(self, dialog, operation, action):
        """Always release the chooser and report failures without losing work."""
        try:
            path = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()
        if path is None:
            return
        try:
            operation(path)
        except (OSError, RuntimeError, ValueError) as exc:
            logging.warning("Could not %s canvas state %s: %s", action, path, exc)
            wx.MessageBox(
                f"Could not {action} canvas state:\n{path}\n\n{exc}\n\n"
                "Your current canvas and any previously saved file are unchanged.",
                f"Canvas state {action} failed", wx.OK | wx.ICON_ERROR, self,
            )

    def on_open_settings(self, event):
        before = self.settings_manager.application_snapshot()
        dlg = SettingsDialog(self, self.settings_manager)
        try:
            result = dlg.ShowModal()
        finally:
            dlg.Destroy()

        if result == wx.ID_OK:
            MainFrame._apply_accepted_settings(self, before)

    def _apply_accepted_settings(self, before):
        after = self.settings_manager.application_snapshot()
        # Only changes affecting navigator ownership enter the existing broad
        # invalidation path. Wheel enablement is read at each wheel event.
        if any(before[key] != after[key] for key in ("sort", "preload", "cache_mb")):
            self.canvas_panel.on_settings_changed()
        if before["background"] != after["background"]:
            self.canvas_panel.canvas_bg = after["background"]
        if any(before[key] != after[key] for key in ("background", "position", "timeout")):
            self.canvas_panel.Refresh()
        controller = getattr(wx.GetApp(), "dialog_appearance", None)
        if controller and before["mode"] != after["mode"]:
            controller.apply_saved()

    def _begin_shutdown(self):
        """Start idempotent, nonblocking application resource cleanup."""
        if self._shutdown_started:
            return False
        self._shutdown_started = True

        self.canvas_panel.shutdown_preloading()
        try:
            if hasattr(self, 'settings_manager') and self.settings_manager:
                self.settings_manager.save()
        except Exception as e:
            logging.warning(f"Failed to save settings on quit: {e}")
        return True

    def on_close(self, event):
        """Cover window-manager closes without waiting for preload I/O."""
        self._begin_shutdown()
        event.Skip()

    def on_quit(self, event):
        """Gracefully terminate the application."""
        self._begin_shutdown()

        # Set successful exit code
        wx.GetApp().set_exit_code(0)

        # Close frame properly before exiting
        self.Close(force=True)

        # Close the application gracefully using wxPython's proper method
        wx.CallAfter(wx.GetApp().ExitMainLoop)

    def on_key_down(self, event):
        keycode = event.GetKeyCode()
        logging.debug(f"Key pressed: {keycode}")
        # ESC or F11 to exit fullscreen
        if canvas_bindings.QUIT[0].matches(event) or keycode == wx.WXK_F11:
            self.on_quit(None)
        # on key "x", run the on_quit function
        elif (canvas_bindings.QUIT[1].matches(event)
              and not self._text_entry_has_focus()):
            self.on_quit(None)
        else:
            event.Skip()

    def _text_entry_has_focus(self):
        """Keep application shortcuts out of editable controls and dialogs."""
        focus = wx.Window.FindFocus()
        if focus is None:
            return False
        if focus.GetTopLevelParent() is not self:
            return True
        return isinstance(focus, wx.TextEntry)
