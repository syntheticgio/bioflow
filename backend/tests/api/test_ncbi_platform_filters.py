"""Pins the SRA platform filter vocabulary the download dialog offers.

This is NCBI's SRA PLATFORM vocabulary, not the SAM PL one -- OXFORD_NANOPORE
rather than ONT, PACBIO_SMRT rather than PACBIO. The two are different
standards and `SamPlatform` must not be used here.

The frontend keeps its own copy in NcbiDownloadDialog.tsx's PLATFORM_FILTERS,
because this repo has no frontend test infrastructure and expects none. This
test pins the backend side so the copy has a source of truth to be checked
against by a reader.
"""

from app.api.v1.ncbi import SRA_PLATFORM_FILTERS


def test_sra_platform_filters_are_the_three_ncbi_tags():
    """Matches NcbiDownloadDialog.tsx's PLATFORM_FILTERS values (minus its
    empty "Any platform" entry, which is a UI affordance rather than a tag).
    """
    assert SRA_PLATFORM_FILTERS == frozenset(
        {"ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE"}
    )


def test_every_filter_is_a_sequencing_platform_member():
    """Every SRA_PLATFORM_FILTERS tag is a valid SequencingPlatform value.

    Adding a new filter for a platform NCBI supports means it must also
    exist in the SequencingPlatform enum, or the enum is incomplete.
    """
    from app.models.object import SequencingPlatform

    for tag in SRA_PLATFORM_FILTERS:
        assert tag in SequencingPlatform._value2member_map_, tag
