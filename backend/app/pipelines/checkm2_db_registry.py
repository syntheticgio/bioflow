"""The CheckM2 quality-prediction database, and where it lives.

One entry, not three. Kraken2 offers a choice because the size/coverage
trade-off there is a real user decision; CheckM2 ships exactly one database
and the user has nothing to pick, so the card needs no picker. The registry
shape is still right -- it is what carries the pin, the checksum and the
memory cost -- it just holds a single row (spec Q1).

Pinned to Zenodo record 7563512 (version `r202`, published 2023-01-24),
never the concept record 7563511. That distinction is the whole point: the
concept DOI is Zenodo's "latest" alias and resolves to whatever the newest
version happens to be, which is exactly the moving target that makes two
runs of the same bin incomparable with nothing to say why.

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
        label="CheckM2 r202 (UniRef100)",
        url=(
            "https://zenodo.org/records/7563512/files/"
            "r202_database.zb.tar.gz?download=1"
        ),
        download_bytes=9322373789,
        # Measured, not fitted -- see the module docstring. DIAMOND holds the
        # reference blocks resident for the duration of the search, and that
        # residency is a property of the database rather than of the bins.
        mem_mb=16384,
        md5="f512b37f35251403763173a001d3a0e7",
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
