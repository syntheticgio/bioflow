"""Bumping rewrites every declaration for a line, and nothing else."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

BUMP = Path(__file__).resolve().parents[1] / "lib" / "bump_version.py"


def run_bump(root: Path, line: str, version: str):
    return subprocess.run(
        [sys.executable, str(BUMP), line, version, "--root", str(root)],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def app_tree(tmp_path):
    """A minimal tree with the app line's four declarations."""
    (tmp_path / "VERSION").write_text("0.1.0\n")
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "version.py").write_text('__version__ = "0.1.0"\n')
    (tmp_path / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "biopipe-backend"\nversion = "0.1.0"\ndescription = "x"\n'
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        json.dumps({"name": "biopipe-frontend", "private": True, "version": "0.1.0"}, indent=2)
        + "\n"
    )
    return tmp_path


@pytest.fixture
def launcher_tree(tmp_path):
    (tmp_path / "launcher" / "src-tauri").mkdir(parents=True)
    (tmp_path / "launcher" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "bioflow-launcher"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    (tmp_path / "launcher" / "package.json").write_text(
        json.dumps({"name": "bioflow-launcher", "version": "0.1.0"}, indent=2) + "\n"
    )
    (tmp_path / "launcher" / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps(
            {
                "productName": "BioFlow Launcher",
                "version": "0.1.0",
                "identifier": "com.bioflow.launcher",
            },
            indent=2,
        )
        + "\n"
    )
    return tmp_path


class TestAppLine:
    def test_writes_every_app_declaration(self, app_tree, launcher_tree):
        r = run_bump(app_tree, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        assert (app_tree / "VERSION").read_text() == "0.2.0\n"
        assert '__version__ = "0.2.0"' in (app_tree / "backend" / "app" / "version.py").read_text()
        assert 'version = "0.2.0"' in (app_tree / "backend" / "pyproject.toml").read_text()
        package_json = json.loads((app_tree / "frontend" / "package.json").read_text())
        assert package_json["version"] == "0.2.0"

    def test_leaves_other_fields_intact(self, app_tree):
        run_bump(app_tree, "app", "0.2.0")

        pyproject = (app_tree / "backend" / "pyproject.toml").read_text()
        assert 'name = "biopipe-backend"' in pyproject
        pkg = json.loads((app_tree / "frontend" / "package.json").read_text())
        assert pkg["name"] == "biopipe-frontend"
        assert pkg["private"] is True

    def test_bumps_the_launcher_line_too(self, app_tree, launcher_tree):
        """#335: one version for both lines. Both fixtures use tmp_path, so
        this reads the same tree."""
        r = run_bump(app_tree, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        assert 'version = "0.2.0"' in (
            app_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads((app_tree / "launcher" / "package.json").read_text())["version"]
            == "0.2.0"
        )
        assert (
            json.loads(
                (app_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
            )["version"]
            == "0.2.0"
        )

    def test_overwrites_a_launcher_version_that_is_ahead(self, app_tree, launcher_tree):
        """CR-7: a launcher-only release leaves the launcher ahead; the next
        combined cut reclaims the number rather than preserving the drift."""
        run_bump(app_tree, "launcher", "0.9.0")
        r = run_bump(app_tree, "app", "0.2.0")
        assert r.returncode == 0, r.stderr

        assert 'version = "0.2.0"' in (
            app_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()

    def test_app_bump_reports_all_seven_files(self, app_tree, launcher_tree):
        r = run_bump(app_tree, "app", "0.2.0")
        written = [line for line in r.stdout.splitlines() if line.strip()]
        assert len(written) == 7, written

    def test_app_prerelease_gives_tauri_conf_the_core_version(self, app_tree, launcher_tree):
        r = run_bump(app_tree, "app", "0.3.0-alpha")
        assert r.returncode == 0, r.stderr

        assert (app_tree / "VERSION").read_text() == "0.3.0-alpha\n"
        assert 'version = "0.3.0-alpha"' in (
            app_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads(
                (app_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
            )["version"]
            == "0.3.0"
        )

    def test_only_the_first_version_key_in_cargo_style_toml(self, app_tree):
        """A dependency's `version = ` must never be mistaken for the package's."""
        (app_tree / "backend" / "pyproject.toml").write_text(
            '[project]\nversion = "0.1.0"\n\n[tool.other]\nversion = "9.9.9"\n'
        )
        run_bump(app_tree, "app", "0.2.0")
        text = (app_tree / "backend" / "pyproject.toml").read_text()
        assert 'version = "0.2.0"' in text
        assert 'version = "9.9.9"' in text

    def test_writes_a_prerelease_version(self, app_tree, launcher_tree):
        r = run_bump(app_tree, "app", "0.3.0-alpha")
        assert r.returncode == 0, r.stderr

        assert (app_tree / "VERSION").read_text() == "0.3.0-alpha\n"
        assert '__version__ = "0.3.0-alpha"' in (
            app_tree / "backend" / "app" / "version.py"
        ).read_text()
        assert 'version = "0.3.0-alpha"' in (
            app_tree / "backend" / "pyproject.toml"
        ).read_text()
        assert (
            json.loads((app_tree / "frontend" / "package.json").read_text())["version"]
            == "0.3.0-alpha"
        )


class TestLauncherLine:
    def test_writes_cargo_and_package_json(self, launcher_tree):
        r = run_bump(launcher_tree, "launcher", "0.1.1")
        assert r.returncode == 0, r.stderr

        assert 'version = "0.1.1"' in (
            launcher_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads((launcher_tree / "launcher" / "package.json").read_text())["version"]
            == "0.1.1"
        )

    def test_writes_tauri_conf_json(self, launcher_tree):
        """The file Tauri actually reads for the bundle filename.

        Regression test for the defect where launcher-v0.2.0 shipped bundles
        named 0.1.0: tauri.conf.json overrides Cargo.toml and was never bumped.
        """
        r = run_bump(launcher_tree, "launcher", "0.1.1")
        assert r.returncode == 0, r.stderr

        conf = json.loads(
            (launcher_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
        )
        assert conf["version"] == "0.1.1"

    def test_tauri_conf_keeps_its_other_keys(self, launcher_tree):
        run_bump(launcher_tree, "launcher", "0.1.1")
        conf = json.loads(
            (launcher_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
        )
        assert conf["productName"] == "BioFlow Launcher"
        assert conf["identifier"] == "com.bioflow.launcher"

    def test_tauri_conf_gets_the_core_version_on_a_prerelease(self, launcher_tree):
        """CR-4: the macOS CFBundleShortVersionString must stay numeric."""
        r = run_bump(launcher_tree, "launcher", "0.5.0-alpha")
        assert r.returncode == 0, r.stderr

        conf = json.loads(
            (launcher_tree / "launcher" / "src-tauri" / "tauri.conf.json").read_text()
        )
        assert conf["version"] == "0.5.0"
        assert 'version = "0.5.0-alpha"' in (
            launcher_tree / "launcher" / "src-tauri" / "Cargo.toml"
        ).read_text()
        assert (
            json.loads((launcher_tree / "launcher" / "package.json").read_text())["version"]
            == "0.5.0-alpha"
        )

    def test_reports_tauri_conf_as_written(self, launcher_tree):
        """release.sh git-adds exactly what this prints, so an unlisted file
        would be bumped but left out of the release commit."""
        r = run_bump(launcher_tree, "launcher", "0.1.1")
        assert "tauri.conf.json" in r.stdout

    def test_missing_tauri_conf_is_an_error(self, tmp_path):
        (tmp_path / "launcher" / "src-tauri").mkdir(parents=True)
        (tmp_path / "launcher" / "src-tauri" / "Cargo.toml").write_text(
            '[package]\nversion = "0.1.0"\n'
        )
        (tmp_path / "launcher" / "package.json").write_text('{\n  "version": "0.1.0"\n}\n')
        r = run_bump(tmp_path, "launcher", "0.1.1")
        assert r.returncode != 0
        assert "tauri.conf.json" in r.stderr


class TestValidation:
    def test_rejects_a_non_semver_version(self, app_tree):
        r = run_bump(app_tree, "app", "0.2")
        assert r.returncode != 0
        assert "semver" in r.stderr.lower()

    def test_rejects_an_unknown_line(self, app_tree):
        r = run_bump(app_tree, "nonsense", "0.2.0")
        assert r.returncode != 0

    def test_rejects_a_missing_file(self, tmp_path):
        r = run_bump(tmp_path, "app", "0.2.0")
        assert r.returncode != 0
        assert "VERSION" in r.stderr

    def test_rejects_a_bare_build_metadata_suffix(self, app_tree):
        r = run_bump(app_tree, "app", "0.2.0-rc")
        assert r.returncode != 0

    def test_rejects_an_uppercase_suffix(self, app_tree):
        r = run_bump(app_tree, "app", "0.2.0-ALPHA")
        assert r.returncode != 0

    def test_rejects_a_v_prefixed_version(self, app_tree):
        r = run_bump(app_tree, "app", "v0.2.0")
        assert r.returncode != 0
