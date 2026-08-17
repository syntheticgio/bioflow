"""Unit tests for GenBank ORIGIN sequence extraction."""

from pathlib import Path

import pytest
from app.config import settings
from app.errors import PermanentError
from app.pipelines import genbank_reader, genbank_sequence
from app.queue import annotation_handlers
from app.queue.registry import JobContext

FIXTURES = Path(__file__).parent.parent / "fixtures" / "genbank"


def _ctx(payload: dict) -> JobContext:
    """A real JobContext, matching test_annotation_contig_lengths_fact.py.

    The real class rather than a fake: a hand-rolled stand-in drifts from
    JobContext silently, and these handlers are cheap to drive for real.
    """
    return JobContext(
        job_id="j1",
        payload=payload,
        epoch=1,
        attempts=1,
        owner="local",
    )


def _read_fasta(path: Path) -> list[tuple[str, str]]:
    """Parse a FASTA into (defline, sequence) pairs, for assertions."""
    records: list[tuple[str, str]] = []
    name = None
    chunks: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            if name is not None:
                records.append((name, "".join(chunks)))
            name = line[1:]
            chunks = []
        else:
            chunks.append(line)
    if name is not None:
        records.append((name, "".join(chunks)))
    return records


class TestSequenceLine:
    def test_strips_counter_and_spaces(self):
        line = "        1 agcttttcat tctgactgca acgggcaata"
        assert genbank_sequence.sequence_line_bases(line) == (
            "agcttttcattctgactgcaacgggcaata"
        )

    def test_handles_a_later_counter(self):
        line = "       61 tgatagcagc ttctgaactg"
        assert genbank_sequence.sequence_line_bases(line) == "tgatagcagcttctgaactg"

    def test_blank_line_yields_nothing(self):
        assert genbank_sequence.sequence_line_bases("   ") == ""

    def test_line_without_counter_still_reads(self):
        # Not every writer emits the counter; the bases are what matter.
        assert genbank_sequence.sequence_line_bases("agct tttc") == "agcttttc"


class TestAccessionFor:
    def test_prefers_version(self):
        assert genbank_reader.accession_for(
            version="NC_000001.3", accession="NC_000001", locus_name="NC_000001"
        ) == "NC_000001.3"

    def test_falls_back_to_accession(self):
        assert genbank_reader.accession_for(
            version="", accession="NC_000001", locus_name="OTHER"
        ) == "NC_000001"

    def test_falls_back_to_locus_name(self):
        assert genbank_reader.accession_for(
            version="", accession="", locus_name="OTHER"
        ) == "OTHER"

    def test_falls_back_to_unknown(self):
        assert genbank_reader.accession_for(
            version="", accession="", locus_name=""
        ) == "unknown"


class TestWriteFasta:
    def test_writes_one_record_from_ecoli_slice(self, tmp_path):
        dest = tmp_path / "out.fna"
        count = genbank_sequence.write_fasta(
            source=FIXTURES / "ecoli_slice.gbff", dest=dest
        )
        assert count == 1
        records = _read_fasta(dest)
        assert len(records) == 1
        assert records[0][0] == "NC_000913.3"
        assert set(records[0][1]) <= set("acgtnACGTN")

    def test_uses_versioned_accession_as_defline(self, tmp_path):
        dest = tmp_path / "out.fna"
        genbank_sequence.write_fasta(
            source=FIXTURES / "two_records.gbff", dest=dest
        )
        # two_records.gbff has two LOCUS records but only one ORIGIN block,
        # so only the record carrying sequence is emitted.
        assert [name for name, _ in _read_fasta(dest)] == ["NC_000001.3"]

    def test_wraps_at_sixty_columns(self, tmp_path):
        dest = tmp_path / "out.fna"
        genbank_sequence.write_fasta(
            source=FIXTURES / "ecoli_slice.gbff", dest=dest
        )
        body = [
            ln for ln in dest.read_text().splitlines() if not ln.startswith(">")
        ]
        assert all(len(line) <= 60 for line in body)
        # Every line but the last of a record must be exactly full.
        assert all(len(line) == 60 for line in body[:-1])

    def test_reads_gzipped_input(self, tmp_path):
        plain = tmp_path / "plain.fna"
        gzipped = tmp_path / "gz.fna"
        genbank_sequence.write_fasta(
            source=FIXTURES / "two_records.gbff", dest=plain
        )
        genbank_sequence.write_fasta(
            source=FIXTURES / "two_records.gbff.gz", dest=gzipped
        )
        assert plain.read_text() == gzipped.read_text()

    def test_no_sequence_yields_zero_records(self, tmp_path):
        source = tmp_path / "featureless.gbff"
        source.write_text(
            "LOCUS       NC_000003               10 bp    DNA     linear\n"
            "VERSION     NC_000003.1\n"
            "FEATURES             Location/Qualifiers\n"
            "     gene            1..10\n"
            "//\n"
        )
        dest = tmp_path / "out.fna"
        assert genbank_sequence.write_fasta(source=source, dest=dest) == 0


class TestAgreesWithReader:
    """GS-4: the two modules must name the same record identically.

    The reason this matters is in `accession_for`'s docstring: contig lengths
    are matched by name between a GenBank and its extracted FASTA.
    """

    @pytest.mark.parametrize(
        "fixture", ["ecoli_slice.gbff", "two_records.gbff"]
    )
    def test_accessions_match(self, fixture, tmp_path):
        source = FIXTURES / fixture
        dest = tmp_path / "out.fna"
        genbank_sequence.write_fasta(source=source, dest=dest)

        extracted = [name for name, _ in _read_fasta(dest)]
        from_reader = [
            r.accession
            for r in genbank_reader.iter_records(source)
            if r.has_sequence
        ]
        assert extracted == from_reader


class TestExtractHandler:
    def test_rejects_a_file_with_no_sequence(self, tmp_path, monkeypatch):
        # Redirects _prepare_workdir's output under tmp_path, the same way the
        # existing annotation handler tests isolate their scratch space.
        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        source = tmp_path / "featureless.gbff"
        source.write_text(
            "LOCUS       NC_000003               10 bp    DNA     linear\n"
            "VERSION     NC_000003.1\n"
            "//\n"
        )
        ctx = _ctx(
            {
                "object_id": "507f1f77bcf86cd799439011",
                "genbank_path": str(source),
                "output_name": "out.fna",
            }
        )
        with pytest.raises(PermanentError, match="no sequence"):
            annotation_handlers.extract_genbank_sequence(ctx)

    def test_writes_the_fasta_and_reports_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "bioinfo_home", tmp_path / "home")
        ctx = _ctx(
            {
                "object_id": "507f1f77bcf86cd799439011",
                "genbank_path": str(FIXTURES / "ecoli_slice.gbff"),
                "output_name": "ecoli_slice.fna",
            }
        )
        result = annotation_handlers.extract_genbank_sequence(ctx)
        assert result["record_count"] == 1
        assert result["output"]["name"] == "ecoli_slice.fna"
        assert Path(result["output"]["tmp_path"]).exists()
