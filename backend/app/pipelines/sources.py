"""External data source catalog behind the Sources help page.

Separate from `tools.py` rather than folded into it: a `Tool` is a binary on
this machine, resolved with `shutil.which` and asked its version with a
subprocess call. A data source is neither -- there is no binary to find and
nothing to probe. NCBI Datasets, E-utilities, and the SRA are just URLs this
application calls, and the only thing that could be "probed" is whether the
network is up right now, which says nothing durable about the source itself.

Deliberately no version field. `Tool.version` exists because a trim
parameter set is meaningless without the fastp version that applied it --
that pairing is what belongs in a methods section. A data source has no
analogous artifact: NCBI Datasets is whatever the API returns today, there is
no build number to pin, and a page that showed one anyway would look like it
was reporting provenance without actually having any. Leaving the field out
entirely says that plainly; a blank or "unknown" string would just look like
a bug.
"""

from dataclasses import asdict, dataclass

# What a source *is*, for grouping on the help page. "api" and "database" are
# both NCBI services reached over HTTP, split because an API is something
# this application calls programmatically while a database entry here (the
# SRA record page) is a link handed to the user, not a call this code makes.
# "reference" is held for a future non-NCBI source and unused today; kept in
# the set now rather than added on first use, since `test_kind_is_from_the_
# known_set` is the thing that would need updating either way.
SOURCE_KINDS = ("api", "database", "reference")


@dataclass(frozen=True)
class DataSource:
    name: str
    kind: str
    summary: str
    # How *this application* uses the source -- the one thing no upstream
    # page can tell a user. Prose, so nothing can verify it mechanically:
    # describe behaviour, not endpoints, so it survives a change to the
    # request shape.
    usage: str
    homepage: str
    docs: str = ""
    citation: str = ""  # human-readable, for a methods section
    citation_url: str = ""
    terms: str = ""


DATA_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        name="NCBI Datasets",
        kind="api",
        summary=(
            "NCBI's genome-oriented data service. Given a GenBank (GCA) or "
            "RefSeq (GCF) assembly accession, returns the full assembly "
            "report in one request -- organism, strain, assembly name, "
            "submitter, release date, and sequencing statistics -- plus the "
            "per-sequence chromosome names and the genome package itself."
        ),
        usage=(
            "Recognizes an assembly accession embedded in an uploaded "
            "filename and looks up the published record to fill in "
            "organism, strain, and assembly metadata automatically, rather "
            "than asking a user to retype what NCBI already knows. The same "
            "accession also drives the 'download a reference assembly' job, "
            "and per-sequence chromosome names it returns are used to label "
            "contigs on the QC screen. Every call is best-effort: a network "
            "failure, a rate limit, or a retired accession skips the lookup "
            "rather than failing the ingest, since the uploaded file is "
            "still perfectly usable without it."
        ),
        homepage="https://www.ncbi.nlm.nih.gov/datasets/",
        docs="https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/rest-api/",
        terms="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    ),
    DataSource(
        name="NCBI E-utilities",
        kind="api",
        summary=(
            "NCBI's general-purpose Entrez query service. Used here against "
            "the SRA database: `esearch` resolves an accession to an "
            "internal UID, and `efetch` returns the full experiment package "
            "-- study, sample, platform, library layout, and submitter-"
            "supplied sample attributes -- as XML."
        ),
        usage=(
            "Recognizes an SRA-family accession (SRR/ERR/DRR and related "
            "prefixes) embedded in an uploaded filename and looks up its "
            "experiment record to fill in organism, platform, library "
            "strategy, and sample attributes automatically. Requests are "
            "throttled to stay under NCBI's unauthenticated rate limit and "
            "retried on a transient error, but a lookup that still fails "
            "skips the enrichment rather than failing the ingest -- the "
            "file is still a perfectly good file without it."
        ),
        homepage="https://www.ncbi.nlm.nih.gov/books/NBK25501/",
        docs="https://www.ncbi.nlm.nih.gov/books/NBK25501/",
        terms="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    ),
    DataSource(
        name="NCBI Sequence Read Archive",
        kind="database",
        summary=(
            "The public archive of raw sequencing data that SRA accessions "
            "(SRR, SRX, SRS, SRP, and their ENA/DDBJ equivalents) resolve "
            "to. This application does not query the SRA directly -- reads "
            "arrive through E-utilities and download through sra-tools --  "
            "but a recognized accession links to its record here."
        ),
        usage=(
            "When an uploaded file's SRA accession is recognized, the "
            "record page for that accession is offered as a link so a user "
            "can see the full submission -- related runs, the study it "
            "belongs to, submitter remarks -- beyond the fields this "
            "application pulls into its own metadata."
        ),
        homepage="https://www.ncbi.nlm.nih.gov/sra",
        docs="https://www.ncbi.nlm.nih.gov/sra/docs/",
        citation="Leinonen et al., Nucleic Acids Research 2011",
        citation_url="https://doi.org/10.1093/nar/gkq1019",
        terms="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    ),
)


def all_sources() -> list[dict]:
    """The catalog, serialized for the API.

    Mirrors `tool_with_meta`'s approach in tools.py: built via `asdict`
    rather than naming each field, so a field added to `DataSource` reaches
    the API without a second edit here.
    """
    return [asdict(s) for s in DATA_SOURCES]
