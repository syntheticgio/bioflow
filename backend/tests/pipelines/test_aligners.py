"""Reference materialization: putting CAS blobs back into the shape tools expect.

These are deliberately heavy on naming. The Phase 6a lesson is that a
well-formed command over a wrongly-named input fails in a way no command-
construction test catches: fastp inferred gzip from a filename, got a blob with
no extension, and read compressed bytes as text. Index files fail the same way
-- bwa-mem2 finds its index by appending suffixes to the reference path, so a
sidecar linked under a name it does not look for produces "index not found"
at best and a confusing error deep into a run at worst.
"""

import pytest

from app.models import SidecarRole
from app.pipelines import aligners
from app.pipelines.aligners import Aligner


class TestIndexFilenames:
    def test_bwa_mem2_needs_all_five_files(self):
        """bwa-mem2's index is a set, not a file. A missing member is not a
        degraded index -- the tool refuses to load it."""
        names = aligners.index_filenames("genome.fna", Aligner.BWA_MEM2)
        assert set(names) == {
            "genome.fna.0123",
            "genome.fna.amb",
            "genome.fna.ann",
            "genome.fna.bwt.2bit.64",
            "genome.fna.pac",
        }

    def test_bwa_mem2_suffixes_are_not_plain_bwas(self):
        """The distinction that is easy to get wrong: plain bwa writes `.bwt`
        and `.sa`, bwa-mem2 writes `.bwt.2bit.64` and `.0123`. Building one and
        looking for the other fails at alignment time, not at index time."""
        suffixes = set(aligners.index_suffixes(Aligner.BWA_MEM2))
        assert ".bwt.2bit.64" in suffixes
        assert ".0123" in suffixes
        assert ".sa" not in suffixes
        assert ".bwt" not in suffixes

    def test_minimap2_is_a_single_file(self):
        """The reason both aligners exist from the start: a one-file index and
        a five-file index keep the sidecar model honest."""
        assert aligners.index_filenames("genome.fna", Aligner.MINIMAP2) == ("genome.fna.mmi",)

    def test_names_build_on_the_reference_filename_not_the_digest(self):
        """Tools derive index names from the path they are given, so the base
        must be the reference's own name. Using the blob digest would produce
        names nothing looks for -- the exact Phase 6a failure."""
        names = aligners.index_filenames("T_brucei_927.fasta", Aligner.MINIMAP2)
        assert names == ("T_brucei_927.fasta.mmi",)

    def test_a_compressed_reference_keeps_its_full_name(self):
        """`.fna.gz` is the whole name as far as suffix appending is concerned;
        stripping the extension would misname every index file."""
        names = aligners.index_filenames("genome.fna.gz", Aligner.MINIMAP2)
        assert names == ("genome.fna.gz.mmi",)


class TestIndexRole:
    def test_each_aligner_maps_to_its_own_sidecar_role(self):
        assert aligners.INDEX_ROLE[Aligner.BWA_MEM2] is SidecarRole.BWA_MEM2_INDEX
        assert aligners.INDEX_ROLE[Aligner.MINIMAP2] is SidecarRole.MINIMAP2_INDEX

    def test_every_aligner_is_accounted_for(self):
        """A new aligner without a role would produce sidecars that the
        "is this reference indexed?" lookup cannot recognize."""
        assert set(aligners.INDEX_ROLE) == set(Aligner)


class TestPlanLinks:
    def test_maps_each_sidecar_to_its_blob(self):
        plan = aligners.plan_links(
            reference_name="genome.fna",
            sidecars={"genome.fna.mmi": "/objects/ab/cd/abcdef"},
        )
        assert plan == {"genome.fna.mmi": "/objects/ab/cd/abcdef"}

    def test_drops_a_sidecar_belonging_to_another_reference(self):
        """Rather than relinking it under a corrected name. An index attached
        to the wrong reference is a bookkeeping error, and quietly renaming it
        would turn that into a plausible-looking wrong alignment."""
        plan = aligners.plan_links(
            reference_name="genome.fna",
            sidecars={"other.fna.mmi": "/objects/ab/cd/abcdef"},
        )
        assert plan == {}

    def test_strips_directory_components_from_a_stored_name(self):
        """Names come from the database and reach `symlink`, so a name carrying
        path separators is reduced to its final component. The traversal is
        neutralized rather than rejected: what remains is a legitimate sidecar
        name, and the link lands inside the workdir where it belongs."""
        plan = aligners.plan_links(
            reference_name="genome.fna",
            sidecars={"../../etc/genome.fna.mmi": "/objects/ab/cd/x"},
        )
        assert plan == {"genome.fna.mmi": "/objects/ab/cd/x"}
        assert all("/" not in name for name in plan)

    def test_an_unindexed_reference_plans_nothing(self):
        assert aligners.plan_links(reference_name="genome.fna", sidecars={}) == {}


class TestMaterialize:
    @pytest.fixture
    def blobs(self, tmp_path):
        """Stand-ins for CAS blobs: named by digest, with no extension."""
        store = tmp_path / "objects"
        store.mkdir()
        ref = store / "a1b2c3"
        ref.write_text(">chr1\nACGT\n")
        mmi = store / "d4e5f6"
        mmi.write_bytes(b"\x00index")
        return {"ref": ref, "mmi": mmi}

    def test_the_reference_appears_under_its_own_name(self, tmp_path, blobs):
        """The whole point. The blob is `a1b2c3` with no extension; the tool
        must see `genome.fna`."""
        result = aligners.materialize(
            workdir=tmp_path / "ref",
            reference_name="genome.fna",
            reference_blob=blobs["ref"],
            sidecars={},
        )
        assert result.reference.name == "genome.fna"
        assert result.reference.read_text() == ">chr1\nACGT\n"

    def test_sidecars_appear_as_siblings(self, tmp_path, blobs):
        """Siblings specifically: bwa-mem2 and samtools find their index by
        appending to the reference path, so a sidecar anywhere else is
        invisible to them no matter how correct the command is."""
        result = aligners.materialize(
            workdir=tmp_path / "ref",
            reference_name="genome.fna",
            reference_blob=blobs["ref"],
            sidecars={"genome.fna.mmi": str(blobs["mmi"])},
        )
        sibling = result.reference.parent / "genome.fna.mmi"
        assert sibling.exists()
        assert sibling.read_bytes() == b"\x00index"

    def test_links_rather_than_copies(self, tmp_path, blobs):
        """A mammalian index is several gigabytes; copying it per run would
        cost more than building it did."""
        result = aligners.materialize(
            workdir=tmp_path / "ref",
            reference_name="genome.fna",
            reference_blob=blobs["ref"],
            sidecars={},
        )
        assert result.reference.is_symlink()

    def test_reports_an_unindexed_reference(self, tmp_path, blobs):
        result = aligners.materialize(
            workdir=tmp_path / "ref",
            reference_name="genome.fna",
            reference_blob=blobs["ref"],
            sidecars={},
        )
        assert result.missing_index is True

    def test_a_retry_replaces_a_stale_link(self, tmp_path, blobs):
        """Retries reuse the job's scratch directory. A link left over from a
        failed attempt, pointing at a half-written blob, would be read as valid
        input -- a wrong answer rather than an error."""
        workdir = tmp_path / "ref"
        workdir.mkdir(parents=True)
        stale = workdir / "genome.fna"
        stale.symlink_to(tmp_path / "does-not-exist")

        result = aligners.materialize(
            workdir=workdir,
            reference_name="genome.fna",
            reference_blob=blobs["ref"],
            sidecars={},
        )
        assert result.reference.read_text() == ">chr1\nACGT\n"

    def test_is_idempotent(self, tmp_path, blobs):
        """Delivery is at-least-once, so any handler may run twice."""
        args = dict(
            workdir=tmp_path / "ref",
            reference_name="genome.fna",
            reference_blob=blobs["ref"],
            sidecars={"genome.fna.mmi": str(blobs["mmi"])},
        )
        first = aligners.materialize(**args)
        second = aligners.materialize(**args)
        assert first.linked == second.linked == ("genome.fna.mmi",)

    def test_a_reference_name_cannot_escape_the_workdir(self, tmp_path, blobs):
        """Names are user-facing and mutable, and this one reaches `symlink`."""
        result = aligners.materialize(
            workdir=tmp_path / "ref",
            reference_name="../escaped.fna",
            reference_blob=blobs["ref"],
            sidecars={},
        )
        assert result.reference.parent == tmp_path / "ref"
        assert not (tmp_path / "escaped.fna").exists()


class TestNewAligners:
    def test_bowtie2_and_hisat2_are_aligners(self):
        assert Aligner.BOWTIE2.value == "bowtie2"
        assert Aligner.HISAT2.value == "hisat2"

    def test_every_aligner_has_an_index_role(self):
        """INDEX_ROLE is indexed by every member in reference_index_status,
        so a missing entry is a KeyError on an unrelated code path."""
        for aligner in Aligner:
            assert aligner in aligners.INDEX_ROLE

    def test_index_roles_are_distinct(self):
        """Two aligners sharing a role would make one reference's index
        satisfy the other's check, and the alignment would fail on a
        malformed index rather than a missing one."""
        roles = [aligners.INDEX_ROLE[a] for a in Aligner]
        assert len(set(roles)) == len(roles)
