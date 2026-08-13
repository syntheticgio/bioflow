import pytest

from e2e.backend import loader


def test_yaml_load(tmp_path):
    (tmp_path / "bioflow").mkdir()
    (tmp_path / "bioflow" / "a.yaml").write_text(
        "name: smoke\ndescription: d\nsteps:\n"
        "  - create_project: { name: x }\n"
        "  - mcp: { tool: whoami, args: {} }\n"
    )
    tests = loader.discover_tests(str(tmp_path))
    assert [t.name for t in tests] == ["smoke"]
    assert tests[0].kind == "yaml"
    assert tests[0].steps[0].verb == "create_project"
    assert tests[0].steps[1].verb == "mcp"


def test_bad_verb_raises(tmp_path):
    (tmp_path / "bioflow").mkdir()
    (tmp_path / "bioflow" / "bad.yaml").write_text("name: b\nsteps:\n  - frobnicate: {}\n")
    with pytest.raises(ValueError):
        loader.discover_tests(str(tmp_path))


def test_missing_name_raises(tmp_path):
    (tmp_path / "bioflow").mkdir()
    (tmp_path / "bioflow" / "noname.yaml").write_text("steps: []\n")
    with pytest.raises(ValueError):
        loader.discover_tests(str(tmp_path))


def test_python_escape_hatch(tmp_path):
    (tmp_path / "bioflow").mkdir()
    (tmp_path / "bioflow" / "py_test.py").write_text(
        "from e2e.backend.primitives import test\n\n"
        '@test("custom", description="d")\n'
        "async def custom(ctx):\n    pass\n"
    )
    tests = loader.discover_tests(str(tmp_path))
    by_name = {t.name: t for t in tests}
    assert "custom" in by_name
    assert by_name["custom"].kind == "python"
    assert by_name["custom"].callable is not None
