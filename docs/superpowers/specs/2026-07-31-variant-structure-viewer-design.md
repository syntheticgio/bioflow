# Variant 3D structure in iCn3D

## Problem

`2026-07-31-variant-annotation-design.md` shipped, and the variants table now
carries `gene`, `consequence`, `aa_change` and `aa_pos`. A row reading
`PKC1 · missense · 866I>866L` says which protein changed and at which residue,
and there it stops. Whether residue 866 sits in a catalytic site, on a binding
interface, or on a disordered surface loop is the next question a missense call
raises, and nothing here can answer it.

That was the stated blocker in both prior specs, and it is gone. What remains
is the risk they flagged and did not resolve: **mapping a gene name to a
structure**, which the genomic-context spec warned "fails for most non-model
organisms and must degrade to *no structure available* as a normal outcome."

This spec resolves that mapping with measurements, and scopes the feature to
where it is honest.

## Goal

A per-variant 3D structure view that opens on the residue the variant changes,
and says "no structure available" — plainly and without apology — the majority
of the time.

## Findings from measuring it

Measured against the real yeast callset (`DRR1066343.bcftools.vcf.gz`, 6,641
variants, annotated against the GCF_000146045.2 GFF3) before this design was
written. Everything below is measured, not assumed.

### The addressable set is smaller than the table

| | count |
|---|---|
| Variants total | 6,641 |
| Annotated with a gene | 4,060 (61%) |
| Carrying an `aa_pos` | 3,955 |
| **Residue-*changing*** (excludes 2,173 synonymous) | **1,782 across 857 genes** |
| — of which missense | 1,653 |

Synonymous variants have an `aa_pos` but change no residue. They are the single
largest annotated class, and pointing a structure viewer at them would be
showing an unchanged protein. The addressable set is 1,782 variants, not 3,955.

### NCBI Structure text search is not usable

The obvious path — Entrez `esearch` against `db=structure`, scoped by organism
— fails in two ways that a hit-rate number alone conceals.

**It mis-parses hyphenated names.** `YDR524W-C AND "Saccharomyces
cerevisiae"[Organism]` translates to `C[All Fields] AND ...` — Entrez reads the
hyphen as a boolean and searches for `C`, returning **4,342** structures that
merely contain a chain C. `YDR210C-D` returns 3,126 the same way. Quoting the
term does *not* help; tagging it `YDR524W-C[All Fields]` does, and correctly
yields 0.

**It matches mentions, not proteins.** Even correctly parsed, the search finds
structures that *reference* the gene. Spot-checked top hits:

- `BMS1` and `FCF1` both return **the same** structure (9N7A), a 40-subunit SSU
  processome; each gene is one subunit of it.
- `MYO2`'s top hit is "Crystal structure of Vac8 bound to Vac17" — Myo2 is not
  in the title at all.
- `SRP1` returns 16 structures, of which **zero** are curated as that protein.

Measured on 120 randomly sampled residue-changing genes (seed 20260731), the
corrected Entrez search reports a 42.5% hit rate. Against curated
cross-references, **10 of its 51 hits are false positives** — it is
over-reporting by roughly a quarter.

### UniProt cross-references are the right source

The same 120 genes, resolved `gene → UniProt → PDB`:

| | |
|---|---|
| Resolved to a UniProt accession | 118 / 120 (98.3%) |
| **Have ≥1 curated PDB** | **42 (35.0%)** |
| No structure | 78 (65.0%) |
| Hit genes with >1 PDB | 31 (74%), median 3, max 230 |

Against Entrez on the same sample: 41 agree, **10 Entrez false positives**, 1
Entrez miss. UniProt also resolves `YDR524W-C` — the name Entrez could not
parse — to P0C1Z1, correctly with no structure.

So the real rate is **35%, not 42.5%**, and the higher number was inflated by
exactly the false matches that would have opened a viewer onto an unrelated
complex.

### `aa_pos` already agrees with UniProt numbering

The residue-mapping step the prior specs called unresolved is, for the common
case, already solved: `bcftools csq` numbers residues from the GFF3 CDS, which
matches UniProt's canonical sequence. Checked across all 118 resolved genes,
`max(aa_pos)` exceeds the UniProt protein length for **one**.

That one is the important finding.

### Gene-symbol collision is real, and is the guard

The outlier is `SRP1`, whose variants reach residue 428 against a 254aa entry.
The cause is not an offset. `gene_exact:SRP1` returns **TIR1** (P10863, "Cold
shock-induced protein TIR1") first, because TIR1 lists SRP1 as an alias; the
correct entry — Q02821, importin subunit alpha, 542aa — ranks second.

Taking the first UniProt hit therefore silently displays *the wrong protein*,
with a residue highlighted at a position that means nothing. This is worse than
showing nothing, and it is invisible without a check.

The `aa_pos > sequence length` comparison catches it for free. It is not a
completeness check; it is the correctness guard, and it is why the resolver
fetches sequence length at all.

## Scope

**In:**

- Resolving a VCF's organism taxid from its reference assembly — the one
  missing piece of existing plumbing, and the first thing to build.
- A structure button on rows that have a gene, an `aa_pos`, and a
  residue-changing consequence.
- A backend resolver: gene → UniProt accession → PDB IDs, with the length
  guard, cached.
- An iCn3D view opening on the resolved structure with the residue highlighted.
- "No structure available" as a normal, unremarkable outcome.

**Out:**

- **Synonymous variants.** They change no residue.
- **AlphaFold / predicted structures.** They would lift coverage from 35% to
  near-total, which is tempting and wrong for now: a predicted model and an
  experimental one warrant different confidence, and conflating them in one
  button is a bigger design question than this spec. Revisit with evidence.
- **Chain mapping within multi-subunit structures.** For a 40-subunit complex,
  knowing *which chain* is the gene's protein needs SIFTS residue-level
  mappings. Deferred; see "Follow-on".
- **Choosing the best of several PDBs.** 74% of hit genes have more than one.
  Ranking by resolution or coverage is a refinement, not a first cut.
- **Non-model organism support as a promise.** Yeast is close to the best case
  — a model organism with thousands of structures. 35% is a ceiling, not a
  typical rate.
- **A local structure database.** Two cached HTTP lookups, consistent with how
  `SequenceViewerModal` already treats NCBI.

## What this is not

- **Not a new pipeline stage.** No tool in `tools.py`, no queue handler, no
  `suggestion_service.py` rule. Everything needed is already in `variants.db`.
  (The CLAUDE.md tool-registration checklist does not apply — worth stating,
  since every recent feature here did need it.)
- **Not offline-capable.** Like the Sequence Viewer, this needs the network and
  must fail visibly, not hang.

## Architecture

### `backend/app/services/structure_lookup.py` (new)

One function, `resolve_structure(gene, organism_taxid) -> StructureHit | None`.

Backend rather than client-side, departing from `SequenceViewerModal`'s
in-component fetch, for three reasons: the length guard needs the protein
sequence length, which is a second field the UI has no other use for; results
are worth caching across rows and sessions, since 857 genes over a paged table
means heavy repetition; and UniProt's rate limits are better respected from one
place.

The taxid comes from the assembly the VCF was called against. `tax_id` is
already captured on assembly metadata (`app/metadata/assembly.py`, surfaced
through `/ncbi` and typed in `types.ts`), but it is `int | None` and reaching
it from a VCF means walking VCF → reference assembly → metadata. **That walk is
the one piece of plumbing this spec adds that does not exist yet**, and it
should be built first — every measurement below assumes an organism-scoped
query, and without the taxid the whole design degrades to the cross-organism
symbol collision that is far worse than the `SRP1` case.

When the taxid is absent — a local assembly with no NCBI metadata — the
resolver returns `None` without querying. An unscoped gene-symbol lookup is not
a degraded answer, it is a wrong one.

Query `gene_exact:<gene> AND organism_id:<taxid> AND reviewed:true`, fetching
`accession,xref_pdb,sequence`. Request several results, not one.

Then, in order:

1. Prefer the first entry whose `sequence.length >= max_aa_pos` for that gene.
   This is what rescues `SRP1`: TIR1 fails at 254, Q02821 passes at 542.
2. If no entry passes, return `None` — *not* the first entry. A gene whose
   residue exceeds every candidate is a resolution failure, and guessing
   defeats the guard.
3. If the chosen entry has no PDB cross-references, return a hit with an empty
   PDB list. "Resolved but no structure" and "could not resolve" are different
   outcomes and the UI may eventually distinguish them.

`reviewed:true` restricts to SwissProt. For yeast this costs nothing (98.3%
resolved) and buys curation. For an organism with no SwissProt coverage it will
resolve almost nothing — the correct outcome, since unreviewed entries are
where symbol collisions are worst.

Cache keyed on `(gene, taxid)`. `organism_service.py` is the pattern to follow
— a read-through Mongo cache where a hit is one indexed document read, a miss
does the remote call and stores it, and every failure yields `None` so the
feature degrades to absence rather than an error. Negative results must be
cached too: 65% of lookups are misses, and an uncached miss means every page
render re-queries every absent gene.

### `backend/app/api/v1/pipelines.py` — one endpoint

Alongside `GET /vcfstats/variants/{object_id}`, which is where the variant
table's data already comes from.

`GET /vcfstats/structure/{object_id}?gene=&aa_pos=` returning the UniProt
accession, PDB list, protein length, and a resolution status. Taking the
`object_id` rather than a taxid keeps the VCF → assembly → taxid walk on the
server, where the reference association already lives; the client knows the
gene, not the organism.

Called when the user clicks, not per row — 65% of rows would resolve to
nothing, and pre-resolving a page would issue dozens of lookups to render
buttons that mostly do nothing.

This makes the button optimistic: it renders when the row *could* have a
structure, and clicking may still land on "none available". That is the right
trade — the alternative is either a slow table or a table that lies by omission.

### `frontend/src/components/StructureViewerModal.tsx` (new)

Modelled on `SequenceViewerModal.tsx`, which already solved the hard parts of
embedding a remote NCBI viewer: a module-scoped load promise, a 15s timeout so
an offline machine reaches the escape hatch rather than spinning, and an
explicit note that this is a runtime outbound dependency loaded on open rather
than at module scope.

iCn3D is simpler in one respect — it embeds as an `<iframe>` with URL
parameters, so there is no `SeqViewOnReady`-style global-polling gap to handle.

The iframe URL carries the PDB ID and a residue selection derived from
`aa_pos`. Two states beyond the viewer itself:

- **Resolving** — the lookup is a network round trip.
- **No structure available** — flat statement, naming the gene. No apology, no
  "try again", no suggestion that something went wrong. It is the majority
  outcome by design.

### `frontend/src/components/VariantTable.tsx`

A button in a new cell, gated on `row.gene && row.aa_pos && isResidueChanging(row.consequence)`.

Gating follows the precedent set for the Sequence Viewer button: render nothing
when the row cannot support it, rather than a disabled control. A disabled
button invites a click and then explains why it was pointless.

`isResidueChanging` is a small pure helper — the complement of the synonymous
case, kept as an allow-list of the consequence types measured in the real data
(`missense`, `frameshift`, `stop_gained`, `inframe_deletion`,
`inframe_insertion`, `start_lost`, `stop_lost`), so an unrecognised future
consequence type does not silently acquire a button.

## Testing

Per CLAUDE.md there is no headless component-testing setup and none is
expected. The pure logic is the testable surface, and it is in the resolver.

`backend/tests/services/test_structure_lookup.py`:

- **The `SRP1` case.** A candidate list whose first entry is too short and whose
  second fits selects the second. This is the regression test for the whole
  design; without it the guard can be removed and everything still looks fine.
- **All candidates too short** returns `None`, not the first.
- **A missing taxid** returns `None` without issuing a request, rather than
  falling back to an unscoped query.
- **Resolved with no PDB refs** is distinct from unresolved.
- **Negative results are cached** — a second call for an absent gene issues no
  second request.
- **Hyphenated names** (`YDR524W-C`) survive query construction intact. Entrez
  mangled these; a test pins that UniProt's path does not.

Per CLAUDE.md's warning that green tests on hand-built fixtures proved nothing
for the suggestion rules, at least one check must run against the real
`variants.db` rather than fixtures — the `aa_pos` vs. protein-length agreement
across resolved genes is the one that would have caught `SRP1`.

Frontend verification is manual at localhost:5173:

1. A missense row with a well-studied gene (`ADH1`, `PGK1`) opens a structure
   with the residue highlighted.
2. A missense row on an uncharacterised ORF reports no structure, and reads as
   ordinary rather than broken.
3. A synonymous row shows no button.
4. An un-annotated VCF shows no buttons anywhere and the table looks unchanged.
5. `SRP1` — if present in the callset — shows either importin alpha or nothing,
   never TIR1.

## Follow-on

**Chain mapping via SIFTS.** For multi-subunit structures, highlighting the
right residue on the *right chain* needs SIFTS UniProt↔PDB residue mappings.
Until then a hit on a large complex may highlight by residue number without
certainty about the chain — acceptable for a first cut on single-chain
structures, and the reason multi-subunit ranking is out of scope here.

**AlphaFold as a labelled second tier.** Would take coverage from 35% toward
near-total for the 63% of resolved genes with no experimental structure. Needs
a confidence-communication design first, and pLDDT colouring is a natural fit.

**Picking among multiple PDBs.** 74% of hits have several; ranking by resolution
or by coverage of the variant residue is a real improvement once there is
evidence about which structure users actually want.

**Measuring a non-model organism.** Every number here is yeast. The *S. aureus*
callset would show what this looks like away from the best case, and would test
whether `reviewed:true` is too strict outside SwissProt's well-covered species.
