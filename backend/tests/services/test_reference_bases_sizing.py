"""The base count the memory estimator is sized from.

`reference_bases` was `reference.size` -- the stored file size. The comment
justifying it said a FASTA carries "about one byte per base plus headers and
newlines", which is true of an *uncompressed* FASTA and wrong by roughly 6x for
the gzipped ones this app downloads from NCBI by default.

Found against the real library (issue #100): the reference from #96 is stored
at 143.6 MB compressed with `facts.total_bases = 448,565,924`. Sizing from
`size` therefore fed the estimator a base count ~6x under the truth, which is
what let an index build be admitted into a budget that could not hold it --
compounding the separately-wrong build coefficient.

`total_bases` is a measured property of *this file*, written by the FASTA
parser. That makes it the right input here, and distinguishes this from
`_infer_genome_size`'s deliberate refusal to use it: that helper picks an
arbitrary candidate object to guess an assembly's size, where a `protein.faa`
roled `reference` would poison the answer. Here the object is not a candidate
but the exact file about to be indexed.
"""

import pytest
from app.services import pipeline_service


class _Ref:
    """Minimal stand-in for a DataObject: only what the sizing helper reads."""

    def __init__(self, size, facts=None):
        self.size = size
        self.facts = facts


def test_measured_base_count_is_preferred_over_file_size():
    """The #96 reference: 143.6 MB on disk, 448.6 Mbp of sequence."""
    ref = _Ref(size=143_556_826, facts={"total_bases": 448_565_924})
    assert pipeline_service.reference_bases_for(ref) == 448_565_924


def test_file_size_is_the_fallback_when_nothing_measured_the_bases():
    """A hand-uploaded uncompressed FASTA with no parsed facts.

    Roughly one byte per base, so this stays the sane approximation it always
    was for that case -- the bug was applying it to compressed files too.
    """
    ref = _Ref(size=12_345_678, facts={})
    assert pipeline_service.reference_bases_for(ref) == 12_345_678


def test_a_compressed_reference_is_never_sized_below_its_file_size():
    """The guard that actually stops the #96 failure.

    If `total_bases` is missing for a compressed reference, falling back to the
    compressed byte count understates the genome. Anything that cannot measure
    the sequence must not report a number *below* the bytes on disk.
    """
    ref = _Ref(size=143_556_826, facts=None)
    assert pipeline_service.reference_bases_for(ref) >= 143_556_826


def test_missing_size_and_facts_is_zero_not_an_error():
    """Callers use `or 0` today and rely on the floor further down."""
    assert pipeline_service.reference_bases_for(_Ref(size=None, facts=None)) == 0


@pytest.mark.parametrize("bad", [0, None, -1])
def test_an_unusable_total_bases_falls_back_to_file_size(bad):
    """A zero or absent measurement is not a claim that the genome is empty."""
    ref = _Ref(size=5_000_000, facts={"total_bases": bad})
    assert pipeline_service.reference_bases_for(ref) == 5_000_000
