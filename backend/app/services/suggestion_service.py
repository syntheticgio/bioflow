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

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.config import settings
from app.errors import ValidationError
from app.logging import get_logger
from app.models import DataObject, FormatKind, ObjectRole, ObjectStatus
from app.pipelines import (
    align_runner,
    aligner_registry,
    assembler_registry,
    assembly_qc_registry,
    lineage_inference,
    tools,
    variant_runner,
)
from app.pipelines.aligners import Aligner
from app.pipelines.organism_taxonomy import is_eukaryotic
from app.services import object_service, pipeline_service, prior_runs

log = get_logger(__name__)


class CardStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


# Re-exported from organism_taxonomy: `lineage_inference` needed the same
# genus classification for compleasm's lineage choice, and this module is in
# app/services while pipelines is the lower layer, so the table moved down
# rather than being duplicated or imported upward. `is_eukaryotic` stays
# importable from here since that is this module's own public name for it,
# used at line ~339 below and by this file's existing tests.


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
    # Runs that already did what this card offers. Filled by
    # `attach_prior_runs` after the builders run, never by a builder -- it is
    # a database question, and the builders are deliberately synchronous and
    # pure.
    prior_runs: list = field(default_factory=list)

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
            "prior_runs": self.prior_runs,
        }


# How to name a chemistry in a sentence. Only the long-read ones: the card
# that uses this is unreachable for the others.
_CHEMISTRY_LABELS: dict = {
    align_runner.ReadChemistry.HIFI: "PacBio HiFi",
    align_runner.ReadChemistry.CLR: "PacBio CLR",
    align_runner.ReadChemistry.ONT_SIMPLEX: "Nanopore simplex",
    align_runner.ReadChemistry.ONT_DUPLEX: "Nanopore duplex",
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


# `GCF_000002445.2_ASM244v1_genomic.fna` -> `ASM244v1`. Mirrors
# `parseAssemblyName` in FileHeadline.tsx, which the align *button* already
# uses to decide it can name one assembly; the card has to agree with the
# button sitting directly above it.
_NCBI_ASSEMBLY = re.compile(
    r"^GC[AF]_\d+\.\d+_(.+?)(?:_genomic)?$", re.IGNORECASE
)


def _assembly_name(filename: str) -> str | None:
    """The assembly a reference filename names, or None if it does not."""
    stem = re.sub(r"\.(fa|fna|fasta)(\.gz)?$", "", filename, flags=re.IGNORECASE)
    match = _NCBI_ASSEMBLY.match(stem)
    return match.group(1) if match else None


def _distinct_assemblies(references: list[DataObject]) -> list[DataObject]:
    """One entry per assembly, keeping the oldest file of each.

    Two copies of `GCF_000002445.2_ASM244v1_genomic.fna` are one reference to
    a user, however many rows they occupy. A filename that names no assembly
    cannot be shown to duplicate anything, so it stays a candidate of its own.
    """
    seen: dict[str, DataObject] = {}
    out: list[DataObject] = []
    for ref in sorted(references, key=lambda r: str(r.id)):
        name = _assembly_name(ref.name)
        if name is None:
            out.append(ref)
            continue
        if name not in seen:
            seen[name] = ref
            out.append(ref)
    return out


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
    # By distinct *assembly*, not by file. A project that downloaded a genome
    # twice, or holds the same assembly under two filenames, has one reference
    # as far as a user is concerned -- counting files would send it to the
    # organism branch below and refuse to align against a genome it plainly
    # has. Files whose names carry no parseable assembly are each their own
    # candidate, which is the conservative reading.
    distinct = _distinct_assemblies(references)
    if len(distinct) == 1:
        only = distinct[0]
        return ReferenceChoice(
            reference_id=str(only.id), reference_name=only.name, usable=True
        )

    # Must stay below the single-reference branch. Hoisted to the top -- which
    # it invites, being the only branch that does not touch `references` -- a
    # project with one reference and a known organism would refuse to align
    # against the file it actually has.
    if organism and organism.strip():
        # Reached with either zero distinct assemblies or several, and those
        # are not the same situation. They shared one message until 2026-08-01,
        # so a project holding two perfectly good genomes was told that
        # fetching one "is not wired up yet" -- a sentence about an empty
        # project, rendered beside two references the user could see.
        #
        # The refusal is still right: the card picks on its own, so it declines
        # rather than guess between distinct genomes (see
        # `test_two_genuinely_different_assemblies_are_two_references`, and the
        # `protein.faa` bug in `dd4fae2` that established the rule). What
        # changed is only that it now declines for the true reason and names
        # the way forward -- the align dialog, where a human does the picking
        # this function deliberately will not do.
        if distinct:
            return ReferenceChoice(
                usable=False,
                reason=(
                    f"This project has {len(distinct)} references. Open Align "
                    "to choose one."
                ),
            )
        return ReferenceChoice(
            usable=False,
            reason=(
                f"Fetching a reference genome for {organism.strip()} is not "
                "wired up yet."
            ),
        )

    # Only ever reached with two or more distinct assemblies, so the sort is
    # not redundant with the branch above.
    if distinct:
        # Oldest first: an ObjectId's hex sorts by the timestamp prefix it
        # starts with, so this names the same reference on every render rather
        # than merely a stable-but-arbitrary one. Switching the key to `name`
        # for alphabetical tidiness would quietly change which one is chosen.
        # `str()` because ids are PydanticObjectId in production and plain str
        # in the tests, which are not mutually comparable.
        chosen = sorted(distinct, key=lambda r: str(r.id))[0]
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
        # HISAT2 and not STAR, deliberately, even though STAR is the faster
        # aligner and the one most published RNA-seq pipelines use. This is a
        # single-user tool on one machine, and the two indexes are not
        # comparable: ~4 GB for HISAT2 against ~30 GB resident for STAR on a
        # human genome. Suggesting STAR would put a card on the Actions tab
        # that the memory estimator blocks on most hardware -- advice that
        # cannot be taken is worse than slightly slower advice that can.
        #
        # STAR stays a deliberate choice from the align dialog, where the
        # estimator's warning is visible next to the thread and memory knobs.
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


def build_variants_card(obj, chemistry) -> SuggestionCard | None:
    """Call variants against the reference this BAM was aligned to.

    Chemistry is a parameter rather than something read here. On a BAM it may
    live on the parent FASTQ, and reaching it is
    `pipeline_service.read_chemistry_for_alignment`'s async database walk --
    which the endpoint has already done by the time it calls this. Taking the
    resolved value keeps this module's builders uniformly synchronous and pure.

    Caller choice comes from `variant_runner.caller_for_chemistry` rather than
    `pipeline_service.default_variant_params`. The delegate encodes the same
    mapping, but it is async purely to re-resolve the chemistry the caller just
    handed us, and it flattens the CLR refusal to `{"caller": None}` -- so this
    card would have to special-case CLR anyway and would pay a second
    provenance walk for the privilege. Going to the shared source of the
    mapping directly gets the same answer with neither cost, and the CLR
    branch below re-raises through that same function so the card's wording
    cannot drift from the launch endpoint's refusal.
    """
    if obj.format.kind is not FormatKind.BAM:
        return None

    long_read = _is_long_read(chemistry)
    title = (
        "Clair3 long-read calls" if long_read else "bcftools short-read calls"
    )
    description = "Call variants against this alignment's reference."

    if chemistry is align_runner.ReadChemistry.CLR:
        # Rendered, not raised. The launch path raises this exact message; the
        # card is that refusal shown before the click rather than after it, so
        # the text comes from the same function instead of being re-typed.
        try:
            variant_runner.caller_for_chemistry(chemistry)
        except ValidationError as exc:
            reason = str(exc)
        else:
            # Unreachable while CLR is refused. Deliberately not a fallback
            # string: a paraphrase here would be the re-typed wording this
            # branch exists to avoid, and it would go stale silently the day
            # CLR became callable. Failing loudly says which contract moved.
            raise AssertionError(
                "caller_for_chemistry no longer refuses CLR; this card's "
                "refusal branch needs revisiting."
            )
        return SuggestionCard(
            kind="variants",
            category="VARIANTS",
            # CLR is long-read, so the title above names Clair3 -- the caller
            # this would have used, and the one being refused.
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    if chemistry is None:
        # Deliberately not defaulted to bcftools the way the *launch* path
        # does. Guessing short-read is a safe fallback for someone who has
        # chosen to run; it is a poor thing to advertise on a card, where the
        # user has no signal that the caller shown is a guess.
        return SuggestionCard(
            kind="variants",
            category="VARIANTS",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason="Unknown sequencing platform for this BAM.",
        )

    caller = variant_runner.caller_for_chemistry(chemistry)

    # Only the caller this chemistry would actually run. Probing both would
    # gate a perfectly runnable Clair3 card on an unrelated missing bcftools.
    tool = tools.clair3() if long_read else tools.bcftools()
    why = (
        "Long, high-accuracy reads: Clair3's model is trained on them."
        if long_read
        else "Short reads: bcftools mpileup is the standard pileup caller."
    )

    if not tool.available:
        # DeepVariant is not the automatic default for any chemistry (see the
        # sidecar design doc) -- Clair3 stays preferred and is tried first,
        # above. But a long-read card gated purely because Clair3's binary is
        # missing, while a DeepVariant image is reachable and covers the same
        # chemistry (ONT_R104/PACBIO), is a card refusing to run work it
        # could actually do. Short reads have no such fallback: DeepVariant's
        # WGS model would need an explicit choice bcftools already covers.
        dv_tool = tools.deepvariant() if long_read else None
        if dv_tool is not None and dv_tool.available:
            caller = variant_runner.VariantCaller.DEEPVARIANT
            tool = dv_tool
            title = "DeepVariant long-read calls"
            why = (
                "Clair3 is not installed; DeepVariant covers this chemistry "
                "too and is available as a fallback."
            )
        else:
            return SuggestionCard(
                kind="variants",
                category="VARIANTS",
                title=title,
                description=description,
                status=CardStatus.UNAVAILABLE,
                reason=f"{tool.name} is not installed.",
            )

    return SuggestionCard(
        kind="variants",
        category="VARIANTS",
        title=title,
        description=description,
        why=why,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/variants",
            # The complete VariantRequest body. Note the key is `bam_id` --
            # this is the one launch endpoint of the three that does not key on
            # `object_id`, and sending the wrong one 422s.
            #
            # `reference_id` is omitted rather than resolved: the server reads
            # it out of the BAM's provenance via `reference_for_bam`, which is
            # a database walk, and a card that guessed wrong would align calls
            # against a reference the BAM was never aligned to.
            "body": {
                "bam_id": str(obj.id),
                "params": {"caller": caller.value},
            },
        },
    )


def build_annotate_card(obj, inputs) -> SuggestionCard | None:
    """Consequence annotation for a called VCF.

    `inputs` is a parameter rather than something resolved here, mirroring
    `build_variants_card`'s chemistry: `resolve_annotation_inputs` walks
    provenance to the reference and lists the project for a GFF3, which is an
    async database round trip the endpoint has already paid for by the time
    this builder runs. Taking the resolved value keeps this module's builders
    uniformly synchronous and pure.

    The reason text is the resolver's, not this card's. Two places deciding
    why something is unavailable drift, and the card is the one the user
    reads -- so it must say exactly what the launcher would enforce.
    """
    if obj.format.kind not in (FormatKind.VCF, FormatKind.BCF):
        return None

    title = "Annotate variants"
    description = "Add gene and protein consequences to these variants."

    csq = tools.bcftools_csq()
    if not csq.available:
        return SuggestionCard(
            kind="annotate",
            category="ANNOTATE",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=csq.error or "bcftools csq is unavailable.",
        )

    if inputs is None or not inputs.ok:
        return SuggestionCard(
            kind="annotate",
            category="ANNOTATE",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(inputs.reason if inputs else "Inputs could not be resolved."),
        )

    # `ok=True` guarantees both reference and annotation are set -- see the
    # single ok return in resolve_annotation_inputs, which is reached only
    # after both are found.
    return SuggestionCard(
        kind="annotate",
        category="ANNOTATE",
        title=title,
        description=description,
        # `resolve_annotation_inputs` matches on `ncbi_assembly_accession`, so
        # there is exactly one candidate annotation by construction -- naming
        # it in `description` cannot disambiguate anything, and every real
        # file here is literally `genomic.gff`, which tells the user nothing.
        # `why` is where the other available cards (preprocess, align,
        # variants) put this kind of detail, and the frontend falls back to
        # `reason` when `why` is absent -- which an available card never has.
        why=f"Consequences called against {inputs.annotation.name}.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/annotate",
            # The complete request body: `/pipelines/annotate` keys on
            # `object_id` alone and resolves the reference/annotation itself,
            # the same walk `inputs` above already came from.
            "body": {"object_id": str(obj.id)},
        },
    )


def build_assemble_card(obj) -> SuggestionCard | None:
    """De novo assembly.

    Shown even when it cannot run, which was this card's whole purpose for
    months: the card count stays stable across files, so the Actions tab does
    not appear to lose steps as you click between them.

    What changed on 2026-08-01 is that it can now run. The old reason -- "No
    assembler is installed" -- was a fact about the host and correct while
    tools.py declared no assembler at all. Flye is installed now, and leaving
    that sentence in place would have been the exact failure CLAUDE.md
    predicts: a card reading "No assembler is installed" beside an installed
    assembler.

    The two remaining refusals are kept distinct on purpose. Short reads have
    an assembler that is not installed, which the user cannot fix today.
    Unknown chemistry is a *missing fact*, which they can fix by running QC --
    and telling them to install something instead would send them nowhere.
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return None

    chemistry = pipeline_service.read_chemistry(obj)
    spec = assembler_registry.spec_for_chemistry(chemistry)

    if spec is None:
        if chemistry is align_runner.ReadChemistry.SHORT:
            return SuggestionCard(
                kind="assemble",
                category="ASSEMBLE",
                title="De novo assembly",
                description="Assemble these reads into contigs without a reference.",
                status=CardStatus.UNAVAILABLE,
                reason=(
                    "Short-read assembly is not installed. Only long reads "
                    "can be assembled here."
                ),
            )
        if chemistry is None or chemistry is align_runner.ReadChemistry.UNKNOWN:
            return SuggestionCard(
                kind="assemble",
                category="ASSEMBLE",
                title="De novo assembly",
                description=(
                    "Assemble these reads into contigs without a reference."
                ),
                status=CardStatus.UNAVAILABLE,
                # Actionable, which is the bar every other card here meets: the
                # assembler's input mode is a claim about read accuracy, and QC
                # is what establishes it.
                reason="Run QC first, to determine how accurate these reads are.",
            )
        # A known long-read chemistry with no assembler. Unreachable today --
        # Flye covers all four -- and written out anyway because the
        # alternative is falling into the "run QC" branch above and telling
        # someone to establish a fact that is already established, which is
        # the kind of wrong advice that survives for months.
        return SuggestionCard(
            kind="assemble",
            category="ASSEMBLE",
            title="De novo assembly",
            description="Assemble these reads into contigs without a reference.",
            status=CardStatus.UNAVAILABLE,
            reason=(
                f"No assembler here handles "
                f"{_CHEMISTRY_LABELS.get(chemistry, chemistry.value)} reads."
            ),
        )

    if not spec.available():
        return SuggestionCard(
            kind="assemble",
            category="ASSEMBLE",
            title="De novo assembly",
            description="Assemble these reads into contigs without a reference.",
            status=CardStatus.UNAVAILABLE,
            reason=spec.unavailable_reason
            or f"{spec.assembler.value} is not installed.",
        )

    mode = assembler_registry.mode_for_chemistry(spec, chemistry)
    return SuggestionCard(
        kind="assemble",
        category="ASSEMBLE",
        title=f"De novo assembly -- {spec.assembler.value}",
        description="Assemble these reads into contigs without a reference.",
        why=f"{_CHEMISTRY_LABELS.get(chemistry, 'Long')} reads, assembled as {mode}.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/assemble",
            # Only the object id. Unlike the trim card, the parameters are not
            # assembled here: genome-size inference walks the project, which is
            # an async database read, and every builder in this module is
            # deliberately synchronous and pure. `/pipelines/assemble` fills
            # them in from `default_assembly_params` when `params` is absent,
            # so the card and the dialog reach the same defaults by one path.
            "body": {"object_id": str(obj.id)},
        },
    )


def build_completeness_card(obj) -> SuggestionCard | None:
    """Assembly completeness, scored by compleasm.

    Not gated on provenance: an uploaded assembly is as eligible as one this
    application produced, so this triggers on shape (FASTA, the right role)
    rather than on `produced_by_job`. That is what makes the role check below
    load-bearing rather than defensive -- `protein.faa` and
    `cds_from_genomic.fna` are the same FormatKind.FASTA the align card
    already had to learn to exclude, and this card has no `derived_from` walk
    to lean on instead.

    Three refusals, kept distinct the way the assemble card keeps its two:
    each names a different fix.
    """
    if obj.format.kind is not FormatKind.FASTA:
        return None
    if obj.role in pipeline_service.COMPLETENESS_EXCLUDED_ROLES:
        return None

    title = "Assembly completeness"
    description = (
        "Score what fraction of a lineage-specific ortholog set can be "
        "found in this assembly (compleasm, a faster BUSCO)."
    )

    tool = tools.compleasm()
    if not tool.available:
        return SuggestionCard(
            kind="completeness",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=tool.error or "compleasm is not installed.",
        )

    organism = obj.metadata.get("organism") if obj.metadata else None
    lineage = lineage_inference.infer_lineage(organism)
    if lineage is None:
        return SuggestionCard(
            kind="completeness",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            # Actionable like the assemble card's "run QC first": naming what
            # the user can supply rather than a fact they cannot act on.
            reason=(
                "No organism metadata to choose a lineage from. Add an "
                "organism, or pick a lineage manually."
            ),
        )

    odb = assembly_qc_registry.COMPLEASM_SPEC.odb
    from app.queue.lineage_handlers import lineage_present

    if not lineage_present(settings.lineages_dir, lineage, odb):
        return SuggestionCard(
            kind="completeness",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                f"The {lineage}_{odb} lineage dataset is not downloaded yet."
            ),
        )

    return SuggestionCard(
        kind="completeness",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=f"Organism: {organism} -> {lineage} ({odb}).",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/completeness",
            "body": {"object_id": str(obj.id), "lineage": lineage, "odb": odb},
        },
    )


def build_quantify_card(obj, annotations) -> SuggestionCard | None:
    """Count reads per gene for this alignment.

    `annotations` is a parameter rather than something looked up here, for the
    same reason `chemistry` is on the variants card: finding them lists the
    project, which is async, and the builders here are uniformly synchronous
    and pure.

    Note what the endpoint filters on. Every annotation in a real project
    arrives from `download_assembly` with `role=None` -- the ingest path only
    assigns a role where format cannot answer, and GFF/GTF answers -- so a
    rule written against `ObjectRole.ANNOTATION` matches nothing while passing
    any test that builds its objects by hand. Checked against the live
    database: 4 GFF/GTF objects, 0 carrying the annotation role. See
    `pipeline_service._is_annotation`.

    Deliberately offered on any BAM rather than only an RNA-seq one. Whether
    an alignment is RNA-seq is not knowable from the file, and the honest
    failure -- a low assignment rate, reported on the counts file -- is more
    useful than a card that hides itself for a reason it cannot verify.
    """
    if obj.format.kind is not FormatKind.BAM:
        return None

    title = "Count reads per gene"
    description = "Count this alignment's reads against a gene annotation."

    tool = tools.featurecounts()
    if not tool.available:
        return SuggestionCard(
            kind="quantify",
            category="EXPRESSION",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=f"{tool.name} is not installed.",
        )

    if not annotations:
        return SuggestionCard(
            kind="quantify",
            category="EXPRESSION",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                "This project has no gene annotation. Download one with the "
                "assembly, or upload a GTF."
            ),
        )

    return SuggestionCard(
        kind="quantify",
        category="EXPRESSION",
        title=title,
        description=description,
        why=(
            "Counts per gene are what a differential expression test needs, "
            "and this alignment has an annotation to count against."
        ),
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/quantify",
            # Like the variants card, this keys on `bam_id` rather than
            # `object_id`. `annotation_id` is omitted so the server resolves
            # it -- preferring the GTF over the GFF3 of the same assembly,
            # which matters because featureCounts' conventional attribute is
            # absent from NCBI's GFF3 entirely.
            "body": {"bam_id": str(obj.id), "params": {}},
        },
    )


async def suggestions_for(obj) -> list[dict]:
    """Every card for one file, in fixed order.

    Fixed order rather than sorted by availability: a card's position should
    not move between files, or the grid becomes something to re-read rather
    than scan. Builders return None for kinds that do not apply to the
    format, so the list is dense.
    """
    if obj.status is not ObjectStatus.READY:
        return []

    references: list[DataObject] = []
    if obj.format.kind is FormatKind.FASTQ:
        # FASTQ only. The align card is the sole consumer, so listing a
        # project's objects on a BAM click would be a query whose result is
        # discarded. READY is pushed into the query rather than filtered
        # after: a project whose non-ready objects fill the limit would
        # otherwise come back with references silently missing.
        candidates = await object_service.list_objects(
            obj.project_id, owner=obj.owner, limit=500, status=ObjectStatus.READY
        )
        # Role, not just format. `REFERENCE_KINDS` is FASTA, and a project that
        # downloaded an assembly from NCBI also holds `protein.faa` and
        # `cds_from_genomic.fna` -- FASTA files that are emphatically not
        # genomes to align against. Counting those made a project with one
        # real reference look like it had four, which sent `resolve_reference`
        # past its single-reference branch into a refusal, beside a reference
        # sitting right there. (That refusal read "fetching a genome for
        # <organism> is not wired up yet" at the time; it now names the count
        # instead, which would have made the same bug easier to see rather
        # than fixing it -- the filter below is still what does that.)
        #
        # The align *dialog* can afford the looser filter because a human
        # picks from the list it shows. A card picks on its own, so it has to
        # be right rather than merely close.
        references = [
            o
            for o in candidates
            if o.format.kind in pipeline_service.REFERENCE_KINDS
            and o.role is ObjectRole.REFERENCE
        ]

    annotations: list[DataObject] = []
    if obj.format.kind is FormatKind.BAM:
        # BAM only: the quantify card is the sole consumer, so listing a
        # project's annotations on a FASTQ click would discard the result.
        annotations = await pipeline_service.annotations_for_project(
            obj.project_id, owner=obj.owner
        )

    chemistry = None
    if obj.format.kind is FormatKind.BAM:
        # BAM only, and awaited here rather than inside the builder: it is a
        # provenance walk to the FASTQ behind the alignment, which keeps the
        # builders uniformly synchronous and pure. On a FASTQ the synchronous
        # `read_chemistry` the builders call themselves is the same answer
        # without the round trip.
        chemistry = await pipeline_service.read_chemistry_for_alignment(obj)

    annotation_inputs = None
    if obj.format.kind in (FormatKind.VCF, FormatKind.BCF):
        # VCF only, and awaited here rather than inside the builder: it walks
        # provenance to the reference and lists the project for a GFF3, which
        # keeps the builders uniformly synchronous.
        annotation_inputs = await pipeline_service.resolve_annotation_inputs(obj)

    builders = (
        ("preprocess", lambda: build_preprocess_card(obj)),
        ("align", lambda: build_align_card(obj, references)),
        ("variants", lambda: build_variants_card(obj, chemistry)),
        ("quantify", lambda: build_quantify_card(obj, annotations)),
        ("annotate", lambda: build_annotate_card(obj, annotation_inputs)),
        ("assemble", lambda: build_assemble_card(obj)),
        ("completeness", lambda: build_completeness_card(obj)),
    )

    cards: list[dict] = []
    for kind, build in builders:
        # One card's contract drifting must not cost the other three. Several
        # builders raise deliberately when an upstream assumption moves --
        # `Aligner(...)` on an unregistered aligner, the CLR assertion in the
        # variants card -- and that loudness is right for the card that broke.
        # Letting it reach the endpoint would be wrong: the whole grid 500s,
        # and the user loses three working shortcuts to operations they can
        # still reach through Computations anyway. Logged at error so the
        # signal survives; the grid renders without the offender.
        try:
            card = build()
        except Exception:
            log.exception(
                "suggestion_builder_failed", kind=kind, object_id=str(obj.id)
            )
            continue
        if card is not None:
            cards.append(card.as_dict())

    # After the builders, not inside them: this is the one part of a card that
    # is a database question rather than a rule about the file.
    await prior_runs.attach_prior_runs(cards, obj, owner=obj.owner)
    return cards
