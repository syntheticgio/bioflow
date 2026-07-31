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
from pydantic import ValidationError as PydanticValidationError
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
    # Compared as bytes: compare_digest raises TypeError on str arguments
    # containing non-ASCII, which `_hash_password` can never produce but a
    # hand-edited or corrupted `password_hash` can. Every other malformed shape
    # here already returns False, and encoding makes that true for this one too.
    return secrets.compare_digest(candidate.encode(), expected.encode())


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
    """Projects and objects owned by `owner`, so a deletion refusal can report
    real counts rather than a generic "not empty".

    Deliberately not a complete tally of everything carrying an `owner`. That
    field lives on TimestampedDocument, so `PipelineRun`, `Job`,
    `UploadSession` and `Schedule` have it too -- but those are derived or
    transient, and none of them is something a user would go and delete to
    empty a profile. Projects and objects are what the refusal message tells
    them to clear, so those are what it counts.

    This is safe to be partial only because `delete_profile` refuses the
    adopted profile outright. Without that guard, an installation whose
    `owner="local"` documents were all runs and jobs -- projects deleted, run
    history kept -- would count zero here and delete the one profile that
    answers for "local".
    """
    return {
        "projects": await Project.find({"owner": owner}).count(),
        "objects": await DataObject.find({"owner": owner}).count(),
    }


async def delete_profile(profile_id: PydanticObjectId | str) -> None:
    """Delete an empty, non-last, non-adopted profile.

    Refuses to delete the last profile even when it owns nothing. A BioFlow
    with zero profiles drops into the first-boot setup screen, and a setup
    screen on an installation that already has blobs on disk is a state
    nothing else in the app is designed for -- the next profile created there
    would adopt a library it has no relationship to.

    Refuses the adopted profile unconditionally, for a related reason: it is
    the only thing that answers for `owner: "local"`, and `create_profile`
    will not let another profile adopt once any profile exists. See the guard
    below.

    All three refusals are conflicts, but they carry *different* codes --
    `last_profile`, `profile_not_empty`, `adopted_legacy_owner` -- because the
    picker recovers from them differently. Only `profile_not_empty` is
    actionable ("delete its projects first", and the counts to say how many);
    the other two are permanent and the UI can only explain them. A shared
    `conflict` code would make the three indistinguishable to the frontend,
    which reads `body.code` and cannot parse prose.

    The codes also give tests something stable to assert on. Asserting the
    exception type alone is not enough here and has already failed once: a
    test for the last-profile refusal kept passing after its guard was
    deleted, because the adopted-owner branch below raised the same
    `ConflictError` and the test could not tell which one it had caught.
    """
    try:
        profile = await Profile.get(profile_id)
    except PydanticValidationError as e:
        # Beanie validates the id before it ever queries, so a malformed one
        # raises pydantic's ValidationError -- which is not an AppError and so
        # has no handler, turning a typo'd path parameter into a 500. Same
        # shape of fix as `get_current_owner` in app/api/deps.py.
        raise ValidationError(f"Malformed profile id: {profile_id}") from e
    if profile is None:
        raise ValidationError(f"Profile not found: {profile_id}")

    if await Profile.find_all().count() == 1:
        raise ConflictError(
            f"Cannot delete {profile.username!r}: it is the only profile",
            details={"username": profile.username},
            code="last_profile",
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
            code="profile_not_empty",
        )

    # Deliberately *after* the count, not before. Both refuse the adopted
    # profile, so ordering cannot change the outcome -- but running the count
    # first is the only place `owner_id()` is observably different from
    # `str(profile.id)`, which keeps that distinction testable instead of
    # short-circuited away. The clearer message wins when the library is
    # non-empty; this one covers the case the count cannot see.
    if profile.adopted_legacy_owner:
        raise ConflictError(
            f"Cannot delete {profile.username!r}: it holds the library that "
            "predates profiles. Everything from before this installation had "
            "profiles is filed under the owner this profile alone answers "
            "for, and no replacement can claim it -- first-boot adoption only "
            "runs when no profile exists. Deleting it would strand that data "
            "with no way to reach it again.",
            details={"username": profile.username, "adopted_legacy_owner": True},
            code="adopted_legacy_owner",
        )

    await profile.delete()
    log.info("profile_deleted", profile_id=str(profile.id), username=profile.username)
