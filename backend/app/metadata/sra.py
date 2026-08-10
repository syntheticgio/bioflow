"""SRA accession detection and NCBI metadata lookup.

Sequencing data pulled from SRA arrives with the accession already in the
filename (`SRR11768093_1.fastq.gz`), and NCBI holds far better metadata than
anyone will retype by hand. Recognizing that and filling in organism, platform,
library strategy and sample attributes automatically is most of the value.

NCBI E-utilities is used rather than the `datasets` CLI: E-utilities covers SRA
directly (datasets is genome/taxonomy oriented), needs no binary in the image,
and `efetch` returns the full EXPERIMENT_PACKAGE including sample attributes.

Everything here is best-effort. A network failure, a rate limit, or a retired
accession must never fail an ingest -- the file is still a perfectly good file.
"""

import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from app.logging import get_logger

log = get_logger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TIMEOUT = 20
# NCBI allows 3 requests/second without an API key. We stay under it.
MIN_INTERVAL = 0.4
MAX_RETRIES = 2
# Identifies us to NCBI, which is what they ask of API consumers.
TOOL = "local-bio-pipeliner"

# INSDC accession prefixes: SRA (NCBI), ERA (EBI), DRA (DDBJ).
RUN_PREFIXES = ("SRR", "ERR", "DRR")
EXPERIMENT_PREFIXES = ("SRX", "ERX", "DRX")
SAMPLE_PREFIXES = ("SRS", "ERS", "DRS")
STUDY_PREFIXES = ("SRP", "ERP", "DRP")
ALL_PREFIXES = RUN_PREFIXES + EXPERIMENT_PREFIXES + SAMPLE_PREFIXES + STUDY_PREFIXES

# Anchored at a word boundary so `MYSRR123` does not match, but
# `SRR11768093_1.fastq.gz` and `run-SRR123.fastq` both do.
_ACCESSION_RE = re.compile(
    rf"(?:^|[^A-Za-z0-9])((?:{'|'.join(ALL_PREFIXES)})\d{{6,}})",
    re.IGNORECASE,
)

_last_request_at = 0.0


@dataclass
class SraMetadata:
    """Normalized subset of an SRA record."""

    run: str | None = None
    experiment: str | None = None
    sample: str | None = None
    study: str | None = None
    bioproject: str | None = None
    biosample: str | None = None
    organism: str | None = None
    taxon_id: str | None = None
    platform: str | None = None
    instrument: str | None = None
    library_strategy: str | None = None
    library_source: str | None = None
    library_selection: str | None = None
    library_layout: str | None = None
    study_title: str | None = None
    experiment_title: str | None = None
    total_spots: int | None = None
    total_bases: int | None = None
    sample_attributes: dict = field(default_factory=dict)

    def to_metadata(self) -> dict:
        """Map onto our schema's field names.

        Only fields we actually model are returned; the rest stay available on
        the dataclass for display without polluting searchable metadata.
        """
        out: dict = {}
        if self.run:
            out["sra_run"] = self.run
        if self.experiment:
            out["sra_experiment"] = self.experiment
        if self.sample:
            out["sra_sample"] = self.sample
        if self.study:
            out["sra_study"] = self.study
        if self.bioproject:
            out["bioproject"] = self.bioproject
        if self.biosample:
            out["biosample"] = self.biosample
            # The schema's `sample_id` field is the user-facing identifier for
            # the biological sample. Default it from the NCBI BioSample accession
            # when nothing else has set it -- an imported run should at least
            # carry *some* sample identifier automatically.
            out.setdefault("sample_id", self.biosample)
        if self.organism:
            out["organism"] = self.organism
        if self.instrument:
            out["platform"] = self.instrument
        if self.library_strategy:
            out["assay"] = _map_strategy(self.library_strategy)
        if self.library_source:
            out["library_source"] = _map_source(self.library_source)
            out["molecule_type"] = _map_molecule_type(self.library_source)
        if self.library_layout:
            out["read_type"] = (
                "paired-end" if self.library_layout.upper() == "PAIRED" else "single-end"
            )
        if self.study_title:
            out["study_title"] = self.study_title

        # Sample attributes are free-form TAG/VALUE pairs from the submitter.
        # A few map cleanly onto our schema; the rest are prefixed so they are
        # visible and searchable without colliding with our field names.
        for tag, value in self.sample_attributes.items():
            key = tag.strip().lower().replace(" ", "_")
            if key in ("source_name", "tissue", "cell_type", "organism_part"):
                out.setdefault("tissue", value)
            elif key in ("strain", "genotype", "developmental_stage", "treatment",
                         "disease", "sex", "age", "cell_line"):
                out.setdefault(key if key != "sex" else "sex", value)
            else:
                out[f"sra_{key}"] = value
        return out


# SRA library strategies map imperfectly onto our assay vocabulary; anything
# unrecognized passes through so information is never lost.
_STRATEGY_MAP = {
    "WGS": "WGS",
    "WXS": "WES",
    "RNA-SEQ": "RNA-seq",
    "ATAC-SEQ": "ATAC-seq",
    "CHIP-SEQ": "ChIP-seq",
    "BISULFITE-SEQ": "Bisulfite-seq",
    "AMPLICON": "Amplicon",
    "TARGETED-CAPTURE": "Targeted panel",
}


def _map_strategy(strategy: str) -> str:
    return _STRATEGY_MAP.get(strategy.strip().upper(), strategy)


# SRA library sources map onto our library_source vocabulary the same way
# strategies do -- anything unrecognized passes through unchanged.
_SOURCE_MAP = {
    "GENOMIC": "Genomic",
    "TRANSCRIPTOMIC": "Transcriptomic",
    "METAGENOMIC": "Metagenomic",
    "METATRANSCRIPTOMIC": "Metatranscriptomic",
    "SYNTHETIC": "Synthetic",
    "VIRAL RNA": "Viral RNA",
    "OTHER": "Other",
}

# Coarse DNA/RNA bucket derived from library source. Not a judgment call SRA
# makes explicitly -- METAGENOMIC and SYNTHETIC are assumed DNA-like (both are
# overwhelmingly genomic-DNA preps in practice), anything not recognized here
# maps to "Other" rather than a guess.
_SOURCE_TO_MOLECULE = {
    "GENOMIC": "DNA",
    "METAGENOMIC": "DNA",
    "SYNTHETIC": "DNA",
    "TRANSCRIPTOMIC": "RNA",
    "METATRANSCRIPTOMIC": "RNA",
    "VIRAL RNA": "RNA",
}


def _map_source(source: str) -> str:
    return _SOURCE_MAP.get(source.strip().upper(), source)


def _map_molecule_type(source: str) -> str:
    return _SOURCE_TO_MOLECULE.get(source.strip().upper(), "Other")


def parse_accession(filename: str) -> str | None:
    """Extract an SRA accession from a filename, or None.

    Handles the common shapes: `SRR11768093.fastq`, `SRR11768093_1.fastq.gz`,
    `SRX8321150_R2.fq`, and accessions embedded mid-name.
    """
    if not filename:
        return None
    match = _ACCESSION_RE.search(filename)
    return match.group(1).upper() if match else None


def accession_kind(accession: str) -> str | None:
    if not accession:
        return None
    prefix = accession[:3].upper()
    if prefix in RUN_PREFIXES:
        return "run"
    if prefix in EXPERIMENT_PREFIXES:
        return "experiment"
    if prefix in SAMPLE_PREFIXES:
        return "sample"
    if prefix in STUDY_PREFIXES:
        return "study"
    return None


def is_valid_accession(accession: str) -> bool:
    return bool(
        accession
        and re.fullmatch(
            rf"(?:{'|'.join(ALL_PREFIXES)})\d{{6,}}", accession.strip().upper()
        )
    )


# --- NCBI client ------------------------------------------------------------


def _throttle() -> None:
    """Keep under NCBI's 3 requests/second limit for unauthenticated use."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _get(url: str) -> bytes | None:
    """Fetch a URL, returning None on any failure.

    Enrichment is a bonus: NCBI being slow, rate-limiting us, or not knowing an
    accession must never turn into a failed ingest.
    """
    for attempt in range(MAX_RETRIES + 1):
        _throttle()
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": f"{TOOL}/0.1"}
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            # 429/5xx are worth retrying; a 400 will fail identically forever.
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(1.0 * (attempt + 1))
                continue
            log.warning("sra_http_error", url=url, status=e.code)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < MAX_RETRIES:
                time.sleep(1.0 * (attempt + 1))
                continue
            log.warning("sra_network_error", url=url, error=str(e))
            return None
    return None


def resolve_uid(accession: str) -> str | None:
    """Map an accession to an internal NCBI UID via esearch."""
    import json

    params = urllib.parse.urlencode(
        {"db": "sra", "term": accession, "retmode": "json", "tool": TOOL}
    )
    body = _get(f"{EUTILS}/esearch.fcgi?{params}")
    if body is None:
        return None
    try:
        ids = json.loads(body)["esearchresult"]["idlist"]
    except (ValueError, KeyError):
        log.warning("sra_esearch_unparseable", accession=accession)
        return None
    return ids[0] if ids else None


def fetch_xml(uid: str) -> str | None:
    params = urllib.parse.urlencode(
        {"db": "sra", "id": uid, "rettype": "xml", "tool": TOOL}
    )
    body = _get(f"{EUTILS}/efetch.fcgi?{params}")
    return body.decode("utf-8", errors="replace") if body else None


def lookup(accession: str) -> SraMetadata | None:
    """Resolve an accession to normalized metadata, or None."""
    accession = accession.strip().upper()
    if not is_valid_accession(accession):
        log.info("sra_invalid_accession", accession=accession)
        return None

    uid = resolve_uid(accession)
    if uid is None:
        log.info("sra_accession_not_found", accession=accession)
        return None

    xml = fetch_xml(uid)
    if xml is None:
        return None

    meta = parse_experiment_xml(xml, requested=accession)
    if meta is not None:
        log.info(
            "sra_lookup_ok",
            accession=accession,
            organism=meta.organism,
            strategy=meta.library_strategy,
        )
    return meta


def parse_experiment_xml(xml: str, requested: str | None = None) -> SraMetadata | None:
    """Parse an EXPERIMENT_PACKAGE into normalized metadata.

    `requested` disambiguates which run is meant when a package contains
    several: asking about SRR11768093 should report that run's statistics, not
    the first run in the experiment.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning("sra_xml_parse_failed", error=str(e))
        return None

    package = root.find(".//EXPERIMENT_PACKAGE")
    if package is None:
        package = root

    meta = SraMetadata()

    experiment = package.find(".//EXPERIMENT")
    if experiment is not None:
        meta.experiment = experiment.get("accession")
        title = experiment.findtext("TITLE")
        if title:
            meta.experiment_title = title.strip()

    study = package.find(".//STUDY")
    if study is not None:
        meta.study = study.get("accession")
    study_title = package.findtext(".//STUDY_TITLE")
    if study_title:
        meta.study_title = study_title.strip()

    for ext_id in package.findall(".//STUDY/IDENTIFIERS/EXTERNAL_ID"):
        if ext_id.get("namespace") == "BioProject":
            meta.bioproject = (ext_id.text or "").strip() or None

    sample = package.find(".//SAMPLE")
    if sample is not None:
        meta.sample = sample.get("accession")
        for ext_id in sample.findall("./IDENTIFIERS/EXTERNAL_ID"):
            if ext_id.get("namespace") == "BioSample":
                meta.biosample = (ext_id.text or "").strip() or None
        meta.organism = _text(sample, ".//SCIENTIFIC_NAME")
        meta.taxon_id = _text(sample, ".//TAXON_ID")

    platform = package.find(".//PLATFORM")
    if platform is not None:
        for child in platform:
            meta.platform = child.tag
            instrument = child.findtext("INSTRUMENT_MODEL")
            if instrument:
                meta.instrument = instrument.strip()
            break

    descriptor = package.find(".//LIBRARY_DESCRIPTOR")
    if descriptor is not None:
        meta.library_strategy = _text(descriptor, "LIBRARY_STRATEGY")
        meta.library_source = _text(descriptor, "LIBRARY_SOURCE")
        meta.library_selection = _text(descriptor, "LIBRARY_SELECTION")
        layout = descriptor.find("LIBRARY_LAYOUT")
        if layout is not None:
            for child in layout:
                meta.library_layout = child.tag
                break

    runs = package.findall(".//RUN")
    chosen = None
    if requested and accession_kind(requested) == "run":
        chosen = next((r for r in runs if r.get("accession") == requested), None)
    if chosen is None and runs:
        chosen = runs[0]
    if chosen is not None:
        meta.run = chosen.get("accession")
        meta.total_spots = _int(chosen.get("total_spots"))
        meta.total_bases = _int(chosen.get("total_bases"))

    for attribute in package.findall(".//SAMPLE_ATTRIBUTES/SAMPLE_ATTRIBUTE"):
        tag = attribute.findtext("TAG")
        value = attribute.findtext("VALUE")
        if tag and value and value.strip().lower() not in ("none", "n/a", "na", ""):
            meta.sample_attributes[tag.strip()] = value.strip()

    return meta


def _text(element, path: str) -> str | None:
    value = element.findtext(path)
    return value.strip() if value and value.strip() else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sra_url(accession: str) -> str:
    return f"https://www.ncbi.nlm.nih.gov/sra/{accession}"
