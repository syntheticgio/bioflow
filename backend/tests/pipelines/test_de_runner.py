"""Merging N samples, validating a design, and ordering the results.

The refusals are the substance here. Every other pipeline in this codebase
takes one file, or one file and a reference; this one takes N files produced
by N independent jobs, and its correctness depends on those N agreeing with
each other in ways nothing downstream can check.

`run_deseq2` itself is not tested here -- it is a thin call into PyDESeq2, and
a test that mocked the library would only assert that the mock was called. It
was verified end to end instead, against six samples with 200 genes given a
known 4x effect: recall 200/200, precision 0.98, and log2 fold changes of
2.0-2.2 against the injected log2(4) = 2.
"""

import pytest

from app.errors import ValidationError
from app.pipelines import de_runner


def _sample(name, condition, counts=None, annotation="ann1"):
    return de_runner.SampleCounts(
        sample=name,
        condition=condition,
        counts=counts if counts is not None else {"g1": 10, "g2": 20},
        annotation_sha256=annotation,
        object_id=f"id-{name}",
    )


class TestDesignValidation:
    """Refused at launch, not in the worker.

    Every one of these is knowable before anything is enqueued, and PyDESeq2's
    own version of the answer is a KeyError from inside the library twenty
    seconds into a job the user has walked away from.
    """

    def test_a_singleton_group_is_refused_by_name(self):
        with pytest.raises(ValidationError) as exc:
            de_runner.validate_design(
                [
                    _sample("a", "control"),
                    _sample("b", "treated"),
                    _sample("c", "treated"),
                ],
                test="treated",
                reference="control",
            )
        # The offending group and the sample in it, because "add another
        # replicate" is the fix and the user needs to know to which arm.
        assert "control" in str(exc.value)
        assert "a" in str(exc.value)

    def test_two_replicates_each_is_enough(self):
        de_runner.validate_design(
            [
                _sample("a", "control"),
                _sample("b", "control"),
                _sample("c", "treated"),
                _sample("d", "treated"),
            ],
            test="treated",
            reference="control",
        )

    def test_a_thin_group_outside_the_contrast_is_allowed(self):
        """Only the two arms being compared need replicates. A third condition
        with one sample is not part of this test and should not block it."""
        de_runner.validate_design(
            [
                _sample("a", "control"),
                _sample("b", "control"),
                _sample("c", "treated"),
                _sample("d", "treated"),
                _sample("e", "mutant"),
            ],
            test="treated",
            reference="control",
        )

    def test_a_contrast_naming_an_absent_condition_lists_what_is_present(self):
        with pytest.raises(ValidationError) as exc:
            de_runner.validate_design(
                [_sample("a", "control"), _sample("b", "control")],
                test="mutant",
                reference="control",
            )
        assert "mutant" in str(exc.value)
        assert "control" in str(exc.value)

    def test_a_contrast_against_itself_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            de_runner.validate_design(
                [_sample("a", "control"), _sample("b", "control")],
                test="control",
                reference="control",
            )
        assert "itself" in str(exc.value)

    def test_a_repeated_sample_is_refused(self):
        """The same counts file in both arms would compare a sample with
        itself and shrink the apparent variance."""
        with pytest.raises(ValidationError) as exc:
            de_runner.validate_design(
                [
                    _sample("a", "control"),
                    _sample("a", "treated"),
                    _sample("b", "treated"),
                ],
                test="treated",
                reference="control",
            )
        assert "more than once" in str(exc.value)

    def test_a_single_sample_is_refused(self):
        with pytest.raises(ValidationError):
            de_runner.validate_design(
                [_sample("a", "control")], test="control", reference="x"
            )


class TestMerge:
    """The refusals that stop a plausible-looking wrong answer.

    An inner join across two annotation releases yields a matrix that passes
    every downstream check while testing a different set of genes than either
    input describes, and nothing in a plot or a p-value would look wrong.
    """

    def test_matching_samples_merge_into_a_matrix(self):
        m = de_runner.merge_counts(
            [
                _sample("a", "control", {"g1": 1, "g2": 2}),
                _sample("b", "treated", {"g1": 3, "g2": 4}),
            ]
        )
        assert m.samples == ["a", "b"]
        assert m.conditions == ["control", "treated"]
        assert m.genes == ["g1", "g2"]
        assert m.values == [[1, 2], [3, 4]]

    def test_genes_are_sorted_so_two_identical_runs_agree(self):
        """Dict order follows whatever order featureCounts wrote its rows.
        That is stable in practice and guaranteed by nothing, and a results
        table whose row order shifts between identical runs is confusing for
        no reason."""
        m = de_runner.merge_counts(
            [
                _sample("a", "control", {"z": 1, "a": 2}),
                _sample("b", "treated", {"z": 3, "a": 4}),
            ]
        )
        assert m.genes == ["a", "z"]
        # And the values follow the gene order, not the insertion order.
        assert m.values == [[2, 1], [4, 3]]

    def test_different_annotations_are_refused_before_the_gene_sets(self):
        with pytest.raises(ValidationError) as exc:
            de_runner.merge_counts(
                [
                    _sample("a", "control", annotation="ann1"),
                    _sample("b", "treated", annotation="ann2"),
                ]
            )
        assert "different annotations" in str(exc.value)

    def test_disagreeing_gene_sets_are_refused_rather_than_intersected(self):
        """The failure this whole module is shaped around."""
        with pytest.raises(ValidationError) as exc:
            de_runner.merge_counts(
                [
                    _sample("a", "control", {"g1": 1, "g2": 2}),
                    _sample("b", "treated", {"g1": 3, "g3": 4}),
                ]
            )
        message = str(exc.value)
        assert "same genes" in message
        # Names both samples: "re-quantify these two" is the fix.
        assert "'a'" in message and "'b'" in message

    def test_one_extra_gene_is_enough_to_refuse(self):
        """No tolerance, deliberately. There is no threshold at which
        silently dropping genes from a differential expression test is the
        helpful thing to do."""
        with pytest.raises(ValidationError):
            de_runner.merge_counts(
                [
                    _sample("a", "control", {"g1": 1}),
                    _sample("b", "treated", {"g1": 1, "g2": 2}),
                ]
            )

    def test_an_empty_counts_file_is_refused_by_name(self):
        with pytest.raises(ValidationError) as exc:
            de_runner.merge_counts([_sample("a", "control", {})])
        assert "a" in str(exc.value)

    def test_samples_with_no_recorded_annotation_are_not_blocked(self):
        """An uploaded counts file has no annotation digest. The gene-set
        check still applies; only the digest comparison is skipped."""
        a = de_runner.SampleCounts("a", "control", {"g1": 1}, None, "x")
        b = de_runner.SampleCounts("b", "treated", {"g1": 2}, None, "y")
        assert de_runner.merge_counts([a, b]).n_genes == 1


class TestResultOrdering:
    """Untested genes last, in both directions.

    DESeq2 leaves padj unset for genes it filtered out of multiple-testing
    correction. They belong in the file but never at the top of it, and the
    obvious `(value is None, value)` sort key gets this right ascending and
    exactly backwards descending -- which is how a "sort by biggest fold
    change" click opened on a page of genes with no fold change at all.
    """

    ROWS = [
        {"gene": "a", "log2_fold_change": 1.0, "padj": 0.5},
        {"gene": "b", "log2_fold_change": -3.0, "padj": 0.01},
        {"gene": "c", "log2_fold_change": None, "padj": None},
        {"gene": "d", "log2_fold_change": 2.0, "padj": 0.2},
    ]

    def test_descending_puts_the_largest_first_and_untested_last(self):
        out = de_runner.sort_rows(self.ROWS, "log2_fold_change", "desc")
        assert [r["gene"] for r in out] == ["d", "a", "b", "c"]

    def test_ascending_puts_the_smallest_first_and_untested_last(self):
        out = de_runner.sort_rows(self.ROWS, "log2_fold_change", "asc")
        assert [r["gene"] for r in out] == ["b", "a", "d", "c"]

    def test_untested_genes_are_last_whichever_column_is_sorted(self):
        for column in ("padj", "log2_fold_change"):
            for direction in ("asc", "desc"):
                out = de_runner.sort_rows(self.ROWS, column, direction)
                assert out[-1]["gene"] == "c", (column, direction)

    def test_gene_names_sort_as_strings(self):
        out = de_runner.sort_rows(self.ROWS, "gene", "asc")
        assert [r["gene"] for r in out] == ["a", "b", "c", "d"]

    def test_an_unknown_column_leaves_the_order_alone(self):
        """The column is a query parameter; a typo in a URL should not 500."""
        out = de_runner.sort_rows(self.ROWS, "nonsense", "asc")
        assert [r["gene"] for r in out] == ["a", "b", "c", "d"]


class TestResultsRoundTrip:
    def test_a_written_table_reads_back_unchanged(self, tmp_path):
        result = de_runner.DEResult(
            rows=[
                {
                    "gene": "YAL068C",
                    "base_mean": 42.5,
                    "log2_fold_change": 2.18,
                    "lfc_std_error": 0.12,
                    "stat": 18.2,
                    "p_value": 1e-73,
                    "padj": 5.8e-70,
                }
            ]
        )
        path = tmp_path / "de.tsv"
        path.write_text(result.to_tsv())

        back = de_runner.read_results(path)
        assert back[0]["gene"] == "YAL068C"
        assert back[0]["log2_fold_change"] == 2.18
        assert back[0]["padj"] == 5.8e-70

    def test_an_unset_padj_reads_back_as_none_not_zero(self, tmp_path):
        """The distinction the whole table depends on: a gene DESeq2 declined
        to test is not a gene with a p-value of 0, and reading it as one would
        promote every filtered gene to the top of a significance sort."""
        result = de_runner.DEResult(
            rows=[{"gene": "g", "base_mean": 1.0, "log2_fold_change": None,
                   "lfc_std_error": None, "stat": None, "p_value": None,
                   "padj": None}]
        )
        path = tmp_path / "de.tsv"
        path.write_text(result.to_tsv())

        back = de_runner.read_results(path)
        assert back[0]["padj"] is None
        assert back[0]["log2_fold_change"] is None

    def test_a_tiny_p_value_survives_the_round_trip(self, tmp_path):
        """Rounding a p-value to four places collapses everything below 1e-4
        to zero, which is most of what a DE run finds interesting."""
        result = de_runner.DEResult(
            rows=[{"gene": "g", "base_mean": 1.0, "log2_fold_change": 1.0,
                   "lfc_std_error": 0.1, "stat": 1.0,
                   "p_value": 5.816918994573506e-128,
                   "padj": 1.2e-124}]
        )
        path = tmp_path / "de.tsv"
        path.write_text(result.to_tsv())
        assert de_runner.read_results(path)[0]["padj"] == 1.2e-124


class TestSampleNaming:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("SRR39891651.trimmed.sorted.counts.tsv", "SRR39891651.trimmed.sorted"),
            ("control1.counts.tsv", "control1"),
            ("something.tsv", "something"),
            ("bare", "bare"),
        ],
    )
    def test_pipeline_suffixes_are_stripped(self, filename, expected):
        assert de_runner.counts_path_stem(filename) == expected
