"""Matching paired-end read files to each other.

`parsers._infer_pair_hint` records that a file looks like an R1 or an R2, but
that alone does not say *which* R2 an R1 belongs to. This derives a pairing key
by removing the mate token from the name: two files that reduce to the same key
and carry opposite hints are mates.

Filename convention is the only signal available. Read IDs inside the files
would be more reliable, but checking them means decompressing two files to
compare their first records, and the convention is near-universal in practice.
The cost of being wrong is bounded: the launch dialog shows the detected mate
and lets it be changed.
"""

import re

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
