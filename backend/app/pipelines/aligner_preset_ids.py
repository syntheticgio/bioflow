"""Preset identifiers shared by parameter models and the aligner registry.

This module deliberately has no pipeline imports. Parameter validation and
registry metadata both depend on these vocabularies, so putting the IDs here
keeps either side from importing the other and creating a cycle.
"""

BOWTIE2_STANDARD_SHORT_READ = "standard_short_read"
BOWTIE2_LONG_INSERT = "long_insert"
BOWTIE2_MATE_PAIR = "mate_pair"
BOWTIE2_ADAPTER_PARTIAL_REFERENCE = "adapter_partial_reference"
BOWTIE2_STRUCTURAL_VARIANT = "structural_variant"
BOWTIE2_REPEAT_MULTIMAPPING = "repeat_multimapping"

BOWTIE2_PRESET_IDS: tuple[str, ...] = (
    BOWTIE2_STANDARD_SHORT_READ,
    BOWTIE2_LONG_INSERT,
    BOWTIE2_MATE_PAIR,
    BOWTIE2_ADAPTER_PARTIAL_REFERENCE,
    BOWTIE2_STRUCTURAL_VARIANT,
    BOWTIE2_REPEAT_MULTIMAPPING,
)
