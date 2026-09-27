"""Write provenance metadata consumed by the Windows PyInstaller spec."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "windows" / "BUILD-METADATA.json"
INPUTS = (
    "pyproject.toml",
    "uv.lock",
    "packaging/nagumix-windows.spec",
    "scripts/build_windows.ps1",
)


def run_git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def digest(relative):
    return sha256((ROOT / relative).read_bytes()).hexdigest()


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    status = run_git("status", "--short", "--untracked-files=all").splitlines()
    payload = {
        "artifact_kind": "unsigned local Windows x64 development candidate",
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_head": run_git("rev-parse", "HEAD"),
        "source_branch": run_git("branch", "--show-current"),
        "source_status": status,
        "input_sha256": {path: digest(path) for path in INPUTS},
        "python": sys.version,
        "platform": platform.platform(),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
