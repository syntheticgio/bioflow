"""Which reads are eligible to polish with, and how they group.

The central case here came from the real database rather than from
reasoning: `ERR16145610.fastq` is a MinION run whose `qc_read_chemistry` is
`short`. The chemistry inference reads read *lengths*, so a nanopore run
carrying short reads infers short -- true about the reads, false about the
data. The obvious rule (`chemistry == "short"`) would let ONT reads through
a short-read polisher, which does not error and quietly degrades the
assembly. Platform has to win over chemistry, and that is what most of these
tests pin down.
"""

from types import SimpleNamespace

from beanie import PydanticObjectId

from app.models import FormatKind, ObjectStatus
from app.services import reference_assembly


def _fastq(
    *,
    name="reads.fastq",
    kind=FormatKind.FASTQ,
    status=ObjectStatus.READY,
    chemistry=None,
    sra_platform=None,
    instrument=None,
    mate=None,
    read_number=None,
    oid=None,
):
    facts = {}
    if chemistry:
        facts["qc_read_chemistry"] = chemistry
    if sra_platform:
        facts["sra_platform"] = sra_platform
    if read_number:
        facts["read_number"] = read_number
    return SimpleNamespace(
        id=oid or PydanticObjectId(),
        name=name,
        format=SimpleNamespace(kind=kind),
        status=status,
        facts=facts,
        metadata={"platform": instrument} if instrument else {},
        mate_object_id=mate,
    )


def _split_fastq(*, platform=None, instrument_model=None, chemistry=None):
    """A post-#525 object: platform is an SRA tag, instrument_model is the
    machine name. `_fastq` above still builds the pre-split shape, on
    purpose -- both must keep working, since a migration cannot reach an
    object that was never in the database."""
    metadata = {}
    if platform:
        metadata["platform"] = platform
    if instrument_model:
        metadata["instrument_model"] = instrument_model
    facts = {"qc_read_chemistry": chemistry} if chemistry else {}
    return SimpleNamespace(
        id=PydanticObjectId(),
        name="reads.fastq",
        format=SimpleNamespace(kind=FormatKind.FASTQ),
        status=ObjectStatus.READY,
        facts=facts,
        metadata=metadata,
        mate_object_id=None,
    )


class TestIsShortReadAfterTheSplit:
    """#525 moved instrument models to their own field. Every case in
    `TestIsShortRead` must reach the same verdict from the new shape."""

    def test_instrument_model_still_resolves_the_platform(self):
        assert reference_assembly.is_short_read(
            _split_fastq(instrument_model="Illumina NovaSeq X Plus")
        )

    def test_nanopore_instrument_model_is_not_short(self):
        assert (
            reference_assembly.is_short_read(
                _split_fastq(instrument_model="MinION", chemistry="short")
            )
            is False
        )

    def test_the_platform_tag_alone_is_enough(self):
        """The point of the split: a closed tag answers directly, with no
        substring inference over a machine name."""
        assert (
            reference_assembly.is_short_read(
                _split_fastq(platform="OXFORD_NANOPORE")
            )
            is False
        )
        assert reference_assembly.is_short_read(_split_fastq(platform="ILLUMINA"))

    def test_platform_tag_beats_a_disagreeing_instrument_model(self):
        """`platform` is the typed, closed field; the model is free text
        that may be a vendor's marketing name. The closed one wins."""
        assert (
            reference_assembly.is_short_read(
                _split_fastq(platform="OXFORD_NANOPORE", instrument_model="NextSeq 550")
            )
            is False
        )

    def test_platform_before_chemistry_precedence_holds(self):
        """The regression this whole path exists to prevent, asserted
        directly on the new shape: chemistry says short, platform says ONT,
        and platform must win."""
        obj = _split_fastq(
            platform="OXFORD_NANOPORE",
            instrument_model="MinION",
            chemistry="short",
        )
        assert obj.facts["qc_read_chemistry"] == "short"
        assert reference_assembly.is_short_read(obj) is False


class TestIsShortRead:
    def test_illumina_instrument_model_is_short(self):
        """`metadata.platform` holds an instrument model, not a platform
        name -- "Illumina NovaSeq X Plus", not "ILLUMINA"."""
        assert reference_assembly.is_short_read(
            _fastq(instrument="Illumina NovaSeq X Plus")
        )

    def test_nanopore_is_not_short_even_when_chemistry_says_short(self):
        """The real-data case. A MinION run whose inferred chemistry is
        `short` must not be offered to a short-read polisher."""
        obj = _fastq(
            name="ERR16145610.fastq", instrument="MinION", chemistry="short"
        )
        assert reference_assembly.is_short_read(obj) is False

    def test_sra_platform_beats_the_instrument_model(self):
        obj = _fastq(instrument="MinION", sra_platform="OXFORD_NANOPORE")
        assert reference_assembly.is_short_read(obj) is False

    def test_pacbio_is_not_short(self):
        assert (
            reference_assembly.is_short_read(
                _fastq(instrument="PacBio RS", chemistry="hifi")
            )
            is False
        )

    def test_unknown_platform_falls_back_to_chemistry(self):
        assert reference_assembly.is_short_read(
            _fastq(sra_platform="SOMETHING_NEW", chemistry="short")
        )

    def test_no_metadata_at_all_counts_as_short(self):
        """Pins the ILLUMINA default rather than working around it.

        `_qc_platform` defaults to ILLUMINA when nothing is recorded, which
        is what makes an uploaded Illumina FASTQ -- typically carrying no
        metadata -- eligible at all. This test exists so that changing that
        default is a deliberate act with a visible consequence here.
        """
        assert reference_assembly.is_short_read(_fastq()) is True

    def test_a_recognised_but_unlisted_platform_is_not_short(self):
        """An explicitly recorded platform this module does not know is
        excluded, unlike a *missing* one. Recorded-and-unrecognised is
        evidence; absent is not."""
        obj = _fastq(sra_platform="SOMETHING_NEW")
        assert reference_assembly.is_short_read(obj) is False

    def test_a_fasta_is_never_short_reads(self):
        assert (
            reference_assembly.is_short_read(
                _fastq(kind=FormatKind.FASTA, instrument="Illumina NovaSeq")
            )
            is False
        )


class TestGrouping:
    def test_mate_linked_files_become_one_set(self):
        a_id, b_id = PydanticObjectId(), PydanticObjectId()
        a = _fastq(name="r_1.fastq", oid=a_id, mate=b_id, read_number=1)
        b = _fastq(name="r_2.fastq", oid=b_id, mate=a_id, read_number=2)
        sets = reference_assembly.group_read_sets([a, b])
        assert len(sets) == 1
        assert [o.name for o in sets[0]] == ["r_1.fastq", "r_2.fastq"]

    def test_pair_order_is_r1_first_regardless_of_input_order(self):
        a_id, b_id = PydanticObjectId(), PydanticObjectId()
        a = _fastq(name="r_1.fastq", oid=a_id, mate=b_id, read_number=1)
        b = _fastq(name="r_2.fastq", oid=b_id, mate=a_id, read_number=2)
        sets = reference_assembly.group_read_sets([b, a])
        assert [o.name for o in sets[0]] == ["r_1.fastq", "r_2.fastq"]

    def test_unpaired_files_are_separate_sets(self):
        sets = reference_assembly.group_read_sets(
            [_fastq(name="a.fastq"), _fastq(name="b.fastq")]
        )
        assert len(sets) == 2

    def test_a_mate_pointing_outside_the_list_is_a_singleton(self):
        """Half a pair -- the other file deleted, or filtered out for being
        long-read -- must not become a pair with itself."""
        obj = _fastq(name="r_1.fastq", mate=PydanticObjectId())
        sets = reference_assembly.group_read_sets([obj])
        assert sets == [[obj]]


class TestShortReadSets:
    def test_excludes_unready_objects(self):
        objs = [_fastq(instrument="Illumina NovaSeq", status=ObjectStatus.INGESTING)]
        assert reference_assembly.short_read_sets(objs) == []

    def test_the_real_yeast_project_shape_yields_exactly_one_set(self):
        """The shape of the real Saccharomyces project on 2026-08-05: one
        Illumina pair, one MinION run, one PacBio run, plus trimmed copies.

        One eligible set is what lets the polish card offer an unambiguous
        launch. If the MinION file leaked through on its `short` chemistry
        there would be three, and the card would refuse for the wrong
        reason -- which is a subtler failure than polishing with it.
        """
        a_id, b_id = PydanticObjectId(), PydanticObjectId()
        objs = [
            _fastq(name="ERR17609896_1.fastq", oid=a_id, mate=b_id, read_number=1,
                   instrument="Illumina NovaSeq X Plus", chemistry="short"),
            _fastq(name="ERR17609896_2.fastq", oid=b_id, mate=a_id, read_number=2,
                   instrument="Illumina NovaSeq X Plus", chemistry="short"),
            _fastq(name="ERR16145610.fastq", instrument="MinION", chemistry="short"),
            _fastq(name="SRR39891651.fastq", instrument="PacBio RS", chemistry="hifi"),
            _fastq(name="ERR16145610.trimmed.fastq", instrument="MinION",
                   chemistry="short"),
        ]
        sets = reference_assembly.short_read_sets(objs)
        assert len(sets) == 1
        assert [o.name for o in sets[0]] == [
            "ERR17609896_1.fastq",
            "ERR17609896_2.fastq",
        ]
