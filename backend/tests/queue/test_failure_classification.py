"""How a killed tool is classified, with and without a hard limit.

Retrying a 137 is right on an unlimited machine -- the host OOM killer fired
under transient pressure and a later attempt may succeed. It is wrong under a
cgroup ceiling, which does not move: the job dies identically on all five
attempts, burning its full runtime each time.
"""

from pathlib import Path

import pytest

from app.errors import PermanentError, RetryableError
from app.queue.pipeline_handlers import _failure


def test_137_is_retryable_when_there_is_no_hard_limit(tmp_path, monkeypatch):
    # The regression guard: existing behaviour on an unlimited machine.
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", None)
    err = _failure(137, tmp_path / "missing.log", tool="minimap2")
    assert isinstance(err, RetryableError)


def test_137_is_terminal_when_a_hard_limit_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(137, tmp_path / "missing.log", tool="minimap2")
    # PermanentError and RetryableError are siblings under AppError, so this
    # single assertion is enough -- it cannot pass for a retryable error.
    assert isinstance(err, PermanentError)


def test_terminal_137_message_names_the_ceiling(tmp_path, monkeypatch):
    # With a known ceiling the cause is known, so the message says it rather
    # than guessing -- the whole reason this branch is worth having.
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(137, tmp_path / "missing.log", tool="minimap2")
    assert "16384 MB hard limit" in str(err)


def test_non_137_exits_are_unaffected_by_the_hard_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.bioflow_hard_mem_mb", 16384)
    err = _failure(1, tmp_path / "missing.log", tool="minimap2")
    assert isinstance(err, PermanentError)
    assert "hard limit" not in str(err)
