# NaguMIX

NaguMIX is a native wxPython application for arranging images on a canvas and
exporting the result. It supports direct file drops, non-destructive viewport
cropping, file navigation, animated GIF controls, saved canvas state, and
asynchronous loading, saving, and export.

This repository snapshot is prepared for source publication. It does not yet
represent a packaged release, and no download or hosted project URL is claimed.

## Development setup

Requirements:

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- the native runtime prerequisites required by wxPython on the host platform

Install the locked dependencies and start the application:

```console
uv sync --locked
uv run python main.py
```

The application normally opens fullscreen. Debugger-attached runs use a
windowed frame.

## Tests

Run the complete discovered suite:

```console
uv run python run_tests.py -v
```

Visible native GUI cases are opt-in:

```powershell
$env:NAGUMIX_GUI_TESTS = "1"
uv run python run_tests.py -v
```

The GitHub workflow runs the Linux suite with the locked dependency set under
Xvfb. The GitLab configuration repeats that Linux evidence and defines a
manual Windows GUI job for an interactive runner. A source test workflow is
not a packaged-application or cross-platform release claim.

## Repository guide

- `src/`: application and platform-neutral support modules
- `tests/`: regression suite and required fixtures
- `assets/`: runtime branding and offline legal resources
- `scripts/`: maintained build and CI helpers
- `docs/USER_GUIDE.md`: current controls and supported workflows
- `docs/development/`: contributor-facing architecture and runtime invariants
- `docs/BRANDING_ASSETS.md`: branding scope and reproducible derivatives
- `docs/LEGAL_AND_RELEASE.md`: licensing and release-source gates
- `docs/WINDOWS_GUI_CI.md`: native Windows runner requirements
- `docs/WINDOWS_PACKAGING.md`: locked local Windows x64 executable build

## Contributing and licensing

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting changes.

NaguMIX-authored application code, tests, scripts, configuration, build files,
and reusable code examples are licensed under
[`AGPL-3.0-or-later`](LICENSE). Original NaguMIX documentation prose is licensed
under [`CC-BY-SA-4.0`](LICENSES/CC-BY-SA-4.0.txt) unless stated otherwise.
Branding and third-party material have separate scopes described in
[NOTICE.md](NOTICE.md) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
