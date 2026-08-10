#!/usr/bin/env python3
"""Rewrite every version declaration for one release line.

Split out of release.sh so the file-rewriting half can be tested against
temporary trees without a git repo. The git ceremony lives in release.sh.

TOML and JSON are edited textually rather than parsed-and-redumped: a
round-trip through a TOML writer would reformat and strip the comments these
files carry, and `json.dump` would reflow package.json. The substitutions are
anchored tightly enough to be safe -- see `_replace_first_version`.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SEMVER = re.compile(r"^\d+\.\d+\.\d+(-alpha|-beta)?$")


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _replace_first_version(path: Path, version: str) -> None:
    """Rewrite the first `version = "..."` line in a TOML file.

    First only, and line-anchored: `[project]`/`[package]` is the first table
    in both files this touches, so the first such line is the package's own.
    A dependency's `version =` further down -- or a `rust-version` -- must not
    be rewritten, which is why the pattern anchors to line start and requires
    the exact key.
    """
    text = path.read_text()
    pattern = re.compile(r'^version\s*=\s*"[^"]*"', re.MULTILINE)
    if not pattern.search(text):
        fail(f"no `version = \"...\"` line in {path}")
    path.write_text(pattern.sub(f'version = "{version}"', text, count=1))


def _replace_json_version(path: Path, version: str) -> None:
    """Rewrite `"version": "..."` in a package.json, preserving formatting."""
    text = path.read_text()
    pattern = re.compile(r'"version"\s*:\s*"[^"]*"')
    if not pattern.search(text):
        fail(f'no `"version": "..."` key in {path}')
    path.write_text(pattern.sub(f'"version": "{version}"', text, count=1))
    # Parse it back to prove the edit did not corrupt the file.
    try:
        json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        fail(f"{path} is not valid JSON after the edit: {exc}")


def _replace_generated_module(path: Path, version: str) -> None:
    text = path.read_text()
    pattern = re.compile(r'^__version__\s*=\s*"[^"]*"', re.MULTILINE)
    if not pattern.search(text):
        fail(f"no `__version__ = \"...\"` line in {path}")
    path.write_text(pattern.sub(f'__version__ = "{version}"', text, count=1))


def bump_app(root: Path, version: str) -> list[Path]:
    version_file = root / "VERSION"
    if not version_file.exists():
        fail(f"no VERSION file at {version_file}")

    module = root / "backend" / "app" / "version.py"
    pyproject = root / "backend" / "pyproject.toml"
    package = root / "frontend" / "package.json"
    for p in (module, pyproject, package):
        if not p.exists():
            fail(f"missing {p}")

    version_file.write_text(f"{version}\n")
    _replace_generated_module(module, version)
    _replace_first_version(pyproject, version)
    _replace_json_version(package, version)
    return [version_file, module, pyproject, package]


def bump_launcher(root: Path, version: str) -> list[Path]:
    cargo = root / "launcher" / "src-tauri" / "Cargo.toml"
    package = root / "launcher" / "package.json"
    for p in (cargo, package):
        if not p.exists():
            fail(f"missing {p}")

    _replace_first_version(cargo, version)
    _replace_json_version(package, version)
    return [cargo, package]


LINES = {"app": bump_app, "launcher": bump_launcher}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("line", choices=sorted(LINES))
    parser.add_argument("version")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if not SEMVER.match(args.version):
        fail(f"{args.version!r} is not semver MAJOR.MINOR.PATCH")

    written = LINES[args.line](args.root.resolve(), args.version)
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
