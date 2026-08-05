"""Matching paired-end read files to each other.

`parsers._infer_pair_hint` records that a file looks like an R1 or an R2, but
that alone does not say *which* R2 an R1 belongs to. This derives a pairing key
by removing the mate token from the name: two files that reduce to the same key
and carry opposite hints are mates.

Filename convention proposes a candidate pair; `verdict()` below confirms or
vetoes it using two signals already captured at ingest and thrown away until
now -- `facts.first_read_ids` and `metadata.read_type`. Neither signal can
*originate* a pairing (a file and its own trimmed derivative share identical
read IDs), so both only rule on what the filename already proposed. This
module still imports nothing beyond `re` and stays ignorant of `DataObject`,
Motor, or the filesystem -- callers pass plain dicts.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# Ordered longest-first: `_R1` must be tried before `_1` so that
# `sample_R1.fastq.gz` reduces on the more specific token.
_MATE_TOKENS: tuple[tuple[str, str], ...] = (
    ("_R1", "R1"),
    ("_R2", "R2"),
    (".R1", "R1"),
    (".R2", "R2"),
    ("_r1", "R1"),
    ("_r2", "R2"),
    (".r1", "R1"),
    (".r2", "R2"),
    ("_1", "R1"),
    ("_2", "R2"),
)

# Suffixes stripped before matching, so `_R1.fastq.gz` and `_R1.fq` both reduce
# to the same stem.
_SUFFIX_RE = re.compile(r"\.(fastq|fq)(\.(gz|bz2|zst))?$", re.IGNORECASE)

# Processing markers this application inserts between the mate token and the
# format suffixes (see fastp_runner.output_name). Without stripping these, a
# trimmed file's token is no longer at the end of the stem and its pair would
# go unrecognized -- which would break pairing on every file the pipeline
# produces.
_MARKER_RE = re.compile(r"\.(trimmed|filtered|merged)$", re.IGNORECASE)

OPPOSITE = {"R1": "R2", "R2": "R1"}

# Which naming scheme a token belongs to. `sample_R1` and `sample_2` reduce to
# the same key but come from different conventions, and pairing them would be a
# guess -- the launch dialog can simply ask instead.
_SCHEME = {"_R1": "R", "_R2": "R", ".R1": "R", ".R2": "R",
           "_r1": "R", "_r2": "R", ".r1": "R", ".r2": "R",
           "_1": "N", "_2": "N"}


def split_mate(name: str) -> tuple[str, str, str] | None:
    """Split a filename into (pairing key, mate, scheme), or None.

    The token is matched at the *end* of the stem, which is what distinguishes
    a mate marker from a coincidence: `sample_R1_run_2.fastq` is an R1 whose
    name happens to end in `_2`, and treating that trailing token as the marker
    would pair it with the wrong file.
    """
    stem = _SUFFIX_RE.sub("", name)
    stem = _MARKER_RE.sub("", stem)

    for token, mate in _MATE_TOKENS:
        if stem.endswith(token):
            return stem[: -len(token)], mate, _SCHEME[token]

    return None


def pairing_key(name: str) -> str | None:
    """The part of a name shared by both mates, or None when unpaired."""
    split = split_mate(name)
    return split[0] if split else None


def mate_of(name: str) -> str | None:
    """Which half of a pair this name is: 'R1', 'R2', or None."""
    split = split_mate(name)
    return split[1] if split else None


def is_mate_of(name: str, other: str) -> bool:
    """True when two filenames name opposite halves of the same pair."""
    a, b = split_mate(name), split_mate(other)
    if a is None or b is None:
        return False
    key_a, mate_a, scheme_a = a
    key_b, mate_b, scheme_b = b
    # Keys are compared case-insensitively so `Sample_R1` matches `sample_R2`,
    # but the keys must not be empty -- a file simply named `_R1.fastq` carries
    # no identity to match on.
    return (
        bool(key_a)
        and key_a.lower() == key_b.lower()
        and scheme_a == scheme_b
        and mate_b == OPPOSITE.get(mate_a)
    )


@dataclass(frozen=True)
class PairInput:
    """Everything `verdict()` needs about one file, as plain data.

    `facts` and `metadata` are already plain dict fields on `DataObject`, so a
    caller that has loaded the object pays nothing extra to build this.
    """

    name: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    NAME_ONLY = "name_only"
    REJECTED_LAYOUT = "rejected_layout"
    REJECTED_READ_IDS = "rejected_read_ids"
    NO_MATCH = "no_match"


def _first_token(read_id: str) -> str:
    """The text up to the first whitespace.

    Mates disagree past this point in the normal case, not the edge case:
    `ERR17609896.1 ... length=150` vs `ERR17609896.1 ... length=149` -- the
    reads are different lengths. Comparing the whole header would reject a
    genuine pair; comparing this token would not.
    """
    return read_id.split(None, 1)[0] if read_id else ""


def _run_field(read_id: str) -> str:
    """The leading structural field, stable across every read in a run.

    SRA-style IDs (`ERR17609896.1`) split on the first `.`; Illumina-style IDs
    (`LH00201:115:...`) split on the first `:` -- the instrument. Used only to
    veto, never to confirm, because it is coarse: it is the same for every read
    in a run regardless of which record a filter left at the front.
    """
    token = _first_token(read_id)
    for sep in (".", ":"):
        if sep in token:
            return token.split(sep, 1)[0]
    return token


def read_ids_agree(a: list[str], b: list[str]) -> bool:
    """True when the two ID lists share at least one first token."""
    tokens_a = {_first_token(i) for i in a}
    tokens_b = {_first_token(i) for i in b}
    return bool(tokens_a & tokens_b)


def read_ids_conflict(a: list[str], b: list[str]) -> bool:
    """True only on positive evidence of difference: no shared run field.

    Deliberately not `not read_ids_agree(...)`. A file filtered independently
    of its mate can have a completely different first token (its own first
    surviving read) while still being the same run -- vetoing on that would
    unpair legitimate mates. The run field is stable regardless of which
    record ended up first, so it is what a veto can safely key on.
    """
    fields_a = {_run_field(i) for i in a}
    fields_b = {_run_field(i) for i in b}
    return bool(fields_a) and bool(fields_b) and not (fields_a & fields_b)


def verdict(a: PairInput, b: PairInput) -> Verdict:
    """Confirm, veto, or fall through on a filename-proposed pairing.

    Order matters: `NO_MATCH` first because it is the overwhelmingly common
    case and the cheapest to decide; the layout veto before the read-ID check
    so a single-end file with no `first_read_ids` is still rejected rather than
    falling through to `NAME_ONLY`.
    """
    if not is_mate_of(a.name, b.name):
        return Verdict.NO_MATCH

    if a.metadata.get("read_type") == "single-end" or b.metadata.get("read_type") == "single-end":
        return Verdict.REJECTED_LAYOUT

    ids_a, ids_b = a.facts.get("first_read_ids"), b.facts.get("first_read_ids")
    if ids_a and ids_b:
        if read_ids_conflict(ids_a, ids_b):
            return Verdict.REJECTED_READ_IDS
        if read_ids_agree(ids_a, ids_b):
            return Verdict.CONFIRMED

    return Verdict.NAME_ONLY
