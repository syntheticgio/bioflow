"""Which pipelines to suggest for a file, and why.

A presentation layer over `pipeline_service`, not a second copy of its
judgement: tool selection, chemistry lookup and reference resolution all
delegate there. What lives here is the mapping from "what we know about this
file" to "what the Actions tab should offer", including the honest reason a
card cannot run.

Every card is `available` with a launch payload or `unavailable` with a
reason. There is deliberately no third state where a gated card runs its own
prerequisite -- that is DAG behaviour, and a real pipeline system will
replace it rather than inherit it.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.models import DataObject, FormatKind
from app.pipelines import align_runner, aligner_registry, tools
from app.pipelines.aligners import Aligner
from app.services import pipeline_service


class CardStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


# Genus -> domain. Hand-maintained and deliberately small: `organism` is free
# text, and this only has to separate "has introns" from "does not" well
# enough to pick a short-read aligner. Matched on the first word of the name.
#
# An unrecognised genus is treated as eukaryotic (see `is_eukaryotic`), so
# this table only needs the prokaryotes it is likely to meet.
_PROKARYOTE_GENERA: frozenset[str] = frozenset({
    "escherichia", "bacillus", "staphylococcus", "streptococcus",
    "salmonella", "pseudomonas", "mycobacterium", "listeria",
    "campylobacter", "clostridium", "vibrio", "helicobacter",
    "neisseria", "klebsiella", "acinetobacter", "enterococcus",
    "lactobacillus", "borrelia", "rickettsia", "chlamydia",
})


def is_eukaryotic(organism: str | None) -> bool:
    """Whether splice-aware alignment is appropriate for this organism.

    Unrecognised and missing names default to True. The asymmetry is
    deliberate: hisat2 on an intron-free genome simply finds no junctions,
    while a non-splice-aware aligner on a genome that has them drops real
    alignments without saying so.
    """
    if not organism or not organism.strip():
        return True
    genus = organism.strip().split()[0].lower()
    return genus not in _PROKARYOTE_GENERA


@dataclass(frozen=True)
class SuggestionCard:
    """One pipeline offer.

    `launch` is `{"endpoint": str, "body": dict}` where `body` is the
    *complete* JSON body for that endpoint, assembled server-side where the
    object id and its defaults are known. The frontend posts it verbatim and
    adds nothing -- the three launch endpoints do not share a request shape
    (`/variants` keys on `bam_id`, the others on `object_id`), so anything
    the client had to merge in would be a shape it had to know about.

    `launch` and `status` must agree: an available card without a payload
    would render as a button that does nothing.
    """

    kind: str
    category: str
    title: str
    description: str
    why: str | None = None
    status: CardStatus = CardStatus.UNAVAILABLE
    reason: str | None = None
    launch: dict | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "why": self.why,
            "status": self.status.value,
            "reason": self.reason,
            "launch": self.launch,
        }


def _is_long_read(chemistry: align_runner.ReadChemistry | None) -> bool:
    return chemistry in (
        align_runner.ReadChemistry.ONT_SIMPLEX,
        align_runner.ReadChemistry.ONT_DUPLEX,
        align_runner.ReadChemistry.HIFI,
        align_runner.ReadChemistry.CLR,
    )


def build_preprocess_card(obj) -> SuggestionCard | None:
    """Adapter trimming and quality filtering.

    Never gated on chemistry. fastp's defaults are safe on both read types,
    and gating it would leave a freshly ingested FASTQ -- the common case,
    since QC has run on very few files -- with no runnable card at all.
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return None

    fastp = tools.fastp()
    chemistry = pipeline_service.read_chemistry(obj)
    long_read = _is_long_read(chemistry)

    description = (
        "Length and quality filtering for long reads."
        if long_read
        else "Adapter trim and length filter."
    )
    why = (
        "Long reads carry no short-read adapters to trim."
        if long_read
        else "Short-read defaults: adapter detection plus a length floor."
    )

    if not fastp.available:
        return SuggestionCard(
            kind="preprocess",
            category="PREPROCESS",
            title="Trim & filter -- fastp",
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason="fastp is not installed.",
        )

    return SuggestionCard(
        kind="preprocess",
        category="PREPROCESS",
        title="Trim & filter -- fastp",
        description=description,
        why=why,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/trim",
            # The complete TrimRequest body: tool settings nest under
            # `params`, and the mate is left out so the server detects it.
            "body": {
                "object_id": str(obj.id),
                "tool": "fastp",
                "params": pipeline_service.default_params("fastp"),
            },
        },
    )


@dataclass(frozen=True)
class ReferenceChoice:
    """Which reference the align card would use, or why it cannot."""

    reference_id: str | None = None
    reference_name: str | None = None
    usable: bool = False
    reason: str | None = None


def resolve_reference(
    references: list[DataObject], organism: str | None
) -> ReferenceChoice:
    """Pick the reference to align against.

    Order is load-bearing. A single uploaded reference wins outright, before
    metadata is consulted: a project with one reference and a known organism
    should align against the file the user actually has, not refuse in favour
    of an accession nothing can fetch yet.

    Note what this deliberately does *not* do: turn an organism into an
    assembly accession. `assembly_accession` is a field on reference files,
    not on the reads this card renders against, and going from a species name
    to an accession means an NCBI call -- which would put a network round trip
    behind every Actions tab render, to fill in a card that is disabled
    regardless. The card names the species; naming the assembly is work for
    whenever fetching is built, behind the launch rather than the render.
    """
    if len(references) == 1:
        only = references[0]
        return ReferenceChoice(
            reference_id=str(only.id), reference_name=only.name, usable=True
        )

    # Must stay below the single-reference branch. Hoisted to the top -- which
    # it invites, being the only branch that does not touch `references` -- a
    # project with one reference and a known organism would refuse to align
    # against the file it actually has.
    if organism and organism.strip():
        return ReferenceChoice(
            usable=False,
            reason=(
                f"Fetching a reference genome for {organism.strip()} is not "
                "wired up yet."
            ),
        )

    # Only ever reached with two or more, so the sort is not redundant with the
    # branch above.
    if references:
        # Oldest first: an ObjectId's hex sorts by the timestamp prefix it
        # starts with, so this names the same reference on every render rather
        # than merely a stable-but-arbitrary one. Switching the key to `name`
        # for alphabetical tidiness would quietly change which one is chosen.
        # `str()` because ids are PydanticObjectId in production and plain str
        # in the tests, which are not mutually comparable.
        chosen = sorted(references, key=lambda r: str(r.id))[0]
        return ReferenceChoice(
            reference_id=str(chosen.id), reference_name=chosen.name, usable=True
        )

    return ReferenceChoice(usable=False, reason="Upload a reference to align.")


# Assays whose reads cross exon junctions. Both spellings of single-cell RNA
# appear in `assay`'s option list, and a scRNA library is still spliced cDNA.
_SPLICED_ASSAYS: frozenset[str] = frozenset({"rna-seq", "scrna-seq"})


def _align_tool_and_why(obj, chemistry) -> tuple[dict, str]:
    """The alignment params to launch with, and the sentence justifying them.

    Tool choice is `pipeline_service.default_align_params`' job, not this
    module's: it already encodes that bwa-mem2 is x86-64 only and that long
    reads belong to minimap2 whatever else is installed. Re-deriving any of
    that here would make the card advertise an aligner the launch endpoint
    would then refuse.

    Splice-awareness is the one thing layered on top, because it is a
    property of the *library* rather than of the reads or the host, and
    nothing below this line knows the assay. It is withheld from prokaryotes
    deliberately -- an intron-free genome has no junctions to find, so hisat2
    would buy nothing and cost a second index.
    """
    params = pipeline_service.default_align_params(obj)

    metadata = obj.metadata or {}
    assay = str(metadata.get("assay") or "").strip().lower()
    organism = metadata.get("organism")

    # Short reads only: hisat2 is a short-read aligner, so an ONT RNA-seq run
    # would align far worse under it than under the long-read choice above.
    if (
        assay in _SPLICED_ASSAYS
        and not _is_long_read(chemistry)
        and is_eukaryotic(organism)
    ):
        # `preset` is left as the delegate wrote it. It is a minimap2 concept,
        # but `Hisat2Params.from_dict` reads only the keys it knows and
        # ignores the rest, so a stray preset is inert rather than a bad flag.
        params = {**params, "aligner": "hisat2"}
        return params, "RNA-seq on a eukaryote: splice-aware alignment."

    # QC already wrote a human-readable justification for the chemistry it
    # inferred. Preferring it keeps the card agreeing with the QC report
    # rather than inventing a second, vaguer account of the same decision.
    reason = (obj.facts or {}).get("qc_read_chemistry_reason")
    if reason:
        return params, str(reason)

    return params, "Chosen from the reads' chemistry and this host's tools."


def build_align_card(obj, references: list[DataObject]) -> SuggestionCard | None:
    """Align reads to a reference.

    Three gates fail independently, so the reason has to be able to name more
    than one. Order is load-bearing where both apply: the reference is what
    the user can fix right now, while chemistry means waiting on a QC job, so
    leading with QC would hide the actionable half behind the slow one.

    A missing tool suppresses both. Neither uploading a reference nor running
    QC makes the card runnable while the binary is absent, so listing them
    alongside would send the user off to do work that changes nothing.
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return None

    chemistry = pipeline_service.read_chemistry(obj)
    choice = resolve_reference(references, (obj.metadata or {}).get("organism"))
    params, why = _align_tool_and_why(obj, chemistry)

    aligner = params["aligner"]
    # Through the registry -- the single place an aligner is declared -- rather
    # than a local table that would drift from it. `Aligner(...)` raises on a
    # name the registry has never heard of, so an unknown aligner fails here
    # instead of rendering a card whose launch the endpoint would refuse.
    kind = Aligner(aligner)
    spec = aligner_registry.spec_for(kind)

    # Only minimap2 takes a preset, so only its title names one. The delegate
    # leaves the key populated on the hisat2 override, where showing it would
    # describe a flag that aligner never receives.
    preset = (params.get("preset") or "") if kind is Aligner.MINIMAP2 else ""
    title = f"{aligner} {preset} -> BAM" if preset else f"{aligner} -> BAM"
    description = (
        f"Align to {choice.reference_name}, sort and index."
        if choice.usable and choice.reference_name
        else "Align these reads against a reference, sort and index."
    )

    # bowtie2 and hisat2 index through a separate binary, each configured by
    # its own setting. Gating on the aligner alone would render an available
    # card whose launch then *succeeds* -- `launch_alignment` requires only the
    # aligner -- and fails later inside the async index job, far from the click
    # that caused it.
    tool = spec.tool()
    builder = spec.builder_tool() if spec.builder_tool else None
    missing = next(
        (t for t in (tool, builder) if t is not None and not t.available), None
    )
    if missing is not None:
        return SuggestionCard(
            kind="align",
            category="ALIGN",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            # Names the binary that is actually absent: sending someone to
            # install hisat2 when hisat2-build is the gap wastes the trip.
            reason=f"{missing.name} is not installed.",
        )

    # Reference before chemistry; see the docstring. Built as a list so the
    # order is one obvious edit rather than nested string formatting.
    blockers: list[str] = []
    if not choice.usable:
        blockers.append(choice.reason or "No reference is available.")
    if chemistry is None:
        blockers.append("Run QC to determine read chemistry.")

    if blockers:
        return SuggestionCard(
            kind="align",
            category="ALIGN",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=" ".join(blockers),
        )

    return SuggestionCard(
        kind="align",
        category="ALIGN",
        title=title,
        description=description,
        why=why,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/align",
            # The complete AlignRequest body. `read_group` is omitted rather
            # than filled: `launch_alignment` merges what it is sent over
            # `default_read_group(obj)`, so sending nothing yields the server's
            # own defaults, while sending a card-invented @RG line would
            # override them with a worse guess.
            "body": {
                "object_id": str(obj.id),
                "reference_id": choice.reference_id,
                "params": params,
            },
        },
    )
