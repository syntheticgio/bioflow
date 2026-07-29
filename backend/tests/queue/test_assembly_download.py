"""Assembly download: component labeling and the disk pre-flight.

The pure decisions, without the network or a worker. The zip layout below is
verbatim from `datasets download genome accession GCF_000002445.2 --include
genome,gff3,protein,cds` (v18.30.1).

The test that matters most is that `cds_from_genomic.fna` is labeled CDS and
not genome. Both files are `.fna` in the same directory, and getting it wrong
puts a CDS file in the aligner's reference picker, where selecting it produces
silently wrong alignments rather than an error.
"""

import json
from pathlib import Path

import pytest

from app.errors import PermanentError
from app.queue import assembly_handlers

CATALOG = {
    "apiVersion": "V2",
    "assemblies": [
        {"files": [{"filePath": "assembly_data_report.jsonl",
                    "fileType": "DATA_REPORT",
                    "uncompressedLengthBytes": "3725"}]},
        {"accession": "GCF_000002445.2",
         "files": [
             {"filePath": "GCF_000002445.2/cds_from_genomic.fna",
              "fileType": "CDS_NUCLEOTIDE_FASTA",
              "uncompressedLengthBytes": "15188456"},
             {"filePath": "GCF_000002445.2/GCF_000002445.2_ASM244v1_genomic.fna",
              "fileType": "GENOMIC_NUCLEOTIDE_FASTA",
              "uncompressedLengthBytes": "26402511"},
             {"filePath": "GCF_000002445.2/genomic.gff",
              "fileType": "GFF3",
              "uncompressedLengthBytes": "12545174"},
             {"filePath": "GCF_000002445.2/protein.faa",
              "fileType": "PROTEIN_FASTA",
              "uncompressedLengthBytes": "5173742"},
         ]},
    ],
}


@pytest.fixture
def extracted(tmp_path: Path) -> Path:
    """A work dir shaped like a real extracted package."""
    data = tmp_path / "ncbi_dataset" / "data"
    acc = data / "GCF_000002445.2"
    acc.mkdir(parents=True)
    (data / "dataset_catalog.json").write_text(json.dumps(CATALOG))
    (data / "assembly_data_report.jsonl").write_text("{}\n")
    (acc / "cds_from_genomic.fna").write_text(">cds\nACGT\n")
    (acc / "GCF_000002445.2_ASM244v1_genomic.fna").write_text(">chr1\nACGT\n")
    (acc / "genomic.gff").write_text("##gff-version 3\n")
    (acc / "protein.faa").write_text(">prot\nMKV\n")
    return tmp_path


class TestLabelFromCatalog:
    def test_cds_is_not_labeled_as_the_genome(self, extracted: Path):
        """THE regression test. Both are .fna in one directory; labeling by
        extension or by matching *_genomic.fna first roles the CDS file as a
        reference genome."""
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        by_name = {s["name"]: s for s in staged}
        assert by_name["cds_from_genomic.fna"]["component"] == "cds"
        assert by_name["cds_from_genomic.fna"]["role"] == "transcript"

    def test_the_genome_fasta_is_the_reference(self, extracted: Path):
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        genome = next(
            s for s in staged if s["name"] == "GCF_000002445.2_ASM244v1_genomic.fna"
        )
        assert genome["component"] == "genome"
        assert genome["role"] == "reference"

    def test_every_component_is_labeled(self, extracted: Path):
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert {s["component"] for s in staged} == {"genome", "gff3", "protein", "cds"}

    def test_the_data_report_is_not_staged(self, extracted: Path):
        """assembly_data_report.jsonl is metadata about the package, not a
        file the user asked for. Ingesting it would put a stray .jsonl in the
        project."""
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert "assembly_data_report.jsonl" not in {s["name"] for s in staged}

    def test_paths_are_absolute(self, extracted: Path):
        """The applier consumes these from a different process; a relative
        path would resolve against the wrong cwd."""
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert all(Path(s["path"]).is_absolute() for s in staged)


class TestLabelWithoutCatalog:
    def test_falls_back_to_filenames(self, extracted: Path):
        """A catalog that NCBI stops shipping must not lose the download."""
        (extracted / "ncbi_dataset" / "data" / "dataset_catalog.json").unlink()
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert {s["component"] for s in staged} == {"genome", "gff3", "protein", "cds"}

    def test_the_fallback_also_gets_cds_right(self, extracted: Path):
        """The filename fallback is where the .fna collision actually bites:
        `cds_from_genomic.fna` must be matched before `*_genomic.fna`."""
        (extracted / "ncbi_dataset" / "data" / "dataset_catalog.json").unlink()
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        by_name = {s["name"]: s["component"] for s in staged}
        assert by_name["cds_from_genomic.fna"] == "cds"
        assert by_name["GCF_000002445.2_ASM244v1_genomic.fna"] == "genome"

    def test_a_non_dict_catalog_falls_back_instead_of_raising(self, extracted: Path):
        """`dataset_catalog.json` containing valid-but-non-dict JSON (the
        literal `null`, a bare number, etc.) must not crash the whole job.
        `json.loads` succeeds, so this only reaches the `.get("assemblies")`
        call -- which raises AttributeError on a non-dict payload unless that
        case is explicitly guarded."""
        (extracted / "ncbi_dataset" / "data" / "dataset_catalog.json").write_text("null")
        staged = assembly_handlers._label_components(extracted, "GCF_000002445.2")
        assert {s["component"] for s in staged} == {"genome", "gff3", "protein", "cds"}
        by_name = {s["name"]: s["component"] for s in staged}
        assert by_name["cds_from_genomic.fna"] == "cds"
        assert by_name["GCF_000002445.2_ASM244v1_genomic.fna"] == "genome"


class TestDiskPreflight:
    def test_a_download_that_cannot_fit_is_refused_up_front(self, tmp_path: Path):
        """Discovering the disk is full after an hour of transfer is too late:
        the space is already spent and the partial output has to be reaped."""
        with pytest.raises(PermanentError, match="disk space"):
            assembly_handlers._check_disk_space(
                tmp_path, 10**15, "GCF_000002445.2"
            )

    def test_no_estimate_means_no_refusal(self, tmp_path: Path):
        """A missing figure is not evidence of a problem, and refusing on it
        would block downloads NCBI has no size for."""
        assembly_handlers._check_disk_space(tmp_path, None, "GCF_000002445.2")
