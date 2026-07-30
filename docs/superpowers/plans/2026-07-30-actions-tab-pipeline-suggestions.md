# Actions Tab Pipeline Suggestions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Actions tab as three sections -- Computations, a grid of rule-chosen pipeline suggestion cards, and a two-column Manage this file -- backed by a new tested backend rule engine.

**Architecture:** A new `suggestion_service.py` maps an object's format, read chemistry, assay, organism and project references onto an ordered list of four cards (`preprocess`, `align`, `variants`, `assemble`), each `available` with a launch payload or `unavailable` with an honest reason. It delegates tool selection to the existing `pipeline_service` helpers rather than duplicating them. The frontend renders the cards and moves the existing computation buttons out of the file headline into the tab.

**Tech Stack:** FastAPI + Beanie/MongoDB backend, pytest; React + TanStack Query frontend, Vite.

**Spec:** `docs/superpowers/specs/2026-07-30-actions-tab-pipeline-suggestions-design.md`

---

## Key context for the implementer

You are working in a single-user local tool. Read `CLAUDE.md` first. Three things that will bite you otherwise:

1. **Run pytest inside the container**, not on the host: `docker compose exec api python -m pytest tests/ -q`. The host venv hits Mongo replica-set errors.
2. **`api` and `web` hot-reload; `worker` does not.** Nothing in this plan touches a queue handler, so you should not need `docker compose restart worker`.
3. **Run `docker compose` from the repo root only**, never a worktree.

### What already exists -- do not reimplement

`app/services/pipeline_service.py` already has the hard parts. The suggestion service is mostly a presentation layer over these:

- `default_align_params(obj)` -> dict with `aligner`, `preset`, `threads`. **Already** picks bwa-mem2 vs minimap2 including the arm64 constraint (bwa-mem2 is x86-64 only) and forces minimap2 for long reads. Delegate to it.

### The launch payload is a complete request body

The three launch endpoints do **not** share a request shape, and tool settings are nested rather than flat. Get this wrong and every card returns 422:

```
POST /pipelines/trim      {object_id, tool, params: {...}, paired?, mate_object_id?}
POST /pipelines/align     {object_id, reference_id, params: {...}, read_group?, paired?}
POST /pipelines/variants  {bam_id, caller, params: {...}, reference_id?}
```

Note `variants` keys on **`bam_id`**, not `object_id`. So a card's `launch.body` is the entire JSON body, built server-side where the object id is already known -- the frontend posts it verbatim and adds nothing. That is why the field is named `body` rather than `params` throughout this plan.
- `default_variant_params(obj)` -> async, dict with `caller`, `threads`. Returns `{"caller": None, ...}` for CLR chemistry.
- `read_chemistry_for_alignment(obj)` -> async, `ReadChemistry | None`. Prefers the BAM's own fact, falls back to the parent FASTQ.
- `reference_for_bam(bam)` -> async, `DataObject | None`.
- `default_params(tool)` -> trim defaults.
- `is_long_read(obj)`, `sam_platform(metadata_platform)`, `REFERENCE_KINDS`, `ALIGNABLE_KINDS`.

`app/pipelines/align_runner.py`: `ReadChemistry` StrEnum (`short`, `ont_simplex`, `ont_duplex`, `hifi`, `clr`, `unknown`), `preset_for_chemistry()`.
`app/pipelines/aligners.py`: `Aligner` StrEnum (`bwa-mem2`, `minimap2`, `bowtie2`, `hisat2`).
`app/pipelines/variant_runner.py`: `VariantCaller` StrEnum (`clair3`, `bcftools`), `caller_for_chemistry()` **raises ValidationError on CLR**.
`app/pipelines/tools.py`: `fastp()`, `minimap2()`, `bwa_mem2()`, `hisat2()`, `clair3()`, `bcftools()` etc, each returning a `Tool` with `.available` and `.version`. Probes are cached.

### One spec gap found while planning

The spec does not cover **CLR chemistry on the variants card**. `caller_for_chemistry` raises `ValidationError` for CLR, and `default_variant_params` returns `caller: None`, because CLR's error rate makes calls that look ordinary and are wrong. Task 5 handles this as a fifth unavailable reason. This is the correct behaviour and matches the existing refusal.

---

## File Structure

**Create:**
- `backend/app/services/suggestion_service.py` -- the rule engine. One module: card dataclass, the genus table, and one builder function per card kind.
- `backend/tests/services/test_suggestion_service.py` -- table-driven rule tests.
- `frontend/src/components/PipelineSuggestions.tsx` -- the card grid.
- `frontend/src/components/ManageFile.tsx` -- the two-column manage layout.
- `frontend/src/components/Computations.tsx` -- the computation button row.

**Modify:**
- `backend/app/api/v1/pipelines.py` -- add the suggestions endpoint.
- `frontend/src/api/types.ts` -- add `PipelineSuggestion`.
- `frontend/src/api/client.ts` -- add `api.suggestions()` and `api.launchSuggestion()`.
- `frontend/src/components/DetailPanel.tsx` -- rewrite `ActionsTab`, remove the headline button row, add the QC empty-state button, invalidate suggestions.
- `frontend/src/components/RoleConverter.tsx` -- `bare` prop (Task 9), broad suggestions invalidation (Task 10).
- `frontend/src/components/PairEditor.tsx` -- `bare` prop; note it has two `.section` returns.
- `frontend/src/components/TrimDialog.tsx` -- heading text only.
- `frontend/src/styles.css` -- card grid and manage-grid styles.
- `CLAUDE.md` -- the new-tool note.

---

### Task 1: Card model and the genus table

**Files:**
- Create: `backend/app/services/suggestion_service.py`
- Create: `backend/tests/services/test_suggestion_service.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_suggestion_service.py`:

```python
"""Rules that turn a file's facts into pipeline suggestions.

Table-driven because the rules are a mapping, not an algorithm: the value is
in pinning each branch, especially the ones whose ordering is load-bearing.
"""

import pytest

from app.services.suggestion_service import (
    CardStatus,
    SuggestionCard,
    is_eukaryotic,
)


class TestGenusClassification:
    @pytest.mark.parametrize(
        "organism",
        ["Escherichia coli", "escherichia coli K-12", "Bacillus subtilis",
         "Staphylococcus aureus"],
    )
    def test_known_prokaryote_genera_are_not_eukaryotic(self, organism):
        assert is_eukaryotic(organism) is False

    @pytest.mark.parametrize(
        "organism",
        ["Homo sapiens", "Saccharomyces cerevisiae S288C",
         "Trypanosoma brucei brucei", "Lycoris aurea"],
    )
    def test_known_eukaryote_genera_are_eukaryotic(self, organism):
        assert is_eukaryotic(organism) is True

    def test_unrecognised_genus_defaults_to_eukaryotic(self):
        """Splice-aware alignment on an intron-free genome degrades
        gracefully; the reverse loses real junctions silently."""
        assert is_eukaryotic("Wobblia lunata") is True

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_organism_defaults_to_eukaryotic(self, value):
        assert is_eukaryotic(value) is True


class TestCardDefaults:
    def test_available_card_carries_a_launch_payload(self):
        card = SuggestionCard(
            kind="preprocess",
            category="PREPROCESS",
            title="Trim & filter -- fastp",
            description="Adapter trim and length filter.",
            why="Short reads.",
            status=CardStatus.AVAILABLE,
            launch={"endpoint": "/pipelines/trim", "body": {"object_id": "abc"}},
        )
        assert card.as_dict()["status"] == "available"
        assert card.as_dict()["launch"]["endpoint"] == "/pipelines/trim"

    def test_unavailable_card_has_no_launch_and_carries_a_reason(self):
        card = SuggestionCard(
            kind="assemble",
            category="ASSEMBLE",
            title="De novo assembly",
            description="Assemble reads into contigs.",
            why=None,
            status=CardStatus.UNAVAILABLE,
            reason="No assembler is installed.",
        )
        data = card.as_dict()
        assert data["launch"] is None
        assert data["reason"] == "No assembler is installed."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.services.suggestion_service'`

- [ ] **Step 3: Write minimal implementation**

Create `backend/app/services/suggestion_service.py`:

```python
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

from dataclasses import dataclass, field
from enum import StrEnum


class CardStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


# Genus -> domain. Hand-maintained and deliberately small: `organism` is free
# text, and this only has to separate "has introns" from "does not" well
# enough to pick a short-read aligner. Matched on the first word of the name.
#
# An unrecognised genus is treated as eukaryotic (see `is_eukaryotic`), so
# this table only needs the prokaryotes it is likely to meet plus enough
# eukaryotes to document the intent.
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
    *complete* JSON body for that endpoint, assembled here where the object
    id and its defaults are known. The frontend posts it verbatim and adds
    nothing -- the three launch endpoints do not share a request shape
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: PASS, 11 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat: add the suggestion card model and genus classification"
```

---

### Task 2: The preprocess card

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Modify: `backend/tests/services/test_suggestion_service.py`

The simplest rule and the one that establishes the builder shape: fastq only, never gated on QC (fastp's defaults are safe on either read type), unavailable only when the tool is missing.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_suggestion_service.py`:

```python
from unittest.mock import patch

from app.models import FormatKind
from app.services.suggestion_service import build_preprocess_card


class _FakeTool:
    def __init__(self, available: bool, version: str = "0.23.4"):
        self.available = available
        self.version = version


def _fake_obj(kind=FormatKind.FASTQ, facts=None, metadata=None, obj_id="abc123"):
    """A stand-in for DataObject carrying only what the rules read.

    A real Beanie document would need a database; the rules are pure
    functions of these attributes, so a namespace is enough. `id` is here
    because the launch body carries it -- the card assembles the complete
    request body server-side.
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        id=obj_id,
        format=SimpleNamespace(kind=kind),
        facts=facts or {},
        metadata=metadata or {},
    )


class TestPreprocessCard:
    def test_not_offered_for_a_bam(self):
        assert build_preprocess_card(_fake_obj(kind=FormatKind.BAM)) is None

    def test_available_for_a_fastq_with_no_qc_yet(self):
        """Not gated on chemistry: fastp's defaults are safe either way, and
        gating it would leave a fresh FASTQ with nothing runnable at all."""
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            card = build_preprocess_card(_fake_obj())
        assert card.status is CardStatus.AVAILABLE
        assert card.launch["endpoint"] == "/pipelines/trim"
        assert card.launch["body"]["tool"] == "fastp"
        assert card.launch["body"]["object_id"] == "abc123"
        # Tool settings nest under params -- TrimRequest's shape, not flat.
        assert "params" in card.launch["body"]

    def test_unavailable_when_fastp_is_not_installed(self):
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(False)):
            card = build_preprocess_card(_fake_obj())
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None
        assert "fastp" in card.reason

    def test_long_read_card_says_it_filters_rather_than_trims_adapters(self):
        with patch("app.services.suggestion_service.tools.fastp",
                   return_value=_FakeTool(True)):
            card = build_preprocess_card(
                _fake_obj(facts={"qc_read_chemistry": "ont_simplex"})
            )
        assert "filter" in card.description.lower()
        assert "adapter" not in card.description.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: FAIL, `ImportError: cannot import name 'build_preprocess_card'`

- [ ] **Step 3: Write minimal implementation**

Add to `backend/app/services/suggestion_service.py`, after the imports:

```python
from app.models import FormatKind
from app.pipelines import align_runner, tools
from app.services import pipeline_service
```

And append:

```python
def _chemistry_of(obj) -> align_runner.ReadChemistry | None:
    """The chemistry fact, or None when QC has not run.

    Unrecognized values degrade to None rather than raising: facts are
    tool-written data, and a stale value should not break a card grid.
    """
    value = (obj.facts or {}).get("qc_read_chemistry")
    if not value:
        return None
    try:
        return align_runner.ReadChemistry(value)
    except ValueError:
        return None


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
    chemistry = _chemistry_of(obj)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: PASS, 15 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat: add the preprocess suggestion card"
```

---

### Task 3: Reference resolution for the align card

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Modify: `backend/tests/services/test_suggestion_service.py`

The four-branch rule from the spec, isolated from card construction so its ordering can be tested directly. **The ordering is the point:** rule 1 claims the exactly-one case before metadata is consulted.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_suggestion_service.py`:

```python
from app.services.suggestion_service import ReferenceChoice, resolve_reference


def _ref(object_id: str, name: str):
    from types import SimpleNamespace
    return SimpleNamespace(id=object_id, name=name)


class TestReferenceResolution:
    def test_exactly_one_uploaded_reference_is_used(self):
        refs = [_ref("aaa", "GCF_000005845.2.fna")]
        choice = resolve_reference(refs, organism="Escherichia coli")
        assert choice.reference_id == "aaa"
        assert choice.usable is True

    def test_one_uploaded_reference_beats_a_known_organism(self):
        """Load-bearing ordering. Reversing it makes the card refuse a
        perfectly good local reference in favour of an unfetchable one --
        worse behaviour on a better-configured project."""
        refs = [_ref("aaa", "local.fna")]
        choice = resolve_reference(refs, organism="Saccharomyces cerevisiae")
        assert choice.usable is True
        assert choice.reference_id == "aaa"

    def test_no_references_but_known_organism_names_the_species(self):
        choice = resolve_reference([], organism="Saccharomyces cerevisiae")
        assert choice.usable is False
        assert "Saccharomyces cerevisiae" in choice.reason
        assert "not wired up" in choice.reason

    def test_many_references_with_known_organism_names_the_species(self):
        refs = [_ref("bbb", "b.fna"), _ref("aaa", "a.fna")]
        choice = resolve_reference(refs, organism="Escherichia coli")
        assert choice.usable is False
        assert "Escherichia coli" in choice.reason

    def test_many_references_without_organism_picks_deterministically(self):
        """Sorted by id, first. A random pick would name a different
        reference on each render, which reads as a bug."""
        refs = [_ref("ccc", "c.fna"), _ref("aaa", "a.fna"), _ref("bbb", "b.fna")]
        first = resolve_reference(refs, organism=None)
        second = resolve_reference(list(reversed(refs)), organism=None)
        assert first.reference_id == "aaa"
        assert second.reference_id == "aaa"
        assert first.usable is True

    def test_nothing_at_all_asks_for_an_upload(self):
        choice = resolve_reference([], organism=None)
        assert choice.usable is False
        assert "Upload a reference" in choice.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: FAIL, `ImportError: cannot import name 'ReferenceChoice'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/suggestion_service.py`:

```python
@dataclass(frozen=True)
class ReferenceChoice:
    """Which reference the align card would use, or why it cannot."""

    reference_id: str | None = None
    reference_name: str | None = None
    usable: bool = False
    reason: str | None = None


def resolve_reference(references: list, organism: str | None) -> ReferenceChoice:
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

    if organism and organism.strip():
        return ReferenceChoice(
            usable=False,
            reason=(
                f"Fetching a reference genome for {organism.strip()} is not "
                "wired up yet."
            ),
        )

    if references:
        # Deterministic: the same card on every render and reload.
        chosen = sorted(references, key=lambda r: str(r.id))[0]
        return ReferenceChoice(
            reference_id=str(chosen.id), reference_name=chosen.name, usable=True
        )

    return ReferenceChoice(usable=False, reason="Upload a reference to align.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: PASS, 21 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat: add reference resolution for the align card"
```

---

### Task 4: The align card

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Modify: `backend/tests/services/test_suggestion_service.py`

Combines three gates: chemistry, reference, and tool. Reason precedence when several fail at once is specified and tested.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_suggestion_service.py`:

```python
from app.services.suggestion_service import build_align_card


class TestAlignCard:
    def test_not_offered_for_a_bam(self):
        assert build_align_card(_fake_obj(kind=FormatKind.BAM), []) is None

    def test_unknown_chemistry_gates_the_card(self):
        card = build_align_card(_fake_obj(), [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.UNAVAILABLE
        assert "Run QC" in card.reason

    def test_long_reads_pick_minimap2_with_the_matching_preset(self):
        obj = _fake_obj(facts={"qc_read_chemistry": "ont_simplex"})
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.status is CardStatus.AVAILABLE
        assert "minimap2" in card.title
        assert card.launch["body"]["params"]["preset"] == "map-ont"

    def test_rna_seq_on_a_eukaryote_picks_hisat2(self):
        obj = _fake_obj(
            facts={"qc_read_chemistry": "short"},
            metadata={"assay": "RNA-seq", "organism": "Saccharomyces cerevisiae"},
        )
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.launch["body"]["params"]["aligner"] == "hisat2"
        assert "splice" in card.why.lower()

    def test_rna_seq_on_a_bacterium_does_not_pick_hisat2(self):
        """Bacteria have no introns, so splice-awareness is wrong there."""
        obj = _fake_obj(
            facts={"qc_read_chemistry": "short"},
            metadata={"assay": "RNA-seq", "organism": "Escherichia coli"},
        )
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        assert card.launch["body"]["params"]["aligner"] != "hisat2"

    def test_both_gates_failing_names_the_reference_first(self):
        """Reference first because it is actionable without waiting on a
        job."""
        card = build_align_card(_fake_obj(), [])
        assert card.status is CardStatus.UNAVAILABLE
        assert card.reason.index("Upload a reference") < card.reason.index("Run QC")

    def test_available_card_carries_a_complete_align_request_body(self):
        obj = _fake_obj(facts={"qc_read_chemistry": "short"})
        card = build_align_card(obj, [_ref("aaa", "ref.fna")])
        body = card.launch["body"]
        assert card.launch["endpoint"] == "/pipelines/align"
        # reference_id is top-level on AlignRequest; aligner settings nest.
        assert body["reference_id"] == "aaa"
        assert body["object_id"] == "abc123"
        assert "aligner" in body["params"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: FAIL, `ImportError: cannot import name 'build_align_card'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/suggestion_service.py`:

```python
def _align_tool_and_why(obj, chemistry) -> tuple[dict, str]:
    """The aligner params and the sentence explaining them.

    Delegates to `pipeline_service.default_align_params`, which already
    handles the aligner choice including the arm64 constraint -- bwa-mem2 is
    x86-64 only, so on Apple silicon minimap2 is not merely preferred but the
    only option. Duplicating that here would mean two places to fix it.

    The one thing added on top is splice-awareness, which the align dialog
    has no reason to consider but a suggestion does.
    """
    params = pipeline_service.default_align_params(obj)
    metadata = obj.metadata or {}
    assay = str(metadata.get("assay") or "")
    organism = metadata.get("organism")

    if assay.lower() in ("rna-seq", "scrna-seq") and not _is_long_read(chemistry):
        if is_eukaryotic(organism):
            params = {**params, "aligner": "hisat2", "preset": ""}
            return params, (
                f"{assay} on {organism or 'a eukaryote'} -- hisat2 aligns "
                "across splice junctions."
            )
        return params, (
            f"{assay} on {organism} -- no introns, so a splice-aware aligner "
            "would add nothing."
        )

    reason = (obj.facts or {}).get("qc_read_chemistry_reason")
    if reason:
        return params, str(reason)
    return params, f"{params.get('aligner')} suits these reads."


def build_align_card(obj, references: list) -> SuggestionCard | None:
    """Align reads against a reference.

    Three independent gates -- chemistry, reference, tool -- any of which can
    block. When several fail at once the reason names the reference problem
    first: it is the one the user can fix now, without waiting for a job.
    A missing tool outranks both, since neither of the others is fixable
    while it is absent.
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return None

    chemistry = _chemistry_of(obj)
    choice = resolve_reference(references, (obj.metadata or {}).get("organism"))
    params, why = _align_tool_and_why(obj, chemistry)

    aligner = str(params.get("aligner") or "")
    preset = str(params.get("preset") or "")
    title = f"{aligner} {preset} -> BAM".replace("  ", " ").strip()
    description = (
        f"Align to {choice.reference_name}, sort and index."
        if choice.reference_name
        else "Align these reads against a reference, sort and index."
    )

    probe = {
        "minimap2": tools.minimap2,
        "bwa-mem2": tools.bwa_mem2,
        "hisat2": tools.hisat2,
    }.get(aligner)
    tool_missing = probe is not None and not probe().available

    reasons: list[str] = []
    if tool_missing:
        reasons = [f"{aligner} is not installed."]
    else:
        if not choice.usable:
            reasons.append(choice.reason)
        if chemistry is None:
            reasons.append("Run QC to determine read chemistry.")

    if reasons:
        return SuggestionCard(
            kind="align",
            category="ALIGN",
            title=title,
            description=description,
            status=CardStatus.UNAVAILABLE,
            reason=" ".join(reasons),
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
            # AlignRequest: reference_id is top-level, aligner settings nest
            # under params. `read_group` is deliberately omitted --
            # `launch_alignment` merges whatever is sent over
            # `default_read_group(obj)`, so sending nothing gets the correct
            # defaults rather than a card-invented @RG line.
            "body": {
                "object_id": str(obj.id),
                "reference_id": choice.reference_id,
                "params": params,
            },
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: PASS, 28 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat: add the align suggestion card"
```

---

### Task 5: The variants and assemble cards

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Modify: `backend/tests/services/test_suggestion_service.py`

Variants is async (chemistry may come from the BAM's parent). CLR is refused outright -- see the spec-gap note above.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/services/test_suggestion_service.py`:

```python
from app.pipelines.align_runner import ReadChemistry
from app.services.suggestion_service import (
    build_assemble_card,
    build_variants_card,
)


class TestVariantsCard:
    def test_not_offered_for_a_fastq(self):
        assert build_variants_card(_fake_obj(), chemistry=None) is None

    def test_long_read_bam_picks_clair3(self):
        card = build_variants_card(
            _fake_obj(kind=FormatKind.BAM), chemistry=ReadChemistry.ONT_SIMPLEX
        )
        assert "Clair3" in card.title

    def test_short_read_bam_picks_bcftools(self):
        card = build_variants_card(
            _fake_obj(kind=FormatKind.BAM), chemistry=ReadChemistry.SHORT
        )
        assert "bcftools" in card.title

    def test_launch_body_keys_on_bam_id_not_object_id(self):
        """VariantRequest is the odd one out. Getting this wrong 422s."""
        card = build_variants_card(
            _fake_obj(kind=FormatKind.BAM), chemistry=ReadChemistry.SHORT
        )
        assert card.launch["body"]["bam_id"] == "abc123"
        assert "object_id" not in card.launch["body"]

    def test_unknown_platform_gates_the_card(self):
        card = build_variants_card(_fake_obj(kind=FormatKind.BAM), chemistry=None)
        assert card.status is CardStatus.UNAVAILABLE
        assert "Unknown sequencing platform" in card.reason

    def test_clr_is_refused_outright(self):
        """CLR's error rate makes calls that look ordinary and are wrong --
        a worse outcome than refusing, because nothing downstream flags
        them."""
        card = build_variants_card(
            _fake_obj(kind=FormatKind.BAM), chemistry=ReadChemistry.CLR
        )
        assert card.status is CardStatus.UNAVAILABLE
        assert "CLR" in card.reason


class TestAssembleCard:
    def test_offered_for_a_fastq_but_never_runnable(self):
        card = build_assemble_card(_fake_obj())
        assert card.status is CardStatus.UNAVAILABLE
        assert card.launch is None

    def test_reason_names_the_missing_assembler_not_the_missing_dag(self):
        """No assembler is installed, which is the blocking constraint --
        the absent pipeline system is true but not what stops it."""
        card = build_assemble_card(_fake_obj())
        assert "assembler" in card.reason.lower()

    def test_not_offered_for_a_bam(self):
        assert build_assemble_card(_fake_obj(kind=FormatKind.BAM)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: FAIL, `ImportError: cannot import name 'build_assemble_card'`

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/suggestion_service.py`:

```python
def build_variants_card(obj, chemistry) -> SuggestionCard | None:
    """Call variants from an alignment.

    Chemistry is passed in rather than read here: on a BAM it may live on the
    parent FASTQ, and resolving that is `pipeline_service`'s async job.

    CLR is refused rather than given a caller, matching
    `variant_runner.caller_for_chemistry`. Clair3 is trained on
    high-accuracy reads, and at CLR's error rate it produces calls that look
    ordinary and are wrong -- worse than refusing, because nothing downstream
    flags them.
    """
    if obj.format.kind is not FormatKind.BAM:
        return None

    if chemistry is align_runner.ReadChemistry.CLR:
        return SuggestionCard(
            kind="variants",
            category="VARIANTS",
            title="Variant calling",
            description="Call and normalise variants from this alignment.",
            status=CardStatus.UNAVAILABLE,
            reason=(
                "PacBio CLR reads are too error-prone for reliable calls. "
                "Use HiFi/CCS reads instead."
            ),
        )

    if chemistry is None:
        return SuggestionCard(
            kind="variants",
            category="VARIANTS",
            title="Variant calling",
            description="Call and normalise variants from this alignment.",
            status=CardStatus.UNAVAILABLE,
            reason="Unknown sequencing platform for this BAM.",
        )

    long_read = _is_long_read(chemistry)
    caller = "clair3" if long_read else "bcftools"
    probe = tools.clair3 if long_read else tools.bcftools
    title = "Clair3 long-read calls" if long_read else "bcftools short-read calls"

    if not probe().available:
        return SuggestionCard(
            kind="variants",
            category="VARIANTS",
            title=title,
            description="Call and normalise variants from this alignment.",
            status=CardStatus.UNAVAILABLE,
            reason=f"{caller} is not installed.",
        )

    return SuggestionCard(
        kind="variants",
        category="VARIANTS",
        title=title,
        description="Call and normalise variants from this alignment.",
        why=(
            f"{chemistry.value} reads -- {caller} is trained for them."
            if long_read
            else f"{chemistry.value} reads -- bcftools suits short-read pileups."
        ),
        status=CardStatus.AVAILABLE,
        launch={
            "endpoint": "/pipelines/variants",
            # VariantRequest keys on `bam_id`, not `object_id` -- the one
            # endpoint of the three that does. `reference_id` is omitted so
            # the server resolves it from the BAM's provenance.
            "body": {"bam_id": str(obj.id), "caller": caller},
        },
    )


def build_assemble_card(obj) -> SuggestionCard | None:
    """De novo assembly. Always unavailable.

    Shown rather than hidden so the card count stays stable across files and
    the capability is discoverable. The reason names the missing assembler
    rather than the missing pipeline system: both are true, but only one is
    the blocking constraint, and the honest one is more useful.
    """
    if obj.format.kind is not FormatKind.FASTQ:
        return None

    return SuggestionCard(
        kind="assemble",
        category="ASSEMBLE",
        title="De novo assembly",
        description="Assemble these reads into contigs without a reference.",
        status=CardStatus.UNAVAILABLE,
        reason="No assembler is installed.",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose exec api python -m pytest tests/services/test_suggestion_service.py -q`
Expected: PASS, 36 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/tests/services/test_suggestion_service.py
git commit -m "feat: add the variants and assemble suggestion cards"
```

---

### Task 6: The suggestions endpoint

**Files:**
- Modify: `backend/app/services/suggestion_service.py`
- Modify: `backend/app/api/v1/pipelines.py:392` (insert before `list_references`)

- [ ] **Step 1: Write the assembling function**

Append to `backend/app/services/suggestion_service.py`:

```python
async def suggestions_for(obj) -> list[dict]:
    """Every card for one file, in fixed order.

    Fixed order rather than sorted by availability: a card's position should
    not move between files, or the grid becomes something to re-read rather
    than scan. Builders return None for kinds that do not apply to the
    format, so the list is dense.
    """
    from app.models import ObjectStatus
    from app.services import object_service

    if obj.status is not ObjectStatus.READY:
        return []

    references: list = []
    if obj.format.kind is FormatKind.FASTQ:
        objects = await object_service.list_objects(obj.project_id, limit=500)
        references = [
            o
            for o in objects
            if o.format.kind in pipeline_service.REFERENCE_KINDS
            and o.status is ObjectStatus.READY
        ]

    chemistry = None
    if obj.format.kind is FormatKind.BAM:
        chemistry = await pipeline_service.read_chemistry_for_alignment(obj)

    cards = [
        build_preprocess_card(obj),
        build_align_card(obj, references),
        build_variants_card(obj, chemistry),
        build_assemble_card(obj),
    ]
    return [c.as_dict() for c in cards if c is not None]
```

- [ ] **Step 2: Add the endpoint**

In `backend/app/api/v1/pipelines.py`, insert immediately **before** the `@router.get("/references/{project_id}")` decorator (currently line 394):

```python
@router.get("/suggestions/{object_id}")
async def list_suggestions(object_id: PydanticObjectId) -> dict:
    """Pipelines worth offering for this file, with the reason for each.

    Advisory: a card is a pre-answered instance of an operation the
    Computations section also offers with a picker in front of it. Nothing
    here launches anything -- the cards carry the same payloads the dialogs
    post.
    """
    from app.services import suggestion_service

    obj = await DataObject.get(object_id)
    if obj is None:
        raise NotFoundError(f"Object not found: {object_id}")

    return {"suggestions": await suggestion_service.suggestions_for(obj)}
```

- [ ] **Step 3: Verify the endpoint responds**

Run against a real FASTQ id from the database:

```bash
docker compose exec api python -c "
import asyncio, json
from app.db.client import connect_to_mongo
from app.models import DataObject, FormatKind
from app.services import suggestion_service
async def main():
    await connect_to_mongo()
    obj = await DataObject.find_one(DataObject.format.kind == FormatKind.FASTQ)
    print(json.dumps(await suggestion_service.suggestions_for(obj), indent=2))
asyncio.run(main())
"
```

Expected: three cards (preprocess, align, assemble) with `status` and `reason` fields. Given QC has run on few files, align will most likely be `unavailable`.

- [ ] **Step 4: Run the full backend suite**

Run: `docker compose exec api python -m pytest tests/ -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/suggestion_service.py backend/app/api/v1/pipelines.py
git commit -m "feat: add the pipeline suggestions endpoint"
```

---

### Task 7: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts:441`

- [ ] **Step 1: Add the type**

Append to `frontend/src/api/types.ts`:

```typescript
/**
 * One pipeline offer for a file, chosen by the backend rule engine.
 *
 * `launch` and `status` always agree: the card renders a disabled button
 * whenever `launch` is null, so an "available" card can never be one that
 * does nothing when clicked.
 */
export interface PipelineSuggestion {
  kind: "preprocess" | "align" | "variants" | "assemble";
  /** Small-caps label above the title, e.g. "ALIGN". */
  category: string;
  title: string;
  description: string;
  /** Why these choices for this file. Present on available cards. */
  why: string | null;
  status: "available" | "unavailable";
  /** Why it cannot run. Present on unavailable cards. */
  reason: string | null;
  /**
   * The complete request body and where to post it, assembled server-side.
   * Posted verbatim -- the three launch endpoints do not share a shape
   * (`/variants` keys on `bam_id`, the others on `object_id`), so anything
   * merged in here would be a shape the client had to know about.
   */
  launch: { endpoint: string; body: Record<string, unknown> } | null;
}
```

- [ ] **Step 2: Add the client method**

In `frontend/src/api/client.ts`, immediately after the `references:` entry (line 441):

```typescript
  /** Pipelines worth offering for this file, with the reason for each. */
  suggestions: (objectId: string) =>
    request<{ suggestions: PipelineSuggestion[] }>(
      `/pipelines/suggestions/${objectId}`,
    ),
```

Add `PipelineSuggestion` to the existing type import at the top of the file.

- [ ] **Step 3: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: add the suggestions type and client method"
```

---

### Task 8: The card grid component

**Files:**
- Create: `frontend/src/components/PipelineSuggestions.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/PipelineSuggestions.tsx`:

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { PipelineSuggestion } from "../api/types";
import { notify } from "../stores/messageStore";

/**
 * Pre-answered pipeline offers for one file.
 *
 * Advisory, which decides the failure mode: an error renders a quiet line
 * rather than an error box, because a failed suggestion is not a broken file.
 */
export function PipelineSuggestions({ objectId }: { objectId: string }) {
  const qc = useQueryClient();
  // No `enabled` guard needed: this component only mounts inside the Actions
  // tab, so mounting *is* the "only when the tab is open" condition the spec
  // asks for. Adding a flag as well would be a second source of truth.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["suggestions", objectId],
    queryFn: () => api.suggestions(objectId),
  });

  const launch = useMutation({
    // Posted exactly as received: the body is already complete, including
    // whichever id key that endpoint wants.
    mutationFn: (card: PipelineSuggestion) =>
      api.launchSuggestion(card.launch!.endpoint, card.launch!.body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      notify.info("Pipeline queued");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const cards = data?.suggestions ?? [];

  if (isLoading) return null;
  if (isError || cards.length === 0) {
    return (
      <div className="section">
        <div className="section-title">Launch a pipeline on this file</div>
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          No pipeline suggestions for this file.
        </div>
      </div>
    );
  }

  // The first available card carries the filled button; the rest are
  // outlined. One primary action per grid, so the eye lands somewhere.
  const firstAvailable = cards.findIndex((c) => c.status === "available");

  return (
    <div className="section">
      <div className="section-title">Launch a pipeline on this file</div>
      <div className="suggestion-grid">
        {cards.map((card, i) => (
          <div key={card.kind} className="suggestion-card">
            <div className="suggestion-category">{card.category}</div>
            <div className="suggestion-title">{card.title}</div>
            <div className="suggestion-desc">{card.description}</div>
            <div className="suggestion-why">
              {card.status === "available" ? card.why : card.reason}
            </div>
            <button
              type="button"
              className={`btn ${i === firstAvailable ? "primary" : ""}`}
              disabled={card.status !== "available" || launch.isPending}
              onClick={() => launch.mutate(card)}
              title={card.status === "available" ? undefined : card.reason ?? ""}
            >
              Launch
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add the generic launcher to the client**

In `frontend/src/api/client.ts`, after the `suggestions:` entry:

```typescript
  /** Post a suggestion card's pre-answered payload to its own endpoint. */
  launchSuggestion: (endpoint: string, body: Record<string, unknown>) =>
    request<{ id: string }>(endpoint, {
      method: "POST",
      body: JSON.stringify(body),
    }),
```

- [ ] **Step 3: Add the styles**

Append to `frontend/src/styles.css`:

```css
/* Pipeline suggestion cards. Four across on a wide panel, collapsing rather
   than shrinking: below ~900px a four-column grid puts three words per line. */
.suggestion-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

.suggestion-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px;
  /* --bg-elevated, not --bg-panel: the cards sit on the panel and need to
     read as raised from it. The palette has no --bg-raised. */
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.suggestion-category {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--accent);
}

/* Broadsheet sets --radius to 2px and squares off most surfaces, so the card
   picks that up from the variable rather than needing an override. Check both
   themes anyway (Task 13) -- --accent is a link blue in the default theme and
   worth a look as a label. */

.suggestion-title {
  font-size: 15px;
  font-weight: 600;
}

.suggestion-desc {
  font-size: 12px;
  color: var(--text-dim);
}

/* Pushed to the bottom so the buttons line up across cards whose text runs
   to different lengths. */
.suggestion-why {
  font-size: 11px;
  color: var(--text-faint);
  margin-top: auto;
  padding-top: 6px;
}
```

- [ ] **Step 4: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/PipelineSuggestions.tsx frontend/src/api/client.ts frontend/src/styles.css
git commit -m "feat: add the pipeline suggestion card grid"
```

---

### Task 9: Computations and Manage sections

**Files:**
- Create: `frontend/src/components/Computations.tsx`
- Create: `frontend/src/components/ManageFile.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Write the Computations component**

Create `frontend/src/components/Computations.tsx`:

```tsx
/**
 * The same operations the suggestion cards offer, with the tool picker in
 * front of them.
 *
 * A card is a pre-answered instance of one of these. QC appears only here
 * because it takes no parameters -- a card version would be identical to the
 * button, with nothing pre-answered.
 */
export function Computations({
  canPreprocess,
  canAlign,
  canCallVariants,
  canQC,
  hasQc,
  alignTarget,
  onStart,
  onRunQC,
  qcPending,
  onReingest,
  reingestPending,
  reingestDisabled,
}: {
  canPreprocess: boolean;
  canAlign: boolean;
  canCallVariants: boolean;
  canQC: boolean;
  hasQc: boolean;
  alignTarget: string | null;
  onStart: (pipeline: "trim" | "align" | "variant") => void;
  onRunQC: () => void;
  qcPending: boolean;
  onReingest: () => void;
  reingestPending: boolean;
  reingestDisabled: boolean;
}) {
  return (
    <div className="section">
      <div className="section-title">Computations</div>
      <div className="section-note">
        Pick the tool and its settings yourself.
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {canPreprocess && (
          <button type="button" className="btn" onClick={() => onStart("trim")}>
            Preprocess
          </button>
        )}
        {canAlign && (
          <button
            type="button"
            className="btn"
            onClick={() => onStart("align")}
            title={
              alignTarget
                ? `Align these reads against ${alignTarget}`
                : "Align these reads against a reference"
            }
          >
            {alignTarget ? `Align to ${alignTarget}` : "Align"}
          </button>
        )}
        {canCallVariants && (
          <button
            type="button"
            className="btn"
            onClick={() => onStart("variant")}
            title="Call variants from this alignment"
          >
            Call variants
          </button>
        )}
        {canQC && (
          <button
            type="button"
            className="btn"
            onClick={onRunQC}
            disabled={qcPending}
            title="Measure read quality"
          >
            {qcPending ? "Running QC…" : hasQc ? "Re-run QC" : "Run QC"}
          </button>
        )}
        <button
          type="button"
          className="btn-text"
          onClick={onReingest}
          disabled={reingestPending || reingestDisabled}
          title="Re-run format detection and header parsing"
        >
          {reingestPending ? "Re-ingesting…" : "Re-ingest"}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Write the ManageFile component**

Create `frontend/src/components/ManageFile.tsx`. This moves the existing Actions content into a two-column layout; the child components are reused unchanged.

```tsx
import { api } from "../api/client";
import type { ObjectDetail as ObjectDetailData } from "../api/types";
import { compressionLabel, formatBytes } from "../lib/format";
import { PairEditor } from "./PairEditor";
import { RoleConverter } from "./RoleConverter";
import { TagEditor } from "./TagEditor";

/**
 * Housekeeping on the entry itself -- nothing here starts a run.
 *
 * Two columns of label/control pairs. Same content as before; the labels move
 * to the left rather than sitting above their controls, which halves the
 * vertical space and lets the eye scan the labels alone.
 */
export function ManageFile({
  obj,
  confirmingDelete,
  setConfirmingDelete,
  remove,
  onTagsChanged,
  metadataDirty,
}: {
  obj: ObjectDetailData;
  confirmingDelete: boolean;
  setConfirmingDelete: (v: boolean) => void;
  remove: { mutate: () => void; isPending: boolean };
  onTagsChanged: () => void;
  metadataDirty: boolean;
}) {
  const hasContent = Boolean(obj.blob && obj.blob_sha256);
  const contentMissing = obj.blob?.state === "missing";
  const downloadable = hasContent && !contentMissing;

  return (
    <div className="section">
      <div className="section-title">Manage this file</div>
      <div className="section-note">
        Housekeeping on the entry itself — nothing here starts a run.
      </div>

      <div className="manage-grid">
        <div className="manage-label">Download</div>
        <div>
          {downloadable ? (
            <>
              <a className="btn" href={api.objectDownloadUrl(obj.id)} download={obj.name}>
                Download file
              </a>
              <div className="manage-note">
                The original file as stored
                {obj.blob?.size != null && <> · {formatBytes(obj.blob.size)}</>}
                {compressionLabel(obj.format.compression) && (
                  <> · still {compressionLabel(obj.format.compression)}-compressed</>
                )}
              </div>
            </>
          ) : (
            <div className="manage-note">
              {contentMissing
                ? "The stored file is not currently available. If it lives on an external drive, check that the drive is mounted."
                : "No stored content to download yet."}
            </div>
          )}
        </div>

        <div className="manage-label">Tags</div>
        <div>
          <TagEditor objectId={obj.id} tags={obj.tags} onChanged={onTagsChanged} />
        </div>

        <div className="manage-label">Role</div>
        <div>
          <RoleConverter obj={obj} metadataDirty={metadataDirty} bare />
        </div>

        {/* PairEditor renders nothing for anything but ready reads, which
            would strand this label with no control beside it. Same condition
            it uses internally, checked here so the whole row drops out. */}
        {obj.format.kind === "fastq" && obj.status === "ready" && (
          <>
            <div className="manage-label">Paired end</div>
            <div>
              <PairEditor obj={obj} bare />
            </div>
          </>
        )}

        <div className="manage-label">Delete</div>
        <div>
          {!confirmingDelete ? (
            <>
              <button
                type="button"
                className="btn danger"
                onClick={() => setConfirmingDelete(true)}
              >
                Delete file
              </button>
              <div className="manage-note">
                {obj.blob?.storage === "external"
                  ? "Removes this entry. The original file on disk is left untouched."
                  : (obj.blob?.ref_count ?? 0) > 1
                    ? `Removes this entry. ${obj.blob!.ref_count - 1} other file(s) share the same content, so the stored data is kept.`
                    : "Removes this entry. The stored data is reclaimed later by garbage collection."}
              </div>
            </>
          ) : (
            <div className="error-box" style={{ marginBottom: 0 }}>
              <div style={{ marginBottom: 8 }}>
                Delete <strong>{obj.name}</strong>? This cannot be undone.
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="btn danger"
                  onClick={() => remove.mutate()}
                  disabled={remove.isPending}
                >
                  {remove.isPending ? "Deleting…" : "Yes, delete"}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setConfirmingDelete(false)}
                  disabled={remove.isPending}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Add the `bare` prop to the two reused editors**

`RoleConverter` and `PairEditor` currently render their own `.section` wrapper with a `.section-title`, which would double up inside the grid. Add an optional `bare` prop to each that skips the wrapper.

Both components currently render their own `.section` wrapper with a `.section-title`, which would double up inside the grid. Add an optional `bare?: boolean` to each `Props` type; when true, omit the wrapper and the title and return only the inner content.

**`RoleConverter.tsx`** is the simple case: one `.section` return at line 76. Extract its children into `const body = (<>…</>)` and return `bare ? body : <div className="section"><div className="section-title">Role</div>{body}</div>`.

**`PairEditor.tsx` has two `.section` returns, not one** — an early return at line 77 for the already-paired case and the main one at line 106. Both need the same treatment, so factor the wrapper into a small local helper rather than repeating the ternary:

```tsx
  const wrap = (body: React.ReactNode) =>
    bare ? (
      body
    ) : (
      <div className="section">
        <div className="section-title">Paired end</div>
        {body}
      </div>
    );
```

Then each return becomes `return wrap(<>…</>);` with the `.section` div and its title removed from both.

**Also note `PairEditor` returns `null` outright** for anything that is not ready reads (line 71). In the manage grid that would leave the static "Paired end" label with nothing beside it. Guard the label/control pair in `ManageFile.tsx` with the same condition the component uses — reads-format and `obj.status === "ready"` — so the row is absent rather than empty on a BAM or a FASTA.

- [ ] **Step 4: Add the styles**

Append to `frontend/src/styles.css`:

```css
/* Manage this file: label/control pairs, two per row on a wide panel. The
   label column is fixed so labels align across rows regardless of control
   height. */
.manage-grid {
  display: grid;
  grid-template-columns: minmax(80px, 110px) 1fr;
  gap: 16px 14px;
  align-items: start;
}

@media (min-width: 900px) {
  .manage-grid {
    grid-template-columns: minmax(80px, 110px) 1fr minmax(80px, 110px) 1fr;
  }
}

.manage-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-faint);
  padding-top: 7px;
}

.manage-note {
  color: var(--text-faint);
  font-size: 11px;
  margin-top: 6px;
}
```

- [ ] **Step 5: Verify it compiles**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/Computations.tsx frontend/src/components/ManageFile.tsx frontend/src/components/RoleConverter.tsx frontend/src/components/PairEditor.tsx frontend/src/styles.css
git commit -m "feat: add the Computations and Manage this file sections"
```

---

### Task 10: Wire the Actions tab together

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx:1001-1120` (replace `ActionsTab`)
- Modify: `frontend/src/components/DetailPanel.tsx:548-623` (remove the headline button row)
- Modify: `frontend/src/components/DetailPanel.tsx:679-690` (pass new props)

- [ ] **Step 1: Replace ActionsTab**

Replace the whole `ActionsTab` function (lines 1001-1120) with:

```tsx
/** The operations that change the record rather than describe it. */
function ActionsTab({
  obj,
  confirmingDelete,
  setConfirmingDelete,
  remove,
  onTagsChanged,
  metadataDirty,
  computations,
}: {
  obj: ObjectDetailData;
  confirmingDelete: boolean;
  setConfirmingDelete: (v: boolean) => void;
  remove: { mutate: () => void; isPending: boolean };
  onTagsChanged: () => void;
  metadataDirty: boolean;
  computations: React.ReactNode;
}) {
  return (
    <>
      {/* Above the grid deliberately: a gated card's reason points up at the
          QC button that resolves it, so it has to be visible from here. */}
      {computations}
      <PipelineSuggestions objectId={obj.id} />
      <ManageFile
        obj={obj}
        confirmingDelete={confirmingDelete}
        setConfirmingDelete={setConfirmingDelete}
        remove={remove}
        onTagsChanged={onTagsChanged}
        metadataDirty={metadataDirty}
      />
    </>
  );
}
```

Add the imports at the top of `DetailPanel.tsx`:

```tsx
import { Computations } from "./Computations";
import { ManageFile } from "./ManageFile";
import { PipelineSuggestions } from "./PipelineSuggestions";
```

Remove the now-unused `TagEditor`, `RoleConverter`, `PairEditor`, `formatBytes` and `compressionLabel` imports **only if** nothing else in the file uses them — check with grep before deleting.

- [ ] **Step 2: Remove the headline button row**

Delete the button row at lines 548-623 (the `<div>` containing the Trim/Align/Call variants/Run QC/Re-ingest buttons), leaving `<FileHeadlineStats stats={stats} />` in place.

- [ ] **Step 3: Pass the computations node to the tab**

At the `{tab === "actions" && (` block (line 679), pass the new prop:

```tsx
            <ActionsTab
              obj={obj}
              confirmingDelete={confirmingDelete}
              setConfirmingDelete={setConfirmingDelete}
              remove={remove}
              onTagsChanged={() => qc.invalidateQueries({ queryKey: ["object", id] })}
              metadataDirty={metadataDirty}
              computations={
                <Computations
                  canPreprocess={canTrim}
                  canAlign={canAlign}
                  canCallVariants={canCallVariants}
                  canQC={canQC}
                  hasQc={hasQc}
                  alignTarget={alignTarget}
                  onStart={startFlow}
                  onRunQC={() => runQC.mutate()}
                  qcPending={runQC.isPending}
                  onReingest={() => reingest.mutate()}
                  reingestPending={reingest.isPending}
                  reingestDisabled={!obj.blob_sha256}
                />
              }
            />
```

- [ ] **Step 4: Invalidate suggestions when facts change**

A card's status is derived from facts, metadata and the project's references, so every mutation that changes one has to invalidate the grid — otherwise it keeps saying "Run QC to determine read chemistry" beside a file that just finished QC.

Add this line alongside the existing invalidations in each `onSuccess`:

```tsx
      qc.invalidateQueries({ queryKey: ["suggestions", id] });
```

in **all four** of:

- `runQC` (around line 397) — the one that un-gates align.
- `reingest` — re-derives format and facts.
- `save` (the metadata mutation, around line 361) — organism and assay both feed the align rule.
- `remove`/role conversion — a file becoming a reference changes the *other* files' align cards.

That last one cannot invalidate by object id, since it changes cards on files other than the one converted. Use the broad key `qc.invalidateQueries({ queryKey: ["suggestions"] })` there to drop every cached grid in the project.

Note `RoleConverter` owns its own mutation; add the broad invalidation inside that component rather than in `DetailPanel`.

- [ ] **Step 5: Verify it compiles and check in the browser**

```bash
docker compose exec web npx tsc --noEmit
```

Then rebuild and look at it:

```bash
docker compose up -d --build api web
```

Open http://localhost:5173, select a FASTQ, open the Actions tab. Expected: three sections in order; the align card `unavailable` with a reason (QC has run on few files); the headline no longer carries the button row.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx
git commit -m "feat: rebuild the Actions tab around suggestions and computations"
```

---

### Task 11: QC empty state and the Preprocess rename

**Files:**
- Modify: `frontend/src/components/DetailPanel.tsx` (`QcTab`, around line 749)
- Modify: `frontend/src/components/TrimDialog.tsx`
- Modify: `frontend/src/components/PipelineToolSelector.tsx`

Moving Run QC out of the headline costs its visibility from the Quality tab, which is where noticing that QC never ran is most likely. This puts the affordance where the absence shows.

- [ ] **Step 1: Add the empty-state button**

In `QcTab`, when `obj.facts.qc_tool` is absent and the file is a FASTQ, render above the existing content:

```tsx
      {!hasQc && obj.format.kind === "fastq" && (
        <div className="section">
          <div className="section-note" style={{ marginBottom: 8 }}>
            No QC has been run on this file yet. Read chemistry, adapter
            content and quality distributions all come from it — and several
            pipeline suggestions stay disabled without it.
          </div>
          <button
            type="button"
            className="btn primary"
            onClick={onRunQC}
            disabled={qcPending}
          >
            {qcPending ? "Running QC…" : "Run QC"}
          </button>
        </div>
      )}
```

`QcTab` currently takes `{ obj, isReference }`. Add `hasQc: boolean`, `onRunQC: () => void` and `qcPending: boolean` to its props and pass them from the `{tab === "qc" && (` block, reusing the same `runQC` mutation the Computations section uses.

- [ ] **Step 2: Rename Trim to Preprocess in the dialog**

In `frontend/src/components/TrimDialog.tsx`, change the user-facing heading text from "Trim" to "Preprocess". Leave the filename, the `/pipelines/trim` route, the job kind and the `trim_*` facts untouched — renaming those is a data migration for a cosmetic gain.

In `frontend/src/components/PipelineToolSelector.tsx`, check for a title derived from the pipeline name (`trim` -> "Trim") and update the display string only.

- [ ] **Step 3: Verify in the browser**

```bash
docker compose up -d --build web
```

Open a FASTQ with no QC facts, Quality tab. Expected: the prompt and a working Run QC button. Click it, confirm the job queues and the Actions tab's align card un-gates once it finishes.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/DetailPanel.tsx frontend/src/components/TrimDialog.tsx frontend/src/components/PipelineToolSelector.tsx
git commit -m "feat: add a QC empty-state action and rename Trim to Preprocess"
```

---

### Task 12: CLAUDE.md note

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the note**

Append a section to `CLAUDE.md`:

```markdown
## Adding a pipeline tool

Registering a tool in `backend/app/pipelines/tools.py` is only half the
change. `backend/app/services/suggestion_service.py` decides which tool each
Actions-tab card recommends, and it is a hand-maintained mapping -- a new tool
that no rule can pick will never be suggested, however cleanly it installs.

The failure mode is silent, which is why this is worth writing down:
installing Flye does not make the Assemble card light up. It leaves a card
reading "No assembler is installed" sitting next to an installed assembler.

So when adding a tool, check `suggestion_service.py` for either a rule that
should now pick it, or a card whose `unavailable` reason has just stopped
being true. The rules have tests in
`backend/tests/services/test_suggestion_service.py`; add the case there.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note that new tools need suggestion rules revisited"
```

---

### Task 13: Final verification

- [ ] **Step 1: Full backend suite**

Run: `docker compose exec api python -m pytest tests/ -q`
Expected: PASS, no regressions.

- [ ] **Step 2: Typecheck**

Run: `docker compose exec web npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Browser pass**

```bash
docker compose up -d --build api web
```

At http://localhost:5173, check each case:

- A **FASTQ with no QC**: preprocess available, align gated on QC (and possibly reference), assemble unavailable.
- A **FASTQ with QC** (4 exist in the database): align available and naming its aligner, `why` text carrying the chemistry reason.
- A **BAM**: variants card only, plus the Results tab unaffected.
- A **FASTA reference**: no cards, quiet empty line rather than an error box.
- **Manage this file**: two columns on a wide window, one on a narrow one; tags, role conversion, pairing and delete all still work.
- **Both themes**: toggle Broadsheet and confirm the cards read correctly in each.

- [ ] **Step 4: Confirm no stale worktree mount**

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```

Expected: no path contains `.claude/worktrees/`.
