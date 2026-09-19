# NaguMIX user guide

NaguMIX opens a canvas where image files can be dropped, arranged, cropped,
and exported. Most commands are available from the canvas or image context
menu.

## Images and layout

- Drop common still-image formats, AVIF, HEIC/HEIF, or GIF files onto the
  canvas. Loading progress and failures appear on the canvas.
- Click an image to select it and drag to move it. White handles resize the
  visible frame while keeping the image scale fixed.
- When a reduced frame hides part of an image, hover inside it and drag the
  compact four-arrow grip to reposition the image beneath the frame.
- Use `+` and `-`, or `Ctrl` plus the vertical mouse wheel, to zoom the selected
  image. An explicit zoom first restores the full frame.
- Use the context menu to duplicate, delete, reset, mark and swap images, or to
  arrange the complete canvas with or without resizing.

## Navigation and animation

The vertical mouse wheel navigates adjacent files when wheel navigation is
enabled. Sorting and preload behavior are configured in Settings.

Animated GIFs start paused. `Space` toggles playback, `,` and `.` step frames,
and the hover controls provide previous, play/pause, next, timeline, and hide
actions. Timeline clicks seek immediately; a paused drag previews a rested
frame and commits the exact release target.

## Save and export

Save Canvas State writes JSON describing source paths and object transforms.
Loading is transactional: the existing canvas remains until every referenced
source has been decoded successfully. Optional file-identification metadata is
best-effort and is not proof of authenticity.

Export Canvas writes PNG, JPEG, WebP, or BMP from the accepted canvas snapshot.
Saving and export use sibling temporary files and replace the destination only
after successful completion. Cancellation or failure before replacement
preserves an existing destination.

## Settings

Settings provides Appearance, Navigation, Arrangement, Advanced, and Licensing
pages. Draft changes take effect only after Save; Cancel, Escape, and closing
the dialog discard them. Licensing texts are bundled locally and do not require
network access.

The settings file is `nagumix_settings.ini` in the current working directory.
It can contain local paths and should not be committed.

## Keyboard summary

| Input | Action |
| --- | --- |
| `Esc` or `X` on the canvas | Exit |
| `+` / `-` | Zoom selected image |
| `,` / `.` | Previous/next GIF frame |
| `Space` | Toggle selected GIF or focused control |
| `Ctrl` + vertical wheel | Zoom selected image |
| Vertical wheel | Navigate files when enabled |

