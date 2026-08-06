"""CRAQ command construction and output parsing.

Same split `quast_runner` and `ragtag_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Two things about CRAQ's output shape drive this module, both read from
upstream's own source (`src/format_results_addAQI.pl`,
`src/runAQI_NGS.sh`) rather than inferred from the README:

- **The final report is `<genome basename>_final.Report`**, not
  `out_final.Report` as the README's prose implies -- that naming applies
  to the sibling `.bed` files. The handler links the assembly under a fixed
  name, so the prefix is predictable.
- **A short-read-only run still prints CSE, S-AQI and AQI columns**, and
  they are meaningless: upstream states CSE is "hardly detected" without
  long reads, but `runAQI_NGS.sh` pipes through the same formatter, which
  unconditionally prints all eight columns and computes AQI as a harmonic
  mean of both halves. Reading the file faithfully therefore *produces* a
  clean, wrong `0.000`. `parse_final_report` takes `has_sms`/`has_ngs` and
  drops the fields the inputs cannot support, so the omission is enforced
  here rather than left to every caller to remember.
"""

import re
from pathlib import Path

# `Avg.CRE(R-AQI)` packs two numbers into one column, e.g. "0.512(94.881)".
# Same shape the upstream formatter matches with /(\S+)\((\S+)\)/.
_PAIRED_FIELD = re.compile(r"^(?P<value>[^()]+)\((?P<aqi>[^()]+)\)$")


def build_craq_command(
    *,
    craq_path: str,
    assembly: Path,
    ngs_bam: Path | None,
    sms_bam: Path | None,
    out_dir: Path,
    threads: int,
    mapq: int = 20,
    break_chimera: bool = False,
) -> list[str]:
    """The argv for `craq` against pre-made BAMs.

    At least one BAM is required; CRAQ has nothing to do without one, and
    reaching here with neither is a caller bug rather than a user error --
    the launch path validates first.

    `-x` (the minimap2 preset) is deliberately absent: upstream ignores it
    when a BAM is supplied, and passing it would suggest this code aligns
    anything, which it does not.

    `-pl` is never passed. Plotting needs pycircos, which is not installed,
    and this application serves no CRAQ-generated document.
    """
    if ngs_bam is None and sms_bam is None:
        raise ValueError("CRAQ needs at least one of ngs_bam or sms_bam")

    cmd = [craq_path, "-g", str(assembly)]
    if ngs_bam is not None:
        cmd += ["-ngs", str(ngs_bam)]
    if sms_bam is not None:
        cmd += ["-sms", str(sms_bam)]
    cmd += ["-q", str(mapq), "-t", str(threads), "-D", str(out_dir)]
    if break_chimera:
        cmd += ["-b", "T"]
    return cmd


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def parse_final_report(text: str, *, has_ngs: bool, has_sms: bool) -> dict:
    """Whole-assembly facts from `<genome>_final.Report`.

    Returns `{}` for anything unreadable rather than raising -- the posture
    `quast_runner.parse_report_tsv` documents: a summary that cannot be read
    must not fail a run that already produced real output.

    Only the `Genome` row (the whole-assembly aggregate) is stored -- not
    `all`, which never appears in a real report. Verified against a real
    1.10 run on 2026-08-06 and confirmed in source
    (`src/final_short_report_minlen.pl:42`, a hardcoded literal, not
    derived from any input filename or chromosome name -- so this is
    upstream-stable, not particular to one run). The per-contig rows below
    it are a different granularity than the fact table holds, and the
    `.bed` files carry the per-locus detail anyway.

    **`has_sms=False` drops every structural field**, including the overall
    AQI, which is a harmonic mean of R-AQI and S-AQI and so inherits its
    meaninglessness. See the module docstring.

    `has_ngs` is accepted for API symmetry with `has_sms` and to document
    intent, but it does not currently gate anything: R-AQI/CRE is written
    unconditionally regardless of its value. This is deliberate, not an
    oversight -- CRAQ only *undercounts* R-AQI without short reads (per the
    module docstring), it does not fabricate a number the way CSE/S-AQI are
    fabricated without long reads, so there is nothing to drop.
    """
    facts: dict = {}
    for line in text.strip().splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 8 or parts[0] != "Genome":
            continue

        _, cov, lowconf, _crh, _csh, cre_field, cse_field, aqi = parts

        covered = _float(cov)
        if covered is not None:
            facts["assembly_error_covered_rate"] = covered
        low = _float(lowconf)
        if low is not None:
            facts["assembly_error_low_confidence_rate"] = low

        cre_match = _PAIRED_FIELD.match(cre_field.strip())
        if cre_match:
            r_aqi = _float(cre_match.group("aqi"))
            if r_aqi is not None:
                facts["assembly_error_r_aqi"] = r_aqi

        if has_sms:
            cse_match = _PAIRED_FIELD.match(cse_field.strip())
            if cse_match:
                s_aqi = _float(cse_match.group("aqi"))
                if s_aqi is not None:
                    facts["assembly_error_s_aqi"] = s_aqi
            overall = _float(aqi)
            if overall is not None:
                facts["assembly_error_aqi"] = overall

        break

    return facts


def count_bed_records(path: Path) -> int | None:
    """How many records a CRAQ `.bed` holds, or None if it does not exist.

    None rather than 0 for a missing file, deliberately: a `.bed` CRAQ never
    wrote is a measurement that did not happen, and storing it as zero would
    claim the opposite of what is true. Same reasoning as dropping CSE facts
    on a short-read-only run.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if line.strip())
