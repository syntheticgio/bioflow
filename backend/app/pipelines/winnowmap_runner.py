"""Command builders for winnowmap's meryl-built repetitive-k-mer index.

Pure functions only: no I/O, no subprocess. Split from `align_runner`
because winnowmap is the one aligner in this application whose "index" is
built by a second tool (meryl) rather than by the aligner itself -- bowtie2
and HISAT2 also use a separate builder binary, but that binary's *own* name
covers what it does. Naming a `meryl`-branded function `build_index_command`
inside `align_runner` would read as if meryl built a generic aligner index,
when what it actually produces is a k-mer weighting file winnowmap consumes
via `-W`, verified against GCI's own README:

    meryl count k=15 output merylDB $asm
    meryl print greater-than distinct=0.9998 merylDB > repetitive_k15.txt
    winnowmap -W repetitive_k15.txt -ax map-pb $asm $reads > out.sam

Two commands for the "index" step because meryl is itself a two-stage tool:
`count` builds the k-mer database, `print greater-than` filters it down to
the repetitive set winnowmap actually wants. Both must run in order; there
is no way to fuse them into fewer than two meryl invocations. The alignment
step itself (the third line above) is built by `align_runner._aligner_argv`
alongside every other aligner -- winnowmap's calling convention is
minimap2's (`-a -x <preset> -R <rg>`, plus `-W <repetitive_kmers>`), since
winnowmap is built on minimap2's own codebase and shares its argument
parser. Verified against a real build: `winnowmap --help` documents `-R STR`
in minimap2's exact `'@RG\\tID:foo\\tSM:bar'` phrasing.
"""

from __future__ import annotations

import shlex
from pathlib import Path


def build_meryl_count_command(
    *,
    meryl_path: str,
    k: int,
    reference: Path,
    output: Path,
    threads: int = 4,
) -> list[str]:
    """`meryl count` over the assembly, into a k-mer database.

    Counts the *assembly's* k-mers, not the reads' -- this is what
    distinguishes winnowmap's meryl step from Merqury's
    `merqury_runner.build_meryl_count_command`, which counts the read set.
    The two are unrelated databases serving unrelated purposes and are never
    interchangeable, even though both are built by the same binary.
    """
    return [
        meryl_path,
        "count",
        f"k={k}",
        f"threads={threads}",
        "output",
        str(output),
        str(reference),
    ]


def build_meryl_print_repetitive_command(
    *, meryl_path: str, distinct: float, database: Path
) -> list[str]:
    """`meryl print greater-than` filtered to the repetitive k-mers.

    Writes to stdout -- GCI's README pipes this directly to a file
    (`> repetitive_k15.txt`) rather than meryl taking an `output` flag for
    this subcommand, so the caller redirects `run_subprocess`'s stdout
    itself rather than this builder returning a path meryl will write to.
    """
    return [
        meryl_path,
        "print",
        "greater-than",
        f"distinct={distinct}",
        str(database),
    ]


def build_meryl_print_repetitive_shell_command(
    *, meryl_path: str, distinct: float, database: Path, output: Path
) -> list[str]:
    """`meryl print greater-than`, redirected to `output`, as a shell argv.

    `run_subprocess` has no stdout-redirect argument of its own -- every
    other command here either writes files directly or is consumed as a
    parsed stream -- so this is a `/bin/sh -c` wrapper the same shape
    `align_runner.build_align_command` already uses for its pipe. Quoted
    with `shlex.quote` for the same reason that one is: `output` and
    `database` derive from paths this application chose, but neither is
    guaranteed free of shell metacharacters.
    """
    argv = build_meryl_print_repetitive_command(
        meryl_path=meryl_path, distinct=distinct, database=database
    )
    quoted = " ".join(shlex.quote(a) for a in argv)
    return ["/bin/sh", "-c", f"{quoted} > {shlex.quote(str(output))}"]
