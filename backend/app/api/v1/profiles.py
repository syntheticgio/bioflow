"""Profile endpoints: the startup picker's whole API.

Every route here is deliberately *outside* the owner partition, and takes no
`OwnerDep`. These are the calls a client makes before it has a profile to send
-- requiring the header to list or choose a profile would mean needing a
profile to get one.

Not an authentication surface. The password checked on select is a speed bump
that stops someone entering the wrong profile by accident on a shared laptop;
it protects nothing, since the API stays unauthenticated and any client can
send any profile's id in the X-BioFlow-Profile header and read that profile's
data without being asked. See
docs/superpowers/specs/2026-07-31-profiles-design.md, "Passwords are a speed
bump".
"""

from beanie import PydanticObjectId
from fastapi import APIRouter, status

from app.api.v1.schemas import ProfileCreate, ProfileOut, ProfileSelect
from app.errors import NotFoundError, ValidationError, WrongProfilePasswordError
from app.models import Profile
from app.models.base import utcnow
from app.services import profile_service

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileOut])
async def list_profiles() -> list[ProfileOut]:
    """Every profile, for the startup picker.

    Sorted by username because that is what the picker matches on and it gives
    a stable order on a fresh install, where `last_used_at` is null for all of
    them.
    """
    profiles = await Profile.find_all().sort("+username").to_list()
    return [ProfileOut.of(p) for p in profiles]


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(body: ProfileCreate) -> ProfileOut:
    profile = await profile_service.create_profile(
        username=body.username,
        password=body.password,
        email=body.email,
        is_first_boot=body.is_first_boot,
    )
    return ProfileOut.of(profile)


@router.post("/{profile_id}/select", response_model=ProfileOut)
async def select_profile(profile_id: str, body: ProfileSelect | None = None) -> ProfileOut:
    """Enter a profile, checking its password if it has one.

    The body is optional: the picker posts nothing at all for a profile with
    no password, which is the default and the common case.

    Nothing is issued on success -- no token, no cookie, no session. The
    client simply starts sending this profile's id in X-BioFlow-Profile, which
    it could have done without ever calling here. That is the honest shape of
    a speed bump, and the reason this route stays cheap.
    """
    profile = await _load(profile_id)

    # `verify_password` returns True for a profile with no hash, so the empty
    # string passed when the client sends no body is only ever compared
    # against a profile that actually has a password set.
    if not profile_service.verify_password(profile, (body.password if body else None) or ""):
        raise WrongProfilePasswordError(f"Wrong password for {profile.username!r}")

    # Written here and nowhere else. The model documents this as "written on
    # successful profile selection, not on every request that uses the
    # profile" -- stamping it in `get_current_owner` instead would turn the
    # picker's sort key into an activity log and put a write on every read.
    # After the check, so a failed attempt cannot float a profile nobody
    # entered to the top of the picker.
    profile.last_used_at = utcnow()
    await profile.save()
    return ProfileOut.of(profile)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str) -> None:
    """Delete an empty, non-last, non-adopted profile.

    The service refuses for three distinct reasons and gives each its own
    `code` (`last_profile`, `profile_not_empty`, `adopted_legacy_owner`) so
    the picker can tell the one actionable refusal from the two permanent
    ones. They are all 409s; the status alone does not distinguish them.
    """
    await profile_service.delete_profile(profile_id)


async def _load(profile_id: str) -> Profile:
    """Fetch a profile by id, turning a malformed id into a 422 rather than a 500.

    Beanie validates the id before it queries, so `Profile.get("not-an-id")`
    raises pydantic's ValidationError -- not an AppError, so it has no handler
    and a typo'd path parameter escapes as an unhandled 500. Taking the
    parameter as a plain `str` and converting here is what makes that
    catchable; annotating it `PydanticObjectId` would hand the same case to
    FastAPI's own validation and produce a `{"detail": ...}` body that
    `frontend/src/api/client.ts` discards. Same shape of fix as
    `get_current_owner` in app/api/deps.py.

    The two failures are deliberately different codes. A malformed id is a bad
    request (422); an id that is well-formed but names nothing is a missing
    resource (404), and it is the *expected* steady-state failure -- a profile
    id remembered in localStorage goes stale the moment that profile is
    deleted. The picker has to tell "that profile is gone, choose another"
    apart from "that is not an id at all", and one shared code cannot carry
    both.
    """
    try:
        oid = PydanticObjectId(profile_id)
    except Exception as e:
        raise ValidationError(f"Malformed profile id: {profile_id}") from e

    profile = await Profile.get(oid)
    if profile is None:
        raise NotFoundError(f"Profile not found: {profile_id}")
    return profile
