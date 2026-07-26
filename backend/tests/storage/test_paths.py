"""Path validation: digest sanitization and register-in-place containment."""

import pytest

from app.errors import ValidationError
from app.storage.paths import blob_rel_path, resolve_registerable, validate_sha256

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
