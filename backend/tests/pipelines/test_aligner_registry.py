"""The registry is the contract between the backend and the dialog.

The tests that matter are the completeness ones: every aligner must have a
spec, and every spec's fields must match the parameter class it names. A
field the form renders but the params class rejects is a dialog the user
cannot submit, and it would not be caught by any per-tool test.
"""

from app.pipelines import align_params, aligner_registry
from app.pipelines.aligners import Aligner


class TestCompleteness:
    def test_every_aligner_has_a_spec(self):
        for aligner in Aligner:
            assert aligner_registry.spec_for(aligner) is not None

    def test_every_spec_names_its_own_aligner(self):
        for aligner in Aligner:
            assert aligner_registry.spec_for(aligner).aligner is aligner

    def test_every_spec_has_a_memory_model(self):
        for aligner in Aligner:
            model = aligner_registry.spec_for(aligner).memory_model
            assert model.fixed_overhead_mb > 0
            assert model.index_bytes_per_ref_base > 0

    def test_every_spec_probes_its_own_binary(self):
        """`spec.tool` is what both `align_handlers._aligner_tool` and the
        align-defaults endpoint use to answer "can this aligner run here".

        This is not hypothetical: the defaults endpoint used to answer with a
        ternary -- bwa-mem2's probe for bwa-mem2, minimap2's for everything
        else -- so bowtie2, HISAT2 and STAR were all reported available
        whenever minimap2 was, and the dialog offered a tool whose launch then
        failed. A spec wired to the wrong probe fails here instead.
        """
        for aligner in Aligner:
            assert aligner_registry.spec_for(aligner).tool().name == aligner.value

    def test_builder_tool_matches_which_aligners_have_a_separate_builder(self):
        """bowtie2 and HISAT2 index through a separate binary (bowtie2-build,
        hisat2-build); bwa-mem2 and minimap2 do not. `align_handlers.build_index`
        dispatches on `builder_tool` being set, so a spec that disagrees with
        `IndexLayout.builder` here would silently point the index build at the
        wrong binary. winnowmap's builder is meryl -- the same separate-binary
        shape, except what meryl produces is consumed via -W rather than
        discovered by suffix."""
        with_builder = {Aligner.BOWTIE2, Aligner.HISAT2, Aligner.WINNOWMAP}
        for aligner in Aligner:
            spec = aligner_registry.spec_for(aligner)
            has_builder_tool = spec.builder_tool is not None
            has_builder_name = spec.index.builder is not None
            assert has_builder_tool == (aligner in with_builder)
            assert has_builder_tool == has_builder_name

    def test_builder_gzip_support_matches_what_the_binaries_accept(self):
        """`align_handlers.build_index` decompresses the reference exactly when
        this flag is False, so a spec that overstates what its builder accepts
        hands a gzipped FASTA to a tool that cannot read one.

        That is #560: hisat2-build exits 1 on a compressed reference, deleting
        the .ht2 files it had already written, and the call site decompressed
        only for STAR. Measured against the binaries this image ships, on both
        plain gzip and bgzip -- bowtie2-build, `bwa-mem2 index` and
        `minimap2 -d` accept both; hisat2-build and STAR reject both.

        Listed explicitly rather than derived so that adding an aligner is a
        decision here, not an inherited default that happens to be wrong.
        """
        rejects_gzip = {Aligner.HISAT2, Aligner.STAR}
        for aligner in Aligner:
            spec = aligner_registry.spec_for(aligner)
            assert spec.builder_accepts_gzip == (aligner not in rejects_gzip)


class TestFieldMetadataMatchesParams:
    def test_every_field_key_is_accepted_by_the_params_class(self):
        """A field the form renders that the params class does not accept is
        a form the user cannot submit."""
        for aligner in Aligner:
            spec = aligner_registry.spec_for(aligner)
            payload = {"aligner": aligner.value}
            for f in spec.fields:
                payload[f.key] = f.default
            params = align_params.from_dict(payload)
            for f in spec.fields:
                assert hasattr(params, f.key), (
                    f"{aligner.value} field {f.key!r} has no params attribute"
                )

    def test_select_fields_declare_choices(self):
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                if f.kind == "select":
                    assert f.choices, f"{f.key} is a select with no choices"

    def test_select_defaults_are_among_their_choices(self):
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                if f.kind == "select":
                    values = [c.value for c in f.choices]
                    assert f.default in values

    def test_every_field_has_help_text(self):
        """The help line is the only explanation a generated form carries,
        so an empty one is a knob with no stated meaning."""
        for aligner in Aligner:
            for f in aligner_registry.spec_for(aligner).fields:
                assert f.help.strip(), f"{aligner.value}.{f.key} has no help"


class TestSerialization:
    def test_schema_is_json_serializable(self):
        """It is served straight to the dialog, so anything not JSON-native
        breaks the endpoint rather than the test that built it."""
        import json

        schema = aligner_registry.schema_for(Aligner.BOWTIE2)
        json.dumps(schema)

    def test_schema_carries_the_field_groups(self):
        schema = aligner_registry.schema_for(Aligner.BOWTIE2)
        groups = {f["group"] for f in schema["fields"]}
        assert "performance" in groups
        assert "biology" in groups
