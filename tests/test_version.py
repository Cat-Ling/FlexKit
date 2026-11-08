from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest

SPEC = importlib.util.spec_from_file_location(
    "flexkit_version_script",
    Path(__file__).resolve().parent.parent / "scripts" / "version.py",
)
assert SPEC is not None and SPEC.loader is not None
version_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(version_mod)


def test_semver_bumps() -> None:
    assert version_mod.bump_semver("0.0.1", "patch") == "0.0.2"
    assert version_mod.bump_semver("0.0.1", "minor") == "0.1.0"
    assert version_mod.bump_semver("0.0.1", "major") == "1.0.0"

    with pytest.raises(ValueError):
        version_mod.parse_semver("v0.0.1")


def test_update_project_version_rewrites_init_and_changelog(tmp_path: Path) -> None:
    pkg_dir = tmp_path / "src" / "flexkit"
    pkg_dir.mkdir(parents=True)
    init_py = pkg_dir / "__init__.py"
    init_py.write_text('__version__ = "0.0.1"\nAPP_NAME = "ChromeOS Flex Installer"\n', encoding="utf-8")

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [0.0.1] - 2026-11-08\n\n- Initial.\n", encoding="utf-8")

    version_mod.update_project_version(tmp_path, "0.0.2", release_date="2026-11-09")
    assert version_mod.read_version(init_py) == "0.0.2"
    cl_text = changelog.read_text(encoding="utf-8")
    assert "## [0.0.2] - 2026-11-09" in cl_text
    assert cl_text.index("[0.0.2]") < cl_text.index("[0.0.1]")
