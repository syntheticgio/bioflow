# UniProt Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Download UniProt protein data -- a whole proteome or a hand-picked set of proteins -- into a project as a stored object, from a dialog that figures out what the user typed.

**Architecture:** A sibling to the assembly download in structure (resolver → service → handler → applier) but not in mechanics: there is no binary, so the handler is `HandlerMode.THREAD` doing a blocking HTTP GET rather than `SUBPROCESS` shelling out. (THREAD, not ASYNC — the urllib call blocks, and the executor is what keeps blocking work off the event loop.) One UniProt `stream` endpoint serves both download shapes, so there is one handler and one `RunKind`; the dialog branches, the job does not.

**Tech Stack:** Python 3.12, FastAPI, Beanie/MongoDB, pytest. React 18 + TypeScript, TanStack Query. UniProt REST API (`rest.uniprot.org`), called with stdlib `urllib` to match `structure_lookup.py`.

**Design spec:** `docs/superpowers/specs/2026-07-31-uniprot-download-design.md`

---

## Read This First

Three facts were measured against the live UniProt API on 2026-07-31. Each one contradicts something an engineer would reasonably assume, and getting any of them wrong produces a feature that looks correct and is not.

1. **The reference-proteome filter is `reference:true`, NOT `proteome_type:1`.**
   `proteome_type:1` looks right and appears in older examples. It returns
   **0 results for every organism**, including ones that definitely have a
   reference proteome.

2. **`organism_id:4932` (yeast, species level) has NO reference proteome.**
   UniProt attaches it to the *strain* taxon `559292`. Measured:
   `organism_id:4932 AND reference:true` → 0, while `organism_id:4932` → 360.
   The fallback in Task 3 is mandatory, not defensive: without it the resolver
   tells users yeast has no proteome.

3. **`X-Total-Results` and the streamed record count differ slightly.**
   Human reviewed reported 20,416 and delivered 20,427. Use the header for
   sizing, never as a post-download assertion.

**Environment notes:**
- **Run tests with `./scripts/wt-pytest.sh`, never `docker compose exec api
  python -m pytest`.** CLAUDE.md prescribes the latter, and it is right for
  the main repo and wrong here: the shared stack bind-mounts
  `/Users/syntheticgio/Programming/local-bio-pipeliner/backend`, so inside a
  worktree that command silently runs *main's* code. Every result would be
  about the wrong tree. A bare host `.venv` is not an option either — it hits
  Mongo replica-set connection errors.
- The runner starts a throwaway Mongo of its own. `conftest.py` hardcodes the
  database name `biopipe_test` and drops every collection at session start, so
  sharing one Mongo with the running stack makes DB-touching tests
  (`test_mate_link`, `test_read_pairing`, `test_variant_taxid`) fail in a
  different combination on every run. Measured on one unchanged tree: 7
  failed, then 1872 passed, then 5 failed. With the private Mongo: five
  consecutive runs, 1872 passed each time.
- `worker` does NOT hot-reload. After changing anything under
  `backend/app/queue/`, run `docker compose restart worker` before re-testing
  a job, or you will test the old in-memory code.
- Run `docker compose` from the **main repo root**
  (`/Users/syntheticgio/Programming/local-bio-pipeliner`), never from a
  worktree. Compose resolves the bind mounts relative to the invocation
  directory and would silently repoint the shared stack at this branch.

---

## File Structure

**Create:**

| Path | Responsibility |
| --- | --- |
| `backend/app/metadata/uniprot.py` | Classify an input string; query UniProt for proteomes and proteins. No DB, no queue. |
| `backend/app/services/uniprot_service.py` | Validate a download request, build the query, create the run, enqueue the job. |
| `backend/app/queue/uniprot_handlers.py` | The `download_uniprot` handler: stream FASTA to tmp/, return a staged description. |
| `backend/app/api/v1/uniprot.py` | `/uniprot/resolve` and `/uniprot/download`. |
| `frontend/src/components/UniProtDownloadDialog.tsx` | One field, four input classes, card or picker. |
| `backend/tests/metadata/test_uniprot_classify.py` | Input classification. |
| `backend/tests/metadata/test_uniprot_queries.py` | Query construction, including the two corrections above. |
| `backend/tests/services/test_uniprot_service.py` | Launch validation and labels. |
| `backend/tests/queue/test_uniprot_download.py` | Handler behaviour with a stubbed transport. |
| `backend/tests/api/test_uniprot_resolve.py` | The two endpoints. |

**Modify:**

| Path | Change |
| --- | --- |
| `backend/app/models/run.py:24-32` | Add `UNIPROT_DOWNLOAD` to `RunKind`. |
| `backend/app/queue/handlers.py` | Import `uniprot_handlers` for its registration side effects. |
| `backend/app/queue/results.py:1263` | Register `_apply_uniprot_download` in `_APPLIERS`. |
| `backend/app/pipelines/sources.py:49` | Add the UniProt `DataSource` entry. |
| `backend/app/api/v1/__init__.py:8,31` | Import and include the new router. |
| `frontend/src/api/types.ts` | Response types. |
| `frontend/src/api/client.ts:412` | `uniprotResolve` and `uniprotDownload`. |
| `frontend/src/components/ProjectExplorer.tsx:348-366` | Menu item and dialog mount. |

**Not modified, and why:** `backend/app/services/suggestion_service.py`. Its
align rule filters on `o.role is ObjectRole.REFERENCE` (line ~670), so an
object ingested as `PROTEIN` is already excluded. This was checked, not
assumed -- CLAUDE.md requires checking, and the answer is that no card's
`unavailable` reason stops being true because proteomes feed no pipeline here.

---

### Task 1: Classify what the user typed

**Files:**
- Create: `backend/app/metadata/uniprot.py`
- Test: `backend/tests/metadata/test_uniprot_classify.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_uniprot_classify.py`:

```python
"""What kind of thing did the user type into the one accession box?

The cases that matter are the near-misses. `EGFR` is a gene symbol, not an
accession, and `GCF_000002445.2` belongs to the NCBI dialog -- both must reach
the free-text branch rather than being sent to UniProt as accessions.
"""

from app.metadata import uniprot


class TestClassify:
    def test_a_proteome_id(self):
        assert uniprot.classify("UP000002311") == uniprot.InputKind.PROTEOME

    def test_a_proteome_id_is_case_insensitive_and_trimmed(self):
        assert uniprot.classify("  up000002311 ") == uniprot.InputKind.PROTEOME

    def test_a_single_accession(self):
        assert uniprot.classify("P0DTC2") == uniprot.InputKind.ACCESSIONS

    def test_a_long_form_accession(self):
        """The 10-character form. Real: it is the first record in yeast's
        own proteome FASTA."""
        assert uniprot.classify("A0A0B7P3V8") == uniprot.InputKind.ACCESSIONS

    def test_several_accessions_separated_by_spaces_or_commas(self):
        assert uniprot.classify("P00533, P0DTC2") == uniprot.InputKind.ACCESSIONS

    def test_a_bare_taxon_id(self):
        assert uniprot.classify("4932") == uniprot.InputKind.TAXON

    def test_a_gene_symbol_is_not_an_accession(self):
        """EGFR looks accession-shaped to a loose regex. Sending it as one
        returns nothing, where a text search finds the protein."""
        assert uniprot.classify("EGFR") == uniprot.InputKind.TEXT

    def test_an_ncbi_assembly_accession_is_text(self):
        """It belongs to the other dialog. Reaching the text branch produces
        an empty search rather than a confusing UniProt error."""
        assert uniprot.classify("GCF_000002445.2") == uniprot.InputKind.TEXT

    def test_an_sra_accession_is_text(self):
        assert uniprot.classify("SRR11768093") == uniprot.InputKind.TEXT

    def test_a_protein_name_is_text(self):
        assert uniprot.classify("spike glycoprotein") == uniprot.InputKind.TEXT

    def test_empty_input_is_text(self):
        assert uniprot.classify("") == uniprot.InputKind.TEXT


class TestParseAccessions:
    def test_splits_on_commas_and_whitespace(self):
        assert uniprot.parse_accessions("P00533, P0DTC2  P0DTC1") == [
            "P00533",
            "P0DTC2",
            "P0DTC1",
        ]

    def test_uppercases_and_deduplicates_preserving_order(self):
        assert uniprot.parse_accessions("p00533 P00533 p0dtc2") == [
            "P00533",
            "P0DTC2",
        ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./scripts/wt-pytest.sh tests/metadata/test_uniprot_classify.py -q
```
Expected: FAIL, `ModuleNotFoundError` or `ImportError: cannot import name 'uniprot'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/metadata/uniprot.py`:

```python
"""Reading UniProt: what an input names, and what UniProt says about it.

Separate from `structure_lookup.py`, which resolves a gene symbol to one
protein for the variants table. This module answers a different question --
"what did the user type, and what can be downloaded for it?" -- and shares
only the choice of transport.

Uses stdlib urllib rather than httpx, which is a dev-only dependency here;
these are simple JSON GETs, and they run in a worker thread.
"""

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum

from app.logging import get_logger

log = get_logger(__name__)

_PROTEOMES_URL = "https://rest.uniprot.org/proteomes/search"
_PROTEOME_ENTRY_URL = "https://rest.uniprot.org/proteomes"
_UNIPROTKB_URL = "https://rest.uniprot.org/uniprotkb/search"
_STREAM_URL = "https://rest.uniprot.org/uniprotkb/stream"

# Matches structure_lookup's timeout, for the same reason: long enough for a
# cold response, short enough that a click does not appear to hang.
_TIMEOUT_SECONDS = 20.0

# How many rows a picker shows. UniProt ranks by relevance and the wanted
# entry is near the top; a larger page turns one lookup into a scan.
_MAX_RESULTS = 25

_PROTEOME_ID = re.compile(r"^UP\d{9}$")

# UniProt's own documented accession pattern. Deliberately strict: a looser
# one classifies the gene symbol EGFR as an accession, which returns nothing.
_ACCESSION = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$"
)

_SPLIT = re.compile(r"[\s,;]+")


class InputKind(StrEnum):
    PROTEOME = "proteome"
    ACCESSIONS = "accessions"
    TAXON = "taxon"
    TEXT = "text"


def classify(raw: str) -> InputKind:
    """What the accession box holds.

    Shape only -- no network. An input that looks like nothing in particular
    becomes TEXT, which is the branch that degrades gracefully: a free-text
    search returning no rows is a comprehensible answer, where a malformed
    accession query is an error the user cannot act on.
    """
    value = (raw or "").strip()
    if not value:
        return InputKind.TEXT

    if _PROTEOME_ID.match(value.upper()):
        return InputKind.PROTEOME

    tokens = [t for t in _SPLIT.split(value.upper()) if t]
    if tokens and all(_ACCESSION.match(t) for t in tokens):
        return InputKind.ACCESSIONS

    if value.isdigit():
        return InputKind.TAXON

    return InputKind.TEXT


def parse_accessions(raw: str) -> list[str]:
    """The accessions in an input, uppercased and deduplicated.

    Order is preserved because the picker lists them in it, and a user who
    pasted a deliberate order should see that order back.
    """
    seen: list[str] = []
    for token in _SPLIT.split((raw or "").strip().upper()):
        if token and token not in seen:
            seen.append(token)
    return seen
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
./scripts/wt-pytest.sh tests/metadata/test_uniprot_classify.py -q
```
Expected: PASS, 13 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/uniprot.py backend/tests/metadata/test_uniprot_classify.py
git commit -m "feat: classify UniProt accession-box input"
```

---

### Task 2: Build the UniProt queries

**Files:**
- Modify: `backend/app/metadata/uniprot.py`
- Test: `backend/tests/metadata/test_uniprot_queries.py`

This task is where the two API corrections live. The tests assert the exact
query strings, because these are the strings that were measured to work.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_uniprot_queries.py`:

```python
"""The query strings, asserted exactly.

These are not arbitrary. Each was measured against the live API on
2026-07-31, and the obvious-looking alternatives return zero rows:

- `proteome_type:1` returns 0 for every organism tried, including ones that
  do have a reference proteome. The working filter is `reference:true`.
- `organism_id:4932 AND reference:true` returns 0, because UniProt attaches
  yeast's reference proteome to the strain taxon 559292. The unfiltered
  `organism_id:4932` returns 360.

Asserting the literal string is the only way a future edit that "tidies" one
of these into the broken form gets caught.
"""

from app.metadata import uniprot


class TestProteomeQueries:
    def test_reference_proteome_for_a_taxon_uses_reference_true(self):
        """NOT proteome_type:1, which returns nothing for every organism."""
        q = uniprot.reference_proteome_query(559292)
        assert q == "organism_id:559292 AND reference:true"
        assert "proteome_type" not in q

    def test_all_proteomes_for_a_taxon_has_no_type_filter(self):
        """The fallback for a species-level taxon such as 4932, where the
        reference query returns nothing but 360 proteomes exist."""
        assert uniprot.all_proteomes_query(4932) == "organism_id:4932"

    def test_organism_name_search_has_no_type_filter(self):
        """Measured: adding one returns 0 for a name that otherwise gives
        481 hits with the right proteome ranked first."""
        q = uniprot.organism_name_query("Saccharomyces cerevisiae")
        assert q == 'organism_name:"Saccharomyces cerevisiae"'
        assert "reference" not in q

    def test_organism_name_quotes_are_stripped_from_input(self):
        """A pasted name may arrive already quoted; doubling them produces a
        query that matches nothing."""
        assert (
            uniprot.organism_name_query('"Homo sapiens"')
            == 'organism_name:"Homo sapiens"'
        )


class TestDownloadQueries:
    def test_a_whole_proteome_reviewed_only(self):
        assert uniprot.download_query(
            proteome_id="UP000002311", accessions=[], reviewed_only=True
        ) == "proteome:UP000002311 AND reviewed:true"

    def test_a_whole_proteome_including_unreviewed(self):
        """No reviewed clause at all -- that is what 'everything' means, and
        it is the 147,506-protein case for human."""
        assert uniprot.download_query(
            proteome_id="UP000002311", accessions=[], reviewed_only=False
        ) == "proteome:UP000002311"

    def test_picked_accessions_ignore_the_reviewed_flag(self):
        """The user named these entries. Filtering out an unreviewed one they
        explicitly asked for would silently return fewer proteins than were
        selected."""
        assert uniprot.download_query(
            proteome_id=None, accessions=["P0DTC2", "P00533"], reviewed_only=True
        ) == "accession:P0DTC2 OR accession:P00533"

    def test_a_single_accession(self):
        assert uniprot.download_query(
            proteome_id=None, accessions=["P0DTC2"], reviewed_only=False
        ) == "accession:P0DTC2"


class TestStreamUrl:
    def test_requests_compressed_fasta(self):
        url = uniprot.stream_url("proteome:UP000002311")
        assert url.startswith("https://rest.uniprot.org/uniprotkb/stream?")
        assert "format=fasta" in url
        assert "compressed=true" in url
        assert "query=proteome%3AUP000002311" in url
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./scripts/wt-pytest.sh tests/metadata/test_uniprot_queries.py -q
```
Expected: FAIL, `AttributeError: module 'app.metadata.uniprot' has no attribute 'reference_proteome_query'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/metadata/uniprot.py`:

```python
def reference_proteome_query(taxon_id: int) -> str:
    """The reference proteome for one taxon.

    `reference:true`, not `proteome_type:1`. The latter is the form that
    looks right and appears in older examples; measured against the live API
    it returns zero rows for every organism tried, including taxon 559292,
    which does have a reference proteome.
    """
    return f"organism_id:{taxon_id} AND reference:true"


def all_proteomes_query(taxon_id: int) -> str:
    """Every proteome for one taxon, reference or not.

    The fallback when the reference query is empty, which is not an edge
    case: taxon 4932 (*S. cerevisiae* at species level, the ID a user is
    most likely to type) has no reference proteome because UniProt attaches
    it to strain taxon 559292. Measured 0 against 360.
    """
    return f"organism_id:{taxon_id}"


def organism_name_query(name: str) -> str:
    """Proteomes for an organism named in words.

    No type filter, for the same measured reason as `all_proteomes_query`:
    adding one returns 0 where the unfiltered query returns 481 with the
    wanted proteome ranked first.
    """
    cleaned = (name or "").strip().strip('"')
    return f'organism_name:"{cleaned}"'


def download_query(
    *, proteome_id: str | None, accessions: list[str], reviewed_only: bool
) -> str:
    """The query the FASTA stream is fetched with.

    One function for both download shapes, because one endpoint serves both:
    a whole proteome and a hand-picked set differ only here.

    `reviewed_only` is deliberately ignored for picked accessions. The user
    named those entries; filtering an unreviewed one back out would hand
    them fewer proteins than they selected, with nothing to explain why.
    """
    if accessions:
        return " OR ".join(f"accession:{a}" for a in accessions)
    query = f"proteome:{proteome_id}"
    if reviewed_only:
        query += " AND reviewed:true"
    return query


def stream_url(query: str) -> str:
    """The FASTA stream endpoint for a query.

    Compressed: a proteome is mostly sequence text and gzip roughly halves
    it (measured 3.9 MB to 1.9 MB for yeast).
    """
    params = urllib.parse.urlencode(
        {"query": query, "format": "fasta", "compressed": "true"}
    )
    return f"{_STREAM_URL}?{params}"
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
./scripts/wt-pytest.sh tests/metadata/test_uniprot_queries.py -q
```
Expected: PASS, 9 passed. (The test file above has 9 methods: 4 + 4 + 1.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/uniprot.py backend/tests/metadata/test_uniprot_queries.py
git commit -m "feat: UniProt query construction, with the measured filters"
```

---

### Task 3: Resolve an input to a card or a picker

**Files:**
- Modify: `backend/app/metadata/uniprot.py`
- Test: `backend/tests/metadata/test_uniprot_resolve.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/metadata/test_uniprot_resolve.py`:

```python
"""Resolution, with the network stubbed.

The test that matters most is `test_a_species_taxon_falls_back_to_all`:
taxon 4932 has no reference proteome, and a resolver without the fallback
reports that yeast has no proteome while 360 sit behind it.
"""

import json

import pytest

from app.metadata import uniprot


@pytest.fixture
def stub_transport(monkeypatch):
    """Replace the one HTTP seam and record what was asked for.

    Patches `uniprot._get_json`, which is the module's only transport
    function -- patching urllib instead would leave the URL construction
    untested, which is the part that was measurably easy to get wrong.
    """
    calls: list[str] = []
    responses: dict[str, dict] = {}

    def fake_get_json(url: str, *, timeout: float = 0.0) -> dict:
        calls.append(url)
        for fragment, payload in responses.items():
            if fragment in url:
                return payload
        return {"results": []}

    monkeypatch.setattr(uniprot, "_get_json", fake_get_json)
    return calls, responses


def _proteome(pid: str, *, ref: bool = True, count: int = 6067, busco: int = 99):
    return {
        "id": pid,
        "proteomeType": "Reference proteome" if ref else "Non Reference proteome",
        "proteinCount": count,
        "strain": "ATCC 204508 / S288c",
        "taxonomy": {"taxonId": 559292, "scientificName": "Saccharomyces cerevisiae"},
        "genomeAssembly": {"assemblyId": "GCA_000146045.2"},
        "proteomeCompletenessReport": {"buscoReport": {"score": busco}},
    }


class TestProteomeResolution:
    def test_a_strain_taxon_uses_the_reference_query(self, stub_transport):
        calls, responses = stub_transport
        responses["reference%3Atrue"] = {"results": [_proteome("UP000002311")]}

        result = uniprot.resolve_taxon(559292)

        assert result.proteome is not None
        assert result.proteome.id == "UP000002311"
        assert result.needs_picker is False

    def test_a_species_taxon_falls_back_to_all(self, stub_transport):
        """Taxon 4932 returns nothing for `reference:true` and 360 for the
        unfiltered query. Without this fallback the dialog says yeast has no
        proteome, which is the single most likely way to ship this broken.
        """
        calls, responses = stub_transport
        responses["organism_id%3A4932+AND+reference"] = {"results": []}
        responses["organism_id%3A4932&"] = {
            "results": [_proteome("UP000037662", ref=False, count=5389, busco=98)]
        }

        result = uniprot.resolve_taxon(4932)

        assert result.needs_picker is True
        assert len(result.candidates) == 1
        assert result.candidates[0].id == "UP000037662"

    def test_the_proteome_carries_its_genome_assembly(self, stub_transport):
        """The cross-link to the NCBI download. Present on the record, so it
        costs nothing to surface."""
        calls, responses = stub_transport
        responses["reference%3Atrue"] = {"results": [_proteome("UP000002311")]}

        result = uniprot.resolve_taxon(559292)

        assert result.proteome.genome_assembly == "GCA_000146045.2"

    def test_busco_is_carried_for_the_picker(self, stub_transport):
        """The signal that makes choosing between strains possible rather
        than arbitrary."""
        calls, responses = stub_transport
        responses["reference%3Atrue"] = {"results": []}
        responses["organism_id%3A4932&"] = {
            "results": [_proteome("UP000188490", ref=False, busco=93)]
        }

        result = uniprot.resolve_taxon(4932)

        assert result.candidates[0].busco_score == 93

    def test_a_taxon_with_nothing_resolves_empty(self, stub_transport):
        calls, responses = stub_transport
        result = uniprot.resolve_taxon(99999999)
        assert result.proteome is None
        assert result.candidates == []


class TestProteinResolution:
    def test_a_text_search_returns_hits(self, stub_transport):
        calls, responses = stub_transport
        responses["uniprotkb"] = {
            "results": [
                {
                    "primaryAccession": "P0DTC2",
                    "uniProtkbId": "SPIKE_SARS2",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "proteinDescription": {
                        "recommendedName": {"fullName": {"value": "Spike glycoprotein"}}
                    },
                    "organism": {"scientificName": "SARS-CoV-2"},
                    "sequence": {"length": 1273},
                }
            ]
        }

        hits = uniprot.search_proteins("spike glycoprotein")

        assert len(hits) == 1
        assert hits[0].accession == "P0DTC2"
        assert hits[0].name == "Spike glycoprotein"
        assert hits[0].length == 1273
        assert hits[0].reviewed is True

    def test_an_unreviewed_entry_is_marked(self, stub_transport):
        calls, responses = stub_transport
        responses["uniprotkb"] = {
            "results": [
                {
                    "primaryAccession": "A0A0B7P3V8",
                    "entryType": "UniProtKB unreviewed (TrEMBL)",
                    "organism": {"scientificName": "Saccharomyces cerevisiae"},
                    "sequence": {"length": 100},
                }
            ]
        }

        hits = uniprot.search_proteins("something")

        assert hits[0].reviewed is False

    def test_a_missing_name_does_not_crash(self, stub_transport):
        """Unreviewed entries frequently have no recommendedName."""
        calls, responses = stub_transport
        responses["uniprotkb"] = {
            "results": [
                {
                    "primaryAccession": "A0A0B7P3V8",
                    "entryType": "UniProtKB unreviewed (TrEMBL)",
                    "organism": {"scientificName": "Yeast"},
                    "sequence": {"length": 100},
                }
            ]
        }

        hits = uniprot.search_proteins("something")

        assert hits[0].name is None


class TestCounts:
    def test_counts_come_from_the_total_header(self, stub_transport, monkeypatch):
        """`X-Total-Results` is what makes the size guard exact rather than
        an estimate, and what shows the ~7x reviewed/unreviewed split."""
        def fake_count(query: str, *, timeout: float = 0.0) -> int | None:
            return 20416 if "reviewed" in query else 147506

        monkeypatch.setattr(uniprot, "count_results", fake_count)

        assert uniprot.count_results("proteome:UP000005640 AND reviewed:true") == 20416
        assert uniprot.count_results("proteome:UP000005640") == 147506


class TestFailureIsNotFatal:
    def test_a_network_failure_resolves_to_nothing(self, monkeypatch):
        """Matches structure_lookup's stance: an outage means the dialog
        finds nothing, not that it returns a 500."""
        def boom(url: str, *, timeout: float = 0.0) -> dict:
            raise OSError("connection refused")

        monkeypatch.setattr(uniprot, "_get_json", boom)

        result = uniprot.resolve_taxon(559292)

        assert result.proteome is None
        assert result.candidates == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./scripts/wt-pytest.sh tests/metadata/test_uniprot_resolve.py -q
```
Expected: FAIL, `AttributeError: module 'app.metadata.uniprot' has no attribute '_get_json'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/app/metadata/uniprot.py`:

```python
@dataclass(frozen=True)
class ProteomeInfo:
    id: str
    name: str
    taxon_id: int | None
    strain: str | None
    protein_count: int | None
    is_reference: bool
    busco_score: int | None
    # The NCBI assembly this proteome's genome came from, when UniProt names
    # one. Surfaced as a link rather than a combined download: the proteome
    # and the assembly are the same organism's two halves, but merging the
    # two providers' dialogs was considered and rejected.
    genome_assembly: str | None


@dataclass(frozen=True)
class ProteinHit:
    accession: str
    entry_id: str | None
    name: str | None
    organism: str | None
    length: int | None
    reviewed: bool


@dataclass
class TaxonResolution:
    """What an organism input produced.

    `needs_picker` is the reference-proteome question answered: False means
    one card is enough, True means the user must choose. It is not derivable
    from `candidates` being non-empty, because the reference case also lists
    the alternatives behind a disclosure.
    """

    proteome: ProteomeInfo | None = None
    candidates: list[ProteomeInfo] = field(default_factory=list)
    needs_picker: bool = False


def _get(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> bytes:
    """The transport, isolated so tests can replace it without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _get_json(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> dict:
    payload = json.loads(_get(url, timeout=timeout))
    return payload if isinstance(payload, dict) else {}


def count_results(query: str, *, timeout: float = _TIMEOUT_SECONDS) -> int | None:
    """How many entries a query matches, from `X-Total-Results`.

    Exact, which is what lets the dialog show the reviewed/unreviewed split
    (roughly sevenfold for human) and guard a large download on a real
    number rather than a byte estimate.

    Not an assertion about the download: the header and the streamed record
    count differ slightly -- human reviewed reported 20,416 and delivered
    20,427 -- so a handler that failed on a mismatch would fail on correct
    downloads.
    """
    params = urllib.parse.urlencode(
        {"query": query, "format": "json", "size": "1", "fields": "accession"}
    )
    try:
        with urllib.request.urlopen(
            f"{_UNIPROTKB_URL}?{params}", timeout=timeout
        ) as response:
            total = response.headers.get("X-Total-Results")
    except Exception as exc:
        log.info("uniprot_count_failed", query=query, error=str(exc))
        return None
    try:
        return int(total)
    except (TypeError, ValueError):
        return None


def _proteome_info(entry: dict) -> ProteomeInfo | None:
    pid = entry.get("id")
    if not isinstance(pid, str):
        return None
    taxonomy = entry.get("taxonomy") or {}
    busco = (entry.get("proteomeCompletenessReport") or {}).get("buscoReport") or {}
    assembly = entry.get("genomeAssembly") or {}
    return ProteomeInfo(
        id=pid,
        name=taxonomy.get("scientificName") or pid,
        taxon_id=taxonomy.get("taxonId"),
        strain=entry.get("strain"),
        protein_count=entry.get("proteinCount"),
        is_reference=entry.get("proteomeType") == "Reference proteome",
        busco_score=busco.get("score"),
        genome_assembly=assembly.get("assemblyId"),
    )


def _search_proteomes(query: str) -> list[ProteomeInfo]:
    params = urllib.parse.urlencode(
        {"query": query, "format": "json", "size": str(_MAX_RESULTS)}
    )
    try:
        payload = _get_json(f"{_PROTEOMES_URL}?{params}")
    except Exception as exc:
        # Matches structure_lookup: an outage means "found nothing", never an
        # error the caller has to handle. The dialog says the same thing for
        # a timeout and a genuinely empty result.
        log.info("uniprot_proteome_search_failed", query=query, error=str(exc))
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    out = []
    for entry in results:
        if isinstance(entry, dict):
            info = _proteome_info(entry)
            if info is not None:
                out.append(info)
    return out


def resolve_proteome(proteome_id: str) -> ProteomeInfo | None:
    """One proteome by its own ID."""
    try:
        payload = _get_json(f"{_PROTEOME_ENTRY_URL}/{proteome_id}?format=json")
    except Exception as exc:
        log.info("uniprot_proteome_lookup_failed", proteome=proteome_id, error=str(exc))
        return None
    return _proteome_info(payload)


def resolve_taxon(taxon_id: int) -> TaxonResolution:
    """The proteomes for one taxon, reference first.

    Two queries, and the second is mandatory rather than defensive. Taxon
    4932 -- *S. cerevisiae* at species level, the ID a user is most likely to
    type for yeast -- has no reference proteome, because UniProt attaches it
    to strain taxon 559292. Measured: `reference:true` returns 0 while the
    unfiltered query returns 360. Skipping the fallback tells the user yeast
    has no proteome.
    """
    reference = _search_proteomes(reference_proteome_query(taxon_id))
    if reference:
        # The alternatives still load, behind a disclosure: the reference is
        # right for the common case, and strain matters in real work.
        others = [p for p in _search_proteomes(all_proteomes_query(taxon_id))
                  if p.id != reference[0].id]
        return TaxonResolution(
            proteome=reference[0], candidates=others, needs_picker=False
        )

    candidates = _search_proteomes(all_proteomes_query(taxon_id))
    return TaxonResolution(
        proteome=None, candidates=candidates, needs_picker=bool(candidates)
    )


def resolve_organism_name(name: str) -> TaxonResolution:
    """Proteomes for an organism named in words.

    Ranked by UniProt's own relevance, which puts the reference proteome
    first for a plain species name. A reference hit at the top takes the card
    path; anything else opens the picker.
    """
    candidates = _search_proteomes(organism_name_query(name))
    if not candidates:
        return TaxonResolution()
    if candidates[0].is_reference:
        return TaxonResolution(
            proteome=candidates[0], candidates=candidates[1:], needs_picker=False
        )
    return TaxonResolution(candidates=candidates, needs_picker=True)


def _protein_hit(entry: dict) -> ProteinHit | None:
    accession = entry.get("primaryAccession")
    if not isinstance(accession, str):
        return None
    description = entry.get("proteinDescription") or {}
    recommended = description.get("recommendedName") or {}
    full_name = (recommended.get("fullName") or {}).get("value")
    return ProteinHit(
        accession=accession,
        entry_id=entry.get("uniProtkbId"),
        # Unreviewed entries frequently carry no recommendedName at all.
        name=full_name if isinstance(full_name, str) else None,
        organism=(entry.get("organism") or {}).get("scientificName"),
        length=(entry.get("sequence") or {}).get("length"),
        reviewed="reviewed" in (entry.get("entryType") or "").lower()
        and "unreviewed" not in (entry.get("entryType") or "").lower(),
    )


def search_proteins(query: str) -> list[ProteinHit]:
    """Proteins matching free text, or a set of named accessions."""
    params = urllib.parse.urlencode(
        {
            "query": query,
            "format": "json",
            "size": str(_MAX_RESULTS),
            "fields": "accession,id,protein_name,organism_name,length,reviewed",
        }
    )
    try:
        payload = _get_json(f"{_UNIPROTKB_URL}?{params}")
    except Exception as exc:
        log.info("uniprot_protein_search_failed", query=query, error=str(exc))
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    out = []
    for entry in results:
        if isinstance(entry, dict):
            hit = _protein_hit(entry)
            if hit is not None:
                out.append(hit)
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
./scripts/wt-pytest.sh tests/metadata/test_uniprot_resolve.py -q
```
Expected: PASS, 10 passed. (The test file above has 10 test methods.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/metadata/uniprot.py backend/tests/metadata/test_uniprot_resolve.py
git commit -m "feat: resolve a UniProt input to a proteome card or a picker"
```

---

### Task 4: Add the RunKind member

**Files:**
- Modify: `backend/app/models/run.py:24-32`
- Test: `backend/tests/services/test_uniprot_service.py` (created in Task 5)

- [ ] **Step 1: Make the change**

In `backend/app/models/run.py`, add to `RunKind` after `ASSEMBLY_DOWNLOAD`:

```python
class RunKind(StrEnum):
    ALIGNMENT = "alignment"
    TRIM = "trim"
    SRA_DOWNLOAD = "sra_download"
    VARIANT_CALLING = "variant_calling"
    # Separate from SRA_DOWNLOAD because RunKind is a display and grouping
    # vocabulary, and "downloaded a genome" reads differently from "downloaded
    # sequencing runs" in the activity view.
    ASSEMBLY_DOWNLOAD = "assembly_download"
    # One member for both UniProt download shapes. A whole proteome and a
    # hand-picked set of proteins are the same request to the same endpoint --
    # only the query differs -- so splitting the enum would describe a
    # distinction the machine does not make. The run label carries it instead.
    UNIPROT_DOWNLOAD = "uniprot_download"
```

- [ ] **Step 2: Verify nothing broke**

Run:
```bash
./scripts/wt-pytest.sh tests/ -q
```
Expected: PASS, same count as before this task.

- [ ] **Step 3: Commit**

```bash
git add backend/app/models/run.py
git commit -m "feat: add UNIPROT_DOWNLOAD to RunKind"
```

---

### Task 5: The launch service

**Files:**
- Create: `backend/app/services/uniprot_service.py`
- Test: `backend/tests/services/test_uniprot_service.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/test_uniprot_service.py`:

```python
"""Launch validation for a UniProt download.

The rules that must hold before any job is queued, tested without HTTP --
the same shape as `test_assembly_service.py`.
"""

import pytest

from app.errors import ValidationError
from app.services import uniprot_service


class TestValidation:
    def test_a_request_with_neither_proteome_nor_accessions_is_rejected(self):
        with pytest.raises(ValidationError, match="proteome or at least one"):
            uniprot_service.validate_request(proteome_id=None, accessions=[])

    def test_a_malformed_proteome_id_is_rejected(self):
        """Queueing a download for it would fail an hour later with a
        UniProt error the user cannot act on."""
        with pytest.raises(ValidationError, match="proteome"):
            uniprot_service.validate_request(proteome_id="UP123", accessions=[])

    def test_a_malformed_accession_is_rejected(self):
        with pytest.raises(ValidationError, match="accession"):
            uniprot_service.validate_request(proteome_id=None, accessions=["EGFR"])

    def test_a_valid_proteome_passes(self):
        uniprot_service.validate_request(proteome_id="UP000002311", accessions=[])

    def test_valid_accessions_pass(self):
        uniprot_service.validate_request(
            proteome_id=None, accessions=["P0DTC2", "P00533"]
        )

    def test_too_many_accessions_are_rejected(self):
        """The URL is a GET query string; a thousand OR clauses exceeds what
        UniProt accepts and fails opaquely."""
        with pytest.raises(ValidationError, match="at once"):
            uniprot_service.validate_request(
                proteome_id=None, accessions=[f"P{i:05d}" for i in range(600)]
            )


class TestLabel:
    def test_a_proteome_label_names_it_and_counts_proteins(self):
        label = uniprot_service.download_label(
            proteome_id="UP000002311",
            accessions=[],
            organism="Saccharomyces cerevisiae",
            protein_count=6067,
        )
        assert "UP000002311" in label
        assert "6,067" in label

    def test_a_proteome_label_without_a_count_still_reads(self):
        label = uniprot_service.download_label(
            proteome_id="UP000002311", accessions=[], organism=None, protein_count=None
        )
        assert "UP000002311" in label

    def test_a_picked_set_counts_the_proteins(self):
        label = uniprot_service.download_label(
            proteome_id=None,
            accessions=["P0DTC2", "P00533"],
            organism=None,
            protein_count=None,
        )
        assert "2" in label
        assert "UniProt" in label

    def test_one_picked_protein_reads_singular(self):
        label = uniprot_service.download_label(
            proteome_id=None, accessions=["P0DTC2"], organism=None, protein_count=None
        )
        assert "P0DTC2" in label


class TestFilename:
    def test_a_proteome_is_named_for_its_id(self):
        assert (
            uniprot_service.output_filename(
                proteome_id="UP000002311", accessions=[], reviewed_only=True
            )
            == "UP000002311_reviewed.fasta"
        )

    def test_an_unreviewed_proteome_says_so(self):
        """The 7x difference between these two files for human is invisible
        once they are both sitting in a project called the same thing."""
        assert (
            uniprot_service.output_filename(
                proteome_id="UP000002311", accessions=[], reviewed_only=False
            )
            == "UP000002311_all.fasta"
        )

    def test_a_single_accession_is_named_for_it(self):
        assert (
            uniprot_service.output_filename(
                proteome_id=None, accessions=["P0DTC2"], reviewed_only=True
            )
            == "P0DTC2.fasta"
        )

    def test_several_accessions_get_a_counted_name(self):
        assert (
            uniprot_service.output_filename(
                proteome_id=None,
                accessions=["P0DTC2", "P00533", "P0DTC1"],
                reviewed_only=True,
            )
            == "uniprot_3_proteins.fasta"
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./scripts/wt-pytest.sh tests/services/test_uniprot_service.py -q
```
Expected: FAIL, `ImportError: cannot import name 'uniprot_service'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/uniprot_service.py`:

```python
"""Launching a UniProt download.

The same shape as `assembly_service`: validate the request, build the
payload, and create the run that groups the resulting job. Kept out of the
router so the launch rules are testable without HTTP.

One job for both download shapes, because one UniProt endpoint serves both:
a whole proteome and a hand-picked set differ only in the query string.
Unlike `assembly_service` there is no `tools.require` here -- there is no
binary to find, only an HTTP GET.
"""

import re

from beanie import PydanticObjectId

from app.errors import ConflictError, NotFoundError, ValidationError
from app.logging import get_logger
from app.metadata import uniprot
from app.models import IoClass, JobClass, JobResources, Project, RunJobRole, RunKind
from app.services import run_service

log = get_logger(__name__)

# A GET query string of OR clauses. UniProt accepts a long one but not an
# unbounded one, and the failure past the limit is an opaque HTTP error
# rather than a message about having asked for too much.
MAX_ACCESSIONS = 500

_PROTEOME_ID = re.compile(r"^UP\d{9}$")


def validate_request(*, proteome_id: str | None, accessions: list[str]) -> None:
    """The request names something downloadable.

    Stricter than the resolver's classification, which guesses at what a user
    typed. By this point the frontend has sent a specific thing, so anything
    malformed is a bug rather than an ambiguous input, and queueing it would
    surface as a UniProt error long after the click.
    """
    if not proteome_id and not accessions:
        raise ValidationError(
            "A download needs a proteome or at least one accession.",
        )

    if proteome_id and not _PROTEOME_ID.match(proteome_id):
        raise ValidationError(
            f"{proteome_id!r} is not a proteome identifier. Expected the "
            "UP000000000 form.",
            details={"proteome_id": proteome_id},
        )

    if len(accessions) > MAX_ACCESSIONS:
        raise ValidationError(
            f"{len(accessions)} accessions is more than the {MAX_ACCESSIONS} "
            "that can be fetched at once. Download in batches.",
            details={"count": len(accessions)},
        )

    for accession in accessions:
        if not uniprot._ACCESSION.match(accession):
            raise ValidationError(
                f"{accession!r} is not a UniProt accession.",
                details={"accession": accession},
            )


def download_label(
    *,
    proteome_id: str | None,
    accessions: list[str],
    organism: str | None,
    protein_count: int | None,
) -> str:
    """A one-line description, built at launch.

    Stored rather than derived so the run stays describable after its jobs
    are TTL-pruned -- the same reason `PipelineRun.params` is denormalized.
    This is also where the two download shapes are distinguished, since they
    share one `RunKind`.
    """
    if proteome_id:
        parts = [f"Download {proteome_id}"]
        if organism:
            parts.append(f"({organism})")
        if protein_count:
            parts.append(f"— {protein_count:,} proteins")
        return " ".join(parts)

    if len(accessions) == 1:
        return f"Download {accessions[0]} from UniProt"
    return f"Download {len(accessions)} proteins from UniProt"


def output_filename(
    *, proteome_id: str | None, accessions: list[str], reviewed_only: bool
) -> str:
    """What the ingested file is called.

    The reviewed suffix is not decoration. Human reviewed and human
    unreviewed differ roughly sevenfold, and once both are sitting in a
    project under the same name there is nothing to tell them apart.
    """
    if proteome_id:
        suffix = "reviewed" if reviewed_only else "all"
        return f"{proteome_id}_{suffix}.fasta"
    if len(accessions) == 1:
        return f"{accessions[0]}.fasta"
    return f"uniprot_{len(accessions)}_proteins.fasta"


async def launch_download(
    *,
    project_id: PydanticObjectId,
    proteome_id: str | None,
    accessions: list[str],
    reviewed_only: bool,
    organism: str | None = None,
    protein_count: int | None = None,
):
    """Queue the download and the run that groups it."""
    from app.queue import queue

    validate_request(proteome_id=proteome_id, accessions=accessions)

    project = await Project.get(project_id)
    if project is None:
        raise NotFoundError(f"Project not found: {project_id}")

    query = uniprot.download_query(
        proteome_id=proteome_id, accessions=accessions, reviewed_only=reviewed_only
    )
    filename = output_filename(
        proteome_id=proteome_id, accessions=accessions, reviewed_only=reviewed_only
    )

    run = await run_service.create_run(
        kind=RunKind.UNIPROT_DOWNLOAD,
        project_id=project_id,
        label=download_label(
            proteome_id=proteome_id,
            accessions=accessions,
            organism=organism,
            protein_count=protein_count,
        ),
        inputs=[],  # Nothing in the project is an input; the source is UniProt.
        params={
            "proteome_id": proteome_id,
            "accessions": accessions,
            "reviewed_only": reviewed_only,
            "query": query,
            "source": "uniprot",
        },
    )

    payload = {
        "project_id": str(project_id),
        "query": query,
        "filename": filename,
        "proteome_id": proteome_id,
        "accessions": accessions,
        "reviewed_only": reviewed_only,
        "organism": organism,
    }

    job = await queue.enqueue(
        "download_uniprot",
        payload=payload,
        job_class=JobClass.USER_INTERACTIVE,
        # Far lighter than the assembly download's HEAVY: a yeast proteome is
        # 3.9 MB and human-with-TrEMBL is the worst realistic case.
        resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
        max_attempts=3,
        # Keyed on (query, project) so a double-click collapses, while the
        # same proteome stays downloadable into a second project.
        dedup_key=f"uniprot_download:{query}:{project_id}",
        project_id=project_id,
    )

    if job is None:
        # Already queued or running from an earlier click, so this run
        # describes no work and must not linger in the activity view.
        await run_service.discard_run(run.id)
        raise ConflictError(
            "That download is already running",
            details={"query": query},
        )

    await run_service.link_job(run.id, job.id, RunJobRole.DOWNLOAD)

    log.info(
        "uniprot_download_launched",
        run_id=str(run.id),
        project_id=str(project_id),
        query=query,
    )
    return run, [str(job.id)]
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
./scripts/wt-pytest.sh tests/services/test_uniprot_service.py -q
```
Expected: PASS, 14 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/uniprot_service.py backend/tests/services/test_uniprot_service.py
git commit -m "feat: UniProt download launch service"
```

---

### Task 6: The download handler

**Files:**
- Create: `backend/app/queue/uniprot_handlers.py`
- Modify: `backend/app/queue/handlers.py`
- Test: `backend/tests/queue/test_uniprot_download.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_uniprot_download.py`:

```python
"""The UniProt download handler, with the transport stubbed.

Deliberately unlike `test_assembly_download.py`: there is no zip, no
checksum manifest, and no path-traversal check, because the response is a
gzipped FASTA stream rather than an archive that writes files.
"""

import gzip
import threading
from pathlib import Path

import pytest

from app.errors import PermanentError, RetryableError
from app.queue import uniprot_handlers
from app.queue.registry import JobContext

FASTA = (
    ">sp|P0DTC2|SPIKE_SARS2 Spike glycoprotein OS=SARS-CoV-2 OX=2697049\n"
    "MFVFLVLLPLVSSQCVNLTTRTQLPPAYTNSFTRGVYYPDKVFRSSVLHS\n"
    ">sp|P00533|EGFR_HUMAN Epidermal growth factor receptor OS=Homo sapiens\n"
    "MRPSGTAGAALLALLAALCPASRALEEKKVCQGTSNKLTQLGTFEDHFLS\n"
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """A JobContext whose scratch directory is a tmp_path."""
    monkeypatch.setattr(
        uniprot_handlers, "_prepare_workdir", lambda ctx, kind: tmp_path
    )
    return JobContext(
        job_id="job-1",
        payload={
            "project_id": "507f1f77bcf86cd799439011",
            "query": "accession:P0DTC2 OR accession:P00533",
            "filename": "uniprot_2_proteins.fasta",
            "accessions": ["P0DTC2", "P00533"],
            "reviewed_only": True,
        },
        epoch=1,
        attempts=1,
        cancel_event=threading.Event(),
    )


@pytest.fixture
def stub_fetch(monkeypatch):
    """Replace the network with a gzipped FASTA body and a release header."""
    state = {"body": gzip.compress(FASTA.encode()), "release": "2026_02"}

    def fake_fetch(url, *, timeout=0.0):
        return state["body"], {"X-UniProt-Release": state["release"]}

    monkeypatch.setattr(uniprot_handlers, "_fetch", fake_fetch)
    return state


class TestDownload:
    def test_it_writes_the_fasta_and_reports_it(self, ctx, stub_fetch, tmp_path):
        result = uniprot_handlers.download_uniprot(ctx)

        staged = result["staged"]
        assert len(staged) == 1
        assert staged[0]["name"] == "uniprot_2_proteins.fasta"
        written = Path(staged[0]["path"]).read_text()
        assert written.startswith(">sp|P0DTC2|")
        assert written.count(">") == 2

    def test_it_counts_the_records_it_actually_got(self, ctx, stub_fetch):
        """Counted from the file rather than trusted from the request,
        because `X-Total-Results` and the delivered count differ slightly --
        human reviewed reported 20,416 and delivered 20,427."""
        result = uniprot_handlers.download_uniprot(ctx)
        assert result["protein_count"] == 2

    def test_it_records_the_uniprot_release(self, ctx, stub_fetch):
        """Real provenance about specific bytes: which release these
        sequences came from."""
        result = uniprot_handlers.download_uniprot(ctx)
        assert result["release"] == "2026_02"

    def test_a_missing_release_header_is_not_fatal(self, ctx, stub_fetch):
        def no_header(url, *, timeout=0.0):
            return gzip.compress(FASTA.encode()), {}

        import app.queue.uniprot_handlers as mod

        mod._fetch = no_header
        result = uniprot_handlers.download_uniprot(ctx)
        assert result["release"] is None

    def test_an_empty_response_is_retryable(self, ctx, stub_fetch):
        """Zero records where records were requested. Better caught here
        than as an ingest of nothing several steps later."""
        stub_fetch["body"] = gzip.compress(b"")

        with pytest.raises(RetryableError, match="no sequences"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_response_that_is_not_fasta_is_retryable(self, ctx, stub_fetch):
        """UniProt returns an HTML error page under load. Ingesting it would
        create an object that looks like a FASTA and is not."""
        stub_fetch["body"] = gzip.compress(b"<html><body>Service busy</body></html>")

        with pytest.raises(RetryableError, match="no sequences"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_missing_query_is_permanent(self, ctx):
        """A retry cannot fix a payload with no query in it."""
        ctx.payload.pop("query")

        with pytest.raises(PermanentError, match="query"):
            uniprot_handlers.download_uniprot(ctx)

    def test_a_missing_project_is_permanent(self, ctx):
        ctx.payload.pop("project_id")

        with pytest.raises(PermanentError, match="project_id"):
            uniprot_handlers.download_uniprot(ctx)

    def test_cancellation_is_observed(self, ctx, stub_fetch):
        from app.errors import JobCancelled

        ctx.cancel_event.set()

        with pytest.raises(JobCancelled):
            uniprot_handlers.download_uniprot(ctx)


class TestUncompressed:
    def test_a_plain_body_is_handled(self, ctx, monkeypatch):
        """`compressed=true` is a request, not a guarantee; a proxy may hand
        back plain text."""
        monkeypatch.setattr(
            uniprot_handlers,
            "_fetch",
            lambda url, *, timeout=0.0: (FASTA.encode(), {}),
        )

        result = uniprot_handlers.download_uniprot(ctx)

        assert result["protein_count"] == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./scripts/wt-pytest.sh tests/queue/test_uniprot_download.py -q
```
Expected: FAIL, `ImportError: cannot import name 'uniprot_handlers'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/queue/uniprot_handlers.py`:

```python
"""Downloading protein sequences from UniProt.

Sibling to `assembly_handlers`, and deliberately much smaller. That module
is built around shelling out to a binary and guarding a multi-gigabyte
transfer; none of that applies here, so this has no subprocess mode, no
lease extension, no disk pre-flight, no extraction factor, and no archive
handling. A yeast proteome is 3.9 MB and human reviewed is 13.7 MB.

THREAD rather than ASYNC: the body is a blocking urllib call, and the
executor is responsible for keeping blocking work off the event loop. An
ASYNC handler doing this would stall the heartbeat and expire its own lease.

Imported by `handlers.py` for the `@handler` registration side effects.
"""

import gzip
import urllib.request
from pathlib import Path

from app.errors import PermanentError, RetryableError
from app.logging import get_logger
from app.models import IoClass, JobClass, JobResources
from app.queue.pipeline_handlers import _prepare_workdir
from app.queue.registry import HandlerMode, JobContext, handler

log = get_logger(__name__)

# Generous: a large proteome stream is a single request that can take a
# while to start, but nothing here runs for minutes the way an assembly
# transfer does.
_TIMEOUT_SECONDS = 300.0


def _fetch(url: str, *, timeout: float = _TIMEOUT_SECONDS) -> tuple[bytes, dict]:
    """The transport, isolated so tests can replace it without a network."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read(), dict(response.headers)


@handler(
    "download_uniprot",
    # THREAD, not SUBPROCESS: there is no binary. Not ASYNC either, because
    # the urllib call blocks.
    mode=HandlerMode.THREAD,
    # USER_INTERACTIVE for the same reason as the other downloads: someone
    # clicked and is watching for the file, and the work waits on UniProt
    # rather than competing with alignments for CPU.
    job_class=JobClass.USER_INTERACTIVE,
    resources=JobResources(cpu=1, mem_mb=256, io=IoClass.LIGHT),
    # Matches the other downloads: a failure is usually the network, and the
    # third attempt genuinely succeeds often enough to be worth it.
    max_attempts=3,
)
def download_uniprot(ctx: JobContext) -> dict:
    """Fetch a FASTA of proteins. The ingest happens in the applier.

    Synchronous: THREAD runs this off the event loop, so the body must not
    await and cannot touch the database. It stages one file under tmp/ and
    returns a description for `_apply_uniprot_download` to persist.

    Idempotent by construction -- each attempt gets a fresh scratch directory
    and rewrites the file whole, so a retry after a partial transfer starts
    clean rather than appending to a truncated FASTA.
    """
    from app.metadata import uniprot

    query = (ctx.payload.get("query") or "").strip()
    if not query:
        raise PermanentError("download_uniprot requires a 'query'")

    project_id = ctx.payload.get("project_id")
    if not project_id:
        raise PermanentError("download_uniprot requires a 'project_id'")

    filename = ctx.payload.get("filename") or "uniprot.fasta"

    work = _prepare_workdir(ctx, kind="uniprot_download")

    ctx.check_cancel()

    ctx.progress(phase="downloading", pct=0.1, message="fetching from UniProt")
    try:
        body, headers = _fetch(uniprot.stream_url(query))
    except Exception as exc:
        # Network failures are the common case and retrying genuinely helps.
        raise RetryableError(f"UniProt request failed: {exc}") from exc

    ctx.check_cancel()

    ctx.progress(phase="writing", pct=0.8, message="writing sequences")
    text = _decode(body)

    # Counted from what arrived rather than trusted from the request.
    # `X-Total-Results` and the delivered record count differ slightly --
    # human reviewed reported 20,416 and delivered 20,427 -- so this is the
    # only honest number, and it also catches an HTML error page, which has
    # no '>' lines at all.
    protein_count = sum(1 for line in text.splitlines() if line.startswith(">"))
    if protein_count == 0:
        raise RetryableError(
            "UniProt returned no sequences for this request. The service may "
            "be busy; this will be retried."
        )

    target = work / filename
    target.write_text(text)

    release = headers.get("X-UniProt-Release")

    ctx.progress(phase="done", pct=1.0, message=f"downloaded {protein_count} proteins")
    log.info(
        "uniprot_download_finished",
        job_id=ctx.job_id,
        query=query,
        proteins=protein_count,
        release=release,
    )

    return {
        "staged": [{"path": str(target.resolve()), "name": filename}],
        "protein_count": protein_count,
        "release": release,
        "query": query,
        "proteome_id": ctx.payload.get("proteome_id"),
        "accessions": ctx.payload.get("accessions") or [],
        "reviewed_only": bool(ctx.payload.get("reviewed_only")),
        "organism": ctx.payload.get("organism"),
        "project_id": project_id,
        "job_id": ctx.job_id,
        "staging_dir": str(work),
    }


def _decode(body: bytes) -> str:
    """The response text, gzipped or not.

    `compressed=true` is a request rather than a guarantee -- a proxy may
    decompress in transit -- so both forms are handled instead of assuming.
    """
    try:
        return gzip.decompress(body).decode("utf-8", "replace")
    except (OSError, EOFError):
        return body.decode("utf-8", "replace")
```

- [ ] **Step 4: Register the module for its side effects**

The handler-module imports live at the **bottom** of
`backend/app/queue/handlers.py` (around line 729), not the top -- deliberately,
since `registry.load_handlers()` imports only that one module. Add
`uniprot_handlers` to that block, keeping it alphabetical:

```python
# Pipeline handlers live in their own modules -- they shell out to external
# tools and carry a different failure model -- but must be imported here, since
# registry.load_handlers() imports only this one.
from app.queue import (  # noqa: E402, F401
    align_handlers,
    assembly_handlers,
    pipeline_handlers,
    sra_handlers,
    summary_handlers,
    uniprot_handlers,
    variant_handlers,
)
```

Do not move this to the top of the file -- the `# noqa: E402` is there because
the placement is intentional.

- [ ] **Step 5: Run the test to verify it passes**

Run:
```bash
./scripts/wt-pytest.sh tests/queue/test_uniprot_download.py -q
```
Expected: PASS, 10 passed. (The test file above has 10 test methods.)

- [ ] **Step 6: Verify the handler registered**

Run:
```bash
docker compose restart worker && sleep 5 && docker compose logs worker --tail 20 | grep handlers_loaded
```
Expected: the `handlers_loaded` line includes `download_uniprot`.

If it does not, the import in Step 4 is in the wrong place. `worker` does not
hot-reload, so the restart is required, not optional.

- [ ] **Step 7: Commit**

```bash
git add backend/app/queue/uniprot_handlers.py backend/app/queue/handlers.py backend/tests/queue/test_uniprot_download.py
git commit -m "feat: UniProt download handler"
```

---

### Task 7: The applier

**Files:**
- Modify: `backend/app/queue/results.py` (add function, register at line ~1263)
- Test: `backend/tests/queue/test_uniprot_apply.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/queue/test_uniprot_apply.py`:

```python
"""Taking a finished UniProt download into the project.

The assertion that matters is the role. A protein FASTA and a reference
genome are both `FormatKind.FASTA`, and only `ObjectRole.PROTEIN` keeps this
file out of the aligner's reference picker -- where selecting it would
produce silently wrong alignments rather than an error.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from app.models import ObjectRole
from app.queue import results

PROJECT_ID = "507f1f77bcf86cd799439011"
JOB_ID = "507f1f77bcf86cd799439012"


@pytest.fixture
def staged_file(tmp_path: Path) -> Path:
    path = tmp_path / "UP000002311_reviewed.fasta"
    path.write_text(">sp|P0DTC2|SPIKE_SARS2 Spike\nMFVFLV\n")
    return path


def _result(staged_file: Path) -> dict:
    return {
        "staged": [{"path": str(staged_file), "name": staged_file.name}],
        "protein_count": 6067,
        "release": "2026_02",
        "query": "proteome:UP000002311 AND reviewed:true",
        "proteome_id": "UP000002311",
        "accessions": [],
        "reviewed_only": True,
        "organism": "Saccharomyces cerevisiae",
        "project_id": PROJECT_ID,
        "job_id": JOB_ID,
    }


@pytest.mark.asyncio
class TestApply:
    async def test_it_ingests_as_a_protein(self, staged_file, monkeypatch):
        ingest = AsyncMock()
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file", ingest
        )
        monkeypatch.setattr(
            "app.services.run_service.run_for_job", AsyncMock(return_value=None)
        )

        await results._apply_uniprot_download(_result(staged_file))

        assert ingest.await_count == 1
        assert ingest.await_args.kwargs["role"] is ObjectRole.PROTEIN

    async def test_provenance_lands_in_facts(self, staged_file, monkeypatch):
        """The query, the release, and whether unreviewed entries were
        included -- what would otherwise be unrecoverable once the file is
        just a FASTA in a project."""
        ingest = AsyncMock()
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file", ingest
        )
        monkeypatch.setattr(
            "app.services.run_service.run_for_job", AsyncMock(return_value=None)
        )

        await results._apply_uniprot_download(_result(staged_file))

        facts = ingest.await_args.kwargs["facts"]
        assert facts["uniprot_query"] == "proteome:UP000002311 AND reviewed:true"
        assert facts["uniprot_release"] == "2026_02"
        assert facts["uniprot_proteome"] == "UP000002311"
        assert facts["uniprot_reviewed_only"] is True
        assert facts["uniprot_protein_count"] == 6067

    async def test_a_result_with_nothing_staged_is_a_no_op(self, monkeypatch):
        ingest = AsyncMock()
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file", ingest
        )

        await results._apply_uniprot_download(
            {"staged": [], "project_id": PROJECT_ID}
        )

        assert ingest.await_count == 0

    async def test_an_ingest_failure_does_not_raise(self, staged_file, monkeypatch):
        """The transfer already succeeded; losing the job over a write-back
        failure would discard the expensive part."""
        monkeypatch.setattr(
            "app.services.object_service.ingest_local_file",
            AsyncMock(side_effect=RuntimeError("disk gone")),
        )

        await results._apply_uniprot_download(_result(staged_file))


class TestRegistration:
    def test_the_applier_is_wired_to_the_handler(self):
        """A handler with no applier downloads a file and silently drops it."""
        assert results._APPLIERS["download_uniprot"] is results._apply_uniprot_download
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./scripts/wt-pytest.sh tests/queue/test_uniprot_apply.py -q
```
Expected: FAIL, `AttributeError: module 'app.queue.results' has no attribute '_apply_uniprot_download'`.

- [ ] **Step 3: Write the implementation**

In `backend/app/queue/results.py`, add this function immediately after
`_apply_assembly_download` (which ends around line 640):

```python
async def _apply_uniprot_download(result: dict) -> None:
    """Take a finished UniProt download into the project.

    Mirrors `_apply_assembly_download`: the handler ran in a worker thread
    and could not touch the database, so the ingest happens here.

    Simpler than the assembly case in one way -- there is exactly one file,
    not up to four -- and the loop is kept anyway so a future multi-file
    shape does not have to reintroduce it.

    No QC and no mate linking: protein sequences have no reads to QC and no
    pair.
    """
    from app.services import object_service, run_service

    staged = result.get("staged") or []
    project_id = result.get("project_id")
    if not staged or not project_id:
        return

    project_id = PydanticObjectId(project_id)
    job_id = result.get("job_id")

    # Provenance, distinct from the biology: which query produced these bytes
    # and which UniProt release they came from. The release is recorded here,
    # per-download, rather than on the `DataSource` entry -- what is true of a
    # source and what is true of one download from it are different claims,
    # and `sources.py` argues at length that a source has no version.
    facts = {
        "uniprot_query": result.get("query"),
        "uniprot_release": result.get("release"),
        "uniprot_proteome": result.get("proteome_id"),
        "uniprot_reviewed_only": bool(result.get("reviewed_only")),
        "uniprot_protein_count": result.get("protein_count"),
        "uniprot_download_source": "uniprot",
    }
    metadata = {}
    if result.get("organism"):
        metadata["organism"] = result["organism"]

    created = []
    for entry in staged:
        try:
            obj = await object_service.ingest_local_file(
                project_id=project_id,
                path=Path(entry["path"]),
                name=entry["name"],
                # The role that keeps a protein FASTA out of the aligner's
                # reference picker. Both are FormatKind.FASTA; only this
                # tells them apart.
                role=ObjectRole.PROTEIN,
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts={k: v for k, v in facts.items() if v is not None},
                metadata=metadata,
            )
        except Exception as e:  # noqa: BLE001 - the transfer already succeeded
            log.error(
                "uniprot_ingest_failed",
                query=result.get("query"),
                name=entry.get("name"),
                error=str(e),
            )
            continue
        created.append(obj)

    if not created:
        log.error("uniprot_download_ingested_nothing", query=result.get("query"))
        return

    # Two steps, matching `_apply_assembly_download`: a job does not know its
    # run, so the run is looked up first. There is no single "attach outputs
    # to this job" call.
    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [o.id for o in created])

    log.info(
        "uniprot_download_applied",
        query=result.get("query"),
        objects=[str(o.id) for o in created],
        proteins=result.get("protein_count"),
    )
```

Then register it in `_APPLIERS` (around line 1263):

```python
    "download_assembly": _apply_assembly_download,
    "download_uniprot": _apply_uniprot_download,
```

`Path`, `PydanticObjectId`, `ObjectRole`, and `log` are already imported at the
top of `results.py` -- no new imports are needed.

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
./scripts/wt-pytest.sh tests/queue/test_uniprot_apply.py -q
```
Expected: PASS, 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/queue/results.py backend/tests/queue/test_uniprot_apply.py
git commit -m "feat: ingest a UniProt download as a protein object"
```

---

### Task 8: The API endpoints

**Files:**
- Create: `backend/app/api/v1/uniprot.py`
- Modify: `backend/app/api/v1/__init__.py:8,31`
- Test: `backend/tests/api/test_uniprot_resolve.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/api/test_uniprot_resolve.py`:

```python
"""The resolve endpoint's dispatch.

One field, four input classes. The test is that each input reaches the right
branch -- a gene symbol must not be sent as an accession, and a species-level
taxon must produce a picker rather than "nothing found".
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import uniprot as uniprot_router
from app.errors import register_exception_handlers
from app.metadata import uniprot as uniprot_meta


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(uniprot_router.router)
    return TestClient(app)


@pytest.fixture
def stub(monkeypatch):
    """Stub every outward call the router can make."""
    calls = {"taxon": [], "name": [], "proteome": [], "proteins": [], "counts": []}

    def fake_resolve_taxon(taxon_id):
        calls["taxon"].append(taxon_id)
        return uniprot_meta.TaxonResolution(
            proteome=uniprot_meta.ProteomeInfo(
                id="UP000002311",
                name="Saccharomyces cerevisiae",
                taxon_id=559292,
                strain="S288c",
                protein_count=6067,
                is_reference=True,
                busco_score=99,
                genome_assembly="GCA_000146045.2",
            ),
            candidates=[],
            needs_picker=False,
        )

    def fake_resolve_organism_name(name):
        calls["name"].append(name)
        return uniprot_meta.TaxonResolution()

    def fake_resolve_proteome(pid):
        calls["proteome"].append(pid)
        return uniprot_meta.ProteomeInfo(
            id=pid,
            name="Saccharomyces cerevisiae",
            taxon_id=559292,
            strain=None,
            protein_count=6067,
            is_reference=True,
            busco_score=99,
            genome_assembly="GCA_000146045.2",
        )

    def fake_search_proteins(query):
        calls["proteins"].append(query)
        return [
            uniprot_meta.ProteinHit(
                accession="P0DTC2",
                entry_id="SPIKE_SARS2",
                name="Spike glycoprotein",
                organism="SARS-CoV-2",
                length=1273,
                reviewed=True,
            )
        ]

    def fake_count(query, **kwargs):
        calls["counts"].append(query)
        return 20416 if "reviewed" in query else 147506

    monkeypatch.setattr(uniprot_meta, "resolve_taxon", fake_resolve_taxon)
    monkeypatch.setattr(uniprot_meta, "resolve_organism_name", fake_resolve_organism_name)
    monkeypatch.setattr(uniprot_meta, "resolve_proteome", fake_resolve_proteome)
    monkeypatch.setattr(uniprot_meta, "search_proteins", fake_search_proteins)
    monkeypatch.setattr(uniprot_meta, "count_results", fake_count)
    return calls


class TestDispatch:
    def test_a_proteome_id_returns_a_proteome(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "UP000002311"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "proteome"
        assert body["proteome"]["id"] == "UP000002311"
        assert stub["proteome"] == ["UP000002311"]

    def test_a_taxon_returns_a_proteome(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "559292"})
        assert resp.status_code == 200
        assert resp.json()["kind"] == "proteome"
        assert stub["taxon"] == [559292]

    def test_free_text_returns_proteins(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "spike glycoprotein"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "proteins"
        assert body["proteins"][0]["accession"] == "P0DTC2"

    def test_a_gene_symbol_reaches_the_protein_search(self, client, stub):
        """EGFR is not an accession. Sending it as one returns nothing."""
        resp = client.post("/uniprot/resolve", json={"query": "EGFR"})
        assert resp.json()["kind"] == "proteins"
        assert stub["proteins"], "should have run a protein search"

    def test_accessions_return_those_proteins(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "P0DTC2"})
        body = resp.json()
        assert body["kind"] == "proteins"
        assert "accession:P0DTC2" in stub["proteins"][0]

    def test_a_proteome_carries_both_counts(self, client, stub):
        """The ~7x reviewed/unreviewed split, shown at the moment of choice
        rather than discovered after downloading 147,506 entries."""
        resp = client.post("/uniprot/resolve", json={"query": "UP000002311"})
        body = resp.json()
        assert body["proteome"]["reviewed_count"] == 20416
        assert body["proteome"]["total_count"] == 147506

    def test_a_proteome_carries_its_genome_assembly(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "UP000002311"})
        assert resp.json()["proteome"]["genome_assembly"] == "GCA_000146045.2"

    def test_an_empty_query_is_rejected(self, client, stub):
        resp = client.post("/uniprot/resolve", json={"query": "   "})
        assert resp.status_code == 422
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./scripts/wt-pytest.sh tests/api/test_uniprot_resolve.py -q
```
Expected: FAIL, `ImportError: cannot import name 'uniprot'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/api/v1/uniprot.py`:

```python
"""UniProt endpoints: resolving what the user typed, and downloading it.

Request and response models live here rather than in `schemas.py`, matching
`ncbi.py` and `pipelines.py`. `schemas.py` holds what several routers share;
nothing else consumes these.

Separate from `ncbi.py` deliberately. Folding UniProt into that router's one
accession box was possible -- the namespaces do not collide -- but its
question, "is this SRA or an assembly?", is coherent because it is about one
provider. Adding "or is it UniProt?" makes one field the door to everything.
"""

import asyncio

from beanie import PydanticObjectId
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.errors import ValidationError
from app.logging import get_logger
from app.metadata import uniprot
from app.services import uniprot_service

log = get_logger(__name__)

router = APIRouter(prefix="/uniprot", tags=["uniprot"])


class ResolveRequest(BaseModel):
    query: str
    project_id: PydanticObjectId | None = None


class ProteomeOut(BaseModel):
    id: str
    name: str
    taxon_id: int | None = None
    strain: str | None = None
    protein_count: int | None = None
    is_reference: bool = False
    busco_score: int | None = None
    # The NCBI assembly this proteome's genome came from. Rendered as a link
    # to the other dialog rather than a combined download.
    genome_assembly: str | None = None
    # Both counts, so the reviewed/unreviewed difference is visible before
    # the download rather than after it. Roughly sevenfold for human.
    reviewed_count: int | None = None
    total_count: int | None = None


class ProteinOut(BaseModel):
    accession: str
    entry_id: str | None = None
    name: str | None = None
    organism: str | None = None
    length: int | None = None
    reviewed: bool = False


class ResolveResponse(BaseModel):
    # "proteome" | "proteins" | "empty"
    kind: str
    proteome: ProteomeOut | None = None
    # Other proteomes for the same organism. Populated for both branches: on
    # a reference hit these sit behind a disclosure, and on a species-level
    # taxon with no reference proteome they are the whole answer.
    candidates: list[ProteomeOut] = Field(default_factory=list)
    needs_picker: bool = False
    proteins: list[ProteinOut] = Field(default_factory=list)
    message: str | None = None


class DownloadRequest(BaseModel):
    project_id: PydanticObjectId
    proteome_id: str | None = None
    accessions: list[str] = Field(default_factory=list)
    reviewed_only: bool = True
    organism: str | None = None
    protein_count: int | None = None


class DownloadAccepted(BaseModel):
    run_id: str
    job_ids: list[str]


def _proteome_out(info: uniprot.ProteomeInfo) -> ProteomeOut:
    return ProteomeOut(
        id=info.id,
        name=info.name,
        taxon_id=info.taxon_id,
        strain=info.strain,
        protein_count=info.protein_count,
        is_reference=info.is_reference,
        busco_score=info.busco_score,
        genome_assembly=info.genome_assembly,
    )


def _protein_out(hit: uniprot.ProteinHit) -> ProteinOut:
    return ProteinOut(
        accession=hit.accession,
        entry_id=hit.entry_id,
        name=hit.name,
        organism=hit.organism,
        length=hit.length,
        reviewed=hit.reviewed,
    )


async def _with_counts(info: uniprot.ProteomeInfo) -> ProteomeOut:
    """A proteome plus both protein counts.

    Two extra requests, run concurrently. Worth it: the reviewed and
    unreviewed sets differ roughly sevenfold for human, and a user who cannot
    see that before clicking discovers it as a 147,506-entry file.
    """
    out = _proteome_out(info)
    reviewed_query = uniprot.download_query(
        proteome_id=info.id, accessions=[], reviewed_only=True
    )
    total_query = uniprot.download_query(
        proteome_id=info.id, accessions=[], reviewed_only=False
    )
    reviewed, total = await asyncio.gather(
        asyncio.to_thread(uniprot.count_results, reviewed_query),
        asyncio.to_thread(uniprot.count_results, total_query),
    )
    out.reviewed_count = reviewed
    out.total_count = total
    return out


@router.post("/resolve", response_model=ResolveResponse)
async def resolve(body: ResolveRequest) -> ResolveResponse:
    """What does this input name, and what can be downloaded for it?

    Every UniProt call runs in a worker thread: they are blocking urllib
    requests, and one on the event loop stalls every other request.
    """
    raw = (body.query or "").strip()
    if not raw:
        raise ValidationError("Enter a proteome, an accession, or a protein name.")

    kind = uniprot.classify(raw)

    if kind is uniprot.InputKind.PROTEOME:
        info = await asyncio.to_thread(uniprot.resolve_proteome, raw.upper())
        if info is None:
            return ResolveResponse(
                kind="empty", message=f"UniProt has no proteome {raw.upper()}."
            )
        return ResolveResponse(kind="proteome", proteome=await _with_counts(info))

    if kind is uniprot.InputKind.ACCESSIONS:
        accessions = uniprot.parse_accessions(raw)
        query = " OR ".join(f"accession:{a}" for a in accessions)
        hits = await asyncio.to_thread(uniprot.search_proteins, query)
        if not hits:
            return ResolveResponse(
                kind="empty", message="UniProt returned nothing for those accessions."
            )
        return ResolveResponse(
            kind="proteins", proteins=[_protein_out(h) for h in hits]
        )

    if kind is uniprot.InputKind.TAXON:
        resolution = await asyncio.to_thread(uniprot.resolve_taxon, int(raw))
        return await _taxon_response(resolution, raw)

    # TEXT: an organism name and a protein name are indistinguishable by
    # shape, so ask. The proteome search runs first and the protein search is
    # the fallback, which degrades toward the more general answer.
    resolution = await asyncio.to_thread(uniprot.resolve_organism_name, raw)
    if resolution.proteome is not None or resolution.candidates:
        return await _taxon_response(resolution, raw)

    hits = await asyncio.to_thread(uniprot.search_proteins, raw)
    if not hits:
        return ResolveResponse(
            kind="empty", message=f"UniProt returned nothing for {raw!r}."
        )
    return ResolveResponse(kind="proteins", proteins=[_protein_out(h) for h in hits])


async def _taxon_response(
    resolution: uniprot.TaxonResolution, raw: str
) -> ResolveResponse:
    """A resolved organism, as a card or a picker."""
    if resolution.proteome is not None:
        return ResolveResponse(
            kind="proteome",
            proteome=await _with_counts(resolution.proteome),
            candidates=[_proteome_out(c) for c in resolution.candidates],
            needs_picker=False,
        )

    if resolution.candidates:
        # No reference proteome, but proteomes exist. This is taxon 4932 --
        # yeast at species level -- where UniProt attaches the reference to
        # strain taxon 559292. Reporting "nothing found" here would be wrong
        # for 360 proteomes.
        return ResolveResponse(
            kind="proteome",
            candidates=[_proteome_out(c) for c in resolution.candidates],
            needs_picker=True,
            message=(
                "No reference proteome for this organism — choose one of its "
                f"{len(resolution.candidates)} proteomes."
            ),
        )

    return ResolveResponse(
        kind="empty", message=f"UniProt has no proteome for {raw!r}."
    )


@router.post(
    "/download",
    response_model=DownloadAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def download(body: DownloadRequest) -> DownloadAccepted:
    """Queue a proteome or a set of proteins for download."""
    run, job_ids = await uniprot_service.launch_download(
        project_id=body.project_id,
        proteome_id=body.proteome_id,
        accessions=body.accessions,
        reviewed_only=body.reviewed_only,
        organism=body.organism,
        protein_count=body.protein_count,
    )
    return DownloadAccepted(run_id=str(run.id), job_ids=job_ids)
```

- [ ] **Step 4: Register the router**

In `backend/app/api/v1/__init__.py`, add `uniprot` to the import list (line 8
area) and include it (line 31 area):

```python
from app.api.v1 import (
    ncbi,
    uniprot,
)
```

```python
api_router.include_router(uniprot.router)
```

Match the existing formatting exactly -- if the imports are one-per-line
alphabetical, keep them that way.

- [ ] **Step 5: Run the test to verify it passes**

Run:
```bash
./scripts/wt-pytest.sh tests/api/test_uniprot_resolve.py -q
```
Expected: PASS, 8 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/uniprot.py backend/app/api/v1/__init__.py backend/tests/api/test_uniprot_resolve.py
git commit -m "feat: UniProt resolve and download endpoints"
```

---

### Task 9: The Sources help page entry

**Files:**
- Modify: `backend/app/pipelines/sources.py:49`

`sources.py` has a completeness test, so this is not optional. Per CLAUDE.md,
verify the licence and citation against UniProt's own site rather than
recalling them -- a wrong licence claim on a page that reads as authoritative
is worse than a blank field.

- [ ] **Step 1: Verify the licence and citation**

`uniprot.org/help/license` is a JavaScript-rendered single-page app: fetching
it returns the loader shell, not the licence text. Use the machine-readable
sources instead, which are also more authoritative -- they are what ships
with the data.

Run:
```bash
./scripts/wt-pytest.sh --version >/dev/null 2>&1; python3 - <<'PY'
import urllib.request

def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace"), dict(r.headers)

# 1. The licence, from the release README and from an entry record.
readme, _ = get("https://ftp.uniprot.org/pub/databases/uniprot/current_release/"
                "knowledgebase/complete/README")
i = readme.lower().find("creative commons")
print("README licence:", " ".join(readme[i-120:i+200].split()))

entry, hdrs = get("https://rest.uniprot.org/uniprotkb/P0DTC2.txt")
for line in entry.splitlines():
    if "Creative Commons" in line or "Copyrighted by" in line:
        print("entry:", line)

# 2. The current release, for the per-download `facts` value.
print("release:", hdrs.get("X-UniProt-Release"))
PY
```

Verified on 2026-07-31, and reproduced from two independent sources:

- **Licence: CC BY 4.0.** The release README states "We have chosen to apply
  the Creative Commons Attribution 4.0 International (CC BY 4.0) License ...
  to all copyrightable parts of our databases. (c) 2002-2024 UniProt
  Consortium", and every entry record carries "Distributed under the Creative
  Commons Attribution (CC BY 4.0) License".
- **`terms` URL: `https://www.uniprot.org/terms`** -- the URL the entry
  records themselves point at, rather than the help page.
- **Citation: "UniProt: the Universal Protein Knowledgebase in 2025",
  DOI `10.1093/nar/gkae1010`.** Confirmed via Europe PMC as the current
  database-issue paper. (There is also a 2025 API-specific paper,
  `10.1093/nar/gkaf394`; the database-issue paper is the right citation for
  the resource itself.)

If the command above reports something different from this, the command wins
-- these values were true on 2026-07-31 and UniProt publishes a new database
issue roughly annually.

- [ ] **Step 2: Add the entry**

In `backend/app/pipelines/sources.py`, add to the `DATA_SOURCES` tuple after
the NCBI entries:

```python
    DataSource(
        name="UniProt",
        kind="api",
        summary=(
            "The UniProt Knowledgebase: curated protein sequences and their "
            "annotation. Entries are either reviewed (Swiss-Prot, manually "
            "curated) or unreviewed (TrEMBL, automatically annotated), and "
            "proteomes group an organism's entries into a downloadable set. "
            "The proteome record also names the genome assembly its "
            "sequences were derived from."
        ),
        usage=(
            "Resolves whatever is typed into the UniProt download box — a "
            "proteome identifier, one or more accessions, a taxon, an "
            "organism name, or a protein name — and offers what it finds as "
            "a downloadable FASTA, stored in the project as protein "
            "sequences. Both the reviewed-only and the complete protein "
            "counts are shown before downloading, because for some organisms "
            "they differ several-fold. Separately, the variants table asks "
            "UniProt which protein a gene symbol names in order to offer a "
            "structure view; that lookup is best-effort and a failure hides "
            "the button rather than failing the table."
        ),
        homepage="https://www.uniprot.org/",
        docs="https://www.uniprot.org/help/api",
        # Verified against uniprot.org/help/publications in Step 1 -- replace
        # with whatever that page currently asks for rather than this note.
        # Verified in Step 1 against the release README, an entry record, and
        # Europe PMC -- not recalled. CC BY 4.0; `terms` is the URL UniProt's
        # own records cite, rather than the JS-rendered help page.
        citation="The UniProt Consortium, Nucleic Acids Research 2025",
        citation_url="https://doi.org/10.1093/nar/gkae1010",
        terms="https://www.uniprot.org/terms",
    ),
```

If Step 1's output disagrees with these values, Step 1 wins -- UniProt
publishes a new database-issue paper roughly annually.

- [ ] **Step 3: Run the completeness test**

Run:
```bash
./scripts/wt-pytest.sh tests/api/test_sources_api.py tests/pipelines -q -k source
```
Expected: PASS. If a completeness test names a required field that is empty,
fill it -- that is the test doing its job.

- [ ] **Step 4: Commit**

```bash
git add backend/app/pipelines/sources.py
git commit -m "docs: add UniProt to the data source catalog"
```

---

### Task 10: Frontend types and client

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts:412`

- [ ] **Step 1: Add the types**

Append to `frontend/src/api/types.ts`:

```typescript
export type UniProtProteome = {
  id: string;
  name: string;
  taxon_id: number | null;
  strain: string | null;
  protein_count: number | null;
  is_reference: boolean;
  busco_score: number | null;
  /** The NCBI assembly this proteome's genome came from, when UniProt names one. */
  genome_assembly: string | null;
  /** Both counts, so the reviewed/unreviewed difference is visible before downloading. */
  reviewed_count: number | null;
  total_count: number | null;
};

export type UniProtProtein = {
  accession: string;
  entry_id: string | null;
  name: string | null;
  organism: string | null;
  length: number | null;
  reviewed: boolean;
};

export type UniProtResolveResponse = {
  kind: "proteome" | "proteins" | "empty";
  proteome: UniProtProteome | null;
  candidates: UniProtProteome[];
  needs_picker: boolean;
  proteins: UniProtProtein[];
  message: string | null;
};

export type UniProtAccepted = {
  run_id: string;
  job_ids: string[];
};
```

- [ ] **Step 2: Add the client methods**

In `frontend/src/api/client.ts`, after `ncbiDownloadAssembly` (around line
430), add:

```typescript
  uniprotResolve: (body: { query: string; project_id?: string | null }) =>
    request<UniProtResolveResponse>("/uniprot/resolve", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  uniprotDownload: (body: {
    project_id: string;
    proteome_id?: string | null;
    accessions?: string[];
    reviewed_only: boolean;
    organism?: string | null;
    protein_count?: number | null;
  }) =>
    request<UniProtAccepted>("/uniprot/download", {
      method: "POST",
      body: JSON.stringify(body),
    }),
```

Add `UniProtResolveResponse` and `UniProtAccepted` to the existing type import
at the top of `client.ts`.

- [ ] **Step 3: Verify it compiles**

Run:
```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat: UniProt API client and types"
```

---

### Task 11: The dialog

**Files:**
- Create: `frontend/src/components/UniProtDownloadDialog.tsx`
- Modify: `frontend/src/components/ProjectExplorer.tsx:348-366`

- [ ] **Step 1: Write the component**

Create `frontend/src/components/UniProtDownloadDialog.tsx`:

```tsx
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";
import type {
  UniProtProteome,
  UniProtProtein,
  UniProtResolveResponse,
} from "../api/types";

/**
 * Find protein sequences at UniProt and download them into a project.
 *
 * Its own dialog rather than a branch inside `NcbiDownloadDialog`. The
 * accession namespaces do not collide, so merging was possible -- but that
 * component already carries two result shapes in 762 lines, and its
 * resolver's question ("is this SRA or an assembly?") is coherent because it
 * is about one provider. This copies the style, not the component.
 *
 * One field, four input classes, decided server-side: a proteome id, one or
 * more accessions, a taxon or organism, or free text. The body switches
 * between a proteome card and a protein picker on what comes back.
 */
export function UniProtDownloadDialog({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();

  const [query, setQuery] = useState("");
  const [resolved, setResolved] = useState<UniProtResolveResponse | null>(null);
  const [reviewedOnly, setReviewedOnly] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [chosenProteome, setChosenProteome] = useState<UniProtProteome | null>(
    null,
  );
  const [showOthers, setShowOthers] = useState(false);

  const resolve = useMutation({
    mutationFn: () => api.uniprotResolve({ query: query.trim(), project_id: projectId }),
    onSuccess: (data) => {
      setResolved(data);
      setChosenProteome(data.proteome);
      setShowOthers(data.needs_picker);
      // Everything found, pre-selected. The common case for a pasted set of
      // accessions is "give me these"; a free-text search is the case where
      // choosing matters, and there the count in the button shows the scale.
      setSelected(new Set(data.proteins.map((p) => p.accession)));
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const download = useMutation({
    mutationFn: () =>
      api.uniprotDownload({
        project_id: projectId,
        proteome_id: chosenProteome?.id ?? null,
        accessions: chosenProteome ? [] : [...selected],
        reviewed_only: reviewedOnly,
        organism: chosenProteome?.name ?? null,
        protein_count: chosenProteome
          ? reviewedOnly
            ? chosenProteome.reviewed_count
            : chosenProteome.total_count
          : null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
      notify.success("Downloading from UniProt");
      onClose();
      navigate("/activity");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const toggle = (accession: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(accession)) next.delete(accession);
      else next.add(accession);
      return next;
    });

  const count = chosenProteome
    ? reviewedOnly
      ? chosenProteome.reviewed_count
      : chosenProteome.total_count
    : selected.size;

  const canDownload = chosenProteome != null || selected.size > 0;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal sra-modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 820, width: "90vw" }}
      >
        <h2>Download from UniProt</h2>

        <form
          className="sra-search"
          onSubmit={(e) => {
            e.preventDefault();
            if (query.trim()) resolve.mutate();
          }}
        >
          <label className="sra-search-accession">
            <span>Search</span>
            <input
              autoFocus
              value={query}
              placeholder="UP000002311, P0DTC2, 559292, spike glycoprotein…"
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          <button
            type="submit"
            className="btn primary"
            disabled={!query.trim() || resolve.isPending}
          >
            {resolve.isPending ? "Looking up…" : "Look up"}
          </button>
        </form>

        <small className="sra-search-hint">
          A proteome (UP…), one or more accessions, a taxon id, an organism
          name, or a protein name.
        </small>

        {resolve.isPending && (
          <div className="empty">
            <span className="spinner" /> Asking UniProt about {query.trim()}…
          </div>
        )}

        {resolved?.message && (
          <div className="warn-box" style={{ fontSize: 12 }}>
            {resolved.message}
          </div>
        )}

        {chosenProteome && (
          <ProteomeCard
            proteome={chosenProteome}
            reviewedOnly={reviewedOnly}
            onReviewedChange={setReviewedOnly}
          />
        )}

        {resolved && resolved.candidates.length > 0 && (
          <div style={{ marginTop: 10 }}>
            {!resolved.needs_picker && (
              <button
                type="button"
                className="sra-group-toggle"
                onClick={() => setShowOthers((s) => !s)}
              >
                {showOthers ? "▾" : "▸"} {resolved.candidates.length} other{" "}
                {resolved.candidates.length === 1 ? "proteome" : "proteomes"} for
                this organism
              </button>
            )}
            {showOthers && (
              <div className="sra-table-wrap">
                <table className="sra-table">
                  <thead>
                    <tr>
                      <th style={{ width: 28 }} />
                      <th>Proteome</th>
                      <th>Strain</th>
                      <th>Proteins</th>
                      <th>BUSCO</th>
                    </tr>
                  </thead>
                  <tbody>
                    {resolved.candidates.map((c) => (
                      <tr key={c.id}>
                        <td>
                          <input
                            type="radio"
                            name="proteome"
                            checked={chosenProteome?.id === c.id}
                            onChange={() => setChosenProteome(c)}
                          />
                        </td>
                        <td className="mono">{c.id}</td>
                        <td className="sra-dim">{c.strain ?? "—"}</td>
                        <td className="sra-num">
                          {c.protein_count?.toLocaleString() ?? "—"}
                        </td>
                        {/* Completeness is what makes choosing between
                            strains possible rather than arbitrary. */}
                        <td className="sra-num">
                          {c.busco_score != null ? `${c.busco_score}%` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {resolved && resolved.proteins.length > 0 && (
          <div className="sra-table-wrap">
            <table className="sra-table">
              <thead>
                <tr>
                  <th style={{ width: 28 }} />
                  <th>Accession</th>
                  <th>Protein</th>
                  <th>Organism</th>
                  <th>Length</th>
                </tr>
              </thead>
              <tbody>
                {resolved.proteins.map((p) => (
                  <ProteinRow
                    key={p.accession}
                    protein={p}
                    checked={selected.has(p.accession)}
                    onToggle={() => toggle(p.accession)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="modal-actions">
          <div
            style={{ marginRight: "auto", fontSize: 12, color: "var(--text-faint)" }}
          >
            {count != null && count > 0 && (
              <>{count.toLocaleString()} proteins</>
            )}
          </div>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={!canDownload || download.isPending}
            onClick={() => download.mutate()}
          >
            {download.isPending ? "Queueing…" : "Download"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** A resolved proteome: what it is, and how much of it to fetch. */
function ProteomeCard({
  proteome,
  reviewedOnly,
  onReviewedChange,
}: {
  proteome: UniProtProteome;
  reviewedOnly: boolean;
  onReviewedChange: (v: boolean) => void;
}) {
  // Only worth offering the choice when it changes the answer. For a fully
  // curated organism both counts are identical and the checkbox is noise.
  const differs =
    proteome.reviewed_count != null &&
    proteome.total_count != null &&
    proteome.reviewed_count !== proteome.total_count;

  return (
    <>
      <div className="sra-summary">
        <div>
          <strong className="mono">{proteome.id}</strong>
          {" · "}
          <span style={{ fontStyle: "italic" }}>{proteome.name}</span>
          {proteome.is_reference && (
            <span className="sra-have-tag" title="UniProt's reference proteome">
              reference
            </span>
          )}
        </div>
        <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
          {[
            proteome.strain,
            proteome.protein_count != null &&
              `${proteome.protein_count.toLocaleString()} proteins`,
            proteome.busco_score != null && `BUSCO ${proteome.busco_score}%`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </div>
        {proteome.genome_assembly && (
          <div style={{ color: "var(--text-faint)", fontSize: 12 }}>
            Genome:{" "}
            <a
              href={`https://www.ncbi.nlm.nih.gov/datasets/genome/${proteome.genome_assembly}/`}
              target="_blank"
              rel="noreferrer"
              className="mono"
            >
              {proteome.genome_assembly}
            </a>{" "}
            — downloadable from the NCBI dialog.
          </div>
        )}
      </div>

      {differs && (
        <label className="trim-check" style={{ marginTop: 10 }}>
          <input
            type="checkbox"
            checked={reviewedOnly}
            onChange={(e) => onReviewedChange(e.target.checked)}
          />
          <span>
            Reviewed entries only (Swiss-Prot)
            <small style={{ display: "block", color: "var(--text-faint)" }}>
              {proteome.reviewed_count?.toLocaleString()} reviewed ·{" "}
              {proteome.total_count?.toLocaleString()} including unreviewed
              (TrEMBL)
            </small>
          </span>
        </label>
      )}
    </>
  );
}

function ProteinRow({
  protein,
  checked,
  onToggle,
}: {
  protein: UniProtProtein;
  checked: boolean;
  onToggle: () => void;
}) {
  return (
    <tr>
      <td>
        <input type="checkbox" checked={checked} onChange={onToggle} />
      </td>
      <td className="mono">
        {protein.accession}
        {protein.reviewed && (
          <span className="sra-have-tag" title="Reviewed (Swiss-Prot)">
            reviewed
          </span>
        )}
      </td>
      <td className="sra-dim">{protein.name ?? "—"}</td>
      <td className="sra-dim" style={{ fontStyle: "italic" }}>
        {protein.organism ?? "—"}
      </td>
      <td className="sra-num">
        {protein.length != null ? `${protein.length.toLocaleString()} aa` : "—"}
      </td>
    </tr>
  );
}
```

- [ ] **Step 2: Mount it**

In `frontend/src/components/ProjectExplorer.tsx`:

Add the import beside the existing one (line 10 area):

```typescript
import { UniProtDownloadDialog } from "./UniProtDownloadDialog";
```

Add state beside `ncbiOpen`:

```typescript
const [uniprotOpen, setUniprotOpen] = useState(false);
```

Add a menu item after the "Download from NCBI…" button (line 348-354 area),
matching its markup exactly:

```tsx
<button
  role="menuitem"
  onClick={() => {
    setAddMenuOpen(false);
    setUniprotOpen(true);
  }}
>
  Download from UniProt…
</button>
```

Add the mount beside the NCBI one (line 364-366 area):

```tsx
{uniprotOpen && (
  <UniProtDownloadDialog
    projectId={projectId}
    onClose={() => setUniprotOpen(false)}
  />
)}
```

- [ ] **Step 3: Verify it compiles**

Run:
```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/UniProtDownloadDialog.tsx frontend/src/components/ProjectExplorer.tsx
git commit -m "feat: UniProt download dialog"
```

---

### Task 12: End-to-end verification

Manual testing in the browser is the actual verification step for anything
UI-facing here -- there is no headless component-testing setup in this repo
and none is expected.

**Read this before running anything in this task.**

There is a contradiction to resolve first. The browser checks below need a
running stack serving *this branch's* code — but this branch is a worktree,
and CLAUDE.md is explicit that `docker compose up` must only ever run from the
main repo root, because the bind mounts are relative paths and Compose would
otherwise silently repoint the user's port-5173 instance at this branch with
no warning. Rebuilding from the main root, meanwhile, serves main — which does
not contain this feature at all, so every browser check would pass or fail for
reasons unrelated to the work.

So do **not** rebuild the shared stack. Choose one:

**Option A — merge first, then verify (simplest).** Land the branch on main,
then rebuild the shared stack from the main repo root exactly as CLAUDE.md
says, and run the browser checks against port 5173 as normal.

**Option B — a private stack on unpublished ports.** CLAUDE.md describes this
for exactly this case: run with a separate project name so the shared instance
keeps serving main.

```bash
cd /Users/syntheticgio/Programming/local-bio-pipeliner/.claude/worktrees/worktree-todos-plan-a8dfce
docker compose -p biopipe-uniprot up -d --build api web worker
```

Confirm it did not disturb the shared stack:

```bash
docker inspect biopipe-worker-1 --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'
```
Expected: still the **main** repo paths, no `.claude/worktrees/`. If a worktree
path appears, the shared stack was repointed — fix it with
`cd /Users/syntheticgio/Programming/local-bio-pipeliner && docker compose up -d --build api web worker`.

Then find the private stack's mapped web port with
`docker compose -p biopipe-uniprot port web 5173` and use that URL below
instead of `localhost:5173`. Tear it down afterwards with
`docker compose -p biopipe-uniprot down`.

**This choice is the user's to make, not the implementer's.** Ask before
proceeding.

- [ ] **Step 3: Confirm the handler loaded**

Run:
```bash
docker compose logs worker --tail 30 | grep handlers_loaded
```
Expected: the list includes `download_uniprot`.

- [ ] **Step 4: Run the whole backend suite**

Run:
```bash
./scripts/wt-pytest.sh tests/ -q
```
Expected: PASS, with the ~59 new tests added by this plan.

- [ ] **Step 5: Check the rules against the real database**

Per CLAUDE.md -- a green suite fed hand-built objects has shipped wrong rules
before. Run:

```bash
docker compose exec api python -c "
import asyncio
from app.metadata import uniprot

# The three measured facts, against the live API rather than a fixture.
print('classify EGFR         ->', uniprot.classify('EGFR'))
print('classify UP000002311  ->', uniprot.classify('UP000002311'))
print('classify P0DTC2       ->', uniprot.classify('P0DTC2'))

r = uniprot.resolve_taxon(4932)
print('taxon 4932 picker     ->', r.needs_picker, 'candidates:', len(r.candidates))
assert r.needs_picker, 'the 4932 fallback is broken'

r = uniprot.resolve_taxon(559292)
print('taxon 559292 proteome ->', r.proteome.id if r.proteome else None)
assert r.proteome is not None, 'the reference query is broken'

print('human reviewed count  ->', uniprot.count_results('proteome:UP000005640 AND reviewed:true'))
print('human total count     ->', uniprot.count_results('proteome:UP000005640'))
"
```

Expected:
```
classify EGFR         -> text
classify UP000002311  -> proteome
classify P0DTC2       -> accessions
taxon 4932 picker     -> True candidates: 25
taxon 559292 proteome -> UP000002311
human reviewed count  -> 20416
human total count     -> 147506
```

The two asserts are the ones that matter: they fail loudly if the
`reference:true` filter or the 4932 fallback regressed.

- [ ] **Step 6: Exercise the dialog**

Open <http://localhost:5173>, choose a project, and use Add → Download from
UniProt… for each case:

1. **`UP000002311`** — a proteome card, 6,067 proteins, BUSCO 99%, and a
   `GCA_000146045.2` link. No reviewed checkbox, because both counts match.
2. **`UP000005640`** — the reviewed checkbox appears, reading roughly
   "20,416 reviewed · 147,506 including unreviewed".
3. **`4932`** — the picker, not "no proteome found". This is the case the
   fallback exists for.
4. **`P0DTC2`** — one protein row, pre-selected.
5. **`spike glycoprotein`** — several protein rows, none pre-selected.

Download case 1 and confirm on the Activity tab that the run appears, the job
succeeds, and the file lands in the project as a protein FASTA.

- [ ] **Step 7: Confirm the role kept it out of the reference picker**

Open the align dialog for a FASTQ in that project. The downloaded
`UP000002311_reviewed.fasta` must **not** appear as a selectable reference.
This is the failure `ObjectRole.PROTEIN` exists to prevent, and the one place
a mistake here would be silent rather than loud.

- [ ] **Step 8: Check the Sources page**

Open <http://localhost:5173/help/sources> and confirm the UniProt entry
renders with its summary, usage, homepage, licence, and citation.

---

### Task 13: Close the TODO entry

Per CLAUDE.md, finishing the work is not finishing the entry -- and this has
already gone wrong three times in this repo.

- [ ] **Step 1: Mark the entry FIXED**

In `docs/TODO.md`, change the heading at line 271:

```markdown
## UniProt download — FIXED
```

Keep the original body. The diagnosis explains why the code looks the way it
does, and the next person hitting something similar needs it.

- [ ] **Step 2: Add the closing note**

Immediately under the heading, before `Raised: 2026-07-31, requested.`:

```markdown
**Fixed 2026-07-31.** `backend/app/metadata/uniprot.py` (classify and query),
`backend/app/services/uniprot_service.py` (launch),
`backend/app/queue/uniprot_handlers.py` (`download_uniprot`),
`_apply_uniprot_download` in `backend/app/queue/results.py`,
`backend/app/api/v1/uniprot.py`, and
`frontend/src/components/UniProtDownloadDialog.tsx`.

What the implementation did differently from this entry:

- **A separate dialog, not a branch in the NCBI one.** The entry did not
  specify, and merging was possible since the namespaces do not collide. It
  was rejected: `NcbiDownloadDialog` is already 762 lines carrying two result
  shapes, and one field that accepts six identifier kinds plus free text is an
  overloaded door rather than a helpful one. The proteome/assembly cross-link
  the merge would have bought is offered as a link on the proteome card
  instead.
- **One `RunKind`, not two, and one handler for both download shapes.** The
  entry anticipated proteomes *or* per-protein FASTA. Both turned out to be
  the same `uniprotkb/stream` request differing only in the query string, so
  the dialog branches and the job does not.
- **Almost none of `assembly_handlers.py` was copied.** The entry called this
  "the same shape as the assembly one," which is true structurally and false
  mechanically: there is no binary, so no `SUBPROCESS` mode, no
  `run_subprocess`, no `tools.require`, no `extend_lease(3600)`, no disk
  pre-flight, no `EXTRACTION_FACTOR`, and no zip/checksum/path-traversal
  handling. The closest existing model for the transport is
  `structure_lookup.py`.
- **`sources.py` needed the entry but not a version field.** UniProt returns
  `X-UniProt-Release` — a real build number, which that module's docstring
  says data sources do not have. The release is recorded per-download in the
  object's `facts`; `DataSource` is unchanged, because what is true of a
  source and what is true of one download from it are different claims.
- **`suggestion_service.py` needed nothing**, checked rather than assumed: its
  align rule already filters on `role is ObjectRole.REFERENCE`, so a
  `PROTEIN` object is excluded by a guard that exists because a downloaded
  assembly's `protein.faa` once broke it.

Measurements taken against the live API on 2026-07-31, since three
plausible-looking choices were wrong:

- `proteome_type:1` returns **0** for every organism tried. The working
  reference filter is `reference:true`.
- `organism_id:4932 AND reference:true` returns **0** while `organism_id:4932`
  returns **360** — UniProt attaches yeast's reference proteome to strain
  taxon 559292, so the species-level ID a user is most likely to type has
  none. The fallback is mandatory.
- Human is **20,416 reviewed** against **147,506** including TrEMBL, which is
  why the reviewed choice is shown rather than defaulted silently.
- Sizes: yeast 6,067 proteins / 3.9 MB; human reviewed 20,427 / 13.7 MB.
- `X-Total-Results` and the delivered record count differ slightly (20,416
  reported, 20,427 delivered), so the header sizes the download and never
  asserts it.
```

- [ ] **Step 3: Commit**

```bash
git add docs/TODO.md
git commit -m "docs: mark the UniProt download entry fixed"
```

---

## Self-Review Notes

**Spec coverage** — every section of
`docs/superpowers/specs/2026-07-31-uniprot-download-design.md` maps to a task:
separate dialog (11), four input classes (1, 3, 8), taxon fallback (3),
`reference:true` (2), reviewed/unreviewed (2, 8, 11), one job (2, 5, 6),
`RunKind` (4), components (5-8, 10-11), what not to copy (6), ingest and role
(7), `sources.py` and the release question (7, 9), testing (12), TODO
closeout (13).

**Out of scope, per the spec** — no pipeline consuming a proteome, no
AlphaFold structures, no combined proteome+assembly download.

**Corrected during review** (noting these because the wrong versions would
each have failed at implementation time):

- `run_service.attach_outputs` does not exist. `_apply_assembly_download`
  calls `run_for_job` and then `record_outputs`, and Task 7 now matches that.
- The handler-module imports are at the **bottom** of `handlers.py` (line
  ~729) behind a `# noqa: E402`, not at the top. Task 6 Step 4 now shows the
  real block.
- The header said `HandlerMode.ASYNC` while the implementation used `THREAD`.
  THREAD is right: the urllib call blocks, and an ASYNC handler doing it would
  stall the heartbeat and expire its own lease.

**Known soft spot for the implementer:**

- Task 9 requires verifying UniProt's licence and citation against their own
  site. Do not fill those fields from memory -- CLAUDE.md is explicit that a
  wrong licence claim on a page that reads as authoritative is worse than a
  blank field.
