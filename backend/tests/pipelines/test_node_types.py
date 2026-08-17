"""The canvas node registry.

This file's most important test is the exhaustiveness one. A launch_* function
absent from both NODE_TYPES and EXCLUDED_LAUNCHES is a tool that installs
cleanly, passes every other test, and simply never appears on the canvas --
the STAR/_SIDECAR_ROLES failure in a new place.
"""

import inspect

import pytest
from beanie import PydanticObjectId

from app.models import ACTIVE_STATES, FormatKind, ObjectRole
from app.models.run import RunKind
from app.pipelines.node_types import (
    EXCLUDED_LAUNCHES,
    NODE_TYPES,
    launch_function_names,
)
from app.services import pipeline_service


class TestExhaustiveness:
    def test_every_launch_function_is_classified(self):
        """Every launch_* either has a node type or is explicitly excluded.

        If this fails after you added a launcher: add a NODE_TYPES entry, or
        add it to EXCLUDED_LAUNCHES *with a comment saying why*. Do not delete
        the assertion.
        """
        classified = {spec.launch_name for spec in NODE_TYPES.values()} | EXCLUDED_LAUNCHES
        assert launch_function_names() == classified

    def test_exclusions_are_real_functions(self):
        """A typo'd exclusion silently stops guarding anything."""
        assert EXCLUDED_LAUNCHES <= launch_function_names()

    def test_no_launcher_is_both_used_and_excluded(self):
        used = {spec.launch_name for spec in NODE_TYPES.values()}
        assert not (used & EXCLUDED_LAUNCHES)


class TestSpecs:
    def test_every_spec_declares_a_callable_launch(self):
        for key, spec in NODE_TYPES.items():
            assert callable(spec.launch), f"{key} has no callable launch adapter"

    def test_every_spec_has_a_label(self):
        """The palette renders these; a blank one is an unusable node."""
        for key, spec in NODE_TYPES.items():
            assert spec.label.strip(), f"{key} has no label"

    def test_port_names_are_unique_within_a_spec(self):
        """Output->port resolution is by declared name, so duplicates make it
        ambiguous."""
        for key, spec in NODE_TYPES.items():
            names = [p.name for p in spec.inputs]
            assert len(names) == len(set(names)), f"{key} has duplicate input ports"
            out_names = [p.name for p in spec.outputs]
            assert len(out_names) == len(set(out_names)), f"{key} has duplicate outputs"

    def test_align_declares_a_reference_port_that_rejects_protein(self):
        """The concrete rule the typing exists for."""
        spec = NODE_TYPES["align"]
        reference = next(p for p in spec.inputs if p.name == "reference")
        assert reference.type.accepts(FormatKind.FASTA, ObjectRole.REFERENCE)
        assert not reference.type.accepts(FormatKind.FASTA, ObjectRole.PROTEIN)

    def test_trim_consumes_fastq_and_produces_trimmed_reads(self):
        spec = NODE_TYPES["trim"]
        reads = next(p for p in spec.inputs if p.name == "reads")
        assert reads.type.accepts(FormatKind.FASTQ, None)
        out = spec.outputs[0]
        assert out.type.role is ObjectRole.TRIMMED_READS

    def test_annotation_export_port_accepts_gff_gtf_bed_and_rejects_genbank(self):
        """The concrete rule this port's multi-format type exists for.

        GenBank is refused because its features span several lines and its
        segment rows correspond to no single line, so the export handler
        cannot subset it. Refusing on the canvas beats failing in the job.
        """
        spec = NODE_TYPES["annotation_export"]
        port = next(p for p in spec.inputs if p.name == "annotation")
        assert port.type.accepts(FormatKind.GFF, ObjectRole.ANNOTATION)
        assert port.type.accepts(FormatKind.GTF, ObjectRole.ANNOTATION)
        assert port.type.accepts(FormatKind.BED, ObjectRole.ANNOTATION)
        assert not port.type.accepts(FormatKind.GENBANK, ObjectRole.ANNOTATION)

    def test_annotation_export_declares_an_annotation_output(self):
        spec = NODE_TYPES["annotation_export"]
        assert [p.name for p in spec.outputs] == ["subset"]
        assert spec.outputs[0].type.role is ObjectRole.ANNOTATION

    def test_annotation_export_output_matches_its_input_formats(self):
        """The subset is written in the source file's own syntax, so the
        output is the same three-format set rather than one fixed format."""
        spec = NODE_TYPES["annotation_export"]
        source = next(p for p in spec.inputs if p.name == "annotation")
        assert spec.outputs[0].type.accepted_formats == source.type.accepted_formats

    def test_annotation_export_creates_no_pipeline_run(self):
        assert NODE_TYPES["annotation_export"].run_kind is None

    def test_annotation_export_declares_its_filter_fields(self):
        """Seven filters plus output_name. top_level_only and parent_status
        are deliberately absent: the handler force-sets the first, and the
        second is an artifact of the Results table's Unresolved view."""
        spec = NODE_TYPES["annotation_export"]
        keys = [f.key for f in spec.param_fields]
        assert keys == [
            "contig",
            "start_min",
            "start_max",
            "feature_type",
            "biotype",
            "name_query",
            "strand",
            "output_name",
        ]
        assert "top_level_only" not in keys
        assert "parent_status" not in keys

    def test_annotation_export_filter_fields_are_grouped_as_filters(self):
        spec = NODE_TYPES["annotation_export"]
        filters = [f for f in spec.param_fields if f.key != "output_name"]
        assert all(f.group == "filters" for f in filters)


class TestRunKindResolution:
    """(run_kind, run_tool) is what workflow_derive matches a run against.

    A duplicate pair makes `_node_type_for` return whichever spec the dict
    happens to list first, silently deriving one tool's run as another tool's
    node -- the same silent-skip shape as the registries CLAUDE.md warns about,
    except here the wrong answer is a plausible-looking node rather than an
    absence.
    """

    def test_run_kind_and_tool_pairs_are_unique(self):
        pairs = [
            (spec.run_kind, spec.run_tool)
            for spec in NODE_TYPES.values()
            if spec.run_kind is not None
        ]
        assert len(pairs) == len(set(pairs)), (
            f"duplicate (run_kind, run_tool) among node types: {pairs}"
        )

    def test_reference_assembly_specs_are_told_apart_by_tool(self):
        """The one RunKind covered by more than one node type."""
        by_tool = {
            spec.run_tool: key
            for key, spec in NODE_TYPES.items()
            if spec.run_kind is RunKind.REFERENCE_ASSEMBLY
        }
        assert by_tool == {
            "ivar": "consensus",
            "polypolish": "polish",
            "ragtag": "scaffold",
        }


class TestAdapterSignatures:
    def test_every_adapter_takes_inputs_and_params(self):
        """The registry's whole purpose is presenting 24 differently-shaped
        launchers behind one call shape."""
        for key, spec in NODE_TYPES.items():
            sig = inspect.signature(spec.launch)
            assert {"inputs", "params", "owner"} <= set(sig.parameters), (
                f"{key}'s adapter does not take (inputs, params, owner)"
            )


class TestAnnotationExportLaunch:
    """The adapter's sidecar handling -- the design's one implicit step."""

    @pytest.mark.asyncio
    async def test_computes_stats_first_when_the_sidecar_is_absent(self, monkeypatch):
        calls = []

        async def fake_ensure(*, object_id, owner):
            calls.append(("ensure", str(object_id)))
            return "job-stats-1"

        async def fake_export(*, object_id, owner, filters, output_name, depends_on=None):
            calls.append(("export", str(object_id)))
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={},
            owner="tester",
        )

        assert calls == [
            ("ensure", "64b7f0000000000000000001"),
            ("export", "64b7f0000000000000000001"),
        ]

    @pytest.mark.asyncio
    async def test_passes_only_the_filters_that_were_set(self, monkeypatch):
        """An empty box means "no bound", not a filter on empty string."""
        seen = {}

        async def fake_ensure(*, object_id, owner):
            return None

        async def fake_export(*, object_id, owner, filters, output_name, depends_on=None):
            seen.update(filters=filters, output_name=output_name)
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={"contig": "chr1", "feature_type": "", "strand": None},
            owner="tester",
        )

        assert seen["filters"] == {"contig": "chr1"}

    @pytest.mark.asyncio
    async def test_no_filters_at_all_is_launchable(self, monkeypatch):
        """Exporting everything is a valid request."""
        seen = {}

        async def fake_ensure(*, object_id, owner):
            return None

        async def fake_export(*, object_id, owner, filters, output_name, depends_on=None):
            seen.update(filters=filters)
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={},
            owner="tester",
        )

        assert seen["filters"] == {}

    @pytest.mark.asyncio
    async def test_export_depends_on_the_stats_job_when_one_was_queued(
        self, monkeypatch
    ):
        """The race the whole feature exists to prevent: both handlers run on
        the same THREAD worker pool, so enqueueing the stats job first is not
        enough -- the export must be held behind it via `depends_on`."""
        seen = {}

        async def fake_ensure(*, object_id, owner):
            return "job-stats-42"

        async def fake_export(*, object_id, owner, filters, output_name, depends_on=None):
            seen["depends_on"] = depends_on
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={},
            owner="tester",
        )

        assert seen["depends_on"] == ["job-stats-42"]

    @pytest.mark.asyncio
    async def test_export_has_no_dependency_when_the_sidecar_already_existed(
        self, monkeypatch
    ):
        """No stats job was needed, so nothing should hold the export back."""
        seen = {}

        async def fake_ensure(*, object_id, owner):
            return None

        async def fake_export(*, object_id, owner, filters, output_name, depends_on=None):
            seen["depends_on"] = depends_on
            return {"job_id": "j1"}

        monkeypatch.setattr(
            pipeline_service, "ensure_annotation_stats", fake_ensure
        )
        monkeypatch.setattr(
            pipeline_service, "launch_annotation_export", fake_export
        )

        spec = NODE_TYPES["annotation_export"]
        await spec.launch(
            inputs={"annotation": "64b7f0000000000000000001"},
            params={},
            owner="tester",
        )

        assert not seen["depends_on"]


class TestActiveAnnotationStatsJobQuery:
    """The lookup that finds an in-flight stats computation to wait on.

    Mirrors TestActiveIndexJobQuery in test_align_launch.py: that query
    shipped broken once, as a Beanie ExpressionField (`Job.state.in_(...)`)
    that raises outside a query context rather than a plain dict `Job.find_one`
    can execute, and it stayed broken because the branch that calls it only
    runs when two launches race for the same in-flight job -- no test staged
    the race, so nothing caught it. Asserting the query shape here is what
    makes `active_annotation_stats_job_query` checkable without staging that
    race for the annotation-export path too.
    """

    def test_matches_only_annotation_stats_jobs(self):
        q = pipeline_service.active_annotation_stats_job_query(PydanticObjectId())
        assert q["type"] == "run_annotation_stats"

    def test_matches_only_jobs_still_in_flight(self):
        q = pipeline_service.active_annotation_stats_job_query(PydanticObjectId())
        states = set(q["state"]["$in"])
        assert states == {s.value for s in ACTIVE_STATES}
        assert "succeeded" not in states
        assert "failed" not in states

    def test_includes_blocked(self):
        """A stats job is never blocked today, but deriving the list from
        ACTIVE_STATES means a state added later is covered without an edit."""
        q = pipeline_service.active_annotation_stats_job_query(PydanticObjectId())
        assert "blocked" in q["state"]["$in"]

    def test_scoped_to_the_annotation(self):
        object_id = PydanticObjectId()
        q = pipeline_service.active_annotation_stats_job_query(object_id)
        assert q["object_id"] == object_id

    def test_is_a_plain_mongo_query(self):
        """Values must be primitives Mongo understands, not Beanie expression
        objects -- the specific mistake that broke the index-build sibling."""
        q = pipeline_service.active_annotation_stats_job_query(PydanticObjectId())
        assert isinstance(q, dict)
        assert all(isinstance(s, str) for s in q["state"]["$in"])
