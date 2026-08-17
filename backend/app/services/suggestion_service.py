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
from app.pipelines.organism_taxonomy import OrganismClass, classify_organism, is_eukaryotic
from app.services import object_service, pipeline_service, prior_runs, reference_assembly

log = get_logger(__name__)


class CardStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    # A tool the card would use is ON_DEMAND_IMAGE and simply has not been
    # pulled yet -- a real, expected first-run state (tools.InstallState.
    # NOT_INSTALLED), not a fault. Distinct from UNAVAILABLE because the two
    # must render differently: UNAVAILABLE is a dead end with a reason,
    # NEEDS_INSTALL is one click from working and the card keeps its launch
    # payload to prove it. Rendering a not-yet-installed optional tool as
    # UNAVAILABLE is the worse of the two wrong answers -- the card reads as
    # permanently broken and the user never learns the tool exists at all.
    NEEDS_INSTALL = "needs_install"


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
    would render as a button that does nothing. NEEDS_INSTALL is the one
    exception to "unavailable means no payload" -- it is not blocked, it is
    one click from working, so it keeps `launch` exactly like an AVAILABLE
    card does. `requires_install` rides alongside it with what that click
    actually costs.
    """

    kind: str
    category: str
    title: str
    description: str
    why: str | None = None
    status: CardStatus = CardStatus.UNAVAILABLE
    reason: str | None = None
    launch: dict | None = None
    # Set only when status is NEEDS_INSTALL: {"tool": name, "download_bytes":
    # n}. The frontend renders this as an offer -- "DeepVariant: 2.8 GB,
    # install?" -- rather than the bare refusal an UNAVAILABLE reason implies.
    requires_install: dict | None = None
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
            "requires_install": self.requires_install,
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

    if obj.role == ObjectRole.TRIMMED_READS:
        return SuggestionCard(
            kind="preprocess",
            category="PREPROCESS",
            title="Trim & filter -- fastp",
            description=(
                "Already trimmed -- this file is the output of a previous trim job. "
                "Re-trimming is unusual; use the QC tab to inspect quality instead."
            ),
            status=CardStatus.UNAVAILABLE,
            reason="This file is already the product of trimming.",
        )

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

    # bwa-mem2 preset auto-selection from organism classification.
    # When the chosen aligner is bwa-mem2, set the preset based on the
    # organism's genome characteristics so the card's launch payload carries
    # sensible defaults. The user can override in the dialog.
    if params.get("aligner") == "bwa-mem2":
        organism_class = classify_organism(organism)
        preset_map = {
            "bacteria": "bacteria",
            "large_repetitive": "large_repetitive",
            "eukaryote": "eukaryote",
        }
        params = {
            **params,
            "preset": preset_map[organism_class.value],
        }
        return params, f"{organism_class.value.replace('_', ' ').title()} preset for bwa-mem2."

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
        elif (
            dv_tool is not None
            and dv_tool.install_state is tools.InstallState.NOT_INSTALLED
        ):
            # An installable DeepVariant must not silently replace an
            # uninstalled Clair3 with a ~3 GB download the user never asked
            # for -- that is what the AVAILABLE-fallback branch above does,
            # and it is only correct once the image is actually there. What
            # this branch offers instead is the *choice*: the card keeps a
            # real launch payload (caller=deepvariant), same as an AVAILABLE
            # card, so accepting the offer is one click through the
            # confirm-then-chain flow, not a dead end that sends the user to
            # a Settings page to figure out what to do next.
            return SuggestionCard(
                kind="variants",
                category="VARIANTS",
                title="DeepVariant long-read calls",
                description=description,
                why=(
                    "Clair3 is not installed; DeepVariant covers this "
                    "chemistry too, but its image has not been pulled yet."
                ),
                status=CardStatus.NEEDS_INSTALL,
                requires_install={
                    "tool": dv_tool.name,
                    "download_bytes": tools.TOOL_META["deepvariant"].download_bytes,
                },
                launch={
                    "endpoint": "/pipelines/variants",
                    "body": {
                        "bam_id": str(obj.id),
                        "params": {
                            "caller": variant_runner.VariantCaller.DEEPVARIANT.value
                        },
                        # The card already states the download size in
                        # `requires_install` before anyone can click it, so
                        # pressing this card's button *is* the consent
                        # `_require_or_offer_install` (pipeline_service.py)
                        # asks for -- the frontend posts this body verbatim
                        # and stays ignorant of the three launch endpoints'
                        # shapes, so the flag belongs here rather than as a
                        # special case in PipelineSuggestions.tsx.
                        "install_optional": True,
                    },
                },
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


def build_annotate_genome_card(obj) -> SuggestionCard | None:
    """Genome annotation for a bacterial or archaeal assembly.

    Gated on organism: only known prokaryote genera are eligible, since Bakta's
    database is bacterial/archaeal and annotating an unknown eukaryote with it
    would produce a confidently wrong result.
    """
    if obj.format.kind is not FormatKind.FASTA:
        return None
    if obj.role in pipeline_service.COMPLETENESS_EXCLUDED_ROLES:
        return None

    # Gate on contig count where already known -- a 200,000-contig draft is
    # not meaningfully annotatable.
    contig_count = obj.facts.get("reference_count") if obj.facts else None
    if isinstance(contig_count, int) and contig_count > 200:
        return None

    organism = obj.metadata.get("organism") if obj.metadata else None
    if classify_organism(organism) is not OrganismClass.BACTERIA:
        return None

    title = "Annotate genome"
    description = (
        "Find genes, tRNAs, rRNAs, CRISPR arrays, and AMR genes in "
        "this bacterial assembly with Bakta."
    )

    bakta_tool = tools.bakta()
    if not bakta_tool.available:
        return SuggestionCard(
            kind="annotate_genome",
            category="ANNOTATE_GENOME",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=bakta_tool.error or "Bakta is unavailable.",
        )

    return SuggestionCard(
        kind="annotate_genome",
        category="ANNOTATE_GENOME",
        title=title,
        description=description,
        why=f"Annotating {organism.strip()} with Bakta.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/annotate-genome",
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


def build_gc_tracks_card(obj) -> SuggestionCard | None:
    """Circos plot: GC content and skew rings for a finished genome.

    Gated on shape (FASTA, not protein/transcript) rather than provenance
    — an uploaded assembly with a known organism is the best case for the
    origin-of-replication diagnostic this card offers.
    """
    if obj.format.kind is not FormatKind.FASTA:
        return None
    if obj.role in pipeline_service.COMPLETENESS_EXCLUDED_ROLES:
        return None

    # Gate on contig count where already known: offering a Circos plot
    # for a 200,000-contig draft offers a run whose output will not
    # render.
    contig_count = obj.facts.get("reference_count") if obj.facts else None
    if isinstance(contig_count, int) and contig_count > 200:
        return None

    title = "Circos plot: GC tracks"
    description = (
        "Draw GC content and GC skew rings around a finished genome. "
        "GC skew can visually pinpoint a bacterial chromosome's origin "
        "of replication."
    )

    return SuggestionCard(
        kind="gc_tracks",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/gc-tracks",
            "body": {"object_id": str(obj.id)},
        },
    )


def build_consensus_card(obj, reference) -> SuggestionCard | None:
    """Amplicon/viral consensus calling, by iVar.

    Anchored on the BAM rather than the reference -- the reverse would be
    one-to-many (a reference has many alignments, the card cannot pick) and
    the foundation (#21) left this choice open for exactly that reason.

    `reference` is the BAM's alignment target, already resolved by the
    orchestrator via `reference_assembly.resolve_alignment_target_for_bam`
    -- an async provenance walk, kept out of this synchronous builder the
    same way `chemistry` is resolved once and passed into
    `build_variants_card`. `reference=None` means that walk raised: no
    recorded target, or an ambiguous one.

    Deliberately not gated on the reference looking viral (genome size,
    organism). That is the `protein.faa` mistake in a new costume -- right
    about the common case, wrong about a legitimate one. Consensus against
    a bacterial or plasmid reference is unusual, not wrong.
    """
    if obj.format.kind not in reference_assembly.ALIGNMENT_KINDS:
        return None

    title = "Consensus sequence"
    description = (
        "Trim amplicon primers (if a scheme is supplied) and call a "
        "consensus sequence from this alignment, using iVar."
    )

    tool = tools.ivar()
    if not tool.available:
        return SuggestionCard(
            kind="consensus",
            category="REFERENCE_ASSEMBLY",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=tool.error or "iVar is not installed.",
        )

    if reference is None:
        return SuggestionCard(
            kind="consensus",
            category="REFERENCE_ASSEMBLY",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=(
                "This alignment has no recorded reference, so its consensus "
                "could not be checked against one."
            ),
        )

    return SuggestionCard(
        kind="consensus",
        category="REFERENCE_ASSEMBLY",
        title=title,
        description=description,
        why=f"Reference: {reference.name}.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/consensus",
            # Primers are opt-in from the dialog, the same way completeness's
            # lineage override is -- the card offers the tool, not a guess at
            # which primer BED (if any) belongs to it.
            "body": {"bam_object_id": str(obj.id)},
        },
    )


def build_polish_card(obj, read_sets) -> SuggestionCard | None:
    """Short-read polishing of a draft assembly, by Polypolish.

    Anchored on the assembly, since that is what gets improved. `read_sets`
    is the project's eligible short-read sets, resolved by the orchestrator
    (an async project listing, kept out of this synchronous builder the same
    way `chemistry` and `alignment_target` are).

    Two gating decisions worth stating, because the tempting version of each
    is wrong:

    **Gated on the reads being short, not on the draft being long-read.**
    "Only offer polishing for ONT assemblies" is the `protein.faa` mistake
    again -- BioFlow often cannot know how an imported assembly was made, and
    polishing a hybrid or short-read assembly is unusual rather than
    incorrect. The rule that *is* safe is about the reads, because Polypolish
    on long reads is meaningless rather than merely unusual.

    **Ambiguity is unavailable, not a guess.** Cards launch directly with the
    body they carry -- there is no dialog between the button and the queue --
    so a card that picked one of several read sets would silently polish with
    whichever it chose. Polishing an assembly with the wrong sample's reads
    produces a plausible assembly that is quietly wrong, so the ambiguous
    case says so instead.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Polish assembly"
    description = (
        "Correct residual base errors in this assembly using short reads, "
        "with Polypolish."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="polish",
            category="REFERENCE_ASSEMBLY",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.polypolish()
    if not tool.available:
        return unavailable(tool.error or "Polypolish is not installed.")

    aligner = tools.bwa_mem2()
    if not aligner.available:
        # Not a detail: Polypolish needs all-alignment input, which is what
        # bwa-mem2 provides here. Without it there is nothing to polish from,
        # and "Polypolish is installed" would be a misleading thing to say.
        return unavailable(
            aligner.error or "bwa-mem2 is not installed, and polishing needs it."
        )

    if not read_sets:
        return unavailable(
            "Polishing needs short reads, and this project has none."
        )
    if len(read_sets) > 1:
        return unavailable(
            f"This project has {len(read_sets)} short-read sets. Polishing "
            "needs a specific one, and picking for you could correct this "
            "assembly with the wrong sample's reads."
        )

    chosen = read_sets[0]
    body = {"draft_object_id": str(obj.id), "reads_object_id": str(chosen[0].id)}
    if len(chosen) > 1:
        body["mate_object_id"] = str(chosen[1].id)

    return SuggestionCard(
        kind="polish",
        category="REFERENCE_ASSEMBLY",
        title=title,
        description=description,
        why=f"Short reads: {', '.join(o.name for o in chosen)}.",
        status=CardStatus.AVAILABLE,
        launch={"endpoint": "/pipelines/polish", "body": body},
    )


def build_scaffold_card(obj, references) -> SuggestionCard | None:
    """Reference-guided scaffolding of a draft assembly, by RagTag.

    Anchored on the draft, matching the polish card. `references` is the
    project's reference-role FASTA, resolved by the orchestrator the same
    way `read_sets` is for polishing.

    Same "ambiguity is unavailable, not a guess" rule `build_polish_card`
    documents, but it fires far more often here: a project holding two
    reference-role FASTA for one organism is the *ordinary* case (the real
    yeast project carries both the GCA and GCF genomic FASTA), not an edge
    case the way several short-read sets is for polishing. When this card is
    unavailable for that reason, the manual scaffold dialog -- which carries
    its own reference chooser -- is where the launch actually happens; this
    card only covers the single-reference case because cards have no room
    for a chooser (see `SuggestionCard.launch`'s own docstring: `body` must
    be the complete request, assembled with nothing left for the client to
    pick).

    Deliberately not gated on the draft looking unscaffolded -- the
    `protein.faa` mistake in the same costume `build_polish_card` warns
    about: rescaffolding an already-scaffolded assembly against a better
    reference is a legitimate workflow, not a mistake to prevent.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Scaffold assembly"
    description = (
        "Order and orient this assembly's contigs against a reference "
        "assembly, with RagTag. Scaffolds are named after the reference's "
        "own sequences, so real structural differences from the reference "
        "will not appear in the result."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="scaffold",
            category="REFERENCE_ASSEMBLY",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.ragtag()
    if not tool.available:
        return unavailable(tool.error or "RagTag is not installed.")

    if not references:
        return unavailable(
            "Scaffolding needs a reference genome to order contigs against, "
            "and this project has none."
        )
    if len(references) > 1:
        return unavailable(
            f"This project has {len(references)} reference assemblies. Use "
            "the Scaffold tool to pick one."
        )

    reference = references[0]
    return SuggestionCard(
        kind="scaffold",
        category="REFERENCE_ASSEMBLY",
        title=title,
        description=description,
        why=f"Reference: {reference.name}.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/scaffold",
            "body": {
                "draft_object_id": str(obj.id),
                "reference_object_id": str(reference.id),
            },
        },
    )


def build_misassembly_card(obj, references) -> SuggestionCard | None:
    """Reference-based misassembly QC for a draft assembly, by QUAST.

    Anchored on the draft, `references` reused verbatim from the orchestrator's
    `scaffold_references` -- the identical input shape `build_scaffold_card`
    takes (a draft assembly plus the project's reference-role FASTA), so the
    same list already excludes the draft from its own candidate pool. Without
    that exclusion a project with one de novo assembly and no other reference
    would offer to QUAST it against itself: a de novo assembly BioFlow
    produced carries `ObjectRole.REFERENCE` (`results.py:1246`), so it is in
    the reference pool by default.

    Same "ambiguity is unavailable, not a guess" rule `build_scaffold_card`
    documents, copied verbatim rather than re-derived: a project holding two
    reference-role FASTA for one organism is the *ordinary* case (the real
    yeast project carries both the GCA and GCF genomic FASTA), not an edge
    case. When this card is unavailable for that reason, the manual dialog --
    which carries its own reference chooser -- is where the launch actually
    happens; this card only covers the single-reference case because cards
    have no room for a chooser (`SuggestionCard.launch`'s own docstring:
    `body` must be the complete request, assembled with nothing left for the
    client to pick).

    `category="ASSEMBLY_QC"`, matching the completeness card -- this
    evaluates an assembly rather than improving it, unlike the
    `REFERENCE_ASSEMBLY` cards (polish, scaffold) beside it in this module.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Check for misassemblies"
    description = (
        "Align this assembly against a reference and report structural "
        "disagreements -- relocations, translocations and inversions -- "
        "that neither contiguity nor completeness can see, with QUAST."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="misassembly",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.quast()
    if not tool.available:
        return unavailable(tool.error or "QUAST is not installed.")

    if not references:
        return unavailable(
            "Misassembly QC needs a reference assembly, and this project "
            "has none."
        )
    if len(references) > 1:
        return unavailable(
            f"This project has {len(references)} reference assemblies. Use "
            "the Misassembly QC tool to pick one."
        )

    reference = references[0]
    return SuggestionCard(
        kind="misassembly",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=f"Reference: {reference.name}.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/misassemblies",
            "body": {
                "draft_object_id": str(obj.id),
                "reference_object_id": str(reference.id),
            },
        },
    )


def build_synteny_card(obj, references) -> SuggestionCard | None:
    """Whole-genome synteny alignment of a draft assembly against a
    reference, by minimap2, for a synteny dot plot.

    Anchored on the draft, `references` reused verbatim from the
    orchestrator's `scaffold_references` -- the identical input shape
    `build_scaffold_card` and `build_misassembly_card` take beside it (a
    draft assembly plus the project's reference-role FASTA, already
    excluding the draft from its own candidate pool). Deliberately not a
    separate listing query: this is the third card fed by that one shared
    list, following the established precedent rather than re-deriving the
    same "role=REFERENCE, FASTA, not this object" filter a third time.

    Unlike its two siblings, this dedups `references` by `blob_sha256`
    before applying the ambiguity gate. Two separate `DataObject` uploads of
    byte-identical content are possible in this system -- `object_service`
    always inserts a new object row before checking blob-level dedup
    (`object_service.py` around the `find_present_blob_by_content` call), so
    dedup there only avoids storing the bytes twice, not the object record --
    and without this step a project holding the same reference genome
    uploaded twice would see this card refuse as "2 reference assemblies"
    for what is, on disk, exactly one. `build_scaffold_card` and
    `build_misassembly_card` do not do this today; that is an existing gap
    in both, not something this card's own behavior depends on, and fixing
    it for them is out of scope here.

    Same "ambiguity is unavailable, not a guess" rule `build_misassembly_card`
    documents: more than one distinct reference is refused rather than
    picked, since a card cannot host a chooser (`SuggestionCard.launch`'s own
    docstring: `body` must be the complete request). The manual Synteny
    dialog, which carries its own reference chooser, is where the launch
    happens when this card is unavailable for that reason.

    `category="ASSEMBLY_QC"`, matching the misassembly and completeness
    cards -- this evaluates an assembly against a reference rather than
    improving the assembly itself, unlike the `REFERENCE_ASSEMBLY` cards
    (polish, scaffold) beside it in this module.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Compare to reference (synteny)"
    description = (
        "Align this assembly against a reference genome and plot it as a "
        "synteny dot plot -- breaks, inversions and translocations show up "
        "as visual discontinuities from the diagonal, with minimap2."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="synteny",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.minimap2()
    if not tool.available:
        return unavailable(tool.error or "minimap2 is not installed.")

    if not references:
        return unavailable(
            "Synteny analysis needs a reference genome to compare against, "
            "and this project has none."
        )

    # Dedup by content: two object records can share one blob (see
    # docstring above). A digest missing from an unwritten fixture or a
    # not-yet-hashed object collapses onto `None`, so at most one such
    # object survives the dedup rather than each counting separately.
    distinct = list({getattr(o, "blob_sha256", None): o for o in references}.values())

    if len(distinct) > 1:
        return unavailable(
            f"This project has {len(distinct)} reference assemblies. Use "
            "the Synteny tool to pick one."
        )

    reference = distinct[0]
    return SuggestionCard(
        kind="synteny",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=f"Reference: {reference.name}.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/synteny",
            "body": {
                "draft_object_id": str(obj.id),
                "reference_object_id": str(reference.id),
            },
        },
    )


def build_assembly_error_card(
    obj, alignments: tuple[list, list, list] | None
) -> SuggestionCard | None:
    """Reference-free assembly error detection for an assembly, by CRAQ.

    Anchored on the assembly; `alignments` is `(short, long, unknown)` --
    the project's READY BAMs whose `derived_from` contains it, pre-split by
    read chemistry via `pipeline_service.alignments_against`, the exact
    split `launch_assembly_error_qc` itself uses to auto-pair. Unlike the
    misassembly card beside it, this gates on *reads*, not on a reference --
    CRAQ needs no second genome, which is what makes it usable for an
    organism with no relative in NCBI.

    Same "ambiguity is unavailable, not a guess" rule `build_misassembly_card`
    documents: more than one short-read candidate, or more than one
    long-read candidate, is refused here rather than silently picking one,
    matching `launch_assembly_error_qc`'s own refusal
    (`len(short) > 1 or len(long_) > 1`). Without this check the card went
    AVAILABLE for a BAM set the launch path would then reject with a
    `ValidationError`, since the card's `object_id`-only launch body has no
    room to name which BAM to use.

    `unknown`-chemistry BAMs are never folded into `short`, mirroring
    `alignments_against`'s own contract (see that function's docstring), and
    they are excluded from both gates here: a project with *only*
    unknown-chemistry alignments reads as "none" (`not short and not
    long_`), the same "needs reads" reason as a project with no alignments
    at all, rather than "one, unknown"; and a project with exactly one
    short-read BAM plus an unknown-chemistry BAM is still unambiguous, since
    `launch_assembly_error_qc` auto-pairs on `short[0]` and never looks at
    `unknown` when both ids are omitted.

    `category="ASSEMBLY_QC"`: this evaluates an assembly rather than
    improving it. Chimera breaking is never offered here -- the card's
    launch body is the complete request, and a suggestion that silently
    rewrites an assembly is not a suggestion.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Detect assembly errors"
    description = (
        "Find misassembled regions from read clipping -- where reads align "
        "only partially, the assembly is usually wrong -- and separate true "
        "errors from heterozygous variants. Needs no reference genome, with "
        "CRAQ."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="assembly_errors",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.craq()
    if not tool.available:
        return unavailable(tool.error or "CRAQ is not installed.")

    short, long_, unknown = alignments or ([], [], [])

    # Mirrors `launch_assembly_error_qc`'s own gate exactly: `unknown` never
    # substitutes for a usable short/long candidate (a project with only
    # chemistry-unknown BAMs is "none", not "one, unknown"), and it never
    # counts toward ambiguity either -- ambiguity is about not knowing which
    # of several *usable* candidates to pick, not about a BAM this endpoint
    # cannot classify at all.
    if not short and not long_:
        return unavailable(
            "Assembly error detection needs reads aligned to this assembly. "
            "Align a read set against it first."
        )

    if len(short) > 1 or len(long_) > 1:
        total = len(short) + len(long_)
        return unavailable(
            f"This assembly has {total} alignment(s); use the tool to "
            "pick which ones to use."
        )

    return SuggestionCard(
        kind="assembly_errors",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=f"{len(short) + len(long_)} alignment(s) against this assembly.",
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/assembly-errors",
            "body": {"object_id": str(obj.id)},
        },
    )


def build_qv_card(obj, read_sets) -> SuggestionCard | None:
    """Reference-free base-level accuracy (QV) for an assembly, by Merqury.

    Anchored on the assembly, matching `build_polish_card` and
    `build_assembly_error_card`. `read_sets` is every read set in the
    project -- grouped mate pairs or singletons, via
    `reference_assembly.group_read_sets` -- resolved by the orchestrator the
    same way `read_sets` is for polishing.

    **Not restricted to short reads.** Merqury's comparison is k-mer based,
    which works against short or long reads alike -- unlike Polypolish,
    which is meaningless on long reads. Gating this card on short reads the
    way `build_polish_card` does would be the same "protein.faa" mistake
    CLAUDE.md warns about: excluding a read set that is actually usable
    because of an assumption that happens to be true for a different tool.

    `category="ASSEMBLY_QC"`, matching completeness/misassembly/
    assembly_errors, not `REFERENCE_ASSEMBLY` like polish or scaffold -- QV
    evaluates an assembly, it does not change it.

    Same "ambiguity is unavailable, not a guess" rule every sibling card
    documents: more than one read set in the project is refused rather than
    picked, since the card's launch body has no room for a chooser and a
    wrong pairing would silently score QV against the wrong sample's reads.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Assess base accuracy (QV)"
    description = (
        "Score this assembly's base-level accuracy against the reads it "
        "came from, with Merqury's reference-free k-mer comparison."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="assembly_qv",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    meryl_tool = tools.meryl()
    if not meryl_tool.available:
        return unavailable(meryl_tool.error or "meryl is not installed.")

    merqury_tool = tools.merqury()
    if not merqury_tool.available:
        return unavailable(merqury_tool.error or "Merqury is not installed.")

    if not read_sets:
        return unavailable(
            "QV assessment needs the reads this assembly was built from, "
            "and this project has none."
        )
    if len(read_sets) > 1:
        # Raw and trimmed versions of the same sample look like distinct
        # read sets, but they're the same biological material -- prefer the
        # trimmed set when that's the only difference, and still refuse when
        # the project genuinely holds reads from multiple distinct samples.
        trimmed_sets = [
            s for s in read_sets
            if all(o.role == ObjectRole.TRIMMED_READS for o in s)
        ]
        raw_sets = [
            s for s in read_sets
            if all(o.role != ObjectRole.TRIMMED_READS for o in s)
        ]
        if len(trimmed_sets) == 1 and len(raw_sets) >= 1 and (
            len(trimmed_sets) + len(raw_sets) == len(read_sets)
        ):
            read_sets = trimmed_sets
        else:
            return unavailable(
                f"This project has {len(read_sets)} read sets. QV assessment "
                "needs a specific one, and picking for you could score this "
                "assembly against the wrong sample's reads."
            )

    chosen = read_sets[0]
    body = {"object_id": str(obj.id), "read_object_id": str(chosen[0].id)}

    return SuggestionCard(
        kind="assembly_qv",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=f"Reads: {', '.join(o.name for o in chosen)}.",
        status=CardStatus.AVAILABLE,
        launch={"endpoint": "/pipelines/assembly-qv", "body": body},
    )


def build_kmer_spectra_card(obj, read_sets) -> SuggestionCard | None:
    """K-mer frequency spectrum and genome characteristics, via meryl.

    Anchored on the assembly. `read_sets` is every read set in the project
    -- same parameter as `build_qv_card`, because meryl's k-mer counting
    works for any read chemistry. The card gates on meryl being installed
    (already probed) and at least one read set being present.

    ``category="ASSEMBLY_QC"`` — this evaluates an assembly, not changes it.
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "K-mer spectrum"
    description = (
        "Compute a k-mer frequency spectrum from reads -- estimate genome "
        "size, ploidy, and heterozygosity -- with meryl."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="kmer_spectra",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    meryl_tool = tools.meryl()
    if not meryl_tool.available:
        return unavailable(meryl_tool.error or "meryl is not installed.")

    if not read_sets:
        return unavailable(
            "Spectra need the reads this assembly was built from, and "
            "this project has none."
        )
    if len(read_sets) > 1:
        trimmed_sets = [
            s for s in read_sets
            if all(o.role == ObjectRole.TRIMMED_READS for o in s)
        ]
        raw_sets = [
            s for s in read_sets
            if all(o.role != ObjectRole.TRIMMED_READS for o in s)
        ]
        if len(trimmed_sets) == 1 and len(raw_sets) >= 1 and (
            len(trimmed_sets) + len(raw_sets) == len(read_sets)
        ):
            read_sets = trimmed_sets
        else:
            return unavailable(
                f"This project has {len(read_sets)} read sets. Spectra "
                "need a specific one, and picking for you could analyse "
                "the wrong sample."
            )

    chosen = read_sets[0]
    body = {"object_id": str(obj.id), "read_object_id": str(chosen[0].id)}

    return SuggestionCard(
        kind="kmer_spectra",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=f"Reads: {', '.join(o.name for o in chosen)}.",
        status=CardStatus.AVAILABLE,
        launch={"endpoint": "/pipelines/meryl-analysis", "body": body},
    )


def build_repeat_density_card(obj) -> SuggestionCard | None:
    """Per-window repeat density track for a finished genome, via meryl.

    Gated on shape (FASTA, not protein/transcript) and contig count --
    a draft with 200,000 contigs has no meaningful density track and
    won't render. meryl is already installed and probed.
    """
    if obj.format.kind is not FormatKind.FASTA:
        return None
    if obj.role in pipeline_service.COMPLETENESS_EXCLUDED_ROLES:
        return None

    contig_count = obj.facts.get("reference_count") if obj.facts else None
    if isinstance(contig_count, int) and contig_count > 200:
        return None

    title = "Circos plot: repeat density"
    description = (
        "Find repeat-rich regions in this genome by k-mer frequency. "
        "A contig break aligned with a repeat band is resolvable with "
        "long reads; a break with no repeat under it is a data-quality "
        "problem."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="repeat_density",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    meryl_tool = tools.meryl()
    if not meryl_tool.available:
        return unavailable(meryl_tool.error or "meryl is not installed.")

    return SuggestionCard(
        kind="repeat_density",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/meryl-analysis",
            "body": {"object_id": str(obj.id)},
        },
    )


def build_continuity_card(
    obj,
    alignments: tuple[list, list, list] | None,
    gci_candidates: tuple[list, list] | None,
) -> SuggestionCard | None:
    """Long-read assembly continuity inspection for an assembly, by GCI.

    Anchored on the assembly, same as `build_assembly_error_card` beside it.
    `alignments` is the `(short, long, unknown)` split
    `pipeline_service.alignments_against` returns, used only to tell "no
    long reads at all" apart from "short-read alignments only" for the
    unavailable message. `gci_candidates` is `(hifi_candidates,
    nano_candidates)`, GCI's own further split of `long_` by chemistry into
    the two slots it actually accepts (`--hifi`/`--nano`) -- computed by
    `pipeline_service._gci_candidates`, the exact helper
    `launch_continuity_qc` uses to auto-pair, so this card applies the
    identical gate the launch path enforces rather than a re-derived
    approximation. CLR BAMs (and anything else `gci_slot_for_chemistry`
    refuses) are dropped by that helper, never counted here.

    Short-read-only projects get their own message rather than the generic
    "align reads first" -- that advice would send the user to redo work
    that cannot help, since GCI takes no short-read input at all.

    **Ambiguity is per-aligner, not per-slot, since winnowmap arrived.**
    Two usable HiFi BAMs against the same assembly is the routine
    cross-check case when they come from two different aligners (minimap2 +
    winnowmap, same reads) -- GCI's own recommendation, not something to
    warn about. It is genuinely ambiguous only when the *same* aligner
    produced more than one candidate in a slot, mirroring
    `launch_continuity_qc`'s identical `_group_gci_candidates_by_aligner`
    check, so this card never claims a launch is blocked when it is not (or
    vice versa).
    """
    if not reference_assembly._is_assembly_like(obj):
        return None
    if obj.status is not ObjectStatus.READY:
        return None

    title = "Inspect assembly continuity"
    description = (
        "Score how well long reads agree with this assembly's structure -- "
        "flagging low-support and low-coverage regions -- with GCI."
    )

    def unavailable(reason: str) -> SuggestionCard:
        return SuggestionCard(
            kind="assembly_continuity",
            category="ASSEMBLY_QC",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=reason,
        )

    tool = tools.gci()
    if not tool.available:
        return unavailable(tool.error or "GCI is not installed.")

    short, long_, _unknown = alignments or ([], [], [])
    hifi_candidates, nano_candidates = gci_candidates or ([], [])

    if not long_:
        if short:
            return unavailable(
                "GCI needs long reads, and this assembly only has "
                "short-read alignments."
            )
        return unavailable(
            "Continuity inspection needs long reads aligned to this "
            "assembly. Align a read set against it first."
        )

    if not hifi_candidates and not nano_candidates:
        return unavailable(
            "This assembly has long-read alignments, but none are HiFi or "
            "ONT -- GCI cannot use PacBio CLR reads."
        )

    hifi_by_aligner = pipeline_service._group_gci_candidates_by_aligner(hifi_candidates)
    nano_by_aligner = pipeline_service._group_gci_candidates_by_aligner(nano_candidates)
    if any(len(group) > 1 for group in (*hifi_by_aligner.values(), *nano_by_aligner.values())):
        total = len(hifi_candidates) + len(nano_candidates)
        return unavailable(
            f"This assembly has {total} usable long-read alignment(s) from "
            "the same aligner; use the tool to pick which ones to use."
        )

    aligners = sorted({*hifi_by_aligner, *nano_by_aligner})
    cross_checked = len(aligners) > 1
    why = f"{len(hifi_candidates) + len(nano_candidates)} long-read alignment(s)"
    why += f" ({', '.join(aligners)}) against this assembly." \
        if aligners else " against this assembly."
    if cross_checked:
        why += " Both aligners will be cross-checked, per GCI's own recommendation."

    return SuggestionCard(
        kind="assembly_continuity",
        category="ASSEMBLY_QC",
        title=title,
        description=description,
        why=why,
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/assembly-continuity",
            "body": {"object_id": str(obj.id)},
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


@dataclass(frozen=True)
class _Prefetched:
    """The async lookups `suggestions_for` does once, before any builder runs.

    Every field is something a builder needs but must not fetch itself: the
    builders are synchronous and pure so they can be unit-tested against plain
    objects, and so one card's database question is not paid for on a click
    that renders a different card. `None` means "could not tell" -- a lookup
    that raised, or one this object's format never triggers -- which each
    builder reports as an unavailable card rather than treating as empty.
    """

    references: list[DataObject]
    annotations: list[DataObject]
    chemistry: object | None
    annotation_inputs: object | None
    alignment_target: object | None
    read_sets: object | None
    scaffold_references: list[DataObject] | None
    all_read_sets: object | None
    assembly_alignments: object | None
    continuity_candidates: object | None


# Fixed order, and the order is behaviour: it is the order cards appear in the
# Actions tab, and a card's position should not move between files or the grid
# becomes something to re-read rather than scan. Appending here puts a card
# last; that is a UI decision, not a formality.
#
# Every `build_*_card` in this module must appear exactly once, which
# `test_every_builder_is_registered` enforces -- a builder that exists but is
# not registered is dead code that reads as a working feature, the failure a
# hand-maintained registry keyed by convention invites.
CARD_BUILDERS: tuple[tuple[str, object], ...] = (
    ("preprocess", lambda obj, ctx: build_preprocess_card(obj)),
    ("align", lambda obj, ctx: build_align_card(obj, ctx.references)),
    ("variants", lambda obj, ctx: build_variants_card(obj, ctx.chemistry)),
    ("quantify", lambda obj, ctx: build_quantify_card(obj, ctx.annotations)),
    ("annotate", lambda obj, ctx: build_annotate_card(obj, ctx.annotation_inputs)),
    ("annotate_genome", lambda obj, ctx: build_annotate_genome_card(obj)),
    ("assemble", lambda obj, ctx: build_assemble_card(obj)),
    ("completeness", lambda obj, ctx: build_completeness_card(obj)),
    ("consensus", lambda obj, ctx: build_consensus_card(obj, ctx.alignment_target)),
    ("polish", lambda obj, ctx: build_polish_card(obj, ctx.read_sets)),
    ("scaffold", lambda obj, ctx: build_scaffold_card(obj, ctx.scaffold_references)),
    (
        "misassembly",
        lambda obj, ctx: build_misassembly_card(obj, ctx.scaffold_references),
    ),
    ("gc_tracks", lambda obj, ctx: build_gc_tracks_card(obj)),
    ("synteny", lambda obj, ctx: build_synteny_card(obj, ctx.scaffold_references)),
    ("kmer_spectra", lambda obj, ctx: build_kmer_spectra_card(obj, ctx.all_read_sets)),
    ("repeat_density", lambda obj, ctx: build_repeat_density_card(obj)),
    (
        "assembly_errors",
        lambda obj, ctx: build_assembly_error_card(obj, ctx.assembly_alignments),
    ),
    ("assembly_qv", lambda obj, ctx: build_qv_card(obj, ctx.all_read_sets)),
    (
        "assembly_continuity",
        lambda obj, ctx: build_continuity_card(
            obj, ctx.assembly_alignments, ctx.continuity_candidates
        ),
    ),
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

    alignment_target = None
    if obj.format.kind in reference_assembly.ALIGNMENT_KINDS:
        # Same reasoning as chemistry above: an async provenance walk, kept
        # out of the synchronous consensus card. None means the walk raised
        # -- no recorded target, or an ambiguous one -- which the card
        # reports as unavailable rather than crashing.
        try:
            alignment_target = await reference_assembly.resolve_alignment_target_for_bam(
                obj, owner=obj.owner
            )
        except Exception:  # noqa: BLE001 - a resolution failure loses one card, not the grid
            alignment_target = None

    # One listing, named once, for every assembly-like card below.
    # `read_sets`, `scaffold_references` and `all_read_sets` are three
    # different filters over the *same* query -- same project, same owner,
    # READY, same default limit -- and all three are gated on the same
    # `_is_assembly_like(obj)`, so they either all run or none do. Writing
    # that query once says so; three separate `await`s of it read as three
    # independent lookups that happen to coincide.
    #
    # This is a readability change, not a performance one. The three spellings
    # it replaced already issued a single round trip between them, so the query
    # count is unchanged -- measured at two per assembly-like click before and
    # after, the second being `alignments_against`'s own listing below.
    #
    # Kept out of the FASTQ `references` listing above deliberately. That one
    # raises the limit to 500 and is gated on FASTQ, which `_is_assembly_like`
    # excludes by construction (it requires FASTA), so the two branches never
    # both run and merging them would only put a different limit in front of
    # one of them.
    project_objects: list[DataObject] | None = None
    if reference_assembly._is_assembly_like(obj):
        # A listing failure loses the cards that need it, not the grid --
        # the same trade each of these branches made when it owned its own
        # query. `None` is what the builders below read as "could not tell".
        try:
            project_objects = await object_service.list_objects(
                obj.project_id, owner=obj.owner, status=ObjectStatus.READY
            )
        except Exception:  # noqa: BLE001 - a listing failure loses one card, not the grid
            project_objects = None

    read_sets = None
    if project_objects is not None:
        # Kept out of the synchronous polish card: `short_read_sets` is the
        # chemistry-filtered candidate list Polypolish accepts.
        try:
            read_sets = reference_assembly.short_read_sets(project_objects)
        except Exception:  # noqa: BLE001 - a filter failure loses one card, not the grid
            read_sets = None

    scaffold_references = None
    if project_objects is not None:
        # Deliberately not the `references` list built above -- that one is
        # gated to the FASTQ branch and stays empty for a FASTA click, which
        # would starve this card of every candidate.
        try:
            scaffold_references = [
                o
                for o in project_objects
                if o.role is ObjectRole.REFERENCE
                and o.format.kind is FormatKind.FASTA
                # A draft that is itself marked REFERENCE -- an already
                # scaffolded assembly, say -- must not be offered as its own
                # reference. launch_scaffold refuses this at launch, but a
                # card that offers a self-referential run in the first place
                # is the bug this real project surfaced: one reference-role
                # FASTA in a project (itself) rendered as an AVAILABLE card
                # naming itself as the target.
                and o.id != obj.id
            ]
        except Exception:  # noqa: BLE001 - a filter failure loses one card, not the grid
            scaffold_references = None

    all_read_sets = None
    if project_objects is not None:
        # Same source as read_sets, but not filtered to short reads: QV's
        # k-mer comparison works for any chemistry, unlike Polypolish, so
        # `build_qv_card` needs every read set in the project rather than
        # `short_read_sets`'s narrower candidate list. `group_read_sets` is
        # the same mate-pairing logic `short_read_sets` itself is built on,
        # just without the chemistry filter.
        try:
            all_read_sets = reference_assembly.group_read_sets(
                [o for o in project_objects if o.format.kind is FormatKind.FASTQ]
            )
        except Exception:  # noqa: BLE001 - a filter failure loses one card, not the grid
            all_read_sets = None

    assembly_alignments = None
    if reference_assembly._is_assembly_like(obj):
        # Same reasoning as scaffold_references above: an async project
        # listing kept out of the synchronous assembly-errors card, computed
        # only for assembly-like FASTA. Delegates to `alignments_against`
        # (`pipeline_service.py`) rather than re-deriving the filter here --
        # that function already does the "READY BAMs whose `derived_from`
        # names this object" lookup *and* the chemistry split into
        # `(short, long, unknown)` that `launch_assembly_error_qc` uses to
        # auto-pair, so `build_assembly_error_card` can apply the identical
        # ambiguity gate the launch path enforces instead of only checking
        # "any alignments at all".
        try:
            assembly_alignments = await pipeline_service.alignments_against(
                obj, owner=obj.owner
            )
        except Exception:  # noqa: BLE001 - a listing failure loses one card, not the grid
            assembly_alignments = None

    # GCI needs its `long_` bucket split further, into the two slots it
    # actually accepts (`--hifi`/`--nano`), refusing CLR the way
    # `launch_continuity_qc` does -- `pipeline_service._gci_candidates` is
    # the exact split that launch path uses to auto-pair, so the card
    # applies the identical gate rather than a re-derived approximation.
    # Computed here (async) rather than inside the sync card builder so it
    # uses the same `read_chemistry_for_alignment` the launch path uses,
    # including its FASTQ-provenance fallback for BAMs aligned before
    # chemistry was copied onto the BAM itself.
    continuity_candidates = None
    if assembly_alignments is not None:
        try:
            _short, long_, _unknown = assembly_alignments
            continuity_candidates = await pipeline_service._gci_candidates(long_)
        except Exception:  # noqa: BLE001 - a listing failure loses one card, not the grid
            continuity_candidates = None

    ctx = _Prefetched(
        references=references,
        annotations=annotations,
        chemistry=chemistry,
        annotation_inputs=annotation_inputs,
        alignment_target=alignment_target,
        read_sets=read_sets,
        scaffold_references=scaffold_references,
        all_read_sets=all_read_sets,
        assembly_alignments=assembly_alignments,
        continuity_candidates=continuity_candidates,
    )

    cards: list[dict] = []
    for kind, build in CARD_BUILDERS:
        # One card's contract drifting must not cost the other three. Several
        # builders raise deliberately when an upstream assumption moves --
        # `Aligner(...)` on an unregistered aligner, the CLR assertion in the
        # variants card -- and that loudness is right for the card that broke.
        # Letting it reach the endpoint would be wrong: the whole grid 500s,
        # and the user loses three working shortcuts to operations they can
        # still reach through Computations anyway. Logged at error so the
        # signal survives; the grid renders without the offender.
        try:
            card = build(obj, ctx)
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
