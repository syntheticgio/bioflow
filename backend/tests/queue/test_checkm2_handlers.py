"""download_checkm2_db's verify-and-promote steps, without the network.

The test_kraken_handlers shape. What matters here is R4: an interrupted or
corrupt download must never be visible to `db_present`, because a
half-present database fails deep inside a scoring job rather than at launch.
"""

import hashlib
import tarfile

import pytest

from app.errors import PermanentError
from app.queue import checkm2_handlers

_DB_INNER = "CheckM2_database/uniref100.KO.1.dmnd"


def _tarball_with(tmp_path, inner_files):
    src = tmp_path / "src"
    for name in inner_files:
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"data-" + name.encode())
    tb = tmp_path / "db.tar.gz"
    with tarfile.open(tb, "w:gz") as tf:
        for name in inner_files:
            tf.add(src / name, arcname=name)
    return tb


def test_verify_md5_accepts_matching(tmp_path):
    tb = _tarball_with(tmp_path, [_DB_INNER])
    digest = hashlib.md5(tb.read_bytes()).hexdigest()
    checkm2_handlers.verify_md5(tb, digest)  # no raise


def test_verify_md5_rejects_mismatch(tmp_path):
    """A mismatched tarball must never be extracted (R4)."""
    tb = _tarball_with(tmp_path, [_DB_INNER])
    with pytest.raises(PermanentError) as e:
        checkm2_handlers.verify_md5(tb, "0" * 32)
    # The message has to say what failed, not just that something did.
    assert "md5" in str(e.value).lower()


def test_extract_and_promote_is_atomic(tmp_path):
    tb = _tarball_with(tmp_path, [_DB_INNER])
    final = tmp_path / "dbs" / "uniref100"

    checkm2_handlers.extract_and_promote(tb, final)

    assert (final / _DB_INNER).is_file()
    # The rename is the commit point: no `.partial` may survive it.
    assert not (final.parent / "uniref100.partial").exists()


def test_extract_failure_leaves_no_final_dir(tmp_path):
    """A corrupt archive must leave nothing that reads as present."""
    bad = tmp_path / "db.tar.gz"
    bad.write_bytes(b"not a gzip stream at all")
    final = tmp_path / "dbs" / "uniref100"

    with pytest.raises(tarfile.ReadError):
        checkm2_handlers.extract_and_promote(bad, final)

    assert not final.exists()
    assert not (final.parent / "uniref100.partial").exists()


def test_promote_replaces_an_existing_directory(tmp_path):
    """A re-download must not merge into whatever was there before."""
    final = tmp_path / "dbs" / "uniref100"
    final.mkdir(parents=True)
    (final / "stale.txt").write_bytes(b"from an older layout")

    tb = _tarball_with(tmp_path, [_DB_INNER])
    checkm2_handlers.extract_and_promote(tb, final)

    assert (final / _DB_INNER).is_file()
    assert not (final / "stale.txt").exists()
