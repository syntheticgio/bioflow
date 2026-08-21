"""The CheckM2 quality-prediction database, and where it lives.

One entry, not three. Kraken2 offers a choice because the size/coverage
trade-off there is a real user decision; CheckM2 ships exactly one database
and the user has nothing to pick, so the card needs no picker. The registry
shape is still right -- it is what carries the pin, the checksum and the
memory cost -- it just holds a single row (spec Q1).

Pinned to Zenodo record 14897628 -- **DIAMONDDB version 3**, published
2025-02-20 -- never the concept record 4626518. That distinction is the whole
point: the concept DOI is Zenodo's "latest" alias and resolves to whatever
the newest version happens to be, which is exactly the moving target that
makes two runs of the same bin incomparable with nothing to say why.

Which version is not a free choice. CheckM2 ships
`version/version_hashes_<ver>.json` listing each database version with an
`incompatible_below_checkm2ver` floor, and 1.1.0's entry for DIAMONDDB
version 3 reads `"incompatible_below_checkm2ver": "1.1.0"` -- so 1.1.0
requires v3 and rejects the older v2 (record 7563512, DOI 10.5281/zenodo.5571251).
Bumping CHECKM2_VERSION therefore means re-reading that file, not assuming
this URL still applies.

`mem_mb` is the in-RAM cost of a predict run with the database loaded --
known a priori from the database, never fitted from the memory model,
because a model fit from unrelated jobs would under-provision exactly into
an OOM (spec Q1, the reasoning kraken_db_registry records from spec K2-C3).

Deliberately NOT using `checkm2 database --download`, even though it exists:
it resolves its own URL at runtime, which is the moving target the pin
exists to prevent, and it puts integrity outside this repo's control.
"""

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class CheckM2DbSpec:
    key: str
    label: str
    url: str  # pinned dated record, never a "latest"/concept alias
    download_bytes: int
    mem_mb: int  # in-RAM cost of a predict run, with headroom
    md5: str  # of the tarball, from the Zenodo record's own metadata
    description: str  # one line: what this database scores


DEFAULT_DB = "uniref100"

CHECKM2_DBS: dict[str, CheckM2DbSpec] = {
    "uniref100": CheckM2DbSpec(
        key="uniref100",
        label="CheckM2 UniRef100/KO (v3)",
        url=(
            "https://zenodo.org/records/14897628/files/"
            "checkm2_database.tar.gz?download=1"
        ),
        download_bytes=1735095710,
        # MEASURED, not fitted and not guessed -- see the module docstring.
        # A real `checkm2 predict` over three bacterial genomes (~12 MB of
        # sequence, 4 threads) peaked at 8,347 MB RSS on 2026-08-21, timed
        # with getrusage(RUSAGE_CHILDREN) so the DIAMOND child is included.
        # 12 GB is that peak plus headroom: the residency is dominated by
        # DIAMOND holding reference blocks from the 3.1 GB .dmnd, which is a
        # property of the database rather than of how many bins are scored,
        # so it does not grow with N the way a per-bin cost would.
        mem_mb=12288,
        md5="07c10655620843b517d0df0c160d911f",
        description=(
            "The UniRef100-derived DIAMOND database CheckM2 scores "
            "completeness and contamination against."
        ),
    ),
}

# The file CheckM2 actually opens, relative to the database directory. The
# tarball extracts a `CheckM2_database/` directory holding this one file.
_DB_FILE = "CheckM2_database/uniref100.KO.1.dmnd"


def db_present(key: str) -> bool:
    """Whether this database is fully on disk.

    Checks the .dmnd file CheckM2 opens rather than the directory: the
    download handler extracts into a `.partial` directory and renames on
    success, so a bare directory with the file missing means a bug rather
    than an in-flight download, and either way it must read as absent.
    """
    if key not in CHECKM2_DBS:
        return False
    return (settings.checkm2_db_dir / key / _DB_FILE).is_file()


def db_file(key: str) -> "object":
    """The path to pass CheckM2's `--database_path`.

    CheckM2 1.1.0 accepts `--database_path` (per run) or the `CHECKM2DB`
    environment variable. The flag wins here: an explicit per-run path is
    testable and cannot be shadowed by ambient environment state.
    """
    return settings.checkm2_db_dir / key / _DB_FILE
