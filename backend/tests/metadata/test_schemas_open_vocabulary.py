"""Every ENUM field is deliberately either open or closed.

An open field's options are suggestions from a vocabulary someone else owns
(NCBI, an instrument vendor, a lab's kit names). A closed field's options are
the complete set, defined by this repo or a published spec. The distinction
drives both the widget and whether an off-list value warns, so a field in
neither camp -- or both -- is a bug.
"""

from app.metadata import schemas
from app.metadata.schemas import FieldType

# Vocabularies this repo or a spec defines completely. Kept as a literal list
# rather than derived, so that adding an ENUM field forces a decision here
# instead of defaulting into one.
CLOSED_ENUM_FIELDS = {
    "sequence_type",   # derived from the SequenceType enum
    "sex",
    "read_type",
    "mate",
    "variant_type",
    "assembly_level",  # NCBI's fixed four
}

OPEN_ENUM_FIELDS = {
    "organism",
    "assay",
    "library_prep",
    "platform",
    "reference_build",
    "aligner",
    "variant_caller",
    "interval_type",
}


def _all_enum_fields():
    """Every ENUM FieldDef defined anywhere in the module.

    Deliberately walks the source tuples rather than `all_known_fields()`.
    That helper builds a dict keyed by field key using `setdefault`, so it
    keeps only the *first* definition per key and silently drops the rest --
    and `reference_build` is defined twice as an ENUM. Checked 2026-08-06: the
    alignment copy happens to win, so a test built on that helper would pass
    while never once looking at the variant copy. Silent-skip, not a test.

    Deduplicated by object identity, not by key: the field tuples are shared
    across several FORMAT_FIELDS/ROLE_FIELDS entries, so a plain walk yields
    the same object many times (`reference_build` five times, `aligner`
    three). Keying by `f.key` instead would collapse the two genuinely
    distinct `reference_build` definitions back into one.
    """
    seen: dict[int, schemas.FieldDef] = {}
    for group in (
        schemas.COMMON_FIELDS,
        *schemas.FORMAT_FIELDS.values(),
        *schemas.ROLE_FIELDS.values(),
    ):
        for field in group:
            if field.type is FieldType.ENUM:
                seen.setdefault(id(field), field)
    return list(seen.values())


def test_the_helper_sees_both_reference_build_enums():
    """Guards the docstring above. Measured 2026-08-06: 15 distinct ENUM
    FieldDef objects over 14 keys, `reference_build` being the only key with
    two. If this collection ever starts deduplicating by key, every other
    test in this file quietly narrows and none of them fails."""
    fields = _all_enum_fields()
    keys = [f.key for f in fields]
    assert keys.count("reference_build") == 2, (
        f"expected both ENUM copies, got {keys.count('reference_build')}"
    )
    assert len(fields) == 15, (
        f"expected 15 distinct ENUM FieldDefs, got {len(fields)}. If you added "
        "one, add it to OPEN_ENUM_FIELDS or CLOSED_ENUM_FIELDS and update this "
        "number."
    )


class TestOpenClosedPartition:
    def test_every_enum_field_is_open_or_closed(self):
        for field in _all_enum_fields():
            in_open = field.key in OPEN_ENUM_FIELDS
            in_closed = field.key in CLOSED_ENUM_FIELDS
            assert in_open or in_closed, (
                f"{field.key} is an ENUM in neither OPEN_ENUM_FIELDS nor "
                "CLOSED_ENUM_FIELDS. Decide whether its vocabulary is owned "
                "by this repo or by someone else."
            )

    def test_no_enum_field_is_both(self):
        overlap = OPEN_ENUM_FIELDS & CLOSED_ENUM_FIELDS
        assert not overlap, f"contradictory: {overlap}"

    def test_open_fields_carry_the_flag(self):
        for field in _all_enum_fields():
            if field.key in OPEN_ENUM_FIELDS:
                assert field.open_vocabulary is True, (
                    f"{field.key} is listed open but its FieldDef does not "
                    "set open_vocabulary=True"
                )

    def test_closed_fields_do_not_carry_the_flag(self):
        for field in _all_enum_fields():
            if field.key in CLOSED_ENUM_FIELDS:
                assert field.open_vocabulary is False, (
                    f"{field.key} is listed closed but sets open_vocabulary"
                )


class TestOtherSentinelIsGone:
    def test_no_open_field_offers_other(self):
        """With free text available, storing the literal 'Other' is strictly
        worse than storing the real answer."""
        for field in _all_enum_fields():
            if field.key in OPEN_ENUM_FIELDS:
                assert "Other" not in field.options, (
                    f"{field.key} still offers 'Other' as a selectable value"
                )

    def test_open_fields_still_offer_suggestions(self):
        """Dropping 'Other' must not empty a list -- the suggestions are the
        entire point of a combo over a plain text box."""
        for field in _all_enum_fields():
            if field.key in OPEN_ENUM_FIELDS:
                assert field.options, f"{field.key} has no suggestions left"
