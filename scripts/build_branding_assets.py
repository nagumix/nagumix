"""Rebuild NaguMIX runtime and platform icons from the public masters.

Run from any working directory with the project's locked Pillow environment.
The master files are inputs and are never modified.
"""

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SOURCES = {
    "light": ASSETS / "branding" / "masters" / "nagumix-logo-light-original.png",
    "dark": ASSETS / "branding" / "masters" / "nagumix-logo-dark-original.png",
}
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _source(path):
    with Image.open(path) as image:
        if image.size != (1254, 1254) or image.mode != "RGBA":
            raise ValueError(f"Expected a 1254px RGBA master: {path}")
        if image.getchannel("A").getextrema() != (0, 255):
            raise ValueError(f"Expected actual transparency: {path}")
        return image.copy()


def main():
    branding = ASSETS / "branding"
    icons = ASSETS / "icons"
    for directory in (branding, icons):
        directory.mkdir(parents=True, exist_ok=True)

    for variant, path in SOURCES.items():
        image = _source(path)
        image.resize((80, 80), Image.Resampling.LANCZOS).save(
            branding / f"nagumix-logo-{variant}-80.png", format="PNG")

        if variant == "dark":
            for size in (128, 256, 512):
                image.resize((size, size), Image.Resampling.LANCZOS).save(
                    icons / f"nagumix-app-{size}.png", format="PNG")
            image.save(
                icons / "nagumix-app.ico",
                format="ICO",
                sizes=[(size, size) for size in ICO_SIZES],
            )
            image.save(icons / "nagumix-app.icns", format="ICNS")


if __name__ == "__main__":
    main()

