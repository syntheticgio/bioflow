"""Internal consistency of the CheckM2 database registry.

Keys are this repo's own strings, not an enum's members, so the
registry-audit exhaustiveness pattern does not apply; these assert the
analogous internal-consistency invariants (the test_kraken_db_registry
shape), plus the pin itself, which is the constraint spec Q1 exists for.
"""

from app.pipelines.checkm2_db_registry import (
    CHECKM2_DBS,
    DEFAULT_DB,
    db_file,
    db_present,
)


def test_default_key_is_registered():
    assert DEFAULT_DB in CHECKM2_DBS


def test_registry_holds_exactly_one_entry():
    # One database, not three: CheckM2 ships a single database and the card
    # needs no picker (spec Q1). A second entry appearing here means either a
    # genuinely new upstream database or a mistake, and both deserve a look.
    assert len(CHECKM2_DBS) == 1


def test_every_entry_is_complete():
    for key, spec in CHECKM2_DBS.items():
        assert spec.key == key
        assert spec.label
        assert spec.url.startswith("https://")
        assert spec.download_bytes > 0
        assert spec.mem_mb > 0
        assert len(spec.md5) == 32
        assert spec.description


def test_url_is_pinned_to_a_dated_record():
    """The exact regression the pin exists to prevent (spec Q1/R4).

    Zenodo serves two kinds of URL: a *version* record (14897628, fixed
    content) and a *concept* record (4626518, an alias for whatever is
    newest). Only the former is reproducible.

    The specific record matters beyond reproducibility: CheckM2 1.1.0
    requires DIAMONDDB **version 3** and rejects the older v2 (record
    7563512) as incompatible, so pinning the wrong one is not a stale-data
    problem but a run that cannot start.
    """
    for spec in CHECKM2_DBS.values():
        assert "latest" not in spec.url
        # The versioned record, not the concept record it belongs to.
        assert "/records/14897628/" in spec.url


def test_db_present_requires_the_diamond_file(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    d = settings.checkm2_db_dir / DEFAULT_DB
    d.mkdir(parents=True)

    # A bare directory reads as ABSENT: the download extracts into
    # `.partial` and renames, so a directory without the file means a bug
    # rather than an in-flight download.
    assert not db_present(DEFAULT_DB)

    target = db_file(DEFAULT_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    assert db_present(DEFAULT_DB)


def test_partial_directory_reads_as_absent(tmp_path, monkeypatch):
    """An interrupted download must be invisible to every consumer (R4)."""
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    partial = settings.checkm2_db_dir / f"{DEFAULT_DB}.partial"
    partial.mkdir(parents=True)
    # Even fully populated, a `.partial` directory is not the final path.
    inner = partial / "CheckM2_database"
    inner.mkdir(parents=True)
    (inner / "uniref100.KO.1.dmnd").write_bytes(b"x")

    assert not db_present(DEFAULT_DB)


def test_unknown_key_is_absent(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    assert not db_present("no-such-database")
