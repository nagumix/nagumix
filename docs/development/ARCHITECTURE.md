# Application architecture

NaguMIX is an event-driven wxPython desktop application. `src/app.py` creates
the application and main frame; `src/main_frame.py` owns top-level command
routing; and `src/canvas_panel.py` coordinates interaction, rendering, and
asynchronous operations.

Each canvas entry is an `ImageObject` with a runtime object identifier, source
path, accepted pixels, position, zoom, and viewport geometry. Runtime identity
is distinct from source-path equality so multiple independent objects may show
the same file. Saved state intentionally serializes source and presentation
data rather than runtime identifiers.

Decoded image work is scheduled through bounded services. Worker threads may
read and decode ordinary data, but they do not create or mutate wx resources.
Publication returns to the GUI thread and checks operation ownership, object
identity, source generation, and cancellation before replacing visible state.
Stale results are rejected.

Rendering uses prepared bitmap caches keyed by the accepted source-pixel
revision and presentation geometry. Cache invalidation is local to the object
whose accepted state changed. Export leases the accepted pixel revisions it
needs, allowing ordinary canvas edits to continue without changing the export
snapshot.

The application has no plugin loading boundary. New behavior should preserve
the existing separation between immutable worker data, GUI-owned wx resources,
and transactional publication.

