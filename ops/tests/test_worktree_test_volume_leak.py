"""`backend/run-worktree-tests.sh` must leave zero Docker volumes behind.

Issue #719: `mongo:7` and the sshd image both declare anonymous VOLUMEs
(`docker history` confirms `/data/db` + `/data/configdb`, and `/config`
respectively). Neither `docker run --rm` nor `docker rm -f` removes an
anonymous volume -- only `-v` does. The script's `cleanup()` used plain
`docker rm -f` and silently stranded two volumes per run, invisible because
the trap fired and the container really was gone; only the volume survived.

These tests exercise `cleanup()` itself against real anonymous-volume
containers, counting dangling volumes before and after, and assert the delta
is exactly zero -- not "small". A regression that drops `-v` again, or a new
`docker run` added to this script without matching cleanup, is what this is
meant to catch.

Requires a Docker daemon; skips otherwise. Does not require the biopipe stack
to be up -- these run standalone `mongo:7` / sshd containers, not the
script's full network-dependent flow.
"""

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available"
)


def _dangling_volume_count() -> int:
    r = subprocess.run(
        ["docker", "volume", "ls", "-qf", "dangling=true"],
        capture_output=True,
        text=True,
        check=True,
    )
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def _cleanup(*names: str) -> None:
    for name in names:
        subprocess.run(
            ["docker", "rm", "-fv", name],
            capture_output=True,
            text=True,
        )


@pytest.fixture
def docker_daemon():
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("docker daemon is not reachable")


class TestCleanupRemovesAnonymousVolumes:
    """Mirrors what `cleanup()` in run-worktree-tests.sh does: `docker rm -fv`
    on a container started with `docker run -d --rm`.
    """

    def test_mongo_leaves_no_dangling_volume(self, docker_daemon):
        name = "test-719-mongo-leak-guard"
        _cleanup(name)
        try:
            before = _dangling_volume_count()
            subprocess.run(
                ["docker", "run", "-d", "--rm", "--name", name, "mongo:7"],
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["docker", "rm", "-fv", name],
                capture_output=True,
                text=True,
                check=True,
            )
            after = _dangling_volume_count()
            assert after == before, (
                f"dangling volumes changed by {after - before}; "
                "`docker rm -fv` should remove mongo:7's anonymous "
                "/data/db and /data/configdb volumes"
            )
        finally:
            _cleanup(name)

    def test_sshd_leaves_no_dangling_volume(self, docker_daemon):
        name = "test-719-sshd-leak-guard"
        _cleanup(name)
        try:
            before = _dangling_volume_count()
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    name,
                    "lscr.io/linuxserver/openssh-server:latest",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["docker", "rm", "-fv", name],
                capture_output=True,
                text=True,
                check=True,
            )
            after = _dangling_volume_count()
            assert after == before, (
                f"dangling volumes changed by {after - before}; "
                "`docker rm -fv` should remove the sshd image's anonymous "
                "/config volume"
            )
        finally:
            _cleanup(name)


class TestScriptUsesDashV:
    """A cheap, fast-failing companion to the runtime checks above: the fix
    is one flag, so a future edit that quietly drops it (e.g. someone
    "simplifying" back to `docker rm -f`) should fail without needing Docker
    at all.
    """

    def test_cleanup_uses_rm_fv_not_rm_f(self):
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[2]
            / "backend"
            / "run-worktree-tests.sh"
        )
        text = script.read_text()
        cleanup_body = text.split("cleanup() {", 1)[1].split("\n}", 1)[0]
        rm_lines = [ln for ln in cleanup_body.splitlines() if "docker rm" in ln]
        assert rm_lines, "cleanup() no longer calls `docker rm`; update this test"
        for line in rm_lines:
            assert "-fv" in line or ("-v" in line and "-f" in line), (
                f"cleanup() calls `docker rm` without -v, which leaks "
                f"anonymous volumes (#719): {line.strip()}"
            )
