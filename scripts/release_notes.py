"""Print one version's section of CHANGELOG.md (used by the release workflow).

    python scripts/release_notes.py 0.2.0 > notes.md
    python scripts/release_notes.py v0.1.1 path/to/CHANGELOG.md
"""

import re
import sys
from pathlib import Path


def notes(version: str, changelog: str) -> str:
    pattern = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.M | re.S)
    m = pattern.search(changelog)
    if not m:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return m.group(1).strip() + "\n"


if __name__ == "__main__":
    version = sys.argv[1].removeprefix("v")
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    sys.stdout.write(notes(version, path.read_text(encoding="utf-8")))
