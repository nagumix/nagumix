# State and asynchronous-operation invariants

## Canvas state compatibility

Saved JSON records source paths, transforms, zoom, viewport state, and optional
validated metadata. Nonbreaking fields may be added. A breaking format change
requires an explicit schema version and migration or a clearly documented
compatibility boundary.

Runtime object IDs, cache entries, worker handles, and wx resources are never
persisted. Loading creates new runtime identities and accepts the candidate
scene only after all sources and saved animation positions validate.

## Accepted snapshots

Drop, navigation, scene load, save, and export have explicit ownership and
cancellation. A worker result may publish only while its owning operation,
target object, source generation, and requested state are still current.
Replacing a scene or making a conflicting edit retires pending work rather than
allowing a late callback to overwrite newer state.

Save and export operate on invocation-time snapshots. Later edits do not alter
those snapshots. File publication is transactional: write and synchronize a
unique sibling temporary file, then atomically replace the destination.

## Image and animation resources

Decoded pixel payloads may move between workers and the GUI thread through
explicit leases. wx images, bitmaps, controls, timers, and events remain
GUI-thread-owned. Animated GIF seeking reconstructs exact frames with bounded
work; displayed and requested frame positions remain distinct while a seek is
pending.

## Viewport behavior

Resizing the visible frame does not change content scale. Frame edges remain
inside the scaled image, content repositioning cannot expose empty space, and
Reset Frame Size restores the complete scaled source at its current content
position. Explicit zoom performs that reset before applying the zoom step.

