#!/usr/bin/env python3
"""Decides whether a Bash command invokes compose against the host's stack.

Reads the command on stdin. Exit 0 means "yes, this is a host compose
invocation"; exit 1 means "no". `block-compose-in-worktree.sh` is the only
caller: it asks this question first, and only then checks whether the working
directory is a worktree.

This exists because the guard used to answer the question with a regex over
the raw command string, which cannot tell an invocation from a mention (#549).
It blocked writing a Python file whose docstring said "docker compose", and
blocked filing the issue that reported it -- because both commands contain
those characters somewhere. Tokenizing is what separates the two: after
shlex, a quoted argument is one opaque token, and heredoc bodies -- which
are data passed to stdin rather than shell commands -- are stripped before
tokenizing so prose carrying shell operators does not misfire (#648).

Deliberately *not* a general shell parser. It reads a command line well
enough to find the command words, and anything it cannot read falls back to
blocking (see `_fail_closed`), because a miss here is the silent failure the
guard exists to prevent.
"""

import re
import shlex
import sys

# Command words that mean the compose call runs somewhere other than this
# host's docker daemon -- inside a container that was just created, or inside
# one already running. Either way it is not the biopipe stack on 5173.
_CONTAINER_SUBCOMMANDS = {"run", "exec"}

# Operators that end one command and start another, so the next token is a
# command word again rather than an argument.
_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "{", "}", "\n"}

_PROJECT_FLAGS = {"-p", "--project-name"}

# Shells that, run as a host command, execute their `-c` argument here. The
# same shells appearing *after* `docker exec`/`docker run` run in a container
# and are never reached, because those subcommands short-circuit first.
_HOST_SHELLS = {"sh", "bash", "zsh", "dash"}


def _looks_like_compose(text: str) -> bool:
    """Cheap check for the phrase appearing anywhere, quoting be damned.

    Used only to decide how to fail when parsing breaks down: a command that
    never mentions compose cannot be a compose invocation however it parses.
    """
    normalized = " ".join(text.split())
    return "docker compose" in normalized or "docker-compose" in normalized


_HEREDOC_RE = re.compile(r'(<<-?)\s*(?:\'([^\']+)\'|"([^"]+)"|\\?([^\s;&|()<>]+))')


def _strip_heredocs(text: str) -> str:
    """Strips heredoc bodies from shell command text before tokenizing.

    A heredoc body (and its terminator line) is data passed to stdin, never a
    command executed by the host shell. Leaving heredoc body lines in the
    input can cause prose containing shell operators (e.g. `;`, `&&`) to be
    split into command segments, some of which may start with `docker compose`
    (#648).

    Line breaks outside heredoc bodies are replaced with `;` so shlex sees
    command boundaries across lines.
    """
    lines = text.splitlines(keepends=True)
    cleaned_lines = []
    pending_heredocs = []  # list of (delimiter, strip_tabs)

    i = 0
    while i < len(lines):
        line = lines[i]

        if pending_heredocs:
            delimiter, strip_tabs = pending_heredocs[0]
            line_str = line.rstrip("\r\n")
            check_str = line_str.lstrip("\t") if strip_tabs else line_str
            if check_str == delimiter:
                pending_heredocs.pop(0)
            i += 1
            continue

        if line.endswith("\n") or line.endswith("\r"):
            cleaned_lines.append(line.rstrip("\r\n") + " ; \n")
        else:
            cleaned_lines.append(line)

        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
            t_idx = 0
            while t_idx < len(tokens):
                tok = tokens[t_idx]
                strip_tabs = False
                delimiter = None

                if tok in ("<<", "<<-"):
                    strip_tabs = (tok == "<<-")
                    if t_idx + 1 < len(tokens):
                        next_tok = tokens[t_idx + 1]
                        if next_tok == "-":
                            if t_idx + 2 < len(tokens):
                                strip_tabs = True
                                delimiter = tokens[t_idx + 2]
                                t_idx += 2
                            else:
                                delimiter = "-"
                                t_idx += 1
                        elif next_tok.startswith("-") and len(next_tok) > 1 and tok == "<<":
                            strip_tabs = True
                            delimiter = next_tok[1:]
                            t_idx += 1
                        else:
                            delimiter = next_tok
                            t_idx += 1
                if delimiter:
                    if delimiter.startswith("\\"):
                        delimiter = delimiter[1:]
                    pending_heredocs.append((delimiter, strip_tabs))
                t_idx += 1
        except ValueError:
            for m in _HEREDOC_RE.finditer(line):
                op = m.group(1)
                delim = m.group(2) or m.group(3) or m.group(4)
                if delim:
                    if delim.startswith("\\"):
                        delim = delim[1:]
                    pending_heredocs.append((delim, op == "<<-"))

        i += 1

    return "".join(cleaned_lines)


def _commands(tokens: list[str]) -> list[list[str]]:
    """Splits a token stream into the separate commands it runs.

    Each returned list still carries any leading `VAR=value` assignments;
    `_leading_assignments` splits those off, because the guard needs to read
    COMPOSE_PROJECT_NAME rather than merely skip past it.
    """
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
            commands.append([])
            continue
        commands[-1].append(token)

    return [command for command in commands if command]


def _names_a_project(command: list[str], assignments: list[str]) -> bool:
    """True when the caller has said which stack they mean.

    An explicit project name is the documented escape hatch: it is also what
    lets ops/worktree-up.sh's own compose calls through.
    """
    if any(a.startswith("COMPOSE_PROJECT_NAME=") for a in assignments):
        return True
    for token in command:
        if token in _PROJECT_FLAGS or token.startswith("--project-name="):
            return True
    return False


def _leading_assignments(tokens: list[str]) -> list[str]:
    assignments = []
    for token in tokens:
        if "=" in token and not token.startswith("-"):
            head, _, _ = token.partition("=")
            if head and head.replace("_", "").isalnum():
                assignments.append(token)
                continue
        break
    return assignments


def _shell_payload(command: list[str]) -> str | None:
    """The script a host shell was asked to run, if this is such a command.

    `bash -c '...'` runs on this host, so its payload has to be read rather
    than treated as an opaque argument. The container cases never reach here.
    """
    word = command[0].rsplit("/", 1)[-1]
    if word not in _HOST_SHELLS:
        return None
    for i, token in enumerate(command[1:], start=1):
        if token == "-c" and i + 1 < len(command):
            return command[i + 1]
    return None


def _is_host_compose(command: list[str], assignments: list[str]) -> bool:
    if not command:
        return False

    word = command[0].rsplit("/", 1)[-1]

    payload = _shell_payload(command)
    if payload is not None:
        return targets_host_compose(payload)

    if word == "docker-compose":
        return not _names_a_project(command, assignments)

    if word != "docker":
        return False

    # `docker exec <container> ...` and `docker run <image> ...` address a
    # container, not this host's stack -- whatever they run inside it.
    args = [a for a in command[1:] if not a.startswith("-")]
    if args and args[0] in _CONTAINER_SUBCOMMANDS:
        return False
    if args and args[0] == "compose":
        return not _names_a_project(command, assignments)

    return False


def targets_host_compose(text: str) -> bool:
    if not text.strip():
        return False

    cleaned = _strip_heredocs(text)
    if not cleaned.strip():
        return False

    try:
        lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # Unbalanced quotes, most often. Fail closed only if the text mentions
        # compose at all; an unparseable `echo "oops` is not this guard's
        # business.
        return _looks_like_compose(text)

    for command in _commands(tokens):
        assignments = _leading_assignments(command)
        if _is_host_compose(command[len(assignments) :], assignments):
            return True

    return False


def main() -> int:
    return 0 if targets_host_compose(sys.stdin.read()) else 1


if __name__ == "__main__":
    sys.exit(main())
