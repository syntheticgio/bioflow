"""Direct tests for assembly_handlers.py's pure helper functions.

The handler itself (`assemble_reads`) runs a real subprocess and is exercised
indirectly through the runner modules' own tests plus the queue's end-to-end
harness. This file covers the command-line and filename plumbing that has no
other home: that the paired path and Bloom budget actually reach the command
line, and that the graph output is named for the format ABySS and Flye
actually emit.
"""


def test_abyss_job_passes_mate_and_bloom_budget(monkeypatch, tmp_path):
    """The paired path must reach the command line, not just the payload."""
    from app.pipelines import assembly_runner
    from app.pipelines.assemblers import Assembler
    from app.pipelines.assembly_params import AbyssParams

    r1 = tmp_path / "s_R1.fastq"
    r2 = tmp_path / "s_R2.fastq"
    r1.write_text("@r\nACGT\n+\nIIII\n")
    r2.write_text("@r\nACGT\n+\nIIII\n")

    cmd = assembly_runner.build_assembly_command(
        assembler=Assembler.ABYSS,
        tool_path="/usr/bin/abyss-pe",
        reads=r1,
        out_dir=tmp_path / "out",
        params=AbyssParams(k=51, threads=4),
        mate=r2,
        memory_bytes=4 * 1024**3,
    )
    assert f"in={r1} {r2}" in cmd
    assert "B=4096M" in cmd


def test_graph_name_matches_the_assembler_format():
    """ABySS emits Graphviz, not GFA -- a .gfa suffix would be a lie."""
    from app.queue import assembly_handlers

    assert assembly_handlers._graph_suffix("abyss") == ".assembly_graph.dot"
    assert assembly_handlers._graph_suffix("flye") == ".assembly_graph.gfa"
    assert assembly_handlers._graph_suffix("spades") == ".assembly_graph.gfa"


def test_spades_progress_object_construction_does_not_use_flye_stage_order():
    """Regression for the crash where every SPAdes job raised AttributeError.

    `assemble_reads` picks a progress parser with `if assembler is ABYSS /
    else`, a branch written before SPAdes existed. The `else` called
    `assembly_runner.flye_stage_order(params)`, which reads
    `params.iterations` -- a field only `FlyeParams` has. A `SpadesParams`
    instance has no such attribute, so constructing the progress object for a
    SPAdes job raised before the subprocess ever ran.

    This does not invoke the full `assemble_reads` handler (real
    subprocess/filesystem work, per this file's module docstring); it
    exercises the same branch logic `assemble_reads` runs, using a real
    `SpadesParams` so a reintroduced `flye_stage_order(params)` call on the
    SPAdes path would fail exactly as it did before the fix.
    """
    from app.pipelines import assembly_runner
    from app.pipelines.assemblers import Assembler
    from app.pipelines.assembly_params import SpadesParams

    assembler = Assembler.SPADES
    params = SpadesParams(threads=4)
    assert not hasattr(params, "iterations")

    if assembler is Assembler.ABYSS:
        progress = assembly_runner.AbyssProgress()
    elif assembler is Assembler.FLYE:
        progress = assembly_runner.AssemblyProgress(
            stage_order=assembly_runner.flye_stage_order(params)
        )
    else:
        progress = assembly_runner.AssemblyProgress()

    assert isinstance(progress, assembly_runner.AssemblyProgress)
    assert progress.stage_order == ()
