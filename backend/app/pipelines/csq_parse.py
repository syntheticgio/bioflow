"""Parsing bcftools' BCSQ consequence field.

Separate from the runner because this is where the edge cases are. The format
is documented as pipe-delimited, but real output carries four different field
counts and two kinds of entry that are not consequences at all, and each of
those was found by running it rather than by reading about it:

    missense|CYS3|rna-NM_...|protein_coding|+|160K>160M|131277A>T   7 fields
    start_lost|SNU23|rna-NM_...|protein_coding|-                    5 fields
    intron|RPL19B||protein_coding                                   4 fields
    @286153                                                         1 field

Measured on 4,152 annotated yeast variants: 4,027 seven-field, 104 four-field,
5 five-field, 141 bare pointers.
"""

import re
from dataclasses import dataclass

# Most severe first. A variant can carry several consequences across
# overlapping transcripts, and the table shows one column -- so which one wins
# has to be a decision rather than whichever bcftools happened to list first.
_SEVERITY = (
    "frameshift",
    "stop_gained",
    "stop_lost",
    "start_lost",
    "splice_acceptor",
    "splice_donor",
    "inframe_deletion",
    "inframe_insertion",
    "inframe_altering",
    "missense",
    "splice_region",
    "coding_sequence",
    "synonymous",
    "stop_retained",
    "start_retained",
    "5_prime_utr",
    "3_prime_utr",
    "non_coding",
    "intron",
    "intergenic",
)
_RANK = {name: i for i, name in enumerate(_SEVERITY)}

# Where a consequence type we do not know about sorts. Deliberately above the
# benign tail rather than below everything: an unrecognised type is more
# likely to be a new bcftools vocabulary entry than something harmless, and a
# consequence nobody has seen before winning the column is visible and
# investigable. Ranking it last -- the obvious default -- meant it silently
# lost to `synonymous`, which is the failure this whole ranking exists to
# prevent.
_UNKNOWN_RANK = _RANK["synonymous"] - 0.5

# Fewer fields than this is not a consequence -- the shortest real form
# (intron) carries four.
_MIN_FIELDS = 4

# "160K>160M" and "99P" both start with the residue number.
_AA_POS = re.compile(r"^(\d+)")


@dataclass(frozen=True)
class Consequence:
    """One variant's effect, already reduced to what a table column shows."""

    consequence: str
    gene: str | None
    transcript: str | None
    aa_change: str | None
    aa_pos: int | None
    #: bcftools prefixed the type with "*", meaning the prediction accounts for
    #: another variant on the same haplotype.
    compound: bool
    #: How many further consequences this variant had, beyond the one kept.
    additional: int


def _parse_one(item: str) -> Consequence | None:
    fields = item.split("|")
    if len(fields) < _MIN_FIELDS:
        return None

    kind = fields[0]
    compound = kind.startswith("*")
    if compound:
        kind = kind[1:]

    aa_change = fields[5] if len(fields) > 5 and fields[5] else None
    aa_pos = None
    if aa_change:
        match = _AA_POS.match(aa_change)
        if match:
            aa_pos = int(match.group(1))

    return Consequence(
        consequence=kind,
        gene=fields[1] or None,
        transcript=fields[2] or None,
        aa_change=aa_change,
        aa_pos=aa_pos,
        compound=compound,
        additional=0,
    )


def parse_bcsq(value: str | None) -> Consequence | None:
    """The most severe consequence in a BCSQ value, or None if it holds none.

    None is an ordinary answer, not a failure: `bcftools query` emits "." for
    every variant in an un-annotated VCF, and for annotated ones only 63% of
    records carried a consequence in the measured run.
    """
    if not value:
        return None
    text = value.strip()
    if not text or text == ".":
        return None

    parsed: list[Consequence] = []
    for item in text.split(","):
        item = item.strip()
        # "@1234" points at another record sharing this haplotype. Skipped per
        # item rather than rejecting the whole value -- a pointer can sit in a
        # list beside a real consequence, and dropping the record would lose a
        # real annotation.
        if not item or item.startswith("@"):
            continue
        one = _parse_one(item)
        if one is not None:
            parsed.append(one)

    if not parsed:
        return None

    best = min(parsed, key=lambda c: _RANK.get(c.consequence, _UNKNOWN_RANK))
    if len(parsed) == 1:
        return best
    return Consequence(
        consequence=best.consequence,
        gene=best.gene,
        transcript=best.transcript,
        aa_change=best.aa_change,
        aa_pos=best.aa_pos,
        compound=best.compound,
        additional=len(parsed) - 1,
    )
