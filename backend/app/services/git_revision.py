"""What git revision the running stack is actually serving.

`docker-compose.override.yml` bind-mounts the checkout's `backend/app` into
the api and worker containers, so the code being executed is whatever that
checkout has on disk -- not whatever `VERSION` claims, and not necessarily
`main`. When the checkout is parked on a stale branch, every merged fix it
predates appears to be missing, and the symptom is always some *other*
subsystem's confident, code-level error message. #452 is the worked example:
a node provision failed with "Compose file not found in API container. The API
image must bundle docker-compose.yml at /srv/." -- text that #426 had deleted
from `main` 27 commits earlier. Nothing in the app, the logs, or the error
pointed at the branch.

So this module exists to say the quiet part out loud, once, at startup.

**Parsed by hand rather than shelled out to `git`.** The image has no git
binary (see `backend/Dockerfile` -- git appears only in build stages that
compile bwa-mem2 and Winnowmap, and none of it survives into the final
stage), and adding one to ship a diagnostic would be a poor trade. Everything
here needs is plain text in `.git`: `HEAD` names a ref, a loose ref file holds
a SHA, and `packed-refs` holds the rest. No object decoding, no pack index.

**"Behind by N commits" is deliberately not reported.** Counting commits means
walking the commit graph, which means inflating and delta-resolving packed
objects in pure Python -- a great deal of fragile code for a number that is
strictly less actionable than the ref comparison already here. `HEAD !=
origin/main` is the whole signal: it is either the branch you meant to be on
or it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.logging import get_logger

log = get_logger(__name__)

# Where docker-compose.override.yml mounts the checkout's .git, read-only.
# Absent in the shipped image, which builds from a source tree it does not
# carry -- see `read_revision`'s None return.
GIT_DIR = Path("/srv/.git")

# The branch a checkout is expected to be serving. Anything else is worth a
# line in the logs, even when it is deliberate (a worktree stack, a release
# branch under test).
EXPECTED_REF = "refs/remotes/origin/main"


@dataclass(frozen=True)
class Revision:
    """The served checkout's git state, as far as refs alone can tell it."""

    sha: str
    """Full 40-character commit SHA of HEAD."""

    branch: str | None
    """Branch name, or None when HEAD is detached."""

    matches_origin_main: bool | None
    """Whether HEAD is the same commit as origin/main.

    None when origin/main is unknown to this checkout (no remote, never
    fetched) -- which is not the same as "no, it differs", and the log line
    says so rather than crying wolf.
    """

    @property
    def short_sha(self) -> str:
        return self.sha[:7]


def _read_packed_refs(git_dir: Path) -> dict[str, str]:
    """Map ref name -> SHA from `.git/packed-refs`.

    `^<sha>` continuation lines are peeled tag targets and are skipped: they
    annotate the *previous* line's ref rather than naming one of their own.
    """
    path = git_dir / "packed-refs"
    try:
        text = path.read_text()
    except OSError:
        return {}

    refs: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name:
            refs[name.strip()] = sha.strip()
    return refs


def _follow_gitdir(git_dir: Path) -> Path | None:
    """Resolve a worktree's `.git` *file* to the real gitdir it names.

    `git worktree add` writes a file, not a directory, holding
    `gitdir: <absolute path>`. That path is a *host* path, so inside a
    container it usually resolves to nothing -- `worktree-up.sh` mounts the
    worktree but not the main checkout's `.git`. None here means exactly
    that, and the caller reports it as an unreadable checkout rather than as
    no checkout at all.
    """
    try:
        text = git_dir.read_text()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:") :].strip())
    return target if (target / "HEAD").exists() else None


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    """SHA for a ref, checking the loose file before `packed-refs`.

    That order is git's own: `git pack-refs` leaves stale entries in
    `packed-refs` when a ref is later updated, and the loose file written by
    the update is what wins.
    """
    # A linked worktree's gitdir holds its own HEAD but shares branch and
    # remote refs with the main checkout, which `commondir` points at. Search
    # both, nearest first.
    roots = [git_dir]
    try:
        common = (git_dir / "commondir").read_text().strip()
    except OSError:
        common = ""
    if common:
        roots.append((git_dir / common).resolve())

    for root in roots:
        try:
            if sha := (root / ref).read_text().strip():
                return sha
        except OSError:
            pass
    for root in roots:
        if sha := _read_packed_refs(root).get(ref):
            return sha
    return None


def read_revision(git_dir: Path = GIT_DIR) -> Revision | None:
    """The served checkout's revision, or None when there is no checkout.

    None is the ordinary answer in a shipped image: the user pulled a
    published container and has no source tree, so there is no branch to be
    parked on and nothing to diagnose. Never raises -- a diagnostic that can
    break startup is worse than no diagnostic.
    """
    if git_dir.is_file():
        # A linked worktree points at its real gitdir by absolute host path.
        followed = _follow_gitdir(git_dir)
        if followed is None:
            return None
        git_dir = followed

    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return None

    if head.startswith("ref: "):
        ref = head[len("ref: ") :].strip()
        sha = _resolve_ref(git_dir, ref)
        branch = ref.removeprefix("refs/heads/")
    else:
        # Detached HEAD: the file holds the SHA itself.
        sha, branch = head, None

    if not sha:
        return None

    origin_main = _resolve_ref(git_dir, EXPECTED_REF)
    matches = None if origin_main is None else origin_main == sha

    return Revision(sha=sha, branch=branch, matches_origin_main=matches)


def log_revision(git_dir: Path = GIT_DIR) -> Revision | None:
    """Log the served revision at startup, loudly when it is not origin/main.

    The warning level is the point: a stale checkout is not a fact about the
    environment, it is a bug waiting to be misattributed to whatever code path
    the user happens to exercise next.
    """
    rev = read_revision(git_dir)
    if rev is None:
        log.info("serving_revision", source="image")
        return None

    fields = {
        "sha": rev.short_sha,
        "branch": rev.branch or "(detached)",
    }
    if rev.matches_origin_main is False:
        log.warning(
            "serving_revision_not_origin_main",
            **fields,
            hint="this checkout is not on origin/main; merged fixes may be missing",
        )
    else:
        log.info("serving_revision", **fields, matches_origin_main=rev.matches_origin_main)
    return rev
