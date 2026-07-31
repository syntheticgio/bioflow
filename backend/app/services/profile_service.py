"""Creating, adopting, and deleting profiles.

**The password here is a speed bump, not security.** It stops someone entering
the *wrong* profile by accident -- a shared laptop, two people, one library. It
protects nothing: the API stays unauthenticated, so any client can put any
profile's id in the X-BioFlow-Profile header and read that profile's data
without ever being asked for a password. `_hash_password` is a salted SHA-256
for exactly that reason, deliberately not bcrypt or argon2 -- adding a
password-hashing dependency would advertise a guarantee the surrounding design
does not make. See docs/superpowers/specs/2026-07-31-profiles-design.md,
"Passwords are a speed bump", and don't let a later change here imply
protection the rest of the app does not provide.

The other thing this module does is first-boot adoption. Every document from
before profiles existed carries `owner: "local"` (the TimestampedDocument
default), and rather than rewrite all of them, the first profile created claims
`"local"` as the owner value its own documents carry. Not one document is
rewritten -- which matters because this repo has no migrations mechanism at
all, and docs/TODO.md records that the last index-definition change took the
API down on startup until the index was dropped by hand.
"""

import hashlib
import secrets

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.errors import ConflictError, ValidationError
from app.logging import get_logger
from app.models import DataObject, Profile, Project

log = get_logger(__name__)


def _hash_password(password: str) -> str:
    """Salted SHA-256, stored as `salt$digest`.

    Per-profile salt so two people who choose the same word do not end up with
    the same stored string. No key stretching, and no apology for that: see the
    module docstring for what this is and is not for.
    """
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}${digest}"


def verify_password(profile: Profile, password: str) -> bool:
    """Whether `password` opens this profile.

    A profile with no password set is open to anyone. That is the default and
    the common case -- returning False for it would turn the speed bump into a
    wall for every user who never asked for one.
    """
    if profile.password_hash is None:
        return True

    salt, _, expected = profile.password_hash.partition("$")
    candidate = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return secrets.compare_digest(candidate, expected)


async def create_profile(
    *,
    username: str,
    password: str | None = None,
    email: str | None = None,
    is_first_boot: bool = False,
) -> Profile:
    """Create a profile; adopt the pre-feature library when `is_first_boot`.

    `is_first_boot` is a claim the caller makes, not a fact, so it gets
    checked. The setup screen is reachable again from a stale tab or a back
    button, and a second adopter would hand an existing library to whoever
    asked last.

    That check is a read-then-write, so it cannot be the only guard: two
    concurrent setup requests can both see an empty collection before either
    inserts. The real guarantee is the `uniq_adopted_legacy_owner` partial
    index, which makes "at most one adopted profile" a database invariant --
    the loser of that race gets a DuplicateKeyError and surfaces as a
    ConflictError here, rather than silently becoming a second owner of
    `"local"`.
    """
    username = username.strip()
    if not username:
        raise ValidationError("Profile username cannot be empty")

    if is_first_boot and await Profile.find_one() is not None:
        raise ValidationError(
            "This installation already has a profile; first-boot setup is done"
        )

    profile = Profile(
        username=username,
        password_hash=_hash_password(password) if password else None,
        email=email,
        adopted_legacy_owner=is_first_boot,
    )
    try:
        await profile.insert()
    except DuplicateKeyError as e:
        raise ConflictError(
            f"A profile named {username!r} already exists",
            details={"username": username},
        ) from e

    log.info(
        "profile_created",
        profile_id=str(profile.id),
        username=username,
        adopted_legacy_owner=profile.adopted_legacy_owner,
    )
    return profile


async def count_owned_documents(owner: str) -> dict[str, int]:
    """How many projects and objects a profile owns, so a deletion refusal
    can report real counts rather than a generic "not empty"."""
    return {
        "projects": await Project.find({"owner": owner}).count(),
        "objects": await DataObject.find({"owner": owner}).count(),
    }


async def delete_profile(profile_id: PydanticObjectId | str) -> None:
    """Delete an empty, non-last profile.

    Refuses to delete the last profile even when it owns nothing. A BioFlow
    with zero profiles drops into the first-boot setup screen, and a setup
    screen on an installation that already has blobs on disk is a state
    nothing else in the app is designed for -- the next profile created there
    would adopt a library it has no relationship to.
    """
    profile = await Profile.get(profile_id)
    if profile is None:
        raise ValidationError(f"Profile not found: {profile_id}")

    if await Profile.find_all().count() == 1:
        raise ConflictError(
            f"Cannot delete {profile.username!r}: it is the only profile",
            details={"username": profile.username},
        )

    # owner_id(), never str(profile.id) -- the adopted profile's documents are
    # filed under "local", and counting by its ObjectId would report zero and
    # let the delete through, stranding the whole pre-feature library under an
    # owner no profile answers to.
    counts = await count_owned_documents(profile.owner_id())
    if any(counts.values()):
        raise ConflictError(
            f"Cannot delete {profile.username!r}: it still owns "
            f"{counts['projects']} project(s) and {counts['objects']} object(s)",
            details={**counts, "username": profile.username},
        )

    await profile.delete()
    log.info("profile_deleted", profile_id=str(profile.id), username=profile.username)
