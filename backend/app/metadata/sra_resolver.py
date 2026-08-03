"""Resolving an INSDC accession to the sequencing runs underneath it.

`sra.py` answers "what is this one file?" during ingest. This answers a
different question -- "what could I download?" -- for an accession that may
name a single run or a study containing hundreds. The two share the HTTP
client, the throttle, and the XML vocabulary; they differ in that this one
fans out and must stay usable when part of the fan-out fails.

**One query shape covers every accession type.** An earlier design had a table
of `elink` calls -- bioproject->sra, biosample->sra, and so on. That turned out
to be unnecessary: `esearch db=sra&term=<accession>` resolves runs, experiments,
samples, studies, BioProjects and BioSamples alike, because NCBI indexes all of
them as searchable fields on the SRA record. It is also more robust. `elink`
requires resolving the accession to a numeric UID in its *own* database first,
and returns an empty linkset rather than an error when that mapping is missing.

Everything here is best-effort in the same way `sra.py` is, with one
difference: a resolution that returns nothing is a result the user sees, not a
silent degradation. Failures are reported in `SraResolution.error` rather than
swallowed.
"""

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from app.logging import get_logger
from app.metadata import ncbi_assembly, sra

log = get_logger(__name__)

# How many UIDs to pull metadata for in one efetch. NCBI accepts far more, but
# the response is ~11 KB of XML per run: at 50 a 288-run study is six requests
# and about 3 MB, where one request for all 288 would be a single 3 MB response
# that fails entirely on a timeout.
FETCH_BATCH = 50

# A hard ceiling on how many runs one resolution will describe. The largest
# public BioProjects hold tens of thousands, and resolving one of those would
# spend minutes against NCBI to build a response no user can act on. The
# response says it was truncated rather than pretending it is complete.
MAX_RUNS = 1000

# Cached in Redis because the drill-down UI is a back-and-forth: opening a
# study, stepping into a sample, going back. An hour is well inside how often
# published run metadata changes (essentially never) and short enough that a
# newly released dataset is not stale for a working day.
CACHE_TTL_SECONDS = 3600

PLATFORMS = ("ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE")

# BioProject and BioSample sit outside the INSDC run hierarchy that sra.py
# knows about, but a user pastes them just as readily.
_BIOPROJECT_PREFIXES = ("PRJNA", "PRJEB", "PRJDB")
# NCBI issues SAMN; EBI issues SAMEA and SAMED; DDBJ issues SAMD. The EBI pair
# carries a letter *after* the SAME stem, which the digits-only pattern below
# would otherwise reject -- so the stem is matched and the optional letter is
# part of the pattern rather than the prefix list.
_BIOSAMPLE_PREFIXES = ("SAMN", "SAME", "SAMD")

# PRJNA631678, SAMN14886310, SAMEA3231268: a known prefix, an optional archive
# letter, then digits.
_DIGITS_AFTER_PREFIX = re.compile(
    rf"(?:{'|'.join(_BIOPROJECT_PREFIXES + _BIOSAMPLE_PREFIXES)})[A-Z]?\d+"
)


@dataclass
class RunInfo:
    """One sequencing run: the thing that can actually be downloaded."""

    accession: str
    experiment: str | None = None
    sample: str | None = None
    study: str | None = None
    bioproject: str | None = None
    biosample: str | None = None
    platform: str | None = None
    instrument: str | None = None
    library_strategy: str | None = None
    library_layout: str | None = None
    library_source: str | None = None
    spots: int | None = None
    bases: int | None = None
    # NCBI's own figure for the archived run, from the RUN element's `size`
    # attribute. Preferred over estimating from `bases`: the archive is
    # compressed, so a bases-derived guess is wrong by a factor that depends on
    # the data. Used for the disk pre-flight before a download.
    bytes: int | None = None
    organism: str | None = None
    title: str | None = None
    sample_attributes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class HierarchyNode:
    """A container above the run level, for the drill-down view."""

    accession: str
    kind: str  # "bioproject" | "biosample" | "study" | "experiment" | "sample"
    title: str | None = None
    platform: str | None = None
    organism: str | None = None
    child_count: int = 0
    total_bases: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SraResolution:
    accession: str
    kind: str
    title: str | None = None
    organism: str | None = None
    hierarchy: list[HierarchyNode] = field(default_factory=list)
    runs: list[RunInfo] = field(default_factory=list)
    total_run_count: int = 0
    total_bytes_estimate: int | None = None
    # True when the study holds more runs than MAX_RUNS. The UI says so rather
    # than showing a truncated list as though it were the whole thing.
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "accession": self.accession,
            "kind": self.kind,
            "title": self.title,
            "organism": self.organism,
            "hierarchy": [h.as_dict() for h in self.hierarchy],
            "runs": [r.as_dict() for r in self.runs],
            "total_run_count": self.total_run_count,
            "total_bytes_estimate": self.total_bytes_estimate,
            "truncated": self.truncated,
            "error": self.error,
        }


def classify(accession: str) -> str | None:
    """What kind of thing an accession names, or None if unrecognized.

    Extends `sra.accession_kind` past the INSDC run hierarchy to the two
    umbrella namespaces a user is just as likely to paste.
    """
    if not accession:
        return None
    upper = accession.strip().upper()
    # Checked before the INSDC prefixes: an assembly lives in a different NCBI
    # service (Datasets, not E-utilities) and resolves down a different path.
    if ncbi_assembly.is_valid_accession(upper):
        return "assembly"
    if upper.startswith(_BIOPROJECT_PREFIXES):
        return "bioproject"
    if upper.startswith(_BIOSAMPLE_PREFIXES):
        return "biosample"
    return sra.accession_kind(upper)


def is_resolvable(accession: str) -> bool:
    """Whether this looks like an accession worth sending to NCBI.

    Checked before the network call so a typo gets an immediate answer rather
    than a two-second round trip to be told nothing was found.
    """
    if not accession:
        return False
    upper = accession.strip().upper()
    kind = classify(upper)
    if kind == "assembly":
        # is_valid_accession already required the version suffix, which is what
        # separates a resolvable accession from a bare GCF_000002445.
        return True
    if kind in ("bioproject", "biosample"):
        return bool(_DIGITS_AFTER_PREFIX.fullmatch(upper))
    return sra.is_valid_accession(upper)


def search_uids(accession: str, *, retmax: int = MAX_RUNS) -> tuple[list[str], int]:
    """SRA UIDs for an accession, and NCBI's total count.

    The count is returned separately from the list because they differ when a
    study exceeds `retmax` -- that difference is exactly what tells the caller
    the result is truncated.
    """
    params = urllib.parse.urlencode(
        {
            "db": "sra",
            "term": accession,
            "retmode": "json",
            "retmax": str(retmax),
            "tool": sra.TOOL,
        }
    )
    body = sra._get(f"{sra.EUTILS}/esearch.fcgi?{params}")
    if body is None:
        return [], 0

    try:
        result = json.loads(body)["esearchresult"]
        return list(result.get("idlist", [])), int(result.get("count", 0))
    except (ValueError, KeyError, TypeError):
        log.warning("sra_resolve_esearch_unparseable", accession=accession)
        return [], 0


def search_runs_by_organism(
    organism: str,
    *,
    retstart: int = 0,
    retmax: int = 20,
    platform_filter: str | None = None,
) -> tuple[list[str], int]:
    """SRA UIDs for an organism name search, and NCBI's total count.

    Unlike `search_uids` (built for a single accession's runs, capped at
    `MAX_RUNS`), this is a real paginated search: an organism can back
    thousands of runs, and `retstart`/`retmax` are esearch's own offset and
    page-size parameters, so the caller pages forward without ever pulling
    more than one page's worth of packages.

    `platform_filter`, when given, is added to the query term itself
    (`AND ILLUMINA[Platform]`) rather than applied as a post-fetch filter like
    `resolve()` does: this search is offset-paginated over a result set that
    can hold thousands of runs, and filtering after the page is fetched would
    silently return fewer than `retmax` rows and make `total_count`/offset
    math lie about what is actually being paged through.
    """
    term = f"{organism}[Organism]"
    if platform_filter:
        term = f"{term} AND {platform_filter}[Platform]"
    params = urllib.parse.urlencode(
        {
            "db": "sra",
            "term": term,
            "retmode": "json",
            "retstart": str(retstart),
            "retmax": str(retmax),
            "tool": sra.TOOL,
        }
    )
    body = sra._get(f"{sra.EUTILS}/esearch.fcgi?{params}")
    if body is None:
        return [], 0

    try:
        result = json.loads(body)["esearchresult"]
        return list(result.get("idlist", [])), int(result.get("count", 0))
    except (ValueError, KeyError, TypeError):
        log.warning("sra_organism_search_unparseable", organism=organism)
        return [], 0


def fetch_packages(uids: list[str]) -> list[ET.Element]:
    """EXPERIMENT_PACKAGE elements for a list of UIDs, fetched in batches.

    A batch that fails is skipped rather than failing the resolution: 250 of
    288 runs is a usable answer, and the alternative is nothing at all.
    """
    packages: list[ET.Element] = []
    for start in range(0, len(uids), FETCH_BATCH):
        batch = uids[start : start + FETCH_BATCH]
        params = urllib.parse.urlencode(
            {"db": "sra", "id": ",".join(batch), "rettype": "xml", "tool": sra.TOOL}
        )
        body = sra._get(f"{sra.EUTILS}/efetch.fcgi?{params}")
        if body is None:
            log.warning("sra_resolve_batch_failed", offset=start, size=len(batch))
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            log.warning("sra_resolve_batch_unparseable", offset=start, error=str(e))
            continue
        found = root.findall(".//EXPERIMENT_PACKAGE")
        packages.extend(found if found else [root])
    return packages


def runs_from_package(package: ET.Element) -> list[RunInfo]:
    """Every run in one EXPERIMENT_PACKAGE.

    An experiment can hold several runs -- a library sequenced across lanes --
    and each is separately downloadable, so each becomes its own row.

    Deliberately tolerant. A missing field yields None rather than an
    exception: a run whose library layout NCBI never recorded is still a run
    the user can download, and dropping it because one attribute is absent
    would be worse than showing it incompletely.
    """
    common = _package_context(package)
    runs: list[RunInfo] = []

    for element in package.findall(".//RUN"):
        accession = element.get("accession")
        if not accession:
            continue
        runs.append(
            RunInfo(
                accession=accession,
                spots=_int(element.get("total_spots")),
                bases=_int(element.get("total_bases")),
                bytes=_int(element.get("size")),
                **common,
            )
        )
    return runs


def _package_context(package: ET.Element) -> dict:
    """The experiment/sample/study fields every run in a package shares."""
    context: dict = {
        "experiment": None,
        "sample": None,
        "study": None,
        "bioproject": None,
        "biosample": None,
        "platform": None,
        "instrument": None,
        "library_strategy": None,
        "library_layout": None,
        "library_source": None,
        "organism": None,
        "title": None,
        "sample_attributes": {},
    }

    experiment = package.find(".//EXPERIMENT")
    if experiment is not None:
        context["experiment"] = experiment.get("accession")
        title = experiment.findtext("TITLE")
        if title and title.strip():
            context["title"] = title.strip()

    study = package.find(".//STUDY")
    if study is not None:
        context["study"] = study.get("accession")
    for ext_id in package.findall(".//STUDY/IDENTIFIERS/EXTERNAL_ID"):
        if ext_id.get("namespace") == "BioProject":
            context["bioproject"] = (ext_id.text or "").strip() or None

    sample = package.find(".//SAMPLE")
    if sample is not None:
        context["sample"] = sample.get("accession")
        for ext_id in sample.findall("./IDENTIFIERS/EXTERNAL_ID"):
            if ext_id.get("namespace") == "BioSample":
                context["biosample"] = (ext_id.text or "").strip() or None
        context["organism"] = _text(sample, ".//SCIENTIFIC_NAME")

    platform = package.find(".//PLATFORM")
    if platform is not None:
        for child in platform:
            # The platform is the *tag name* -- <ILLUMINA>, <OXFORD_NANOPORE> --
            # not an attribute. This is what the QC dispatch keys on.
            context["platform"] = child.tag
            instrument = child.findtext("INSTRUMENT_MODEL")
            if instrument and instrument.strip():
                context["instrument"] = instrument.strip()
            break

    descriptor = package.find(".//LIBRARY_DESCRIPTOR")
    if descriptor is not None:
        context["library_strategy"] = _text(descriptor, "LIBRARY_STRATEGY")
        context["library_source"] = _text(descriptor, "LIBRARY_SOURCE")
        layout = descriptor.find("LIBRARY_LAYOUT")
        if layout is not None:
            for child in layout:
                context["library_layout"] = child.tag  # PAIRED | SINGLE
                break

    attributes: dict = {}
    for attribute in package.findall(".//SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE"):
        tag = attribute.findtext("TAG")
        value = attribute.findtext("VALUE")
        if tag and value and value.strip().lower() not in ("none", "n/a", "na", ""):
            attributes[tag.strip()] = value.strip()
    context["sample_attributes"] = attributes

    return context


def build_hierarchy(runs: list[RunInfo]) -> list[HierarchyNode]:
    """The containers the resolved runs belong to, for the drill-down view.

    Derived from the runs rather than fetched separately: the packages already
    name every parent, so a second round of queries would tell us what we
    already know.

    Grouped at the *sample* level. A study's structure is study -> sample ->
    experiment -> run, and the sample is the biologically meaningful middle:
    it is what a user recognizes ("liver, replicate 2") where an experiment
    accession is a library-prep detail.
    """
    by_sample: dict[str, list[RunInfo]] = {}
    for run in runs:
        key = run.biosample or run.sample or run.experiment or run.accession
        by_sample.setdefault(key, []).append(run)

    nodes: list[HierarchyNode] = []
    for key, members in by_sample.items():
        first = members[0]
        bases = [m.bases for m in members if m.bases]
        nodes.append(
            HierarchyNode(
                accession=key,
                kind="biosample" if first.biosample == key else "sample",
                title=first.title,
                platform=first.platform,
                organism=first.organism,
                child_count=len(members),
                total_bases=sum(bases) if bases else None,
            )
        )

    nodes.sort(key=lambda n: n.accession)
    return nodes


def resolve(accession: str, *, platform_filter: str | None = None) -> SraResolution:
    """Resolve any INSDC accession to the runs beneath it.

    Uncached: `resolve_cached` is the entry point callers should use.
    """
    accession = (accession or "").strip().upper()
    kind = classify(accession) or "unknown"

    if kind == "assembly":
        # This resolver answers "which runs can I download". An assembly has
        # none: it is a published genome, resolved through
        # ncbi_assembly_components and downloaded by a different handler. Returning
        # an explanatory error beats an esearch that truthfully reports no
        # sequencing runs and reads as "this accession is broken".
        return SraResolution(
            accession=accession,
            kind=kind,
            error=(
                f"{accession} is a genome assembly, not sequencing data. "
                "Resolve it through the assembly endpoint."
            ),
        )

    if not is_resolvable(accession):
        return SraResolution(
            accession=accession,
            kind=kind,
            error=(
                f"{accession!r} is not a recognized accession. Expected a run "
                "(SRR/ERR/DRR), experiment (SRX), sample (SRS), study (SRP), "
                "BioProject (PRJNA), or BioSample (SAMN)."
            ),
        )

    uids, total = search_uids(accession)
    if not uids:
        return SraResolution(
            accession=accession,
            kind=kind,
            error=f"No sequencing runs found for {accession} at NCBI.",
        )

    packages = fetch_packages(uids)
    if not packages:
        return SraResolution(
            accession=accession,
            kind=kind,
            total_run_count=total,
            error=(
                f"Found {total} record(s) for {accession}, but NCBI returned no "
                "usable metadata. This is usually transient -- try again."
            ),
        )

    runs: list[RunInfo] = []
    for package in packages:
        try:
            runs.extend(runs_from_package(package))
        except Exception as e:  # noqa: BLE001 - one bad package must not lose the rest
            log.warning("sra_resolve_package_failed", accession=accession, error=str(e))

    # A run accession resolves to its whole experiment, which may hold sibling
    # runs the user did not ask about. Narrow to the one named.
    if kind == "run":
        exact = [r for r in runs if r.accession == accession]
        if exact:
            runs = exact

    all_runs = runs
    if platform_filter:
        runs = [r for r in runs if (r.platform or "").upper() == platform_filter.upper()]

    first = all_runs[0] if all_runs else None
    sizes = [r.bytes for r in runs if r.bytes]

    resolution = SraResolution(
        accession=accession,
        kind=kind,
        title=first.title if first else None,
        organism=first.organism if first else None,
        hierarchy=build_hierarchy(runs),
        runs=runs,
        total_run_count=len(runs),
        total_bytes_estimate=sum(sizes) if sizes else None,
        truncated=total > len(uids),
    )

    if platform_filter and not runs and all_runs:
        # Not an error: the accession resolved fine, the filter just excluded
        # everything. Saying so beats an empty table with no explanation.
        found = sorted({r.platform for r in all_runs if r.platform})
        resolution.error = (
            f"No {platform_filter} runs in {accession}. "
            f"It contains: {', '.join(found) or 'runs with no platform recorded'}."
        )

    log.info(
        "sra_resolved",
        accession=accession,
        kind=kind,
        runs=len(runs),
        of_total=total,
        truncated=resolution.truncated,
        platform_filter=platform_filter,
    )
    return resolution


async def resolve_cached(
    accession: str, *, platform_filter: str | None = None
) -> SraResolution:
    """`resolve`, memoized in Redis for an hour.

    The network calls are synchronous and slow enough to block the event loop
    -- a 288-run study is six round trips to NCBI -- so the miss path runs in a
    worker thread.

    A cache failure is never fatal. Redis being unavailable makes this slower,
    not broken, which is the right trade for what is fundamentally a lookup.
    """
    import asyncio

    key = f"sra:resolve:{accession.strip().upper()}:{platform_filter or 'all'}"

    try:
        from app.db.redis_client import get_redis

        cached = await get_redis().get(key)
        if cached:
            log.debug("sra_resolve_cache_hit", accession=accession)
            return _from_dict(json.loads(cached))
    except Exception as e:  # noqa: BLE001 - a cache miss must never fail a lookup
        log.debug("sra_resolve_cache_read_failed", error=str(e))

    resolution = await asyncio.to_thread(
        resolve, accession, platform_filter=platform_filter
    )

    # Errors are not cached: they are usually transient (a rate limit, a
    # timeout), and caching one would make a retry pointless for an hour.
    if resolution.error is None:
        try:
            from app.db.redis_client import get_redis

            await get_redis().set(
                key, json.dumps(resolution.as_dict()), ex=CACHE_TTL_SECONDS
            )
        except Exception as e:  # noqa: BLE001
            log.debug("sra_resolve_cache_write_failed", error=str(e))

    return resolution


def _from_dict(raw: dict) -> SraResolution:
    return SraResolution(
        accession=raw.get("accession", ""),
        kind=raw.get("kind", "unknown"),
        title=raw.get("title"),
        organism=raw.get("organism"),
        hierarchy=[HierarchyNode(**h) for h in raw.get("hierarchy", [])],
        runs=[RunInfo(**r) for r in raw.get("runs", [])],
        total_run_count=raw.get("total_run_count", 0),
        total_bytes_estimate=raw.get("total_bytes_estimate"),
        truncated=raw.get("truncated", False),
        error=raw.get("error"),
    )


def _text(element: ET.Element, path: str) -> str | None:
    value = element.findtext(path)
    return value.strip() if value and value.strip() else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
