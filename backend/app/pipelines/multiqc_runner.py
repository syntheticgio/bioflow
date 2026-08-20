"""MultiQC command construction.

The same split `quast_runner` and `ragtag_runner` use: pure functions over
strings and paths, testable without a container, a queue, or a binary.

Verified against a real `multiqc 1.35` install on 2026-08-20, running over
genuine FastQC and fastp output in the layout `run_qc` writes. Three
findings from that run shape this module:

- **`--no-version-check` is not optional.** MultiQC checks PyPI for a newer
  release on every invocation by default. In a worker with no outbound
  network that is a hang; with one, it is a network round-trip on a job
  that should touch nothing but the filesystem.
- **Exit 0 does not mean a report exists.** Pointed at a directory it finds
  nothing parseable in, MultiQC logs "No analysis results found" and exits
  zero, having written no file. `report_path` exists so a caller can check
  for the artifact rather than trusting the return code -- the same shape
  as the "fastp exited 0 but produced no output" guard in
  `queue/pipeline_handlers.py`.
- **There is no threads flag.** MultiQC parses files serially; an earlier
  draft of the spec passed one, and it does not exist.

No parsing half here, unlike `quast_runner`. MultiQC's own `multiqc_data/`
JSON duplicates numbers this application already stores as facts from each
tool's native output, and re-reading them through a second parser would
create two code paths that are supposed to agree -- the bug
`quast_runner`'s docstring records `assembly_n50` being deleted for. The
report is an artifact the user opens, not a source of facts.
"""

from pathlib import Path

# What MultiQC names its report. Fixed rather than configurable: the route
# that serves it resolves a fixed filename under the project's directory,
# so a caller free to rename the output could write a report nothing could
# then find.
REPORT_FILENAME = "multiqc_report.html"

# MultiQC's machine-readable sibling directory. Not parsed (see the module
# docstring) but named here because the report links to it, and a report
# whose data directory is missing degrades rather than failing visibly.
DATA_DIRNAME = "multiqc_data"


def build_multiqc_command(
    *,
    multiqc_path: str,
    input_dir: Path,
    out_dir: Path,
    title: str | None = None,
) -> list[str]:
    """The argv for a MultiQC run over `input_dir`, writing into `out_dir`.

    `input_dir` is positional. MultiQC walks it recursively and decides
    which of its modules apply by matching filenames and file contents, so
    the caller's job is staging the right files rather than naming tools.

    `--force` because regenerating a project's report overwrites in place:
    without it MultiQC preserves the existing report and writes the new one
    under a suffixed name, which the serving route -- resolving a fixed
    filename -- would never show.

    `title` is optional and omitted entirely when absent rather than passed
    empty, which would render a blank heading instead of MultiQC's default.
    """
    cmd = [
        multiqc_path,
        str(input_dir),
        "-o",
        str(out_dir),
        "--force",
        # See the module docstring: a network call on every run otherwise.
        "--no-version-check",
        # The log is captured to a file, not a terminal.
        "--no-ansi",
    ]

    if title:
        cmd += ["--title", title]

    return cmd


def report_path(out_dir: Path) -> Path:
    """Where `build_multiqc_command` leaves its report, if it wrote one.

    Callers must check this exists rather than trusting MultiQC's exit
    code, which is zero even when it found nothing to report on. See the
    module docstring.
    """
    return out_dir / REPORT_FILENAME


def data_dir(out_dir: Path) -> Path:
    """The report's `multiqc_data/` sibling, if MultiQC wrote one."""
    return out_dir / DATA_DIRNAME
