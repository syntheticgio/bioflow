"""minimap2 synteny alignment command construction and PAF parsing.

Same split `ragtag_runner` and `quast_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

This is the alignment step behind a synteny dot-plot: reference on one axis,
draft assembly contigs on the other, plotted as line segments so a break,
inversion, or translocation shows up as a visual discontinuity rather than a
number in a table. The runner itself does no plotting -- it produces argv for
minimap2 and turns its PAF output into plain positional facts a later task's
handler stores and a frontend component renders.

Reference argument comes before draft, matching both minimap2's own
`target query` convention and `ragtag_runner`'s `<reference> <query>` order
(see that module's docstring) -- keeping the two runners consistent avoids
reintroducing the same argument-order trap in a second tool.
"""

from pathlib import Path

from app.pipelines import ragtag_runner

# Below this, a PAF block is more likely alignment noise (a short spurious
# match) than a real syntenic block worth plotting -- and at genome scale a
# few hundred bp is invisible on the axis regardless.
MIN_SEGMENT_LENGTH = 1000

# A hard ceiling on how many segments the frontend ever has to render. Without
# one, a distant-genome comparison that produces hundreds of thousands of
# short alignments can hang the browser tab drawing them.
MAX_SYNTENY_SEGMENTS = 10_000


def build_synteny_command(
    *,
    minimap2_path: str,
    reference: Path,
    draft: Path,
    divergence: str,
    threads: int,
) -> list[str]:
    """The argv for `minimap2`, producing PAF on stdout.

    Reuses `ragtag_runner.mm2_params_for_divergence` rather than picking a
    preset independently -- one divergence enum should mean the same minimap2
    preset everywhere it is used in this app. Its return value is a single
    string (`"-x asm5"`) meant for `--mm2-params`, not an argv list, so it is
    split on whitespace before being placed here.

    `--secondary=no` is not optional: secondary alignments plot as off-diagonal
    scatter that reads as a translocation to anyone looking at the dot-plot,
    when it is really minimap2 reporting a second, weaker placement for the
    same query block. Suppressing them here means the plot only ever shows
    each query block once, at its best alignment.

    Returns a plain argv list -- no shell redirection. A later handler wraps
    this in `polypolish_runner.redirect_stdout` to send PAF to a file, the
    same posture `polypolish_runner.build_polish_command` and the winnowmap
    runner's equivalent take despite also writing to stdout.
    """
    preset_args = ragtag_runner.mm2_params_for_divergence(divergence).split()
    return [
        minimap2_path,
        *preset_args,
        "--secondary=no",
        "-t",
        str(threads),
        str(reference),
        str(draft),
    ]


def parse_paf(text: str) -> dict:
    """Alignment blocks from minimap2's PAF output.

    PAF is tab-separated: 12 mandatory columns, then a variable number of
    trailing `tag:type:value` fields (`NM:i:12`, `tp:A:P`, ...). Only the
    fixed prefix is parsed; anything from index 12 on is ignored. A line with
    fewer than 12 fields is malformed and skipped rather than raised -- a
    single bad line must not fail a run that otherwise produced a usable
    alignment.

    Returns:
        {
            "segments": [(target_name, target_start, target_end,
                          query_name, query_start, query_end, strand), ...],
            "target_lengths": {name: length, ...},
            "query_lengths": {name: length, ...},
            "synteny_segments_partial": True,  # present only if capped
        }

    `target_lengths`/`query_lengths` are collected from every well-formed
    record, before the minimum-length filter and before the cap -- they
    describe the genome's own structure (every contig and chromosome that
    appeared in the alignment, aligned or not), not which segments happened
    to survive filtering. An axis scaled only to filtered segments would crop
    an unaligned chromosome out of the plot entirely, which is itself a
    finding worth showing.

    Segments are filtered on target-axis length (`target_end - target_start`)
    against `MIN_SEGMENT_LENGTH`, then capped to `MAX_SYNTENY_SEGMENTS` by
    keeping the *longest* survivors, not the first N encountered. PAF is
    emitted in query order, so keeping the first N would keep every segment
    from the first few contigs and none from the rest -- a positional bias
    that reads as "the genome only aligns at one end," which looks like a
    real structural finding rather than a sampling artifact.
    """
    target_lengths: dict[str, int] = {}
    query_lengths: dict[str, int] = {}
    segments: list[tuple] = []

    for line in text.splitlines():
        if not line.strip():
            continue

        fields = line.split("\t")
        if len(fields) < 12:
            continue

        query_name = fields[0]
        target_name = fields[5]

        try:
            query_len = int(fields[1])
            query_start = int(fields[2])
            query_end = int(fields[3])
            strand = fields[4]
            target_len = int(fields[6])
            target_start = int(fields[7])
            target_end = int(fields[8])
        except ValueError:
            continue

        query_lengths[query_name] = query_len
        target_lengths[target_name] = target_len

        if target_end - target_start < MIN_SEGMENT_LENGTH:
            continue

        segments.append(
            (target_name, target_start, target_end, query_name, query_start, query_end, strand)
        )

    result: dict = {
        "segments": segments,
        "target_lengths": target_lengths,
        "query_lengths": query_lengths,
    }

    if len(segments) > MAX_SYNTENY_SEGMENTS:
        segments.sort(key=lambda s: s[2] - s[1], reverse=True)
        result["segments"] = segments[:MAX_SYNTENY_SEGMENTS]
        result["synteny_segments_partial"] = True

    return result
