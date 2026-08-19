"""download_kraken_db's verify-and-promote steps, without the network."""

import hashlib
import tarfile

import pytest

from app.errors import PermanentError
from app.queue import kraken_handlers


def _tarball_with(tmp_path, inner_files):
    src = tmp_path / "src"
    src.mkdir()
    for name in inner_files:
        (src / name).write_bytes(b"data-" + name.encode())
    tb = tmp_path / "db.tar.gz"
    with tarfile.open(tb, "w:gz") as tf:
        for name in inner_files:
            tf.add(src / name, arcname=name)
    return tb


def test_verify_md5_accepts_matching(tmp_path):
    tb = _tarball_with(tmp_path, ["hash.k2d"])
    digest = hashlib.md5(tb.read_bytes()).hexdigest()
    kraken_handlers.verify_md5(tb, digest)  # no raise


def test_verify_md5_rejects_mismatch(tmp_path):
    tb = _tarball_with(tmp_path, ["hash.k2d"])
    with pytest.raises(PermanentError):
        kraken_handlers.verify_md5(tb, "0" * 32)


def test_extract_and_promote_is_atomic(tmp_path):
    tb = _tarball_with(tmp_path, ["hash.k2d", "opts.k2d", "taxo.k2d"])
    final = tmp_path / "dbs" / "standard-8"
    kraken_handlers.extract_and_promote(tb, final)
    assert (final / "hash.k2d").is_file()
    assert not (final.parent / "standard-8.partial").exists()


def test_extract_failure_leaves_no_final_dir(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"this is not a tarball")
    final = tmp_path / "dbs" / "standard-8"
    with pytest.raises(Exception):  # noqa: B017 -- any failure mode is fine here
        kraken_handlers.extract_and_promote(bad, final)
    assert not final.exists()
