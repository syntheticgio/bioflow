"""Path validation: digest sanitization and register-in-place containment."""

import pytest
from app.errors import NotFoundError, ValidationError
from app.storage.paths import (
    blob_rel_path,
    resolve_registerable,
    resolve_report_file,
    validate_sha256,
)

VALID = "a" * 64


class TestValidateSha256:
    def test_accepts_valid(self):
        assert validate_sha256(VALID) == VALID

    def test_normalizes_case_and_whitespace(self):
        assert validate_sha256(f"  {'A' * 64}  ") == "a" * 64

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "a" * 63,
            "a" * 65,
            "g" * 64,  # not hex
            "../" + "a" * 61,
            "a" * 32 + "/" + "a" * 31,
            "a" * 60 + "\n" + "a" * 3,
        ],
    )
    def test_rejects_invalid(self, bad):
        """Digests become filesystem paths, so this is a containment boundary."""
        with pytest.raises(ValidationError):
            validate_sha256(bad)


class TestBlobRelPath:
    def test_two_level_sharding(self):
        assert blob_rel_path(VALID) == f"aa/{VALID}"

    def test_distinct_prefixes_shard_apart(self):
        a = blob_rel_path("ab" + "0" * 62)
        b = blob_rel_path("cd" + "0" * 62)
        assert a.split("/")[0] != b.split("/")[0]


class TestResolveRegisterable:
    def test_accepts_path_inside_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.bioinfo_register_roots", str(tmp_path)
        )
        target = tmp_path / "data.fastq"
        target.write_text("x")
        assert resolve_registerable(str(target)) == target.resolve()

    def test_rejects_path_outside_root(self, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside.fastq"
        outside.write_text("x")
        monkeypatch.setattr("app.config.settings.bioinfo_register_roots", str(allowed))
        with pytest.raises(ValidationError, match="outside the allowed roots"):
            resolve_registerable(str(outside))

    def test_rejects_traversal(self, tmp_path, monkeypatch):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (tmp_path / "secret.txt").write_text("secret")
        monkeypatch.setattr("app.config.settings.bioinfo_register_roots", str(allowed))
        with pytest.raises(ValidationError):
            resolve_registerable(str(allowed / ".." / "secret.txt"))

    def test_rejects_symlink_escaping_root(self, tmp_path, monkeypatch):
        """Resolution happens before containment, so a symlink out is rejected
        rather than followed."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        link = allowed / "innocent.fastq"
        link.symlink_to(secret)

        monkeypatch.setattr("app.config.settings.bioinfo_register_roots", str(allowed))
        with pytest.raises(ValidationError, match="outside the allowed roots"):
            resolve_registerable(str(link))

    def test_rejects_relative_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config.settings.bioinfo_register_roots", str(tmp_path))
        with pytest.raises(ValidationError, match="absolute"):
            resolve_registerable("relative/path.fastq")


class TestResolveReportFile:
    """Containment for client-supplied report paths.

    These are the cases neither report endpoint covers today. The endpoints'
    own traversal suites cover the ordinary `../` attacks; what is pinned here
    is the behavior that both call sites currently get only incidentally, via
    an `.is_file()` that happens to be ANDed in.
    """

    @pytest.fixture
    def root(self, tmp_path):
        (tmp_path / "report.tsv").write_text("col\tval\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.tsv").write_text("col\tval\n")
        return tmp_path

    def test_returns_a_file_directly_under_the_root(self, root):
        assert resolve_report_file(root, "report.tsv") == root / "report.tsv"

    def test_returns_a_file_in_a_subdirectory(self, root):
        assert resolve_report_file(root, "sub/nested.tsv") == root / "sub" / "nested.tsv"

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty: resolves to the root directory itself
            "..",
            "../secret.tsv",
            "sub/../../secret.tsv",
            "/etc/passwd",
        ],
    )
    def test_rejects_traversal_and_absolute_paths(self, root, bad):
        with pytest.raises(NotFoundError):
            resolve_report_file(root, bad)

    def test_rejects_the_root_itself(self, root):
        """A directory is not a report. Both call sites rely on this today."""
        with pytest.raises(NotFoundError):
            resolve_report_file(root, ".")

    def test_rejects_a_directory(self, root):
        with pytest.raises(NotFoundError):
            resolve_report_file(root, "sub")

    def test_rejects_a_missing_file(self, root):
        with pytest.raises(NotFoundError):
            resolve_report_file(root, "nope.tsv")

    def test_rejects_a_symlink_escaping_the_root(self, root, tmp_path_factory):
        """The one input the `..` prefilter does not catch.

        Both endpoints reject this today only because `.is_file()` follows the
        link to a path outside the root. Pinning it makes that explicit.
        """
        outside = tmp_path_factory.mktemp("outside")
        secret = outside / "secret.tsv"
        secret.write_text("private\n")
        (root / "link.tsv").symlink_to(secret)

        with pytest.raises(NotFoundError):
            resolve_report_file(root, "link.tsv")
