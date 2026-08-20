"""Liftoff command construction for annotation transfer.

Pure: no liftoff binary is invoked, only the argument list is checked.
"""

from pathlib import Path

from app.pipelines.liftoff_runner import build_liftoff_command


def test_build_liftoff_command_core_flags():
    cmd = build_liftoff_command(
        liftoff_path="liftoff",
        target=Path("/work/target.fa"),
        reference=Path("/work/ref.fa"),
        reference_annotation=Path("/work/ref.gff3"),
        out_gff=Path("/work/target.liftoff.gff3"),
        threads=8,
        copies=True,
        unmapped_gff=Path("/work/target.liftoff.unmapped.gff3"),
    )
    # target and reference are positional, in that order.
    assert cmd[0] == "liftoff"
    assert cmd[1] == str(Path("/work/target.fa"))
    assert cmd[2] == str(Path("/work/ref.fa"))
    # feature annotation
    i_g = cmd.index("-g")
    assert cmd[i_g + 1] == str(Path("/work/ref.gff3"))
    # output
    i_o = cmd.index("-o")
    assert cmd[i_o + 1] == str(Path("/work/target.liftoff.gff3"))
    # unmapped
    i_u = cmd.index("-u")
    assert cmd[i_u + 1] == str(Path("/work/target.liftoff.unmapped.gff3"))
    # threads
    i_t = cmd.index("-t")
    assert cmd[i_t + 1] == "8"
    # copies flag present by default
    assert "-copies" in cmd


def test_build_liftoff_command_no_copies_flag():
    cmd = build_liftoff_command(
        liftoff_path="liftoff",
        target=Path("/work/target.fa"),
        reference=Path("/work/ref.fa"),
        reference_annotation=Path("/work/ref.gff3"),
        out_gff=Path("/work/target.liftoff.gff3"),
        threads=4,
        copies=False,
        unmapped_gff=None,
    )
    assert "-copies" not in cmd
    # no -u when unmapped path is None
    assert "-u" not in cmd


def test_build_liftoff_command_preserves_ordering():
    """Flags must come after the positional target/reference so a trailing
    filename is not consumed as an option argument."""
    cmd = build_liftoff_command(
        liftoff_path="liftoff",
        target=Path("t.fa"),
        reference=Path("r.fa"),
        reference_annotation=Path("r.gff3"),
        out_gff=Path("t.gff3"),
        threads=2,
        copies=False,
        unmapped_gff=None,
    )
    assert cmd.index("-g") > cmd.index("r.fa")
    assert cmd.index("-o") > cmd.index("r.fa")
