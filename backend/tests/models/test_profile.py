import pytest
from pymongo.errors import DuplicateKeyError

from app.models import Profile

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

    async def test_owner_id_is_its_own_stringified_object_id(self):
        profile = await Profile(
            username="hopper", display={"emoji": "⚓", "colour": "#4a9eff"}
        ).insert()

        assert str(profile.id) == profile.owner_id()
