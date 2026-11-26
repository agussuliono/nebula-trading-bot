"""Repo hygiene: released version and build-artifact ignores stay consistent.

These tests guard two gaps found in the 2026-11-25 V1 audit:
- README's top "**Version:**" line lagged behind pyproject.toml and
  ``nebula.__version__`` (0.16.5 vs 0.16.6).
- ``.gitignore`` did not cover ``*.egg-info/``, ``build/`` or ``dist/``,
  so a future ``pip install -e .`` could leave untracked build artifacts.
No production code is involved; both checks read repo files only.
"""

import re
import tomllib
from pathlib import Path

from nebula import __version__ as package_version

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _readme_top_version() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^\*\*Version:\*\*\s*([0-9]+\.[0-9]+\.[0-9]+)", readme, re.M)
    assert match is not None, "README must state a top '**Version:** X.Y.Z' line"
    return match.group(1)


def test_readme_version_matches_package_version():
    assert _readme_top_version() == _pyproject_version() == package_version


def test_readme_test_status_section_matches_current_version():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"## Test status for v{package_version}" in readme


def test_build_and_runtime_dependencies_are_hash_locked():
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        build_requires = tomllib.load(handle)["build-system"]["requires"]

    assert build_requires == ["setuptools==84.2.1", "wheel==0.48.0"]

    build_lock = (REPO_ROOT / "requirements-build.lock").read_text(encoding="utf-8")
    for package in ("setuptools==84.2.1", "wheel==0.48.0", "packaging==26.4"):
        assert package in build_lock
    assert "--hash=sha256:" in build_lock

    runtime_lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "requests==2.34.2" in runtime_lock
    assert "--hash=sha256:" in runtime_lock


def test_vm_workflow_install_blocks_are_indented_yaml():
    for name in ("okx-demo-preflight.yml", "okx-demo-forward-test.yml"):
        text = (REPO_ROOT / ".github" / "workflows" / name).read_text(
            encoding="utf-8"
        )
        assert "        run: |\n          python -m pip install" in text, (
            f"{name} install commands must be indented inside the run block"
        )


def test_gitignore_covers_build_artifacts():
    ignored = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for pattern in ("*.egg-info/", "build/", "dist/"):
        assert pattern in ignored, f".gitignore must cover {pattern}"
