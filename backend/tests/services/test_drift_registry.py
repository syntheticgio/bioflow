"""Exhaustiveness for the report-root registry.

The failure this guards against is silent: a new report type nobody adds to
REPORT_ROOTS is simply never checked for drift, with nothing raising. Same
shape as the STAR/_SIDECAR_ROLES failure described in CLAUDE.md.
"""

from app.config import settings
from app.services import drift_service


class TestReportRootRegistry:
    def test_every_status_fact_is_classified(self):
        classified = set(drift_service.REPORT_ROOTS) | drift_service.REPORTS_WITHOUT_DIRS
        assert drift_service.ALL_REPORT_STATUS_FACTS == classified

    def test_no_fact_is_both_mapped_and_excluded(self):
        overlap = set(drift_service.REPORT_ROOTS) & drift_service.REPORTS_WITHOUT_DIRS
        assert overlap == set()

    def test_every_mapped_root_is_a_real_report_root(self):
        known = {
            settings.qc_reports_dir,
            settings.bam_stats_dir,
            settings.vcf_stats_dir,
            settings.annotation_stats_dir,
        }
        assert set(drift_service.REPORT_ROOTS.values()) <= known

    def test_each_root_is_claimed_by_exactly_one_predicate(self):
        roots = list(drift_service.REPORT_ROOTS.values())
        assert len(roots) == len(set(roots))


class TestClaimsReport:
    def test_qc_predicate_requires_a_string(self):
        assert drift_service.object_claims_report({"qc_tool": "fastp"}, "qc_tool")
        assert not drift_service.object_claims_report({"qc_tool": None}, "qc_tool")
        assert not drift_service.object_claims_report({}, "qc_tool")

    def test_annotation_predicate_requires_status_ok(self):
        fact = "annotation_stats_status"
        assert drift_service.object_claims_report({fact: "ok"}, fact)
        assert not drift_service.object_claims_report({fact: "failed"}, fact)
        assert not drift_service.object_claims_report({}, fact)

    def test_summary_predicates_require_presence(self):
        fact = "bam_stats_summary"
        assert drift_service.object_claims_report({fact: {"mean_depth": 30}}, fact)
        assert not drift_service.object_claims_report({fact: None}, fact)
        assert not drift_service.object_claims_report({}, fact)
