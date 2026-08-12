"""GenBank feature-table grammar: locations, qualifiers, and feature rows.

Pure functions with no I/O, the sibling of `annotation_parse.py` and for the
same reason -- the format edge cases are most of what this file is, and they
are testable as plain calls only if nothing here touches a file.

GenBank is 1-based inclusive, which is what `Feature` already uses, so unlike
BED there is no coordinate conversion here.
"""

import re
from dataclasses import dataclass

# A single position (`467`) or a range (`100..200`), any bound optionally
# fuzzy (`<1`, `>200`).
_RANGE_RE = re.compile(r"^<?(\d+)(?:\s*\.\.\s*>?(\d+))?$")

# A between-position (`102^103`), marking a site between two bases rather
# than a span. Stored as the left base alone.
_BETWEEN_RE = re.compile(r"^(\d+)\^(\d+)$")


@dataclass(frozen=True)
class Location:
    """Where a feature sits, with its segments kept separate.

    `segments` is never collapsed to outer bounds: a `join` describes a
    feature that does not occupy the gaps between its parts, and flattening
    it would claim coverage over introns it does not cover (#294).
    """

    segments: list[tuple[int, int]]
    strand: str
    fuzzy: bool


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside parentheses.

    `join(complement(1..10),20..30)` splits into two parts, not three: a
    naive `text.split(",")` would cut `complement(1..10` in half.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def parse_location(text: str) -> Location | None:
    """A GenBank location string, or None if it cannot be read.

    None rather than an exception: an unrecognized grammar costs one feature,
    not the whole file. The handler counts these as malformed.
    """
    text = (text or "").strip()
    if not text:
        return None

    # A remote reference names another record. There is no contig in this
    # file it could attach to, so it is skipped rather than misplaced.
    if ":" in text:
        return None

    if text.startswith("complement(") and text.endswith(")"):
        inner = parse_location(text[len("complement(") : -1])
        if inner is None:
            return None
        return Location(segments=inner.segments, strand="-", fuzzy=inner.fuzzy)

    for keyword in ("join(", "order("):
        if text.startswith(keyword) and text.endswith(")"):
            segments: list[tuple[int, int]] = []
            fuzzy = False
            # The feature's strand is its first segment's. A mixed-strand
            # join cannot be expressed in one column; every segment is still
            # preserved, which is what the constraint actually requires.
            strand = "+"
            for i, part in enumerate(_split_top_level(text[len(keyword) : -1])):
                sub = parse_location(part.strip())
                if sub is None:
                    return None
                segments.extend(sub.segments)
                fuzzy = fuzzy or sub.fuzzy
                if i == 0:
                    strand = sub.strand
            if not segments:
                return None
            return Location(segments=segments, strand=strand, fuzzy=fuzzy)

    m = _BETWEEN_RE.match(text)
    if m:
        # `102^103` stores the left base so the site lands somewhere real on
        # the locus chart rather than being dropped.
        left = int(m.group(1))
        return Location(segments=[(left, left)], strand="+", fuzzy=False)

    m = _RANGE_RE.match(text)
    if not m:
        return None
    start = int(m.group(1))
    # A bare position is a one-base feature: start and end are the same.
    end = int(m.group(2)) if m.group(2) else start
    if end < start:
        return None
    return Location(
        segments=[(start, end)],
        strand="+",
        fuzzy="<" in text or ">" in text,
    )


def parse_qualifiers(lines: list[str]) -> dict[str, str]:
    """The `/key="value"` block beneath a feature's location.

    Values wrap across lines; a continuation is any line not starting with
    `/`. Malformed lines are skipped rather than raised, the posture
    `parse_gff_attributes` documents for the same kind of data.

    A repeated key keeps the first occurrence. `/db_xref` legitimately
    repeats, but the caller preserves the raw block separately, so the
    dropped values remain visible in the feature's attributes column.
    """
    out: dict[str, str] = {}
    key: str | None = None
    parts: list[str] = []

    def flush() -> None:
        if key is None:
            return
        # /translation is a protein sequence: GenBank wraps it mid-residue,
        # not on word boundaries, so its parts must join with no separator.
        # Every other qualifier wraps prose on word boundaries and rejoins
        # with a space.
        value = "".join(parts) if key == "translation" else " ".join(parts)
        out.setdefault(key, value.strip().strip('"'))

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("/"):
            flush()
            body = line[1:]
            if "=" in body:
                k, _, v = body.partition("=")
                key, parts = k.strip(), [v.strip()]
            else:
                # A valueless qualifier such as /pseudo. Its presence is the
                # information, so the key is stored with an empty value.
                key, parts = body.strip(), [""]
        elif key is not None:
            parts.append(line)
        # A continuation with no open key is malformed; skipped silently.

    flush()
    return out
