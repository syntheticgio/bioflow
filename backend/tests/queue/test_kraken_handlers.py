"""download_kraken_db's verify-and-promote steps, without the network."""

import hashlib
import tarfile

import pytest

from app.errors import PermanentError
from app.queue import kraken_handlers
from app.queue.pipeline_handlers import _named_link


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


def test_build_classification_facts_with_mismatch():
    kraken_rows = [
        {"pct": 5.0, "clade_reads": 500, "direct_reads": 500,
         "rank": "U", "taxid": 0, "name": "unclassified"},
        {"pct": 94.0, "clade_reads": 9400, "direct_reads": 9000,
         "rank": "S", "taxid": 1280, "name": "Staphylococcus aureus"},
    ]
    facts = kraken_handlers.build_classification_facts(
        kraken_rows=kraken_rows,
        bracken_rows=[],
        metadata_organism="Escherichia coli",
        db_key="standard-8",
        bracken_note=None,
    )
    tax = facts["taxonomy"]
    assert tax["db_key"] == "standard-8"
    assert tax["bracken_used"] is False
    assert tax["taxa"][0]["name"] == "Staphylococcus aureus"
    assert facts["taxonomy_mismatch"]["claimed"] == "Escherichia coli"


def test_build_classification_facts_records_bracken_skip():
    kraken_rows = [
        {"pct": 1.0, "clade_reads": 100, "direct_reads": 100,
         "rank": "S", "taxid": 562, "name": "Escherichia coli"},
    ]
    facts = kraken_handlers.build_classification_facts(
        kraken_rows=kraken_rows,
        bracken_rows=[],
        metadata_organism=None,
        db_key="viral",
        bracken_note="bracken exited 1",
    )
    assert facts["taxonomy"]["bracken_skipped"] == "bracken exited 1"
    assert "taxonomy_mismatch" not in facts


@pytest.mark.parametrize(
    "mean, expected",
    [
        (None, 100),
        (140, 150),
        (500, 300),
    ],
)
def test_nearest_bracken_read_len(mean, expected):
    assert kraken_handlers._nearest_bracken_read_len(mean) == expected


def test_named_link_makes_gzip_suffix_detectable_on_extensionless_blob(tmp_path):
    """Regression for the gzip-detection bug: `classify_reads` computes
    `gzipped=reads.suffix == ".gz"` straight off the path `_resolve_input`
    returns. For a managed blob that path is the extensionless hash name
    (`objects_dir/ab/abcdef...`), so without routing it through
    `_named_link` first, `.suffix` is never `.gz` and `--gzip-compressed`
    is silently never passed to Kraken2 even though the underlying bytes
    are gzip-compressed.

    This asserts the fix's actual mechanism: `_named_link` given the
    blob's real name (as now supplied via `reads_name`/`mate_name` in the
    job payload, set in `launch_classify_reads`) produces a path whose
    suffix *is* `.gz`, which is exactly the expression `classify_reads`
    evaluates. Before the fix (calling `.suffix` on the raw blob path
    with no `_named_link` step), this would fail: an extensionless path's
    `.suffix` is `""`, not `".gz"`.
    """
    work = tmp_path / "workdir"
    work.mkdir()

    # A managed blob: extensionless, hash-named, exactly like blob_path(digest).
    objects_dir = tmp_path / "objects" / "ab"
    objects_dir.mkdir(parents=True)
    blob = objects_dir / "abcdef0123456789"
    blob.write_bytes(b"\x1f\x8b" + b"fake-gzip-body")  # gzip magic bytes

    # Sanity: the raw blob path, unmodified, is exactly the broken case.
    assert blob.suffix != ".gz"

    linked = _named_link(work, blob, "in_sample_R1.fastq.gz")

    assert linked.suffix == ".gz"
    assert linked.name == "in_in_sample_R1.fastq.gz"
    assert linked.is_symlink()
    assert linked.resolve() == blob.resolve()

    # This is the literal expression classify_reads uses to build the
    # kraken2 command; confirm it now evaluates correctly.
    gzipped = linked.suffix == ".gz"
    assert gzipped is True
