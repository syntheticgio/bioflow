"""Internal consistency of the Kraken2 database registry.

Keys are this repo's own strings, not an enum's members, so the
registry-audit exhaustiveness pattern does not apply; these assert the
analogous internal-consistency invariants instead (spec K2-D4).
"""

from app.pipelines.kraken_db_registry import DEFAULT_DB, KRAKEN_DBS, db_present


def test_default_key_is_registered():
    assert DEFAULT_DB in KRAKEN_DBS


def test_every_entry_is_complete():
    for key, spec in KRAKEN_DBS.items():
        assert spec.key == key
        assert spec.label
        assert spec.url.startswith("https://")
        assert spec.url.endswith(".tar.gz")
        assert spec.download_bytes > 0
        assert spec.mem_mb > 0
        assert len(spec.md5) == 32
        assert spec.description


def test_urls_are_unique_and_pinned():
    urls = [s.url for s in KRAKEN_DBS.values()]
    assert len(urls) == len(set(urls))
    # A pinned snapshot has a date component; "latest" aliases drift.
    for url in urls:
        assert "latest" not in url


def test_db_present_requires_all_three_k2d_files(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
    d = settings.kraken_dbs_dir / "standard-8"
    d.mkdir(parents=True)
    assert not db_present("standard-8")
    for name in ("hash.k2d", "opts.k2d", "taxo.k2d"):
        (d / name).write_bytes(b"x")
    assert db_present("standard-8")
