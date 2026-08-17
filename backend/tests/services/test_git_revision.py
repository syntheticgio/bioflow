"""Reading the served checkout's revision straight out of `.git`.

Every test builds a `.git` directory by hand rather than shelling out to git,
for the same reason the module parses by hand: the image has no git binary, so
a test that needed one would pass locally and fail in CI's own container.
"""

from pathlib import Path

from app.services.git_revision import Revision, log_revision, read_revision

MAIN_SHA = "df67a0f4c0ffee1234567890abcdef0123456789"
OTHER_SHA = "95ed6733deadbeef1234567890abcdef01234567"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git_dir(
    tmp_path: Path,
    *,
    head: str,
    refs: dict[str, str] | None = None,
    packed: dict[str, str] | None = None,
) -> Path:
    git = tmp_path / ".git"
    _write(git / "HEAD", head + "\n")
    for name, sha in (refs or {}).items():
        _write(git / name, sha + "\n")
    if packed:
        lines = ["# pack-refs with: peeled fully-peeled sorted "]
        lines += [f"{sha} {name}" for name, sha in packed.items()]
        _write(git / "packed-refs", "\n".join(lines) + "\n")
    return git


class TestReadRevision:
    def test_reports_branch_and_sha_from_loose_refs(self, tmp_path):
        git = _git_dir(
            tmp_path,
            head="ref: refs/heads/main",
            refs={"refs/heads/main": MAIN_SHA, "refs/remotes/origin/main": MAIN_SHA},
        )

        rev = read_revision(git)

        assert rev == Revision(sha=MAIN_SHA, branch="main", matches_origin_main=True)
        assert rev.short_sha == MAIN_SHA[:7]

    def test_flags_a_checkout_parked_on_another_branch(self, tmp_path):
        """The #452 case: on a feature branch, behind the merged fix."""
        git = _git_dir(
            tmp_path,
            head="ref: refs/heads/fix/394-pin-node-ssh-host-keys",
            refs={
                "refs/heads/fix/394-pin-node-ssh-host-keys": OTHER_SHA,
                "refs/remotes/origin/main": MAIN_SHA,
            },
        )

        rev = read_revision(git)

        assert rev.branch == "fix/394-pin-node-ssh-host-keys"
        assert rev.sha == OTHER_SHA
        assert rev.matches_origin_main is False

    def test_reads_refs_out_of_packed_refs(self, tmp_path):
        """`git pack-refs` (or a fresh clone) leaves no loose ref file."""
        git = _git_dir(
            tmp_path,
            head="ref: refs/heads/main",
            packed={"refs/heads/main": MAIN_SHA, "refs/remotes/origin/main": MAIN_SHA},
        )

        rev = read_revision(git)

        assert rev.sha == MAIN_SHA
        assert rev.matches_origin_main is True

    def test_a_loose_ref_beats_a_stale_packed_one(self, tmp_path):
        """packed-refs keeps stale entries after an update; the loose file wins."""
        git = _git_dir(
            tmp_path,
            head="ref: refs/heads/main",
            refs={"refs/heads/main": MAIN_SHA},
            packed={"refs/heads/main": OTHER_SHA, "refs/remotes/origin/main": MAIN_SHA},
        )

        assert read_revision(git).sha == MAIN_SHA

    def test_ignores_peeled_tag_lines_in_packed_refs(self, tmp_path):
        git = _git_dir(tmp_path, head="ref: refs/heads/main")
        _write(
            git / "packed-refs",
            "# pack-refs with: peeled fully-peeled sorted \n"
            f"{MAIN_SHA} refs/heads/main\n"
            f"^{OTHER_SHA}\n"
            f"{MAIN_SHA} refs/remotes/origin/main\n",
        )

        rev = read_revision(git)

        assert rev.sha == MAIN_SHA
        assert rev.matches_origin_main is True

    def test_detached_head_reports_the_sha_with_no_branch(self, tmp_path):
        git = _git_dir(
            tmp_path,
            head=OTHER_SHA,
            refs={"refs/remotes/origin/main": MAIN_SHA},
        )

        rev = read_revision(git)

        assert rev.branch is None
        assert rev.sha == OTHER_SHA
        assert rev.matches_origin_main is False

    def test_unknown_origin_main_is_not_reported_as_a_mismatch(self, tmp_path):
        """No remote is not the same claim as "you are on the wrong branch"."""
        git = _git_dir(
            tmp_path,
            head="ref: refs/heads/main",
            refs={"refs/heads/main": MAIN_SHA},
        )

        assert read_revision(git).matches_origin_main is None

    def test_no_git_directory_means_running_an_image(self, tmp_path):
        """The shipped container has no source tree, and that is not an error."""
        assert read_revision(tmp_path / "nope" / ".git") is None

    def test_an_unresolvable_head_ref_reports_nothing(self, tmp_path):
        """HEAD naming a ref that exists nowhere -- a freshly `git init`ed tree
        with no commit. Better to say nothing than to invent a revision."""
        git = _git_dir(tmp_path, head="ref: refs/heads/main")

        assert read_revision(git) is None


class TestLinkedWorktrees:
    """`git worktree add` writes a `.git` *file*, and splits refs across two
    directories -- which is what `worktree-up.sh`'s stacks are mounting."""

    def _worktree(self, tmp_path: Path, *, branch: str, sha: str) -> tuple[Path, Path]:
        common = tmp_path / "main" / ".git"
        _write(common / "refs" / "remotes" / "origin" / "main", MAIN_SHA + "\n")
        _write(common / "refs" / "heads" / branch, sha + "\n")

        wt_gitdir = common / "worktrees" / "wt"
        _write(wt_gitdir / "HEAD", f"ref: refs/heads/{branch}\n")
        _write(wt_gitdir / "commondir", "../..\n")

        pointer = tmp_path / "wt" / ".git"
        _write(pointer, f"gitdir: {wt_gitdir}\n")
        return pointer, wt_gitdir

    def test_follows_the_pointer_file_and_the_shared_refs(self, tmp_path):
        pointer, _ = self._worktree(tmp_path, branch="feat/thing", sha=OTHER_SHA)

        rev = read_revision(pointer)

        assert rev.branch == "feat/thing"
        assert rev.sha == OTHER_SHA
        # origin/main lives only in the common dir, reached via commondir.
        assert rev.matches_origin_main is False

    def test_an_unreachable_gitdir_reports_nothing(self, tmp_path):
        """The pointer holds a *host* path, which usually does not exist
        inside the container. Nothing to say beats a wrong answer."""
        pointer = tmp_path / "wt" / ".git"
        _write(pointer, "gitdir: /Users/someone/checkout/.git/worktrees/wt\n")

        assert read_revision(pointer) is None


class TestLogRevision:
    def test_never_raises_on_a_malformed_git_directory(self, tmp_path):
        """A diagnostic that can break startup is worse than no diagnostic."""
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").mkdir()  # a directory where a file belongs

        assert log_revision(git) is None

    def test_returns_the_revision_it_logged(self, tmp_path):
        git = _git_dir(
            tmp_path,
            head="ref: refs/heads/main",
            refs={"refs/heads/main": MAIN_SHA, "refs/remotes/origin/main": MAIN_SHA},
        )

        assert log_revision(git).sha == MAIN_SHA
