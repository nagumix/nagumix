"""Offline licensing resources for source checkouts and collected bundles."""

from functools import lru_cache

from .brand_resources import resource_path


PUBLIC_SOURCE_ORGANIZATION_URL = "https://github.com/nagumix"
LICENSE_IDENTIFIER = "AGPL-3.0-or-later"

LEGAL_DOCUMENTS = (
    ("Project notice and scope", "legal/PROJECT-NOTICE.txt"),
    ("Release source information", "legal/RELEASE-SOURCE.txt"),
    ("GNU AGPL version 3", "legal/AGPL-3.0.txt"),
    ("CC BY-SA 4.0", "legal/CC-BY-SA-4.0.txt"),
    ("Third-party notice index", "legal/THIRD-PARTY-NOTICES.txt"),
    ("Pillow 12.3.0 notices", "legal/third-party/Pillow-12.3.0-LICENSE.txt"),
    ("wxPython 4.3.1 notices", "legal/third-party/wxPython-4.3.1-LICENSE.txt"),
    ("pillow-heif 1.7.0 license", "legal/third-party/pillow-heif-1.7.0-LICENSE.txt"),
    ("pillow-heif bundled libraries", "legal/third-party/pillow-heif-1.7.0-BUNDLED-NOTICES.txt"),
)


@lru_cache(maxsize=len(LEGAL_DOCUMENTS))
def read_legal_resource(relative):
    """Read a bundled UTF-8 legal resource without depending on the cwd."""
    expected = dict(LEGAL_DOCUMENTS).values()
    if relative not in expected:
        raise ValueError("Unknown legal resource")
    path = resource_path(relative)
    if path is None:
        raise FileNotFoundError(f"Missing bundled legal resource: {relative}")
    return path.read_text(encoding="utf-8")
