# Branding assets

The NaguMIX name, logos, icons, and other branding artwork are outside the
AGPL and CC-BY-SA grants that cover application code and documentation.
Unmodified branding may accompany unmodified NaguMIX source or binaries
published by the project. Modified builds and forks must replace the branding
unless separately permitted and must not imply endorsement or official status.

The authoritative shipping inputs are:

- `assets/branding/masters/nagumix-logo-light-original.png`
- `assets/branding/masters/nagumix-logo-dark-original.png`

Run the following command from the repository root to reproduce the smaller
header images and platform icon containers:

```console
uv run python scripts/build_branding_assets.py
```

The build script derives the themed 80-pixel headers and application PNG, ICO,
and ICNS files. Runtime lookup supports source execution, frozen executable
directories, and the PyInstaller bundle root. A packager must still collect the
`assets/` tree explicitly; bundle-root detection does not package files by
itself.

The dark mark is the stable application-icon source. The Settings header picks
the light or dark header asset from the resolved native appearance.

