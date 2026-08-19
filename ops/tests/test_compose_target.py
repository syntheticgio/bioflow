"""`ops/hooks/compose_target.py`: does a Bash command invoke host compose?

This is the decision `block-compose-in-worktree.sh` rests on, split out of the
hook so it can be tested without a worktree, a payload, or Docker.

The tests are organised around the distinction #549 found the hook could not
make: text that *is* a compose invocation the shell will run, versus the same
characters appearing somewhere the shell will never execute -- inside a quoted
argument, a heredoc body, or a comment. The old regex saw only characters, so
writing a Python file whose docstring mentioned compose was blocked, as was
filing the issue that reported it.

Both directions matter. A miss here is the silent failure the guard exists to
prevent (port 5173 quietly serving a worktree), so the "should block" cases
are as load-bearing as the "should allow" ones.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPO_ROOT / "ops" / "hooks" / "compose_target.py"


def targets_host_compose(command: str) -> bool:
    """Runs the module as the hook does: command on stdin, exit code answers."""
    result = subprocess.run(
        [sys.executable, str(MODULE)],
        input=command,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), (
        f"unexpected exit {result.returncode}: {result.stderr}"
    )
    return result.returncode == 0


# --- Invocations that really would hit the shared stack -------------------

BLOCKED = [
    "docker compose up -d",
    "docker compose up -d --build api web worker",
    "docker-compose up",
    "docker compose restart worker",
    "docker compose exec api python -m pytest tests/ -q",
    "docker compose down",
    # Not the first command in the line.
    "cd backend && docker compose up -d",
    "make thing; docker compose up",
    "true || docker compose logs api",
    # Env assignments precede the command word.
    "FOO=bar docker compose up",
    # Extra whitespace is still an invocation.
    "docker   compose   up",
    # A compose call inside an explicit host shell is still a host call.
    "bash -c 'docker compose up -d'",
    # A real call after a heredoc is still a call (#648): dropping the body
    # must not swallow what comes after the terminator.
    "cat > /tmp/s.md <<'SPEC'\nprose only\nSPEC\ndocker compose up -d",
    "cat > /tmp/s.md <<'SPEC'\nprose only\nSPEC\n&& docker compose up -d",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_real_invocations_are_recognised(command):
    assert targets_host_compose(command), command


# --- Text that mentions compose without invoking it ------------------------

ALLOWED_MENTIONS = [
    # #549 case 3: a heredoc file write whose body mentions compose.
    "cat > test_foo.py <<'EOF'\n\"\"\"Checks docker compose up behaviour.\"\"\"\nEOF",
    'cat > notes.md <<EOF\nRun docker compose up -d to start.\nEOF',
    # #648: heredoc prose that carries a shell operator forms a segment that
    # starts with `docker compose` in the token stream. The body is data.
    "cat > /tmp/s.md <<'SPEC'\n"
    "The stack (docker compose up -d from main) keeps running.\nSPEC",
    "cat > /tmp/s.md <<'SPEC'\n"
    "Build it; docker compose up -d --build api web worker applies the change.\nSPEC",
    "cat > /tmp/s.md <<'SPEC'\n"
    'Rebuild and restart: npm run build && docker compose up -d api.\nSPEC',
    # The reported repro: a quoted command with its own quotes inside.
    "cat > /tmp/example.md <<'EOF2'\n"
    'Run `docker compose exec api python -c "..."` to check.\nEOF2',
    # #549 case 4: filing an issue whose body quotes the phrase.
    'gh issue create --title x --body "the guard blocks docker compose up"',
    "gh pr comment 1 --body 'docker-compose is mentioned here'",
    # Searching for the phrase is not running it.
    "grep -rn 'docker compose' ops/",
    'rg "docker compose up" --glob "*.md"',
    # A comment is never executed.
    "ls # docker compose up would be wrong here",
    # Writing the phrase into a file by other means.
    "echo 'docker compose up' > /tmp/note.txt",
    "python3 -c \"print('docker compose up')\"",
]


@pytest.mark.parametrize("command", ALLOWED_MENTIONS)
def test_mentions_are_not_invocations(command):
    assert not targets_host_compose(command), command


# --- Commands that target a container, not the host ------------------------

ALLOWED_CONTAINER = [
    # #549 case 1: a probe inside a throwaway container.
    "docker run --rm img sh -c 'docker compose version'",
    # #549 case 2: a compose call inside a sidecar, against its own file.
    "docker exec sidecar sh -c 'docker compose -f /app/x.yml pull worker'",
    "docker exec -it foo bash -c \"docker compose ps\"",
]


@pytest.mark.parametrize("command", ALLOWED_CONTAINER)
def test_container_internal_calls_are_not_host_calls(command):
    assert not targets_host_compose(command), command


# --- The existing escape hatch: an explicitly named project ----------------

ALLOWED_NAMED_PROJECT = [
    "docker compose -p other up",
    "docker compose --project-name other up",
    "COMPOSE_PROJECT_NAME=other docker compose up",
]


@pytest.mark.parametrize("command", ALLOWED_NAMED_PROJECT)
def test_named_project_passes_through(command):
    assert not targets_host_compose(command), command


# --- Commands with nothing to do with compose ------------------------------

ALLOWED_UNRELATED = [
    "./ops/worktree-up.sh",
    "./backend/run-worktree-tests.sh tests/ -q",
    "docker ps",
    "docker inspect biopipe-worker-1",
    "git status",
    "",
]


@pytest.mark.parametrize("command", ALLOWED_UNRELATED)
def test_unrelated_commands_pass(command):
    assert not targets_host_compose(command), command


# --- Input the tokenizer cannot parse --------------------------------------


def test_unparseable_command_mentioning_compose_is_blocked():
    """An unbalanced quote must fail closed, not open.

    shlex raises on this. The guard protects against a silent, expensive
    mistake, so a command it cannot read but which mentions compose is
    treated as an invocation -- the caller can name a project to proceed.
    """
    assert targets_host_compose('docker compose up "unclosed')


def test_unparseable_command_without_compose_passes():
    """Failing closed applies only to commands that mention compose at all."""
    assert not targets_host_compose('echo "unclosed')
