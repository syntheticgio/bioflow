"""_StreamPump: line splitting, observation, and log writing, driven directly.

`test_subprocess.py` covers the same pump through real processes, which is the
right shape for the properties that need a real kernel (a process group dying,
a pipe reaching EOF). It cannot control *chunk boundaries*: a real pipe decides
for itself where a read stops, so "a line split across two reads" and "an empty
read mid-stream" are unreachable from there and appear only against a real tool
under real timing -- exactly the failure class #403 is about.

These tests drive `_StreamPump.run()` against a fake stream instead, where the
chunking is the input. No subprocess, no thread, no sleeping.
"""

import subprocess
from dataclasses import dataclass, field

from app.queue import executor as executor_module
from app.queue.executor import _StreamPump
from app.queue.registry import JobContext


class FakeStream:
    """A stdout stand-in that hands out exactly the chunks it was given.

    `read1` is what the pump prefers, so this defines it; each call returns one
    prepared chunk and `b""` afterwards, which is what EOF looks like to the
    pump's read loop.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.read_count = 0

    def read1(self, _size: int) -> bytes:
        self.read_count += 1
        return self.chunks.pop(0) if self.chunks else b""


class RaisingStream:
    """A stream whose pipe dies mid-read -- what a killed child leaves behind."""

    def __init__(self, before: list[bytes]) -> None:
        self.before = list(before)

    def read1(self, _size: int) -> bytes:
        if self.before:
            return self.before.pop(0)
        raise OSError("pipe closed by the dying child")


class NoRead1Stream(FakeStream):
    """Some file-like objects only offer `read`; the pump falls back to it."""

    read1 = None  # type: ignore[assignment]

    def __getattribute__(self, name):
        # hasattr(stream, "read1") must be False for the fallback branch.
        if name == "read1":
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def read(self, size: int) -> bytes:
        return FakeStream.read1(self, size)


@dataclass
class RecordingParser:
    """A ProgressParser that matches lines containing `token`."""

    name: str = "fake-tool"
    token: str = "PROGRESS"
    seen: int = 0
    fed: list[str] = field(default_factory=list)

    def feed(self, line: str) -> bool:
        self.fed.append(line)
        if self.token not in line:
            return False
        self.seen += 1
        return True

    def snapshot(self) -> dict:
        return {"message": f"seen {self.seen}"}


def make_ctx(**kw) -> JobContext:
    return JobContext(
        job_id=kw.pop("job_id", "job-1"), payload={}, epoch=1, attempts=0, owner="local", **kw
    )


def drive(
    chunks: list[bytes],
    *,
    parser=None,
    on_line=None,
    log_file=None,
    ctx: JobContext | None = None,
    stream=None,
) -> _StreamPump:
    """Run a pump to completion over `chunks` and hand back the pump."""
    proc = subprocess.CompletedProcess(args=[], returncode=0)
    proc.stdout = stream if stream is not None else FakeStream(chunks)  # type: ignore[attr-defined]
    pump = _StreamPump(proc, log_file, parser, on_line, ctx or make_ctx())  # type: ignore[arg-type]
    pump.run()
    return pump


def lines_from(chunks: list[bytes], **kw) -> list[str]:
    seen: list[str] = []
    drive(chunks, on_line=seen.append, **kw)
    return seen


class TestChunkBoundaries:
    """The same bytes must produce the same lines however the reads land."""

    def test_one_chunk_holding_several_lines(self):
        assert lines_from([b"a\nb\nc\n"]) == ["a", "b", "c"]

    def test_a_line_split_across_two_reads(self):
        """The case a real pipe produces at random and no subprocess test can
        force: a read ends in the middle of a line."""
        assert lines_from([b"hel", b"lo\n"]) == ["hello"]

    def test_a_line_split_across_many_reads(self):
        assert lines_from([b"a", b"b", b"c", b"\n"]) == ["abc"]

    def test_a_delimiter_split_from_its_line(self):
        assert lines_from([b"done", b"\nnext\n"]) == ["done", "next"]

    def test_an_empty_read_mid_stream_is_end_of_stream(self):
        """`b""` is EOF, not a short read -- the pump stops there. Whatever is
        already buffered is still flushed as a final line."""
        assert lines_from([b"before\n", b"", b"after\n"]) == ["before"]

    def test_trailing_output_without_a_newline_is_delivered(self):
        """A tool killed mid-write, or one that simply does not end with a
        newline, must not have its last line swallowed."""
        assert lines_from([b"no trailing newline"]) == ["no trailing newline"]

    def test_identical_output_chunked_three_ways_agrees(self):
        payload = b"alpha\nbeta\rgamma\r\ndelta\n"
        whole = lines_from([payload])
        byte_at_a_time = lines_from([payload[i : i + 1] for i in range(len(payload))])
        lumpy = lines_from([payload[:3], payload[3:9], payload[9:]])
        assert whole == byte_at_a_time == lumpy == ["alpha", "beta", "gamma", "delta"]

    def test_falls_back_to_read_when_read1_is_absent(self):
        seen: list[str] = []
        drive([], on_line=seen.append, stream=NoRead1Stream([b"x\ny\n"]))
        assert seen == ["x", "y"]


class TestDelimiters:
    def test_carriage_return_splits_a_line(self):
        """A `\\r`-redrawn progress bar emits no `\\n` until the tool is done."""
        assert lines_from([b"\rp: 10%\rp: 50%\rp: 100%\n"]) == ["p: 10%", "p: 50%", "p: 100%"]

    def test_crlf_is_one_delimiter_not_two(self):
        assert lines_from([b"a\r\nb\r\n"]) == ["a", "b"]

    def test_crlf_split_across_reads_is_still_one_delimiter(self):
        """The `\\r` and the `\\n` landing in different reads is the case that
        turns one delimiter into two and injects a phantom blank line."""
        assert lines_from([b"a\r", b"\nb\r\n"]) == ["a", "b"]

    def test_a_leading_empty_split_is_dropped(self):
        """A `\\r`-first stream splits with nothing before the delimiter. That
        is an artifact of the delimiter, not a line the tool printed."""
        assert lines_from([b"\rfirst\n"]) == ["first"]

    def test_a_bar_starting_after_a_completed_line_emits_no_blank(self):
        """The same `\\r` artifact, but mid-stream rather than at the start: a
        progress bar redraws with a leading `\\r` after the tool has already
        printed a normal line. `start\\r\\n` then `\\rprogress` leaves a `\\r`
        at the head of the buffer, which splits with nothing before it.

        This is what a real tool does (`printf 'start\\r\\n'` followed by a
        `\\r`-redrawn bar) and it put a blank line between every heading and
        the bar underneath it in the run log."""
        assert lines_from([b"start\r\n\rprogress: 10%\rprogress: 50%\n"]) == [
            "start",
            "progress: 10%",
            "progress: 50%",
        ]

    def test_a_later_blank_line_is_real_output(self):
        """A bare `print()` after real output is output, and is delivered --
        the `\\n`-delimited empty line the `\\r` guard must not swallow."""
        assert lines_from([b"first\n\nthird\n"]) == ["first", "", "third"]

    def test_consecutive_redraws_emit_no_blanks(self):
        """A bar that redraws with nothing between two `\\r`s."""
        assert lines_from([b"a\n\r\rb\n"]) == ["a", "b"]


class TestDecoding:
    def test_invalid_utf8_is_replaced_not_raised(self):
        seen = lines_from([b"bad \xff byte\n"])
        assert len(seen) == 1
        assert "bad" in seen[0] and "byte" in seen[0]

    def test_a_multibyte_character_split_across_reads_survives(self):
        """The incremental decoder's reason for existing: a UTF-8 sequence
        straddling a read boundary must not decode to two replacement chars."""
        payload = "héllo ✓\n".encode()
        seen = lines_from([payload[:2], payload[2:5], payload[5:]])
        assert seen == ["héllo ✓"]


class TestObservers:
    def test_the_parser_snapshot_reaches_ctx_progress(self):
        ctx = make_ctx()
        received: list[dict] = []
        ctx._progress_cb = received.append

        pump = drive([b"PROGRESS one\nnoise\nPROGRESS two\n"], parser=RecordingParser(), ctx=ctx)
        assert received == [{"message": "seen 1"}, {"message": "seen 2"}]
        assert pump.update_count == 2
        assert pump.line_count == 3

    def test_unrecognizable_output_publishes_nothing_and_does_not_raise(self):
        """A stream a parser understands none of is a normal stream -- it must
        leave progress state untouched rather than corrupting or clearing it."""
        ctx = make_ctx()
        received: list[dict] = []
        ctx._progress_cb = received.append

        pump = drive([b"total garbage\nmore of it\n"], parser=RecordingParser(), ctx=ctx)
        assert received == []
        assert pump.update_count == 0
        assert pump.line_count == 2

    def test_a_raising_parser_does_not_stop_the_pump(self):
        """Progress is advisory: a parser bug must not cost the remaining
        output, nor the log, nor the job."""

        class BoomParser:
            name = "boom"

            def feed(self, line: str) -> bool:
                raise ValueError("bad parse")

            def snapshot(self) -> dict:
                return {}

        pump = drive([b"a\nb\nc\n"], parser=BoomParser())
        assert pump.line_count == 3

    def test_a_raising_on_line_does_not_stop_the_pump(self):
        seen: list[str] = []

        def boom(line: str) -> None:
            seen.append(line)
            raise ValueError("bad callback")

        pump = drive([b"a\nb\n"], on_line=boom)
        assert seen == ["a", "b"]
        assert pump.line_count == 2

    def test_no_observer_at_all_still_counts_lines(self):
        assert drive([b"a\nb\n"]).line_count == 2


class TestLogWriting:
    def test_lines_are_written_with_newlines_restored(self, tmp_path):
        log = tmp_path / "job.log"
        with open(log, "w", encoding="utf-8") as fh:
            drive([b"\rp: 10%\rp: 50%\n"], log_file=fh)
        assert log.read_text() == "p: 10%\np: 50%\n"

    def test_a_failing_log_write_does_not_stop_the_pump(self, tmp_path):
        """A full disk must not cost the run: the log is a record of the work,
        not the work."""

        class BrokenLog:
            def write(self, _text: str) -> None:
                raise OSError("no space left on device")

            def flush(self) -> None:
                pass

        seen: list[str] = []
        pump = drive([b"a\nb\n"], on_line=seen.append, log_file=BrokenLog())
        assert seen == ["a", "b"]
        assert pump.line_count == 2

    def test_the_log_is_written_even_when_the_observer_raises(self, tmp_path):
        log = tmp_path / "job.log"

        def boom(_line: str) -> None:
            raise ValueError("bad parse")

        with open(log, "w", encoding="utf-8") as fh:
            drive([b"kept\n"], on_line=boom, log_file=fh)
        assert log.read_text() == "kept\n"


class TestAbnormalEnd:
    def test_no_output_at_all_ends_the_pump(self):
        """A silent tool must terminate the pump rather than block it. If the
        read loop needed output to exit, this would hang instead of return."""
        pump = drive([])
        assert pump.line_count == 0
        assert pump.update_count == 0

    def test_a_dying_pipe_keeps_the_output_it_already_read(self, monkeypatch):
        """What cancellation leaves behind: the child is killed, the pipe
        raises mid-read. Lines already delivered are the only evidence of how
        far the run got, and must survive."""
        debug: list[tuple] = []
        monkeypatch.setattr(
            executor_module.log, "debug", lambda event, **kw: debug.append((event, kw))
        )
        seen: list[str] = []
        drive([], on_line=seen.append, stream=RaisingStream([b"phase one done\n"]))

        assert seen == ["phase one done"]
        assert any(event == "output_pump_ended" for event, _ in debug)

    def test_a_pipe_dying_before_any_output_is_not_an_error(self):
        seen: list[str] = []
        drive([], on_line=seen.append, stream=RaisingStream([]))
        assert seen == []
