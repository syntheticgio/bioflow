"""The failure a unit test of the parser cannot catch.

`_ContigCoverage` merges overlapping intervals. A join feature's parent row
spans its introns, so feeding that parent to the coverage accumulator after
its segments fills the intron back in -- silently, with nothing raising and
no test failing on its own. The number is just wrong.
"""

from app.pipelines.annotation_stats import AnnotationAccumulator
from app.pipelines.genbank_parse import iter_features

JOIN_FEATURE = [
    "     CDS             join(100..200,300..400)",
    '                     /gene="thrA"',
]


def test_intron_is_not_counted_as_covered():
    rows = list(iter_features(JOIN_FEATURE, accession="NC_1"))
    parent, *segments = rows

    acc = AnnotationAccumulator(contig_lengths={"NC_1": 1000})
    for row in segments:
        acc.add(row)
    facts = acc.finish()

    # 101 bases + 101 bases. NOT 301, which is what the parent's outer span
    # would give and which would claim the 99-base intron as covered.
    assert facts["annotation_per_contig"][0]["covered_bases"] == 202


def test_feeding_the_parent_would_overcount():
    # Pins the reason the handler excludes segment-bearing parents from
    # coverage. If this ever stops being true, the exclusion can go.
    rows = list(iter_features(JOIN_FEATURE, accession="NC_1"))

    acc = AnnotationAccumulator(contig_lengths={"NC_1": 1000})
    for row in rows:  # parent included -- the bug
        acc.add(row)

    assert acc.finish()["annotation_per_contig"][0]["covered_bases"] == 301
