"""Turning a ProvenanceChain into a prompt, and checking what comes back.

Four layers stop fabrication here. Three are structural and one depends on
the model behaving:

1. The model gets no database access and no free text -- only this chain,
   the same object the structured report rendered. There is no path by which
   it learns a fact the report does not already show.
2. Gaps are supplied as content to state, not blanks to fill.
3. `verify_containment` rejects output introducing an unsupported version or
   tool name. Deterministic, and it fails closed.
4. The system prompt constrains the task to rephrasing.

Layer 3's limit, stated plainly because someone will otherwise assume it is
total: it catches invented versions and tool names, which is the high-cost
class. It does not catch an invented causal claim -- "trimmed to remove
adapter contamination" when nothing measured contamination. That is mitigated
by layer 4 and by the structured report being the citable artifact, not
eliminated.
"""

import re

from app.services.provenance_report import render_markdown
from app.services.provenance_walker import ProvenanceChain

SYSTEM_PROMPT = """\
You rewrite a structured record of how a bioinformatics file was produced \
into a short methods paragraph suitable for a scientific manuscript.

Rules, in order of importance:

1. Use ONLY the facts given below. Do not add a tool, a version, a parameter, \
a step, or a purpose that is not in the record.
2. Where the record says a fact was not recorded, say so in the paragraph. \
Do not infer, guess, or substitute a plausible value. "aligned with bwa-mem2 \
(version not recorded)" is correct and expected.
3. Do not interpret results or claim significance. You are describing what \
was run, not what it showed.
4. Keep the order of steps exactly as given.
5. Write plainly, in past tense, one paragraph.
"""

# A version claim: two or more dot-separated numeric components, with an
# optional leading "v" -- the conventional way bioinformatics tools are cited
# in prose ("bwa-mem2 v2.2.1"). The "v" is matched but not captured: group(1)
# is the bare numeric portion, which is what `supported_tokens` actually
# stores, so a "v"-prefixed fabrication is still checked against the same
# supported set as a bare one rather than silently failing to match at all.
# ("v" and a digit are both word characters, so a plain `\b` before the "v"
# would not have kept "v2.2.1" from matching -- it was the missing "v?" that
# left it unmatched, not the boundary.)
#
# Deliberately not `\d+` alone -- a year, a thread count and a sample size
# are ordinary numbers, and rejecting those would make the check unusable.
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+[\w.\-]*)", re.IGNORECASE)


def build_prompt(chain: ProvenanceChain) -> tuple[str, str]:
    """System and user prompts for one chain.

    The user prompt is the rendered markdown report verbatim. That is
    deliberate: the model sees exactly what the user sees, so anything it
    states is checkable against the same text.
    """
    return SYSTEM_PROMPT, render_markdown(chain)


def supported_tokens(chain: ProvenanceChain) -> set[str]:
    """Every tool name and version the chain actually recorded."""
    tokens: set[str] = set()
    for node in chain.nodes.values():
        step = node.produced_by
        if step is None:
            continue
        if step.tool:
            tokens.add(step.tool.lower())
        if step.tool_version:
            tokens.add(step.tool_version.lower())
        for value in step.params.values():
            tokens.add(str(value).lower())
    return tokens


def verify_containment(prose: str, chain: ProvenanceChain) -> str | None:
    """Why this prose must be rejected, or None if it is safe to show.

    Fails closed: an unrecognized version-shaped token is a rejection, not a
    warning. A methods paragraph carrying one invented version is worse than
    no paragraph at all.
    """
    supported = supported_tokens(chain)

    for match in _VERSION_RE.finditer(prose):
        numeric = match.group(1)
        if numeric.lower().rstrip(".,;)") not in supported:
            return f"unsupported version token: {match.group(0)}"

    lowered = prose.lower()
    known_tools = {
        node.produced_by.tool.lower()
        for node in chain.nodes.values()
        if node.produced_by is not None and node.produced_by.tool
    }
    # A tool named in the prose that the chain never recorded. Checked
    # against a fixed vocabulary rather than by parsing prose, because
    # extracting "the tool" from a sentence is exactly the kind of guess this
    # module exists to avoid.
    #
    # Matched on a word boundary rather than substring containment: "bwa" is
    # a substring of the recorded tool "bwa-mem2", and substring containment
    # would flag a supported tool's own name as unsupported. `\b` treats `-`
    # as a boundary (it is not a word character), so "bwa" inside
    # "bwa-mem2" still matches the pattern -- the guard is the `in
    # known_tools` check on the *candidate itself* below, together with a
    # negative lookahead/lookbehind so "bwa" does not fire when it is really
    # part of the longer, supported "bwa-mem2".
    for candidate in _COMMON_TOOLS:
        if candidate in known_tools:
            continue
        pattern = r"(?<![\w-])" + re.escape(candidate) + r"(?![\w-])"
        if re.search(pattern, lowered):
            return f"unsupported tool name: {candidate}"

    return None


# Tools this app knows about, used only to catch a model naming one that did
# not run. Missing a name means a miss, never a false rejection -- which is
# the right direction for a list nobody can keep exhaustive.
_COMMON_TOOLS = frozenset(
    {
        "bwa-mem2", "bwa", "minimap2", "bowtie2", "star", "dragmap", "hisat2",
        "fastp", "cutadapt", "trimmomatic", "fastqc", "nanoplot",
        "clair3", "deepvariant", "bcftools", "freebayes", "gatk",
        "flye", "spades", "hifiasm", "canu", "raven",
        "featurecounts", "salmon", "kallisto", "htseq",
        "samtools", "polypolish", "racon", "medaka", "ragtag", "quast",
    }
)
