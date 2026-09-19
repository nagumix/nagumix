# NaguMIX contributor guidance

NaguMIX is a native wxPython collage application. Keep changes compatible with
Python 3.12 and the dependency versions locked in `uv.lock`.

- Treat `ImageObject.object_id` as runtime canvas identity. A source path is not
  object identity because multiple objects may show the same file.
- Keep wx resources and control mutation on the GUI thread. Background workers
  operate on ordinary immutable data and publish through guarded callbacks.
- Preserve ownership, generation, and cancellation checks for asynchronous
  drop, navigation, scene-load, save, export, and GIF work.
- Preserve transactional file and scene publication. A failed or cancelled
  operation must not replace the previous destination or visible scene.
- Viewport resizing changes the visible frame at fixed scale; content
  repositioning must not reveal empty space.
- Keep the saved JSON format backward-compatible unless a versioned migration
  is intentionally designed and documented.
- Rebuild branding derivatives with `scripts/build_branding_assets.py`; do not
  replace or reinterpret the authoritative masters casually.
- Run focused tests while developing and the complete discovered suite before
  claiming broad regression coverage. Set `NAGUMIX_GUI_TESTS=1` only where a
  real visible desktop is available.

Architecture and compatibility details are under `docs/architecture/`.

