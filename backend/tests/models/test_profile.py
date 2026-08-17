import pytest
from app.models import Profile
from pymongo.errors import DuplicateKeyError

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


class TestProfile:
    async def test_username_is_required_and_unique(self):
        await Profile(username="ada", display={"emoji": "🧬", "colour": "#4a9eff"}).insert()

        # Named rather than a blind Exception: a bare `raises(Exception)` here
        # would also pass if the model failed to validate at all, which is the
        # opposite of what this asserts. The uniqueness comes from the
        # `uniq_username` index, so the duplicate surfaces from pymongo.
        with pytest.raises(DuplicateKeyError):
            await Profile(username="ada", display={"emoji": "🔬", "colour": "#000"}).insert()

    async def test_password_hash_defaults_to_none(self):
        profile = await Profile(
            username="grace", display={"emoji": "⚓", "colour": "#4a9eff"}
        ).insert()

        assert profile.password_hash is None

    async def test_owner_id_returns_a_plain_non_empty_string(self):
        profile = await Profile(
            username="hopper", display={"emoji": "⚓", "colour": "#4a9eff"}
        ).insert()

        owner = profile.owner_id()

        # `owner` is typed `str` on TimestampedDocument, and PydanticObjectId
        # subclasses ObjectId, not str -- one leaking through here would not
        # fail at the boundary, it would fail much later as a query that
        # silently matches nothing. This is the assertion that must survive
        # first-boot adoption, when the returned value stops tracking the id.
        assert type(owner) is str
        assert owner

        # Correct for a profile that did not adopt the legacy owner, which is
        # every profile today.
        assert owner == str(profile.id)
