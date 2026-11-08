#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re
import subprocess
import sys

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSION_ASSIGN_RE = re.compile(r'^(__version__\s*=\s*")([^"]+)(")', re.MULTILINE)


def parse_semver(raw: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(raw.strip())
    if not m:
        raise ValueError(f"Invalid semantic version: {raw!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump_semver(current: str, part: str) -> str:
    major, minor, patch = parse_semver(current)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unknown bump part: {part!r}")


def read_version(init_file: Path) -> str:
    text = init_file.read_text(encoding="utf-8")
    m = VERSION_ASSIGN_RE.search(text)
    if not m:
        raise RuntimeError(f"__version__ assignment not found in {init_file}")
    parse_semver(m.group(2))
    return m.group(2)


def update_project_version(
    root: Path,
    new_version: str,
    *,
    release_date: str | None = None,
) -> str:
    parse_semver(new_version)
    init_file = root / "src" / "flexkit" / "__init__.py"
    changelog = root / "CHANGELOG.md"

    text = init_file.read_text(encoding="utf-8")
    updated, count = VERSION_ASSIGN_RE.subn(rf"\g<1>{new_version}\g<3>", text, count=1)
    if count != 1:
        raise RuntimeError(f"Failed to update __version__ in {init_file}")
    init_file.write_text(updated, encoding="utf-8")

    stamp = release_date or date.today().isoformat()
    header = f"## [{new_version}] - {stamp}\n"
    if changelog.exists():
        cl_text = changelog.read_text(encoding="utf-8")
        if f"## [{new_version}]" not in cl_text:
            lines = cl_text.splitlines(keepends=True)
            insert_at = 1 if lines and lines[0].startswith("# ") else 0
            entry = f"\n{header}\n- Release v{new_version}.\n"
            lines.insert(insert_at, entry)
            changelog.write_text("".join(lines), encoding="utf-8")
    else:
        changelog.write_text(f"# Changelog\n\n{header}\n- Release v{new_version}.\n", encoding="utf-8")

    return new_version


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    init_file = root / "src" / "flexkit" / "__init__.py"

    parser = argparse.ArgumentParser(prog="version.py", description="FlexKit version manager")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("show")

    bump_p = sub.add_parser("bump")
    bump_p.add_argument("part", choices=("major", "minor", "patch"))
    bump_p.add_argument("--tag", action="store_true")

    set_p = sub.add_parser("set")
    set_p.add_argument("version")
    set_p.add_argument("--tag", action="store_true")

    args = parser.parse_args(argv)
    current = read_version(init_file)

    if args.cmd in (None, "show"):
        print(current)
        return 0

    target = bump_semver(current, args.part) if args.cmd == "bump" else args.version
    update_project_version(root, target)
    print(target)

    if args.tag:
        subprocess.run(["git", "tag", "-a", f"v{target}", "-m", f"Release v{target}"], cwd=root, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
