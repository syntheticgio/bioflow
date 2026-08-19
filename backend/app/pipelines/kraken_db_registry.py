"""The Kraken2 databases this application offers, and where they live.

Three pre-built indexes from the Langmead k2 collection
(https://benlangmead.github.io/aws-indexes/k2), pinned to the 6/26/2026
snapshot -- the newest listed at the time this registry was written. Full-size
databases are deliberately absent: they need on the order of 100 GB RAM to
load, which guarantees an OOM on the local machines this application targets
(spec 2026-08-18-kraken2-classification-design.md).

`mem_mb` is the in-RAM load size for the classify job's `JobResources` --
known a priori from the database, never fitted from the memory model,
because a model fit from unrelated jobs would under-provision exactly
into an OOM (spec K2-C3).
"""

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class KrakenDbSpec:
    key: str
    label: str
    url: str  # pinned dated snapshot, never a "latest" alias
    download_bytes: int
    mem_mb: int  # in-RAM load size, with headroom
    md5: str  # of the tarball, from the index's own .md5 file
    description: str  # one line: what this database can see


DEFAULT_DB = "standard-8"

KRAKEN_DBS: dict[str, KrakenDbSpec] = {
    "standard-8": KrakenDbSpec(
        key="standard-8",
        label="Standard-8",
        url="https://genome-idx.s3.amazonaws.com/kraken/k2_standard_08_GB_20260626.tar.gz",
        download_bytes=5946578575,
        mem_mb=9216,
        md5="7685f43cce057c2ca18511c925399b72",
        description=(
            "Archaea, bacteria, viruses, plasmids, human, and common "
            "vector contaminants -- the default for identity and "
            "contamination screening."
        ),
    ),
    "pluspf-8": KrakenDbSpec(
        key="pluspf-8",
        label="PlusPF-8",
        url="https://genome-idx.s3.amazonaws.com/kraken/k2_pluspf_08_GB_20260626.tar.gz",
        download_bytes=5933654083,
        mem_mb=9216,
        md5="79a153b99f045bc2ae95e6d57c17a02d",
        description="Standard plus protozoa and fungi.",
    ),
    "viral": KrakenDbSpec(
        key="viral",
        label="Viral",
        url="https://genome-idx.s3.amazonaws.com/kraken/k2_viral_20260626.tar.gz",
        download_bytes=572487594,
        mem_mb=1024,
        md5="2eecc5fe6eef12cb23b524996bfb7d08",
        description=(
            "Viruses only -- small and fast, but blind to bacterial or "
            "human contamination."
        ),
    ),
}


def db_present(key: str) -> bool:
    """Whether this database is fully on disk.

    Checks the three .k2d files Kraken2 needs rather than the directory:
    the download handler extracts into a .partial dir and renames on
    success, so a bare directory with missing files means a bug rather
    than an in-flight download, and either way it must read as absent.
    """
    d = settings.kraken_dbs_dir / key
    return all((d / name).is_file() for name in ("hash.k2d", "opts.k2d", "taxo.k2d"))
