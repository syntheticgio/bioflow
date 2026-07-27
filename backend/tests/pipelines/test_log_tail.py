"""Bounded reading of captured job output.

A fastp run on a large library writes a lot, and the log endpoint is polled
while the job runs -- so it must never pull the whole file into memory.
"""

from app.api.v1.jobs import LOG_READ_BYTES, _read_tail


class TestReadTail:
    def test_returns_the_last_lines(self, tmp_path):
        p = tmp_path / "job.log"
        p.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")

        lines, truncated, size = _read_tail(p, 10)
        assert lines == [f"line {i}" for i in range(90, 100)]
        assert truncated
        assert size == p.stat().st_size

    def test_a_short_file_is_not_truncated(self, tmp_path):
        p = tmp_path / "job.log"
        p.write_text("one\ntwo\nthree\n")

        lines, truncated, _ = _read_tail(p, 200)
        assert lines == ["one", "two", "three"]
        assert not truncated

    def test_reads_only_the_end_of_a_large_file(self, tmp_path):
        """The property that matters: a 300 MB log must not become 300 MB of
        resident memory because someone opened the activity view."""
        p = tmp_path / "job.log"
        filler = "x" * 200
        with open(p, "w") as f:
            for i in range(20000):  # ~4 MB, well over the read window
                f.write(f"{i} {filler}\n")

        lines, truncated, size = _read_tail(p, 5)
        assert len(lines) == 5
        assert truncated
        assert size > LOG_READ_BYTES
        # The tail is genuinely the end of the file, not the start.
        assert lines[-1].startswith("19999 ")

    def test_drops_a_partial_first_line(self, tmp_path):
        """Seeking mid-file lands in the middle of a line; showing that
        fragment would look like real output."""
        p = tmp_path / "job.log"
        line = "y" * 1000
        with open(p, "w") as f:
            for i in range(500):  # ~500 KB, forces a seek
                f.write(f"{i:04d}-{line}\n")

        lines, _, _ = _read_tail(p, 1000)
        # Every returned line must be whole -- the fragment is dropped.
        assert all(len(line_) == 1005 for line_ in lines), "a partial line survived"

    def test_missing_file_is_not_an_error(self, tmp_path):
        lines, truncated, size = _read_tail(tmp_path / "absent.log", 10)
        assert lines == []
        assert not truncated
        assert size == 0

    def test_empty_file(self, tmp_path):
        p = tmp_path / "job.log"
        p.write_text("")
        lines, truncated, size = _read_tail(p, 10)
        assert lines == []
        assert not truncated
        assert size == 0

    def test_invalid_utf8_does_not_raise(self, tmp_path):
        """Tool output is not guaranteed to be valid UTF-8."""
        p = tmp_path / "job.log"
        p.write_bytes(b"good line\nbad \xff byte\n")
        lines, _, _ = _read_tail(p, 10)
        assert len(lines) == 2

    def test_a_file_without_a_trailing_newline(self, tmp_path):
        p = tmp_path / "job.log"
        p.write_text("one\ntwo")
        lines, _, _ = _read_tail(p, 10)
        assert lines == ["one", "two"]
