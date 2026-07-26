"""Search filter construction and the metadata filter mini-syntax."""

import pytest
from beanie import PydanticObjectId

from app.services.search_service import (
    SearchQuery,
    build_filter,
    parse_metadata_filters,
)


class TestTextSearch:
    def test_matches_filename_case_insensitively(self):
        f = build_filter(SearchQuery(text="sample"))
        assert f["name"]["$regex"] == "sample"
        assert f["name"]["$options"] == "i"

    def test_regex_metacharacters_are_escaped(self):
        """A user typing 'sample(1)' means that literal string, not a group --
        and an unescaped regex is both wrong and a performance risk."""
        f = build_filter(SearchQuery(text="sample(1).fastq"))
        assert f["name"]["$regex"] == r"sample\(1\)\.fastq"

    def test_no_text_means_no_name_filter(self):
        assert "name" not in build_filter(SearchQuery())


class TestFilters:
    def test_format_kinds_use_in(self):
        f = build_filter(SearchQuery(kinds=["bam", "cram"]))
        assert f["format.kind"] == {"$in": ["bam", "cram"]}

    def test_tags_require_all_not_any(self):
        """Filters narrow. Selecting two tags should mean 'both', not 'either'."""
        f = build_filter(SearchQuery(tags=["qc-pass", "cohort-a"]))
        assert f["tags"] == {"$all": ["qc-pass", "cohort-a"]}

    def test_size_range(self):
        f = build_filter(SearchQuery(size_min=1000, size_max=5000))
        assert f["size"] == {"$gte": 1000, "$lte": 5000}

    def test_open_ended_size_range(self):
        assert build_filter(SearchQuery(size_min=1000))["size"] == {"$gte": 1000}

    def test_project_scope(self):
        pid = PydanticObjectId()
        assert build_filter(SearchQuery(project_id=pid))["project_id"] == pid

    def test_filters_combine(self):
        f = build_filter(
            SearchQuery(text="a", kinds=["bam"], tags=["x"], size_min=1)
        )
        assert {"name", "format.kind", "tags", "size"} <= set(f)


class TestMetadataFilters:
    def test_equality(self):
        f = build_filter(SearchQuery(metadata={"sample_id": "P-041"}))
        assert f["metadata.sample_id"] == "P-041"

    def test_range_operators(self):
        f = build_filter(SearchQuery(metadata={"mean_coverage": {"gte": 30}}))
        assert f["metadata.mean_coverage"] == {"$gte": 30}

    def test_exists(self):
        f = build_filter(SearchQuery(metadata={"batch": {"exists": True}}))
        assert f["metadata.batch"] == {"$exists": True}

    def test_contains_is_escaped(self):
        f = build_filter(SearchQuery(metadata={"notes": {"contains": "a.b"}}))
        assert f["metadata.notes"]["$regex"] == r"a\.b"

    def test_in_operator(self):
        f = build_filter(SearchQuery(metadata={"organism": {"in": ["a", "b"]}}))
        assert f["metadata.organism"] == {"$in": ["a", "b"]}

    def test_unknown_operator_is_ignored_not_crashed(self):
        f = build_filter(SearchQuery(metadata={"x": {"bogus": 1}}))
        assert "metadata.x" not in f


class TestFilterSyntax:
    """The compact `key=value` form used in query strings, so a search is
    fully described by its URL."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("sample_id=P-041", {"sample_id": "P-041"}),
            ("mean_coverage>=30", {"mean_coverage": {"gte": 30}}),
            ("mean_coverage<=100", {"mean_coverage": {"lte": 100}}),
            ("lane>2", {"lane": {"gt": 2}}),
            ("lane<5", {"lane": {"lt": 5}}),
            ("organism!=Other", {"organism": {"ne": "Other"}}),
            ("notes~urgent", {"notes": {"contains": "urgent"}}),
            ("batch=*", {"batch": {"exists": True}}),
            ("batch", {"batch": {"exists": True}}),
        ],
    )
    def test_parses_each_operator(self, raw, expected):
        assert parse_metadata_filters([raw]) == expected

    def test_numeric_values_become_numbers(self):
        """Otherwise a range query would compare strings and give nonsense."""
        parsed = parse_metadata_filters(["coverage>=30"])
        assert parsed["coverage"]["gte"] == 30
        assert isinstance(parsed["coverage"]["gte"], int)

    def test_float_values(self):
        assert parse_metadata_filters(["x>=1.5"])["x"]["gte"] == pytest.approx(1.5)

    def test_non_numeric_stays_a_string(self):
        assert parse_metadata_filters(["build=GRCh38"])["build"] == "GRCh38"

    def test_multiple_filters(self):
        parsed = parse_metadata_filters(["sample_id=P-041", "coverage>=30"])
        assert len(parsed) == 2

    def test_empty_and_malformed_entries_are_skipped(self):
        assert parse_metadata_filters(["", "=novalue"]) == {}

    def test_value_containing_the_operator_char(self):
        """Partition on the first occurrence, so a value may contain '='."""
        assert parse_metadata_filters(["notes=a=b"])["notes"] == "a=b"
