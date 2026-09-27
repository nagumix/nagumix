# Windows executable build

The maintained package is an unsigned Windows x64 one-folder application. It is
tested on Windows 11 with CPython 3.12 as the build interpreter. The output
directory is a unit: copy or extract the complete `NaguMIX` folder and start
`NaguMIX.exe`; do not copy the executable by itself.

## Locked local build

Install `uv`, then run from the repository root in PowerShell:

```powershell
.\scripts\build_windows.ps1
```

The script uses the project lock and the `packaging` dependency group in a
separate ignored `.venv-packaging` environment. It writes a source/input-hash
record into the package, builds `dist\NaguMIX`, and creates the version/platform
labelled zip beside it. The pinned recipe is repeatable; byte-identical output
is not claimed.

## Runtime data and prerequisites

Frozen builds store settings and diagnostics under
`%LOCALAPPDATA%\NaguMIX` as `nagumix_settings.ini` and `nagumix.log`. Source
runs retain the historical current-directory INI behavior. The application
does not write settings into its bundle or change the process working directory.

The build is unsigned, has no installer, and does not add shortcuts, file
associations, an update service, or system-wide files. Windows may therefore
show an unrecognized-app warning. The inspected candidate contains app-local
`MSVCP140.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `ucrtbase.dll`, and
API-set forwarders collected from the locked Python/wxPython environment. A
system Visual C++ Redistributable was therefore not established as a prerequisite
for this candidate, but clean-machine validation and redistribution provenance
for those app-local files remain release gates. The Python-level startup warning
cannot run when a native bootloader dependency fails first.

## Verification boundary

Every candidate needs inspection of PyInstaller warnings and the actual payload,
plus a packaged smoke covering launch from an unrelated directory and a copied
path containing spaces; branding in light/dark appearance; offline Licensing;
settings Save/reopen and Cancel; PNG, JPEG, alpha, GIF, AVIF and HEIC/HEIF;
navigation, GIF stepping, canvas save/load, image export and normal close.
Inspect exported pixels and confirm there are no writes to the bundle or source.

A build on a development machine is not a clean-machine or missing-runtime test.
It does not establish Windows ARM64, macOS, Linux, installer, signing, hosted CI,
release, codec-patent, or redistribution clearance. `BUILD-METADATA.json` records
the local source and inputs but is not a substitute for durable matching source.
