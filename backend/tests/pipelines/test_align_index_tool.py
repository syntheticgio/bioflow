"""Which binary's path lands in the index-build command.

`_index_tool` is the one place `build_index` decides between the aligner's
own binary and a separate builder (bowtie2-build, hisat2-build). A copy-paste
mistake here -- using the aligner's `Tool` where the builder's belongs, or
vice versa -- would swap which binary's path is baked into the command while
every other part of the job still "succeeds," so it is tested in isolation
from the rest of the handler.
"""

from dataclasses import replace

from app.pipelines import aligner_registry
from app.pipelines.aligners import Aligner
from app.pipelines.tools import Tool
from app.queue.align_handlers import _index_tool


def _tool(name: str) -> Tool:
    return Tool(name=name, path=f"/usr/bin/{name}", version="1.0")


class TestIndexTool:
    def test_bowtie2_uses_the_builder_tool_not_the_aligner_tool(self, monkeypatch):
        aligner_tool = _tool("bowtie2")
        builder = _tool("bowtie2-build")
        base_spec = aligner_registry.spec_for(Aligner.BOWTIE2)
        fake_spec = replace(base_spec, builder_tool=lambda: builder)
        monkeypatch.setattr(aligner_registry, "spec_for", lambda a: fake_spec)

        resolved = _index_tool(Aligner.BOWTIE2, aligner_tool)

        assert resolved.path == "/usr/bin/bowtie2-build"
        assert resolved.path != aligner_tool.path

    def test_hisat2_uses_the_builder_tool_not_the_aligner_tool(self, monkeypatch):
        aligner_tool = _tool("hisat2")
        builder = _tool("hisat2-build")
        base_spec = aligner_registry.spec_for(Aligner.HISAT2)
        fake_spec = replace(base_spec, builder_tool=lambda: builder)
        monkeypatch.setattr(aligner_registry, "spec_for", lambda a: fake_spec)

        resolved = _index_tool(Aligner.HISAT2, aligner_tool)

        assert resolved.path == "/usr/bin/hisat2-build"
        assert resolved.path != aligner_tool.path

    def test_bwa_mem2_has_no_separate_builder_and_reuses_the_aligner_tool(self):
        aligner_tool = _tool("bwa-mem2")

        resolved = _index_tool(Aligner.BWA_MEM2, aligner_tool)

        assert resolved is aligner_tool

    def test_minimap2_has_no_separate_builder_and_reuses_the_aligner_tool(self):
        aligner_tool = _tool("minimap2")

        resolved = _index_tool(Aligner.MINIMAP2, aligner_tool)

        assert resolved is aligner_tool

    def test_registry_agrees_with_the_real_bowtie2_and_hisat2_specs(self):
        """Not a mock -- the actual REGISTRY entries have builder_tool set for
        bowtie2/HISAT2 and unset for bwa-mem2/minimap2, matching IndexLayout's
        `builder` field one for one."""
        assert aligner_registry.spec_for(Aligner.BOWTIE2).builder_tool is not None
        assert aligner_registry.spec_for(Aligner.HISAT2).builder_tool is not None
        assert aligner_registry.spec_for(Aligner.BWA_MEM2).builder_tool is None
        assert aligner_registry.spec_for(Aligner.MINIMAP2).builder_tool is None
