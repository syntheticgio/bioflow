"""The exit code a killed tool actually arrives with.

`run_subprocess` returns `subprocess.Popen.returncode` unchanged, and Python
reports a signal death as the *negative* signal number -- -9 for SIGKILL. 137
is the shell's `128 + signal` convention, which nothing in this process ever
produces. Every OOM classifier here was written against 137, so an OOM-killed
job fell through to the generic branch: the user got "bwa-mem2 exited -9" with
no mention of memory (issue #96), and on an unlimited host the kill was
classified terminal instead of retryable.

These tests pin the convention at the boundary that produces it, so a
classifier keyed on the wrong number cannot pass.
"""

import os
import signal
import subprocess
import time

import pytest

from app.errors import PermanentError, RetryableError
from app.queue import download_failures
from app.queue.pipeline_handlers import _failure, _killed_by_signal
from app.queue.tool_handlers import _pull_failure


def test_sigkill_surfaces_as_negative_nine_not_137():
    """The premise every classifier below depends on.

    Asserted against a real process rather than assumed, because the whole
    bug was a plausible-looking assumption about this number.
    """
    proc = subprocess.Popen(["sleep", "30"])
    time.sleep(0.1)
    os.kill(proc.pid, signal.SIGKILL)
    assert proc.wait() == -9


@pytest.mark.parametrize("code", [-9, 137])
def test_pipeline_kill_is_retryable_when_there_is_no_hard_limit(
    code, tmp_path, monkeypatch
):
    """Both conventions classify identically -- -9 is what really arrives.

    137 stays covered because a tool invoked through a shell wrapper could
    still report it, and nothing is gained by making that case worse.
    """
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", None)
    err = _failure(code, tmp_path / "missing.log", tool="bwa-mem2")
    assert isinstance(err, RetryableError)
    assert "out of memory" in str(err)


@pytest.mark.parametrize("code", [-9, 137])
def test_pipeline_kill_is_terminal_under_a_hard_limit(code, tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(code, tmp_path / "missing.log", tool="bwa-mem2")
    assert isinstance(err, PermanentError)
    assert "16384 MB hard limit" in str(err)


@pytest.mark.parametrize("code", [-9, 137])
def test_download_kill_names_memory(code, tmp_path):
    err = download_failures.classify_failure(
        code, tmp_path / "missing.log", "SRR1", tool="fasterq-dump"
    )
    assert isinstance(err, RetryableError)
    assert "out of memory" in str(err)


@pytest.mark.parametrize("code", [-9, 137])
def test_image_pull_kill_is_reported_as_killed(code, tmp_path):
    err = _pull_failure(code, tmp_path / "missing.log", "quay.io/biocontainers/x")
    assert isinstance(err, RetryableError)
    assert "killed" in str(err)


@pytest.mark.parametrize("code", [-9, -15, 137, 143])
def test_killed_by_signal_accepts_both_conventions(code):
    assert _killed_by_signal(code)


@pytest.mark.parametrize("code", [1, 2, 127, 0, -1, 255])
def test_killed_by_signal_rejects_ordinary_exits(code):
    """-1 is SIGHUP and 255 is a common "generic failure" exit.

    Neither is a kill this classifier should claim as one: SIGHUP is not the
    OOM killer, and 255 is an ordinary nonzero exit.
    """
    assert not _killed_by_signal(code)


def test_ordinary_exit_is_unaffected_by_the_hard_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(1, tmp_path / "missing.log", tool="bwa-mem2")
    assert isinstance(err, PermanentError)
    assert "hard limit" not in str(err)
