"""The datasets CLI probe.

Version parsing is the part worth testing: `datasets --version` prints
"datasets version: 18.30.1", which is a different shape from the bare
"1.2.3" most tools print, and the tools panel shows whatever this returns.
"""

from app.pipelines import tools


class TestDatasetsProbe:
    def test_datasets_is_in_all_tools(self):
        """A tool absent from all_tools() never appears in the tools panel,
        so a missing dependency would surface as a confusing job failure
        instead of a visible "not installed"."""
        names = [t.name for t in tools.all_tools()]
        assert "datasets" in names

    def test_datasets_has_tool_meta(self):
        """all_tools_with_meta joins on name; a missing entry means the tool
        renders with no description at all."""
        metas = {m["name"]: m for m in tools.all_tools_with_meta()}
        assert "datasets" in metas
        assert metas["datasets"]["pipelines"] == ["download"]

    def test_version_prefix_is_stripped(self):
        """`datasets --version` prints "datasets version: 18.30.1". Showing
        the whole line in a version column would be noise."""
        assert tools._clean_version("datasets version: 18.30.1") == "18.30.1"
