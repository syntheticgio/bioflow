"""Bounded aggregates over a stream of features.

Everything here is O(contigs) or O(distinct types) in memory, never O(features)
-- a human GFF3 has millions of rows and the accumulator runs inside the same
single pass that builds the database.

The one exception is per-contig interval merging, which holds a list of merged
intervals per contig. That is bounded by the annotation's structure rather than
its row count (merged intervals collapse as they overlap), and on a dense
annotation it converges to roughly one interval per gene-dense region.
"""

from app.pipelines.annotation_parse import Feature

# Histogram edges for feature length, in bases. Chosen for annotation shapes:
# exons are 10^2, genes 10^3-10^4, and a whole-chromosome feature is an
# outlier worth seeing rather than clipping.
_LENGTH_BINS: tuple[int, ...] = (
    0, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 50_000, 100_000,
)


def parse_header_directives(lines: list[str]) -> dict:
    """Provenance from the `##`/`#!` header block.

    Only the directives worth showing in the provenance line. An unknown
    directive is ignored rather than stored: this feeds a one-line UI string,
    not a general metadata store.
    """
    meta: dict = {}
    for line in lines:
        line = line.strip()
        if line.startswith("##gff-version"):
            parts = line.split()
            if len(parts) > 1:
                meta["gff_version"] = parts[1]
        elif line.startswith("#!genome-build "):
            meta["genome_build"] = line[len("#!genome-build "):].strip()
        elif line.startswith("#!annotation-source "):
            meta["annotation_source"] = line[len("#!annotation-source "):].strip()
        elif line.startswith("##source-version "):
            meta["source_version"] = line[len("##source-version "):].strip()
    return meta


class _ContigCoverage:
    """Merged intervals for one contig.

    Kept sorted and merged on insert rather than sorted at the end, because
    the end is where memory is tightest and a full sort of every interval on a
    3M-feature file is exactly what this accumulator exists to avoid.
    """

    def __init__(self) -> None:
        self.intervals: list[list[int]] = []

    def add(self, start: int, end: int) -> None:
        # Fast path: features usually arrive in coordinate order, so the new
        # interval either extends the last one or starts after it.
        if self.intervals:
            last = self.intervals[-1]
            if start > last[1] + 1:
                self.intervals.append([start, end])
                return
            if start >= last[0]:
                last[1] = max(last[1], end)
                return

        # Slow path: out-of-order input. Insert and re-merge.
        self.intervals.append([start, end])
        self.intervals.sort()
        merged: list[list[int]] = []
        for iv in self.intervals:
            if merged and iv[0] <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], iv[1])
            else:
                merged.append(iv)
        self.intervals = merged

    def covered_bases(self) -> int:
        return sum(end - start + 1 for start, end in self.intervals)


class AnnotationAccumulator:
    """Every bounded number the Results view shows, from one pass.

    `contig_lengths` comes from the reference's facts via the job payload --
    the handler runs in a worker process and cannot query for it.
    """

    def __init__(self, *, contig_lengths: dict[str, int]) -> None:
        self._contig_lengths = contig_lengths
        self._total = 0
        self._top_level = 0
        self._malformed = 0
        self._types: dict[str, int] = {}
        self._biotypes: dict[str, int] = {}
        self._attr_keys: dict[str, int] = {}
        self._per_contig_count: dict[str, int] = {}
        self._coverage: dict[str, _ContigCoverage] = {}
        self._length_counts = [0] * (len(_LENGTH_BINS) + 1)

    def add(self, f: Feature) -> None:
        self._total += 1
        if f.parent is None:
            self._top_level += 1
        if f.type:
            self._types[f.type] = self._types.get(f.type, 0) + 1
        if f.biotype:
            self._biotypes[f.biotype] = self._biotypes.get(f.biotype, 0) + 1

        self._per_contig_count[f.contig] = self._per_contig_count.get(f.contig, 0) + 1
        cov = self._coverage.get(f.contig)
        if cov is None:
            cov = self._coverage[f.contig] = _ContigCoverage()
        cov.add(f.start, f.end)

        length = f.end - f.start + 1
        for i, edge in enumerate(_LENGTH_BINS):
            if length <= edge:
                self._length_counts[i] += 1
                break
        else:
            self._length_counts[-1] += 1

    def add_malformed(self) -> None:
        self._malformed += 1

    def add_attribute_keys(self, keys) -> None:
        for key in keys:
            self._attr_keys[key] = self._attr_keys.get(key, 0) + 1

    def finish(self) -> dict:
        per_contig = []
        for name, count in sorted(
            self._per_contig_count.items(), key=lambda kv: -kv[1]
        ):
            length = self._contig_lengths.get(name)
            covered = self._coverage[name].covered_bases()
            # Clamped: a feature running past a contig's recorded length
            # (a mismatched annotation/reference pair) must not report
            # coverage above 1.0, which reads as a bug in the chart.
            if length:
                covered = min(covered, length)
            per_contig.append(
                {
                    "name": name,
                    "length": length,
                    "count": count,
                    "covered_bases": covered,
                    # None, not 0.0: an unknown contig length is not zero
                    # coverage, and a chart must not draw it as an empty bar.
                    "covered_fraction": (
                        round(covered / length, 6) if length else None
                    ),
                    "per_mb": (
                        round(count / (length / 1_000_000), 3) if length else None
                    ),
                }
            )

        histogram = []
        for i, edge in enumerate(_LENGTH_BINS):
            low = 0 if i == 0 else _LENGTH_BINS[i - 1] + 1
            histogram.append({"min": low, "max": edge, "count": self._length_counts[i]})
        histogram.append(
            {
                "min": _LENGTH_BINS[-1] + 1,
                "max": None,
                "count": self._length_counts[-1],
            }
        )

        facts: dict = {
            "annotation_feature_count": self._total,
            "annotation_top_level_count": self._top_level,
            "annotation_contig_count": len(self._per_contig_count),
            "annotation_per_contig": per_contig,
            "annotation_length_histogram": histogram,
        }
        # Each of these is omitted rather than emitted empty: the UI renders a
        # block only when its key is present, and an empty dict would draw an
        # empty chart instead of nothing.
        if self._types:
            facts["annotation_type_counts"] = self._types
        if self._biotypes:
            facts["annotation_biotype_counts"] = self._biotypes
        if self._attr_keys:
            facts["annotation_attribute_keys"] = self._attr_keys
        if self._malformed:
            facts["annotation_malformed_lines"] = self._malformed
        return facts
