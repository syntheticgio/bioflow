"""What a protein FASTA header names, if anything.

Pure string parsing -- no network, no database. Separate from `uniprot.py`,
which asks UniProt questions; this module only reads the text in the file.

Two header shapes resolve, because they are the two that actually appear in
files this app handles:

- UniProt's own FASTA format (`>sp|P00924|ENO1_YEAST ...`), which a proteome
  download from `uniprot_service` produces.
- NCBI RefSeq protein (`>NP_009342.1 Cdc19p [...]`), which the `protein.faa`
  component of an NCBI assembly download produces.

Everything else -- most importantly annotation-tool output such as
`>KLLIPMDF_00023 hypothetical protein` -- names no identifier. That is an
ordinary answer rather than a failure: it is where the structure-prediction
path belongs, and prediction is the follow-up this design defers.

The patterns are deliberately strict, for the reason `uniprot.is_valid_accession`
records: a loose accession pattern classifies the gene symbol EGFR as an
accession, which resolves to nothing and looks like a lookup failure rather
than a parse error.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class RefKind(StrEnum):
    UNIPROT = "uniprot"
    REFSEQ = "refseq"


@dataclass(frozen=True)
class ProteinRef:
    """One header's identifier, and which database it belongs to.

    The kind matters to the resolver: a UniProt accession is looked up
    directly, while a RefSeq one goes through UniProt's cross-reference index.
    """

    kind: RefKind
    accession: str


# UniProt's own documented accession pattern, copied from `uniprot._ACCESSION`
# rather than imported: that one anchors a whole token (`^...$`) because it
# validates a user-typed box, while this one matches inside a pipe-delimited
# header. Two anchorings of one pattern; the shared part is the pattern text.
_UNIPROT_ACCESSION = (
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}"
)

# `>sp|P00924|ENO1_YEAST ...` and its TrEMBL `tr|` counterpart.
_UNIPROT_HEADER = re.compile(rf"^(?:sp|tr)\|({_UNIPROT_ACCESSION})\|", re.IGNORECASE)

# NP_/XP_/WP_ plus digits, with an optional version suffix that is dropped --
# UniProt's cross-reference index is keyed on the unversioned form.
_REFSEQ_HEADER = re.compile(r"^((?:NP|XP|WP)_\d+)(?:\.\d+)?\b", re.IGNORECASE)


def parse_header(header: str) -> ProteinRef | None:
    """The accession a FASTA header names, or None.

    Accepts the header with or without its leading `>`, since callers differ
    on whether they have stripped it.

    None means "this header names nothing we can resolve" and is the expected
    answer for annotation-tool output. It is not an error and must not be
    logged as one -- for a de-novo annotated proteome it is every record.
    """
    text = (header or "").strip()
    if text.startswith(">"):
        text = text[1:].strip()
    if not text:
        return None

    # Only the first whitespace-delimited token can hold the identifier; the
    # rest is free-text description, and searching it would match an accession
    # mentioned in prose.
    token = text.split()[0]

    match = _UNIPROT_HEADER.match(token)
    if match:
        return ProteinRef(kind=RefKind.UNIPROT, accession=match.group(1).upper())

    match = _REFSEQ_HEADER.match(token)
    if match:
        return ProteinRef(kind=RefKind.REFSEQ, accession=match.group(1).upper())

    return None
