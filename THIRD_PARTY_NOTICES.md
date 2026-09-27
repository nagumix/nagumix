# Third-party notices

NaguMIX's license does not replace dependency licenses. The offline copies
below were taken from distributions matching the locked Windows Python 3.12
environment on 2026-09-19.

| Component | Version | Bundled notice copy | SHA-256 of inspected installed notice |
| --- | --- | --- | --- |
| Pillow | 12.3.0 | `assets/legal/third-party/Pillow-12.3.0-LICENSE.txt` | `4F7866A74802C6326F81FAFF59A56546B6AEC2B10B91973E0E9308DE95E79857` |
| wxPython | 4.3.1 | `assets/legal/third-party/wxPython-4.3.1-LICENSE.txt` | `0D5CCA7FB8EE32D9313E3B1660A009846CAAEF4FE166E493C7CA4DFD95A0AB5C` |
| pillow-heif | 1.7.0 | `assets/legal/third-party/pillow-heif-1.7.0-LICENSE.txt` | `9E2635F155B00AF5A46CB2F2C9B052072EC546D59837EE74EFC9CBA2CBB83F3D` |
| pillow-heif bundled libraries | 1.7.0 wheel inventory | `assets/legal/third-party/pillow-heif-1.7.0-BUNDLED-NOTICES.txt` | `5142993DE7EDE4427C3C74335958198D803AC92AC7A7D0E6705A0EEA77CC32B1` |

The Windows packaging definition also collects the build interpreter's Python
license, PyInstaller's `COPYING.txt` (including its bootloader exception), and
the PyInstaller hooks contribution license into the frozen legal-resource tree.
Their exact files and versions come from the locked build environment and must
be inventoried for every candidate.

The Pillow notice includes its bundled-library notices. The pillow-heif bundle
notice identifies libheif 1.23.3, libde265 1.1.2, x265 4.2, and MinGW runtime
components. The inspected x265 source grant permits GPL version 2 or later,
which provides a scoped GPLv3/AGPLv3 compatibility route. This is not clearance
for every future binary or any codec patent question.

Before publishing a package, inventory the actual platform artifact, retain all
applicable notices, and provide any required corresponding source, build,
installation, and relinking material. Platform runtimes and installer tooling
remain outside this source-environment inventory until a real package is
audited. See `docs/LEGAL_AND_RELEASE.md`.
