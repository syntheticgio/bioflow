"""Every metadata field explains itself.

`FieldDef.help` is what the Record form's ⓘ marker shows, and the marker is
rendered only when `help` is set. A field without one therefore looks finished
-- a labelled input, no marker, nothing broken -- while quietly being the one
field on the form nobody can interpret. That is how the form shipped with
help on roughly half its fields and nothing failing (#795).

So the rule here is coverage, not a companion exemption set. Unlike the
metric-info registry on the frontend, which exempts rows whose label already
says everything, every field on this form is a question being asked of the
user: if the label alone were enough there would be nothing to decide, and if
it is not, the marker is the only place the answer can be given. `help` is a
sentence about what to put in the box and why it matters downstream -- not a
restatement of the label, which is what the length and shape checks below
are for.
"""

from app.metadata import schemas


def _all_fields() -> list[schemas.FieldDef]:
    """Every FieldDef defined anywhere in the module.

    Deduplicated by object identity rather than by key, for the reason
    `test_schemas_open_vocabulary._all_enum_fields` spells out: the field
    tuples are shared across several FORMAT_FIELDS/ROLE_FIELDS entries, and
    several keys (`reference_build`, `source`, `organism`) are defined more
    than once with genuinely different text. Keying by `field.key` would
    collapse those and let an unexplained copy hide behind an explained one.
    """
    seen: dict[int, schemas.FieldDef] = {}
    for group in (
        schemas.COMMON_FIELDS,
        *schemas.FORMAT_FIELDS.values(),
        *schemas.ROLE_FIELDS.values(),
    ):
        for field in group:
            seen.setdefault(id(field), field)
    return list(seen.values())


def test_the_helper_sees_every_duplicate_definition():
    """Guards the docstring above. Measured 2026-08-23: 61 distinct FieldDef
    objects, with `reference_build` defined four times (alignment, variants,
    reference, intervals) and `source` and `organism` twice each. If this
    collection ever starts deduplicating by key, the coverage test below
    quietly narrows and never fails."""
    fields = _all_fields()
    keys = [f.key for f in fields]
    assert keys.count("reference_build") == 4, (
        f"expected all four reference_build definitions, got "
        f"{keys.count('reference_build')}"
    )
    assert len(fields) == 61, (
        f"expected 61 distinct FieldDefs, got {len(fields)}. If you added a "
        "field, give it help text and update this number."
    )


class TestHelpCoverage:
    def test_every_field_has_help(self):
        missing = [f.key for f in _all_fields() if not f.help]
        assert missing == [], (
            f"fields with no help text, so no ⓘ marker on the form: {missing}"
        )

    def test_help_is_a_sentence_not_a_restated_label(self):
        """A three-word gloss passes a truthiness check while explaining
        nothing, which is the failure mode this whole file exists for."""
        too_short = [
            (f.key, f.help)
            for f in _all_fields()
            if f.help is not None and len(f.help) < 30
        ]
        assert too_short == [], f"help text too short to explain anything: {too_short}"

    def test_help_ends_a_sentence(self):
        unpunctuated = [
            (f.key, f.help)
            for f in _all_fields()
            if f.help is not None and not f.help.rstrip().endswith((".", "?", "!"))
        ]
        assert unpunctuated == [], f"help text is not a sentence: {unpunctuated}"

    def test_help_does_not_merely_repeat_the_label(self):
        repeats = [
            (f.key, f.help)
            for f in _all_fields()
            if f.help is not None and f.help.rstrip(".").strip().lower()
            == f.label.strip().lower()
        ]
        assert repeats == [], f"help text restates the label: {repeats}"


class TestHelpReachesTheForm:
    def test_to_dict_carries_help(self):
        """The form reads the serialized field, not the dataclass. A help
        string that never crosses that boundary is not on the form."""
        for field in _all_fields():
            assert field.to_dict()["help"] == field.help
