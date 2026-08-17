"""Prompt construction and the containment check.

The containment check is the only anti-fabrication layer that does not
depend on the model behaving, so it gets the most tests. It catches invented
versions and tool names -- the high-cost class. It does NOT catch invented
causal claims ("to remove adapter contamination"), and these tests do not
pretend otherwise.
"""

from app.services.provenance_prompt import (
    build_prompt,
    supported_tokens,
    verify_containment,
)
from app.services.provenance_walker import (
    Gap,
    GapKind,
    Node,
    ProvenanceChain,
    Step,
)
from beanie import PydanticObjectId

BAM = PydanticObjectId()


def _chain_with(step, gaps=()):
    node = Node(
        object_id=BAM,
        name="aligned.bam",
        role=None,
        kind="spine",
        produced_by=step,
        parents=(),
    )
    return ProvenanceChain(
        target=node,
        nodes={BAM: node},
        order=(BAM,),
        gaps=tuple(gaps),
    )


def _align_step(version="2.2.1"):
    return Step(
        job_type="align_reads",
        verb="aligned with",
        tool="bwa-mem2",
        tool_version=version,
    )


def test_prompt_contains_the_facts():
    system, user = build_prompt(_chain_with(_align_step()))
    assert "bwa-mem2" in user
    assert "2.2.1" in user


def test_prompt_forbids_inventing_facts():
    system, _ = build_prompt(_chain_with(_align_step()))
    lowered = system.lower()
    assert "do not" in lowered
    assert "invent" in lowered or "infer" in lowered


def test_gaps_are_supplied_as_content_to_state():
    """The model is told 'version not recorded' as a fact to repeat, not as
    a blank to fill."""
    chain = _chain_with(
        _align_step(version=None),
        gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
    )
    _, user = build_prompt(chain)
    assert "version not recorded" in user


def test_supported_tokens_include_tools_and_versions():
    tokens = supported_tokens(_chain_with(_align_step()))
    assert "bwa-mem2" in tokens
    assert "2.2.1" in tokens


def test_containment_accepts_prose_using_only_supported_facts():
    chain = _chain_with(_align_step())
    prose = "Reads were aligned with bwa-mem2 2.2.1."
    assert verify_containment(prose, chain) is None


def test_containment_rejects_an_invented_version():
    """The failure this whole feature exists to prevent."""
    chain = _chain_with(_align_step())
    prose = "Reads were aligned with bwa-mem2 2.2.9."
    reason = verify_containment(prose, chain)
    assert reason is not None
    assert "2.2.9" in reason


def test_containment_rejects_an_invented_version_with_leading_v():
    """The conventional bioinformatics citation style ("tool vX.Y.Z") must
    not be a bypass. A regex anchored on a bare digit would never even see
    a "v"-prefixed token, letting a fabricated version through unchecked."""
    chain = _chain_with(_align_step())
    prose = "Reads were aligned with bwa-mem2 v2.2.9."
    reason = verify_containment(prose, chain)
    assert reason is not None
    assert "2.2.9" in reason


def test_containment_rejects_an_invented_tool():
    chain = _chain_with(_align_step())
    prose = "Reads were aligned with bowtie2 2.2.1."
    assert verify_containment(prose, chain) is not None


def test_containment_rejects_a_version_filled_into_a_gap():
    chain = _chain_with(
        _align_step(version=None),
        gaps=(Gap(kind=GapKind.VERSION_UNRECORDED, object_id=BAM),),
    )
    prose = "Reads were aligned with bwa-mem2 0.7.17."
    assert verify_containment(prose, chain) is not None


def test_containment_allows_ordinary_numbers():
    """A year or a count is not a version claim; rejecting those would make
    the check unusable."""
    chain = _chain_with(_align_step())
    prose = "In 2026, reads were aligned with bwa-mem2 2.2.1 across 24 samples."
    assert verify_containment(prose, chain) is None
