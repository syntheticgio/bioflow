"""Turning a file's facts and metadata into a prompt worth answering.

The hard part of this feature is not calling the model, it is deciding what to
put in front of it. A `DataObject`'s `facts` dict is an open bag that has grown
one parser at a time: it holds per-position quality curves, MAPQ histograms,
contig length maps and sampling bookkeeping alongside the handful of numbers a
person actually reads. Passing all of it produces a summary that dutifully
recites array lengths and sampling parameters, which is worse than no summary.

So this module is mostly a *selection* policy. Per format it picks the small set
of measurements that carry the interpretive weight -- the ones a person would
look at to decide "is this file usable, and for what" -- and it names them in
words rather than shipping raw keys, because `qc_before_filtering.q30_rate` is
a key and "93.1% of bases at Q30 or better" is a fact.

Organism gets special handling. It is the single piece of context that turns a
number into a judgement: 41% GC is unremarkable for human and conspicuous for
*Plasmodium*, and only the species says which. When it is known the prompt says
so explicitly and asks for organism-aware interpretation; when it is not, the
prompt says that too, so the model does not invent a species from the filename.
"""

from typing import Any

# Curves, histograms and long lists: real data, but not narrative material.
# Excluded by name so an unrecognized future key still reaches the model rather
# than being silently dropped by a whitelist.
_BULK_KEYS = frozenset(
    {
        "quality_per_position",
        "base_composition",
        "mapq_histogram",
        "insert_size_histogram",
        "reference_lengths",
        "reference_names",
        "coverage_bins",
        "contig_lengths",
    }
)

# Bookkeeping about how the numbers were produced. Useful in the facts table,
# noise in a paragraph about data quality.
_PLUMBING_KEYS = frozenset(
    {
        "stats_sampled_reads",
        "stats_sampled_bases",
        "stats_sampling",
        "sampled_records",
        "reference_names_truncated",
        "sample_names_truncated",
        "has_index",
        "qc_fastqc_report",
        "qc_fastp_report",
        # Retained for MultiQC to parse later, not a number anyone reads.
        "qc_fastp_data",
        "qc_nanoplot_report",
        "trim_report_path",
    }
)


def _pct(value: Any) -> str | None:
    """fastp reports rates as fractions; people read them as percentages."""
    if not isinstance(value, (int, float)):
        return None
    return f"{value * 100:.1f}%"


def _num(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.2f}"


def _line(label: str, value: str | None, unit: str = "") -> str | None:
    if value is None:
        return None
    return f"- {label}: {value}{unit}"


def _short_read_qc(facts: dict) -> list[str]:
    """The fastp/FastQC numbers that decide whether short reads are usable."""
    measured = facts.get("qc_before_filtering")
    if not isinstance(measured, dict):
        return []

    lines = [
        _line("Total reads", _num(measured.get("total_reads"))),
        _line("Total bases", _num(measured.get("total_bases"))),
        # The headline quality figure for short reads, and the one most often
        # quoted in a methods section.
        _line("Bases at Q30 or better", _pct(measured.get("q30_rate"))),
        _line("Bases at Q20 or better", _pct(measured.get("q20_rate"))),
        _line("GC content", _pct(measured.get("gc_content"))),
        _line("Mean read length (R1)", _num(measured.get("read1_mean_length")), " bp"),
        _line("Mean read length (R2)", _num(measured.get("read2_mean_length")), " bp"),
        _line("Duplication rate", _pct(facts.get("qc_duplication_rate"))),
        _line("Insert size peak", _num(facts.get("qc_insert_size_peak")), " bp"),
    ]

    adapters = facts.get("qc_adapters")
    if isinstance(adapters, dict):
        found = [v for v in (adapters.get("read1_sequence"), adapters.get("read2_sequence")) if v]
        if found:
            # The sequence itself is not interesting prose, but its presence is:
            # it means adapter read-through is measurable and trimming is
            # indicated. Say that rather than printing the oligo.
            lines.append(
                f"- Adapter contamination detected ({len(found)} adapter sequence(s) found)"
            )

    return [line for line in lines if line]


def _long_read_qc(facts: dict) -> list[str]:
    """NanoPlot's numbers. Length distribution carries the weight here, not Q30:
    ONT/PacBio quality is expected to be lower, and N50 is the figure that says
    whether an assembly or a structural-variant call is plausible."""
    lines = [
        _line("Total reads", _num(facts.get("qc_total_reads"))),
        _line("Total bases", _num(facts.get("qc_total_bases"))),
        _line("Read length N50", _num(facts.get("qc_read_length_n50")), " bp"),
        _line("Mean read length", _num(facts.get("qc_mean_read_length")), " bp"),
        _line("Median read length", _num(facts.get("qc_median_read_length")), " bp"),
        _line("Read length std. dev.", _num(facts.get("qc_read_length_stdev")), " bp"),
        _line("Mean quality", _num(facts.get("qc_mean_quality")), " (Phred)"),
        _line("Median quality", _num(facts.get("qc_median_quality")), " (Phred)"),
    ]
    chemistry = facts.get("qc_read_chemistry")
    if isinstance(chemistry, str) and chemistry not in ("", "unknown"):
        reason = facts.get("qc_read_chemistry_reason")
        suffix = f" (inferred: {reason})" if isinstance(reason, str) and reason else ""
        lines.append(f"- Read chemistry: {chemistry}{suffix}")
    return [line for line in lines if line]


def _ingest_stats(facts: dict) -> list[str]:
    """What the ingest sampler measured, for a file QC has not been run on.

    Explicitly labelled as sampled downstream, because these come from the first
    few thousand records rather than the whole file and a summary that presents
    them as whole-file truth would be wrong.
    """
    lines = [
        _line("GC content", _num(facts.get("gc_content_percent")), "%"),
        _line("Mean quality", _num(facts.get("mean_quality")), " (Phred)"),
        _line("Lowest per-position quality", _num(facts.get("min_position_quality")), " (Phred)"),
        _line("Quality encoding", facts.get("quality_encoding")),
        _line("Read length", _num(facts.get("read_length")), " bp"),
        _line("Shortest read", _num(facts.get("read_length_min")), " bp"),
        _line("Longest read", _num(facts.get("read_length_max")), " bp"),
        _line("Estimated read count", _num(facts.get("read_count_estimate"))),
    ]
    return [line for line in lines if line]


def _alignment_stats(facts: dict) -> list[str]:
    """Mapping rate, pairing and duplication: the three numbers that say whether
    an alignment succeeded, plus the reference it was aligned against."""
    lines = [
        _line("Total reads", _num(facts.get("total_reads"))),
        _line("Mapped reads", _num(facts.get("mapped_reads"))),
        _line(
            "Mapping rate",
            _num(facts.get("mapped_pct") or facts.get("mapped_percent")),
            "%",
        ),
        _line("Properly paired", _num(facts.get("properly_paired_pct")), "%"),
        _line(
            "Duplicates",
            _num(facts.get("duplicate_pct") or facts.get("duplicate_percent")),
            "%",
        ),
        # Only one of these is ever present: STAR's MAPQ codes have no
        # meaningful mean, so ingest reports the uniquely-mapped fraction
        # instead. See STAR_MAPQ_UNIQUE in sequence_stats.
        _line("Mean mapping quality", _num(facts.get("mean_mapping_quality"))),
        _line("Uniquely mapped", _num(facts.get("uniquely_mapped_percent")), "%"),
        _line("Reference sequences", _num(facts.get("reference_count"))),
        _line("Reference total length", _num(facts.get("reference_total_length")), " bp"),
        _line("Sort order", facts.get("sort_order")),
        _line("Aligned by", facts.get("aligned_by")),
    ]
    samples = facts.get("sample_names")
    if isinstance(samples, list) and samples:
        lines.append(f"- Samples: {', '.join(str(s) for s in samples[:5])}")
    platforms = facts.get("platforms")
    if isinstance(platforms, list) and platforms:
        lines.append(f"- Sequencing platform: {', '.join(str(p) for p in platforms[:3])}")
    return [line for line in lines if line]


def _variant_stats(facts: dict) -> list[str]:
    lines = [
        _line("Variant records sampled", _num(facts.get("sampled_records"))),
        _line("Samples in file", _num(facts.get("sample_count"))),
        _line("Contigs", _num(facts.get("reference_count"))),
        _line("VCF version", facts.get("vcf_version")),
        _line("Called by", facts.get("called_by")),
    ]
    types = facts.get("variant_types_sampled")
    if isinstance(types, list) and types:
        lines.append(f"- Variant types present: {', '.join(str(t) for t in types)}")
    filters = facts.get("filters")
    if isinstance(filters, list) and filters:
        lines.append(f"- FILTER values defined: {', '.join(str(f) for f in filters[:8])}")
    samples = facts.get("sample_names")
    if isinstance(samples, list) and samples:
        lines.append(f"- Sample names: {', '.join(str(s) for s in samples[:5])}")
    return [line for line in lines if line]


def _reference_stats(facts: dict) -> list[str]:
    lines = [
        _line("Sequences", _num(facts.get("sequence_count") or facts.get("reference_count"))),
        _line(
            "Total length",
            _num(facts.get("total_length") or facts.get("reference_total_length")),
            " bp",
        ),
        _line("GC content", _num(facts.get("gc_content_percent")), "%"),
        _line("N50", _num(facts.get("n50")), " bp"),
        _line("Longest sequence", _num(facts.get("longest_sequence")), " bp"),
        _line("Assembly level", facts.get("assembly_level")),
        _line("Assembly name", facts.get("assembly_name") or facts.get("ncbi_assembly_name")),
    ]
    return [line for line in lines if line]


def _trim_provenance(facts: dict) -> list[str]:
    """What trimming did, when this file is trimming output. A summary of a
    trimmed file that does not mention it was trimmed is misleading."""
    if not facts.get("trimmed_by"):
        return []
    lines = [
        _line("Trimmed with", facts.get("trimmed_by")),
        _line("Reads before trimming", _num(facts.get("trim_reads_before"))),
        _line("Reads after trimming", _num(facts.get("trim_reads_after"))),
        _line("Reads passing filter", _pct(facts.get("trim_pass_rate"))),
        _line("Q30 after trimming", _pct(facts.get("trim_q30_after"))),
    ]
    return [line for line in lines if line]


def _metadata_lines(metadata: dict) -> list[str]:
    """User and NCBI metadata, minus the keys that are pure plumbing.

    Kept generic rather than whitelisted: this dict is schema-driven and
    user-extensible, so the interesting keys are exactly the ones this module
    cannot know about ahead of time.
    """
    skip = {"organism"}  # stated separately and more prominently
    lines = []
    for key, value in sorted(metadata.items()):
        if key in skip or value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value[:6])
        if isinstance(value, dict):
            continue
        text = str(value)
        if len(text) > 200:
            text = text[:200] + "..."
        label = key.replace("_", " ").capitalize()
        lines.append(f"- {label}: {text}")
    return lines[:25]


def _residual_facts(facts: dict, already_used: set[str]) -> list[str]:
    """Scalar facts no format-specific section claimed.

    The safety net for the open-dict problem: a parser that starts writing a new
    key gets it into the prompt without anyone remembering to update this file.
    Scalars only, and capped, so the net cannot drag in the bulk data above.
    """
    lines = []
    for key, value in sorted(facts.items()):
        if key in already_used or key in _BULK_KEYS or key in _PLUMBING_KEYS:
            continue
        if key.startswith(("qc_", "trim_")):
            continue  # handled by the QC/trim sections or deliberately dropped
        if isinstance(value, bool):
            continue
        if not isinstance(value, (str, int, float)):
            continue
        text = str(value)
        if len(text) > 120:
            continue
        label = key.replace("_", " ").capitalize()
        lines.append(f"- {label}: {text}")
    return lines[:12]


# Keys the format-specific sections above consume. Used only to keep the
# residual net from repeating them.
_CLAIMED = frozenset(
    {
        "qc_before_filtering", "qc_duplication_rate", "qc_insert_size_peak", "qc_adapters",
        "qc_total_reads", "qc_total_bases", "qc_read_length_n50", "qc_mean_read_length",
        "qc_median_read_length", "qc_read_length_stdev", "qc_mean_quality", "qc_median_quality",
        "qc_read_chemistry", "qc_read_chemistry_reason", "qc_tool", "qc_tool_version",
        "gc_content_percent", "mean_quality", "min_position_quality", "quality_encoding",
        "read_length", "read_length_min", "read_length_max", "read_count_estimate",
        "total_reads", "mapped_reads", "mapped_pct", "mapped_percent", "properly_paired_pct",
        "duplicate_pct", "duplicate_percent", "mean_mapping_quality",
        "uniquely_mapped_percent", "mapq_scale", "reference_count",
        "reference_total_length", "sort_order", "aligned_by", "sample_names", "platforms",
        "sampled_records", "sample_count", "vcf_version", "called_by", "variant_types_sampled",
        "filters", "sequence_count", "total_length", "n50", "longest_sequence",
        "assembly_level", "assembly_name", "ncbi_assembly_name", "trimmed_by",
        "trim_reads_before", "trim_reads_after", "trim_pass_rate", "trim_q30_after",
    }
)


SYSTEM_PROMPT = (
    "You are a bioinformatics core facility analyst writing a short note about a "
    "sequencing data file for the scientist who owns it.\n\n"
    "Write 2-4 sentences of plain prose. No headings, no bullet points, no "
    "markdown, no preamble such as 'Here is a summary'. Start directly with the "
    "substance.\n\n"
    "What to focus on, in order of importance:\n"
    "1. Whether the data looks usable, and for what kind of analysis.\n"
    "2. Anything a scientist would want flagged: low quality scores, adapter "
    "contamination, high duplication, unexpected GC content, a low mapping rate, "
    "unusually short reads, or a read count too small for the intended use.\n"
    "3. Interpretation specific to the organism, when one is given -- whether GC "
    "content, genome size or read counts are in line with what that species "
    "would be expected to show.\n\n"
    "Rules you must follow:\n"
    "- Only use the numbers given to you. Never invent a measurement, a species, "
    "a platform or a conclusion the data does not support.\n"
    "- If a value is described as sampled or estimated, do not present it as a "
    "whole-file measurement.\n"
    "- If the data looks unremarkable, say so plainly and briefly. Do not "
    "manufacture concerns to fill space.\n"
    "- Do not recommend specific software or command lines.\n"
    "- Do not restate every number. Cite only the few that carry your point."
)


# The organism blurb is a different kind of request from the file summary above,
# and the difference is worth being explicit about. The file summary restates
# measurements it was handed, so its failure mode is bounded: it can misread
# numbers, but the numbers are there. This asks the model to recall facts about
# a species from its own weights, which it can simply get wrong -- and a small
# local model is exactly where that is most likely.
#
# Two things follow. The prompt pushes hard toward the stable, textbook-level
# facts a model is least likely to confabulate (what the organism is, roughly
# how big its genome is, why anyone sequences it) and away from the precise
# figures it is most likely to invent. And the UI labels the result as
# background rather than data -- see the `AiSummary` note about page colour.
ORGANISM_SYSTEM_PROMPT = (
    "You write brief, factual background notes about organisms for a "
    "bioinformatics application. Your reader is a scientist who is looking at a "
    "sequencing file from this organism.\n\n"
    "Write 2-3 sentences of plain prose. No headings, no bullet points, no "
    "markdown, no preamble such as 'Here is a summary'. Start directly with the "
    "substance.\n\n"
    "Cover, in whatever order reads best:\n"
    "- What kind of organism it is, in everyday terms: a bacterium, a yeast, a "
    "parasitic protozoan, a flowering plant, a mammal.\n"
    "- Why it matters -- as a model organism, a pathogen, an industrial "
    "workhorse, an agricultural species, or whatever it is actually known for.\n"
    "- One genuinely interesting or memorable fact about it.\n\n"
    "Rules you must follow:\n"
    "- Stick to well-established, textbook-level facts. This is background "
    "colour, not a reference a decision will be made from.\n"
    "- Do not state precise statistics -- exact genome sizes, chromosome "
    "counts, gene counts or discovery dates -- unless you are certain. An "
    "approximation in words ('a compact genome', 'a few thousand genes') is "
    "always better than a precise number that might be wrong.\n"
    "- Do NOT name formal taxonomic ranks -- no kingdom, phylum, class, order "
    "or family names, and no Latin clade names. Describing an organism in "
    "ordinary language ('a single-celled fungus', 'a soil bacterium') is "
    "always correct; naming the wrong order is a plain factual error and the "
    "single easiest mistake to make here.\n"
    "- Never invent a discoverer, a date, or a claim to fame.\n"
    "- Be careful with 'single-celled'. It is true of bacteria, yeasts and "
    "protozoa, and false of every animal and plant -- worms, flies, fish and "
    "weeds are all many-celled, however small or simple they are. If you are "
    "not sure, just name the kind of organism and leave cellularity out.\n"
    "- If you do not recognize the organism, say only what its name implies "
    "and keep it to one sentence. Do not fill the space with invention.\n"
    "- Write about the organism itself. Say nothing about sequencing files, "
    "data quality, or the reader's analysis."
)


def build_organism_prompt(organism: str) -> str:
    """The user turn for an organism blurb.

    Trivially small on purpose: the species name is the entire input, and the
    system prompt carries all of the shaping.
    """
    return (
        f"Write the background note for this organism: {organism}\n\n"
        "Follow every rule in your instructions."
    )


def build_user_prompt(
    *,
    name: str,
    format_kind: str,
    role: str | None,
    organism: str | None,
    facts: dict,
    metadata: dict,
) -> str | None:
    """Assemble the prompt, or None when there is nothing worth summarizing.

    Returning None matters: a file whose facts amount to "it is 4 GB and gzipped"
    has no narrative in it, and asking anyway produces confident filler. The
    threshold is deliberately low but non-zero.
    """
    sections: list[str] = []

    header = [f"File name: {name}", f"Format: {format_kind}"]
    if role:
        header.append(f"Role: {role}")
    if organism:
        header.append(f"Organism: {organism}")
    else:
        header.append(
            "Organism: not recorded -- do not guess the species, and do not make "
            "claims that depend on knowing it."
        )
    sections.append("\n".join(header))

    # QC first when present: it is whole-file and authoritative, unlike the
    # ingest sampler's numbers.
    qc_lines = _short_read_qc(facts) or _long_read_qc(facts)
    if qc_lines:
        tool = facts.get("qc_tool")
        label = (
            f"Quality control measurements (whole file, via {tool})"
            if tool
            else "Quality control measurements"
        )
        sections.append(f"{label}:\n" + "\n".join(qc_lines))

    if format_kind in ("fastq", "fasta") and (ingest := _ingest_stats(facts)):
        # Named as sampled every time, since the model cannot otherwise tell
        # these apart from the QC block above.
        sections.append(
            "Statistics from a sample of the first records at ingest (not the "
            "whole file):\n" + "\n".join(ingest)
        )

    if role == "reference" or format_kind == "fasta":
        if ref := _reference_stats(facts):
            sections.append("Reference sequence statistics:\n" + "\n".join(ref))

    if format_kind in ("bam", "sam", "cram"):
        if aln := _alignment_stats(facts):
            sections.append("Alignment statistics:\n" + "\n".join(aln))

    if format_kind in ("vcf", "bcf"):
        if var := _variant_stats(facts):
            sections.append("Variant call statistics:\n" + "\n".join(var))

    if trim := _trim_provenance(facts):
        sections.append("Trimming provenance:\n" + "\n".join(trim))

    if meta := _metadata_lines(metadata):
        sections.append("Sample and study metadata:\n" + "\n".join(meta))

    if residual := _residual_facts(facts, set(_CLAIMED)):
        sections.append("Other recorded facts:\n" + "\n".join(residual))

    # The header alone is not a summary's worth of input.
    if len(sections) < 2:
        return None

    sections.append(
        "Write the note now, following every rule in your instructions."
    )
    return "\n\n".join(sections)
