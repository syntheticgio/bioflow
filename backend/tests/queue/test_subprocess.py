"""Subprocess execution: output streaming and process-group cancellation.

These tests spawn real processes. That is deliberate -- the properties under
test (a whole process *group* dying, a pipe reaching EOF, output surviving the
kill) are exactly the ones a mock would assert away.
"""

import os
import signal
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field

import pytest

from app.errors import JobCancelled
from app.queue import executor as executor_module
from app.queue.executor import run_subprocess
from app.queue.registry import JobContext


def make_ctx(**kw) -> JobContext:
    return JobContext(
        job_id=kw.pop("job_id", "job-1"), payload={}, epoch=1, attempts=0, owner="local", **kw
    )


def py(script: str) -> list[str]:
    """A python -c invocation, dedented so tests can use readable literals."""
    return [sys.executable, "-c", textwrap.dedent(script)]


class TestExitCodes:
    def test_returns_the_child_exit_code(self):
        assert run_subprocess(make_ctx(), py("import sys; sys.exit(0)")) == 0
        assert run_subprocess(make_ctx(), py("import sys; sys.exit(3)")) == 3


class TestLogCapture:
    def test_writes_output_to_the_log_without_a_callback(self, tmp_path):
        """The pre-existing fd-redirect path: the kernel copies the bytes and
        this process never sees them."""
        log = tmp_path / "job.log"
        code = run_subprocess(
            make_ctx(),
            py("print('hello'); import sys; print('to stderr', file=sys.stderr)"),
            log_path=str(log),
        )
        assert code == 0
        contents = log.read_text()
        assert "hello" in contents
        assert "to stderr" in contents  # stderr is merged into stdout

    def test_appends_rather_than_truncating(self, tmp_path):
        """A retried job must not erase the previous attempt's log."""
        log = tmp_path / "job.log"
        log.write_text("attempt 1\n")
        run_subprocess(make_ctx(), py("print('attempt 2')"), log_path=str(log))
        contents = log.read_text()
        assert "attempt 1" in contents
        assert "attempt 2" in contents

    def test_runs_without_a_log_path(self):
        assert run_subprocess(make_ctx(), py("print('discarded')")) == 0


class TestOnLine:
    def test_receives_lines_in_order(self):
        seen: list[str] = []
        code = run_subprocess(
            make_ctx(),
            py("[print(f'line {i}') for i in range(5)]"),
            on_line=seen.append,
        )
        assert code == 0
        assert seen == [f"line {i}" for i in range(5)]

    def test_still_writes_the_log(self, tmp_path):
        """Streaming is additive: enabling it must not cost the log file."""
        log = tmp_path / "job.log"
        seen: list[str] = []
        run_subprocess(
            make_ctx(), py("print('captured')"), log_path=str(log), on_line=seen.append
        )
        assert seen == ["captured"]
        assert "captured" in log.read_text()

    def test_captures_stderr(self):
        """fastp reports progress on stderr, so this is the path that matters."""
        seen: list[str] = []
        run_subprocess(
            make_ctx(),
            py("import sys; print('progress: 50%', file=sys.stderr)"),
            on_line=seen.append,
        )
        assert seen == ["progress: 50%"]

    def test_lines_have_no_trailing_newline(self):
        seen: list[str] = []
        run_subprocess(make_ctx(), py("print('bare')"), on_line=seen.append)
        assert seen == ["bare"]

    def test_a_raising_callback_does_not_fail_the_job(self):
        """Progress is advisory everywhere else; a parser bug must not destroy
        hours of completed work."""

        def boom(line: str) -> None:
            raise ValueError("bad parse")

        assert run_subprocess(make_ctx(), py("print('x')"), on_line=boom) == 0

    def test_invalid_utf8_does_not_crash(self):
        """Tool output is not guaranteed to be valid UTF-8."""
        seen: list[str] = []
        code = run_subprocess(
            make_ctx(),
            py("""
                import sys
                sys.stdout.buffer.write(b'bad \\xff byte\\n')
                sys.stdout.buffer.flush()
            """),
            on_line=seen.append,
        )
        assert code == 0
        assert len(seen) == 1

    def test_output_arrives_before_the_process_exits(self):
        """The point of streaming: progress during a long run, not a dump at
        the end. The child prints, waits, then exits -- so a callback that only
        fires after exit would see nothing before the sleep elapses."""
        arrived = threading.Event()
        run_subprocess(
            make_ctx(),
            py("""
                import sys, time
                print('started', flush=True)
                time.sleep(1.5)
            """),
            on_line=lambda line: arrived.set(),
        )
        assert arrived.is_set()

    def test_carriage_return_redraws_are_delivered_as_lines(self):
        """fasterq-dump/prefetch redraw a single progress line with `\\r`
        rather than printing a new line each time. A reader that only splits
        on `\\n` never sees these mid-transfer -- the callback fires once at
        the end when the process closes stdout, which is what made the SRA
        download progress bar look frozen the whole transfer."""
        seen: list[str] = []
        code = run_subprocess(
            make_ctx(),
            py("""
                import sys
                for pct in (10, 50, 100):
                    sys.stdout.write(f'\\rprogress: {pct}%')
                    sys.stdout.flush()
                sys.stdout.write('\\n')
            """),
            on_line=seen.append,
        )
        assert code == 0
        assert seen == ["progress: 10%", "progress: 50%", "progress: 100%"]

    def test_handles_a_large_volume_of_output(self):
        """A chatty tool must not deadlock on a full pipe buffer -- the reason
        the reader runs on its own thread."""
        seen: list[str] = []
        code = run_subprocess(
            make_ctx(),
            py("[print(f'line {i}' + 'x' * 200) for i in range(5000)]"),
            on_line=seen.append,
        )
        assert code == 0
        assert len(seen) == 5000


class TestCancellation:
    def test_cancelling_raises_and_kills_the_child(self):
        ctx = make_ctx()
        ctx.cancel_event.set()  # cancelled before the poll loop's first check

        with pytest.raises(JobCancelled):
            run_subprocess(ctx, py("import time; time.sleep(30)"))

    def test_cancelling_mid_run_raises(self):
        ctx = make_ctx()
        threading.Timer(0.3, ctx.cancel_event.set).start()

        started = time.monotonic()
        with pytest.raises(JobCancelled):
            run_subprocess(ctx, py("import time; time.sleep(30)"))
        assert time.monotonic() - started < 20  # not waiting out the sleep

    def test_cancelling_works_while_streaming(self):
        ctx = make_ctx()
        threading.Timer(0.3, ctx.cancel_event.set).start()

        with pytest.raises(JobCancelled):
            run_subprocess(
                ctx,
                py("""
                    import time
                    while True:
                        print('working', flush=True)
                        time.sleep(0.05)
                """),
                on_line=lambda line: None,
            )

    def test_a_silent_child_is_still_cancellable(self):
        """The reason the reader is on its own thread: a tool that produces no
        output for minutes must not block cancellation behind a pending read."""
        ctx = make_ctx()
        threading.Timer(0.3, ctx.cancel_event.set).start()

        started = time.monotonic()
        with pytest.raises(JobCancelled):
            run_subprocess(
                ctx, py("import time; time.sleep(30)"), on_line=lambda line: None
            )
        assert time.monotonic() - started < 20

    @pytest.mark.parametrize("streaming", [False, True], ids=["piped-fd", "streaming"])
    def test_kills_the_whole_process_group(self, tmp_path, streaming):
        """The property `start_new_session` exists for.

        A pipeline is `fastp | ...`; killing only the direct child orphans the
        rest, which keeps running and keeps consuming the machine. The child
        here spawns a grandchild that would outlive a naive kill, and both must
        be gone.

        The grandchild is given DEVNULL for its stdio so it does not hold the
        inherited pipe. Otherwise killing the parent alone closes the write end
        and the grandchild dies of SIGPIPE -- which would make this test pass
        in streaming mode without the process group ever being signalled.
        """
        pidfile = tmp_path / "grandchild.pid"
        ctx = make_ctx()
        threading.Timer(0.5, ctx.cancel_event.set).start()

        cmd = py(f"""
            import subprocess, sys, time
            child = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(60)'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            open({str(pidfile)!r}, 'w').write(str(child.pid))
            print('spawned', flush=True)
            time.sleep(60)
        """)

        with pytest.raises(JobCancelled):
            run_subprocess(ctx, cmd, on_line=(lambda line: None) if streaming else None)

        grandchild_pid = int(pidfile.read_text())
        # Derived from the grace period rather than hardcoding a number that
        # silently stops covering it if the constant changes. The margin is
        # generous because a loaded CI runner escalates to SIGKILL later than
        # an idle laptop -- though note the CI failure that prompted this was
        # not slowness at all, it was `_pid_alive` misreading a reaped zombie
        # (see its comment); widening this alone did not fix it.
        deadline = time.monotonic() + executor_module.SUBPROCESS_GRACE_SECONDS + 45
        while time.monotonic() < deadline:
            if not _pid_alive(grandchild_pid):
                break
            time.sleep(0.2)

        assert not _pid_alive(grandchild_pid), (
            f"grandchild {grandchild_pid} survived cancellation -- "
            "the process group was not signalled"
        )

    def test_partial_output_survives_cancellation(self, tmp_path):
        """A cancelled run's log is the only evidence of how far it got."""
        log = tmp_path / "job.log"
        ctx = make_ctx()
        threading.Timer(0.5, ctx.cancel_event.set).start()

        with pytest.raises(JobCancelled):
            run_subprocess(
                ctx,
                py("""
                    import time
                    print('phase one done', flush=True)
                    time.sleep(30)
                """),
                log_path=str(log),
                on_line=lambda line: None,
            )
        assert "phase one done" in log.read_text()


@dataclass
class _FakeParser:
    """A minimal ProgressParser: matches every line containing `token`."""

    name: str = "fake-tool"
    token: str = "PROGRESS"
    seen: int = 0
    calls: list = field(default_factory=list)

    def feed(self, line: str) -> bool:
        if self.token not in line:
            return False
        self.seen += 1
        return True

    def snapshot(self) -> dict:
        self.calls.append(self.seen)
        return {"message": f"seen {self.seen}"}


class TestParser:
    def test_snapshot_reaches_ctx_progress(self):
        """The forwarding contract parser= exists for: whatever keys
        snapshot() returns arrive at ctx.progress() unmodified, including one
        no current hand-written parser emits -- that is the drift this seam
        removes."""
        ctx = make_ctx()
        received: list[dict] = []
        ctx._progress_cb = received.append

        parser = _FakeParser()
        code = run_subprocess(
            ctx,
            py("print('PROGRESS one'); print('noise'); print('PROGRESS two')"),
            parser=parser,
        )
        assert code == 0
        assert received == [{"message": "seen 1"}, {"message": "seen 2"}]

    def test_feed_false_publishes_nothing(self):
        ctx = make_ctx()
        received: list[dict] = []
        ctx._progress_cb = received.append

        run_subprocess(ctx, py("print('irrelevant line')"), parser=_FakeParser())
        assert received == []

    def test_passing_both_on_line_and_parser_is_an_error(self):
        with pytest.raises(ValueError):
            run_subprocess(
                make_ctx(),
                py("print('x')"),
                on_line=lambda line: None,
                parser=_FakeParser(),
            )

    def test_a_raising_parser_does_not_fail_the_job(self):
        """Same advisory-only guarantee as on_line, extended to parser=."""

        class BoomParser:
            name = "boom"

            def feed(self, line: str) -> bool:
                raise ValueError("bad parse")

            def snapshot(self) -> dict:
                return {}

        assert run_subprocess(make_ctx(), py("print('x')"), parser=BoomParser()) == 0


class TestParserSilence:
    """structlog here writes straight to stdout via PrintLoggerFactory,
    bypassing stdlib logging entirely -- caplog cannot see it. Patch
    executor.log directly instead."""

    def test_warns_when_a_parser_never_matches_past_the_floor(self, monkeypatch):
        monkeypatch.setattr(executor_module, "PARSER_SILENCE_FLOOR_S", 0.05)
        warnings: list[tuple] = []
        monkeypatch.setattr(
            executor_module.log, "warning", lambda event, **kw: warnings.append((event, kw))
        )
        ctx = make_ctx()

        run_subprocess(
            ctx,
            py("""
                import time
                print('hello')
                time.sleep(0.2)
            """),
            parser=_FakeParser(token="NEVER MATCHES"),
        )

        assert len(warnings) == 1
        event, kw = warnings[0]
        assert event == "progress_parser_silent"
        assert kw["parser"] == "fake-tool"
        assert kw["line_count"] == 1

    def test_no_warning_when_the_parser_matches(self, monkeypatch):
        monkeypatch.setattr(executor_module, "PARSER_SILENCE_FLOOR_S", 0.05)
        warnings: list[tuple] = []
        monkeypatch.setattr(
            executor_module.log, "warning", lambda event, **kw: warnings.append((event, kw))
        )
        ctx = make_ctx()

        run_subprocess(
            ctx,
            py("""
                import time
                print('PROGRESS')
                time.sleep(0.2)
            """),
            parser=_FakeParser(),
        )

        assert warnings == []

    def test_no_warning_below_the_floor(self, monkeypatch):
        """A short job with a parser that never matched is not evidence of a
        broken parser -- most short jobs never print anything worth matching."""
        monkeypatch.setattr(executor_module, "PARSER_SILENCE_FLOOR_S", 30.0)
        warnings: list[tuple] = []
        monkeypatch.setattr(
            executor_module.log, "warning", lambda event, **kw: warnings.append((event, kw))
        )
        ctx = make_ctx()

        run_subprocess(
            ctx, py("print('irrelevant')"), parser=_FakeParser(token="NEVER MATCHES")
        )

        assert warnings == []


def _pid_alive(pid: int) -> bool:
    """True if the pid exists and has not been reaped.

    A killed grandchild is reparented to init and reaped there, so it vanishes
    rather than lingering as a zombie.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Guard against the pid having been recycled into a zombie we can signal.
    #
    # Read /proc directly rather than shelling out to `ps`. /proc is always
    # there on Linux, while `ps` comes from procps and is absent from slim
    # images -- and the old `except OSError: return True` turned that absence
    # into "every zombie is alive", failing the process-group cancellation
    # tests with the opposite of what had happened: they reported the group
    # was never signalled when it had been signalled and reaped correctly.
    # That is why this matters in a job container in particular, where PID 1
    # is the job's shell rather than an init that reaps orphans.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[-1].split()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    # State is the first field after the comm parenthesis; "Z" is a zombie,
    # i.e. dead and merely awaiting a reap.
    return bool(fields) and fields[0] != b"Z"


def test_signal_module_is_used_for_group_termination():
    """Pins the mechanism rather than the symptom: killpg on the group, not
    kill on the pid. A refactor to proc.kill() would pass every behavioural
    test on Linux CI and silently orphan pipelines in practice."""
    import inspect

    from app.queue import executor

    source = inspect.getsource(executor._terminate_group)
    assert "killpg" in source
    assert "getpgid" in source
    assert signal.SIGTERM.name in source
    assert signal.SIGKILL.name in source
