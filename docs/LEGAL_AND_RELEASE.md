# Licensing and release-source policy

NaguMIX-authored application code and reusable examples are licensed under
`AGPL-3.0-or-later`. Original NaguMIX documentation prose is licensed under
`CC-BY-SA-4.0` unless a file says otherwise. Branding and third-party material
retain the separate scopes recorded in `NOTICE.md` and
`THIRD_PARTY_NOTICES.md`.

The Settings Licensing page reads the complete texts under `assets/legal/`
without network access. When dependency versions change, refresh notice copies
from the installed locked distributions, compare their hashes, and inspect
actual native payloads rather than relying only on Python package metadata.

Every packaged release requires a platform-specific inventory covering the
application, Python, wxPython/wxWidgets, Pillow codecs, pillow-heif and its
native libraries, packaging tools, hooks, runtimes, and installer components.
Applicable notices and corresponding source, build, installation, or relinking
material must accompany the release.

Before publishing a package, replace the development marker in
`assets/legal/RELEASE-SOURCE.txt` with the version, exact source commit, and a
durable public URL for matching source. A moving default branch is not
release-matched source. Source tests and a development checkout do not establish
binary-license or codec-patent clearance.

