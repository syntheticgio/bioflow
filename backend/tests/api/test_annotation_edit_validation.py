"""Validation for annotation edits (#297).

These tests exercise the pure validation helpers directly, without Mongo or
the filesystem -- the same seam the routes call before touching either. The
full save/materialize path needs a real source file and database, so it is
covered by the queue/handler tests and the browser, not here.
"""


from app.api.v1.pipelines import (
    _check_identity_keys,
    _validate_edit_value,
)


class TestValidateEditValue:
    def test_rejects_tab_in_value(self):
        err = _validate_edit_value(
            new_value="foo\tbar", field="type", fmt="gff", old_attributes_line=None
        )
        assert err is not None
        assert "Tab" in err

    def test_rejects_newline_in_value(self):
        err = _validate_edit_value(
            new_value="foo\nbar", field="type", fmt="gff", old_attributes_line=None
        )
        assert err is not None

    def test_start_must_be_positive_integer(self):
        err = _validate_edit_value(
            new_value="0", field="start", fmt="gff", old_attributes_line=None
        )
        assert err == "start must be a positive integer"

    def test_start_rejects_non_integer(self):
        err = _validate_edit_value(
            new_value="abc", field="start", fmt="gff", old_attributes_line=None
        )
        assert err == "start must be a positive integer"

    def test_type_must_not_be_empty(self):
        err = _validate_edit_value(
            new_value="  ", field="type", fmt="gff", old_attributes_line=None
        )
        assert err == "type must not be empty"

    def test_source_has_no_special_validation(self):
        err = _validate_edit_value(
            new_value="RefSeq", field="source", fmt="gff", old_attributes_line=None
        )
        assert err is None

    def test_attributes_valid_gff(self):
        err = _validate_edit_value(
            new_value="ID=gene1;Name=foo",
            field="attributes",
            fmt="gff",
            old_attributes_line="ID=gene1",
        )
        assert err is None

    def test_attributes_rejects_malformed(self):
        err = _validate_edit_value(
            new_value="not a valid attribute string",
            field="attributes",
            fmt="gff",
            old_attributes_line=".",
        )
        assert err is not None
        assert "Attribute value" in err


class TestIdentityKeys:
    def test_gff_id_change_rejected(self):
        err = _check_identity_keys(
            new_attrs="ID=gene2;Name=foo", old_attrs="ID=gene1;Name=foo", fmt="gff"
        )
        assert err is not None
        assert "ID" in err

    def test_gff_parent_change_rejected(self):
        err = _check_identity_keys(
            new_attrs="ID=gene1;Parent=mRNA2",
            old_attrs="ID=gene1;Parent=mRNA1",
            fmt="gff",
        )
        assert err is not None
        assert "Parent" in err

    def test_gff_name_change_allowed(self):
        err = _check_identity_keys(
            new_attrs="ID=gene1;Name=bar", old_attrs="ID=gene1;Name=foo", fmt="gff"
        )
        assert err is None

    def test_gtf_gene_id_change_rejected(self):
        err = _check_identity_keys(
            new_attrs='gene_id "g2"; transcript_id "t1"',
            old_attrs='gene_id "g1"; transcript_id "t1"',
            fmt="gtf",
        )
        assert err is not None
        assert "gene_id" in err

    def test_gtf_transcript_id_change_rejected(self):
        err = _check_identity_keys(
            new_attrs='gene_id "g1"; transcript_id "t2"',
            old_attrs='gene_id "g1"; transcript_id "t1"',
            fmt="gtf",
        )
        assert err is not None
        assert "transcript_id" in err

    def test_adding_a_new_key_is_allowed(self):
        err = _check_identity_keys(
            new_attrs="ID=gene1;Name=foo",
            old_attrs="ID=gene1",
            fmt="gff",
        )
        assert err is None
