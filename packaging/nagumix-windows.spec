# Maintained Windows x64 one-folder build definition.
from pathlib import Path
from importlib.metadata import distribution
import sys


root = Path(SPEC).resolve().parent.parent
assets = root / "assets"
metadata = root / "build" / "windows" / "BUILD-METADATA.json"
pyinstaller_license = distribution("pyinstaller").locate_file(
    "pyinstaller-6.22.3.dist-info/licenses/COPYING.txt")
hooks_license = distribution("pyinstaller-hooks-contrib").locate_file(
    "pyinstaller_hooks_contrib-2026.7.dist-info/licenses/LICENSE")

datas = [
    (str(assets / "branding"), "assets/branding"),
    (str(assets / "icons"), "assets/icons"),
    (str(assets / "legal"), "assets/legal"),
    (str(metadata), "."),
    (str(Path(sys.base_prefix) / "LICENSE.txt"), "assets/legal/third-party"),
    (str(pyinstaller_license), "assets/legal/third-party"),
    (str(hooks_license), "assets/legal/third-party"),
]

a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=["pillow_heif"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NaguMIX",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(assets / "icons" / "nagumix-app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NaguMIX",
)
