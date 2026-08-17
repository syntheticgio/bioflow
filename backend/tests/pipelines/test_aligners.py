"""Reference materialization: putting CAS blobs back into the shape tools expect.

These are deliberately heavy on naming. The Phase 6a lesson is that a
well-formed command over a wrongly-named input fails in a way no command-
construction test catches: fastp inferred gzip from a filename, got a blob with
no extension, and read compressed bytes as text. Index files fail the same way
-- bwa-mem2 finds its index by appending suffixes to the reference path, so a
sidecar linked under a name it does not look for produces "index not found"
at best and a confusing error deep into a run at worst.
"""

from pathlib import Path

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


class TestDirectoryLayout:
    """STAR's index is a directory, stored flat and reassembled on use.

    The pair of translations is what these cover: a stored
    `genome.fna.STARindex.SA` has to become `genome.fna.STARindex/SA` on disk
    and nothing else, and `--genomeDir` has to name the directory rather than
    the reference inside it. Getting either wrong produces "genome directory
    does not exist" at run time -- loud, but only after the index has been
    built and the job has been queued.
    """

    def test_members_are_stored_flat_under_the_reference_name(self):
        """So `owns_sidecar` and every database record keep working without
        knowing a directory is involved."""
        names = aligners.index_filenames("genome.fna", Aligner.STAR)
        assert "genome.fna.STARindex.SA" in names
        assert "genome.fna.STARindex.Genome" in names
        assert all(name.startswith("genome.fna") for name in names)
        assert all("/" not in name for name in names)

    def test_the_members_are_the_eight_star_actually_writes(self):
        """Verified by running STAR 2.7.11b genomeGenerate without an
        annotation, not recalled. Requiring a file STAR does not write (the
        exonInfo/geneInfo/transcriptInfo trio, which needs --sjdbGTFfile)
        would fail every index build after a successful one."""
        assert set(aligners.STAR_MEMBERS) == {
            "Genome",
            "SA",
            "SAindex",
            "chrLength.txt",
            "chrName.txt",
            "chrNameLength.txt",
            "chrStart.txt",
            "genomeParameters.txt",
        }

    def test_log_out_is_not_an_index_member(self):
        """STAR writes Log.out into the directory too. It is a build
        transcript that STAR never reads back, so carrying it would store a
        log file forever as though it were part of the index."""
        assert "Log.out" not in aligners.STAR_MEMBERS

    def test_the_annotated_members_are_the_fifteen_star_actually_writes(self):
        """Verified by running STAR 2.7.11b genomeGenerate with
        --sjdbGTFfile against the real yeast reference and GTF
        (GCF_000146045.2_R64_genomic.gtf). The TODO that asked for this
        predicted only four extra files -- exonInfo.tab, geneInfo.tab,
        transcriptInfo.tab, sjdbList.out.tab -- from STAR's documentation.
        The real build writes seven: the prediction missed exonGeTrInfo.tab,
        sjdbInfo.txt and sjdbList.fromGTF.out.tab. Trusting the prediction
        would have repeated the exact failure the base STAR_MEMBERS list's own
        comment warns about -- requiring files a real build does not
        produce, or (here) missing files a real build does."""
        assert set(aligners.STAR_ANNOTATED_MEMBERS) == set(aligners.STAR_MEMBERS) | {
            "exonGeTrInfo.tab",
            "exonInfo.tab",
            "geneInfo.tab",
            "sjdbInfo.txt",
            "sjdbList.fromGTF.out.tab",
            "sjdbList.out.tab",
            "transcriptInfo.tab",
        }

    def test_annotated_index_uses_a_separate_directory_from_the_plain_one(self):
        """A reference should be able to carry both: one build from before a
        GTF was available, one from after. Sharing STAR_DIR_SUFFIX would mean
        the second build's files land beside (or overwrite) the first's."""
        plain = aligners.index_filenames("genome.fna", Aligner.STAR)
        annotated = aligners.index_filenames(
            "genome.fna", Aligner.STAR, annotated=True
        )
        assert not set(plain) & set(annotated)
        assert all(".STARindex.annotated." in name for name in annotated)

    def test_index_suffixes_annotated_is_star_only(self):
        for aligner in (Aligner.BWA_MEM2, Aligner.MINIMAP2, Aligner.BOWTIE2,
                        Aligner.HISAT2):
            with pytest.raises(ValueError):
                aligners.index_suffixes(aligner, annotated=True)

    def test_layout_for_annotated_is_star_only(self):
        for aligner in (Aligner.BWA_MEM2, Aligner.MINIMAP2, Aligner.BOWTIE2,
                        Aligner.HISAT2):
            with pytest.raises(ValueError):
                aligners.layout_for(aligner, annotated=True)

    def test_index_role_annotated_is_a_distinct_role(self):
        from app.models import SidecarRole

        assert aligners.index_role(Aligner.STAR) == SidecarRole.STAR_INDEX
        assert (
            aligners.index_role(Aligner.STAR, annotated=True)
            == SidecarRole.STAR_ANNOTATED_INDEX
        )

    def test_workdir_path_puts_a_member_inside_the_directory(self):
        layout = aligners.layout_for(Aligner.STAR)
        assert (
            layout.workdir_path("genome.fna", "genome.fna.STARindex.SA")
            == "genome.fna.STARindex/SA"
        )

    def test_workdir_path_leaves_a_non_member_alone(self):
        """`fai` travels in the same sidecar dict and belongs beside the
        reference. Forcing it into the index directory would hide it from
        samtools, which looks for it next to the FASTA."""
        layout = aligners.layout_for(Aligner.STAR)
        assert layout.workdir_path("genome.fna", "genome.fna.fai") == "genome.fna.fai"

    def test_workdir_path_is_identity_for_the_other_layouts(self):
        for aligner in (Aligner.BWA_MEM2, Aligner.MINIMAP2, Aligner.BOWTIE2):
            layout = aligners.layout_for(aligner)
            for name in aligners.index_filenames("genome.fna", aligner):
                assert layout.workdir_path("genome.fna", name) == name

    def test_reference_argument_is_the_directory_not_the_reference(self):
        """STAR takes --genomeDir. Handed the reference path, it reports a
        missing genome directory."""
        layout = aligners.layout_for(Aligner.STAR)
        arg = layout.reference_argument(Path("/w/ref/genome.fna"))
        assert arg == "/w/ref/genome.fna.STARindex"

    def test_plan_links_without_a_layout_leaves_members_flat(self):
        """The default matters because `variant_handlers` materializes a
        reference without naming an aligner. Flat members are what STAR sees
        as a missing directory -- an error rather than a wrong answer, which
        is the right way for the omission to fail."""
        plan = aligners.plan_links(
            reference_name="genome.fna",
            sidecars={"genome.fna.STARindex.SA": "/objects/ab/cd/x"},
        )
        assert plan == {"genome.fna.STARindex.SA": "/objects/ab/cd/x"}

    def test_plan_links_sanitizes_before_translating(self):
        """The traversal is neutralized on the *stored* name, so a crafted
        name cannot use the directory translation to escape the workdir."""
        plan = aligners.plan_links(
            reference_name="genome.fna",
            sidecars={"../../etc/genome.fna.STARindex.SA": "/objects/ab/cd/x"},
            layout=aligners.layout_for(Aligner.STAR),
        )
        assert plan == {"genome.fna.STARindex/SA": "/objects/ab/cd/x"}
        assert not any(part == ".." for part in Path(*plan).parts)

    def test_a_reference_with_only_a_fai_counts_as_having_no_star_index(self):
        """The gate `align_reads` uses. `missing_index` alone is True only
        when *nothing* linked, so a reference carrying just its `.fai` passed
        it and the run proceeded into STAR, which failed with a missing
        genome directory -- an error that reads as a corrupt index rather
        than an absent one."""
        ref = aligners.MaterializedRef(
            directory=Path("/w/ref"),
            reference=Path("/w/ref/genome.fna"),
            linked=("genome.fna.fai",),
        )
        assert ref.missing_index is False  # something was linked
        assert ref.missing_index_for(
            aligners.layout_for(Aligner.STAR), "genome.fna"
        ) is True

    def test_a_complete_star_index_satisfies_the_gate(self):
        layout = aligners.layout_for(Aligner.STAR)
        linked = tuple(
            layout.workdir_path("genome.fna", name)
            for name in layout.filenames("genome.fna")
        )
        ref = aligners.MaterializedRef(
            directory=Path("/w/ref"),
            reference=Path("/w/ref/genome.fna"),
            linked=(*linked, "genome.fna.fai"),
        )
        assert ref.missing_index_for(layout, "genome.fna") is False

    def test_a_partial_star_index_does_not(self):
        """STAR refuses to load an incomplete genome directory, so a missing
        member is an absent index rather than a degraded one."""
        layout = aligners.layout_for(Aligner.STAR)
        linked = tuple(
            layout.workdir_path("genome.fna", name)
            for name in layout.filenames("genome.fna")
        )[:-1]
        ref = aligners.MaterializedRef(
            directory=Path("/w/ref"),
            reference=Path("/w/ref/genome.fna"),
            linked=linked,
        )
        assert ref.missing_index_for(layout, "genome.fna") is True

    def test_materialize_builds_a_real_directory_of_links(self, tmp_path):
        blob = tmp_path / "blob"
        blob.write_text("index bytes")
        fai = tmp_path / "faiblob"
        fai.write_text("chr1\t100\t6\t60\t61\n")

        ref_blob = tmp_path / "refblob"
        ref_blob.write_text(">chr1\n")

        materialized = aligners.materialize(
            workdir=tmp_path / "work",
            reference_name="genome.fna",
            reference_blob=ref_blob,
            sidecars={
                "genome.fna.STARindex.SA": str(blob),
                "genome.fna.STARindex.Genome": str(blob),
                "genome.fna.fai": str(fai),
            },
            layout=aligners.layout_for(Aligner.STAR),
        )

        genome_dir = materialized.directory / "genome.fna.STARindex"
        assert genome_dir.is_dir()
        assert (genome_dir / "SA").read_text() == "index bytes"
        assert (genome_dir / "Genome").read_text() == "index bytes"
        # The .fai stays beside the reference, where samtools looks for it.
        assert (materialized.directory / "genome.fna.fai").exists()
        assert not (genome_dir / "genome.fna.fai").exists()


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


class TestIndexLayout:
    def test_suffix_layout_reference_argument_is_the_reference_path(self):
        """bwa-mem2 and minimap2 take the reference itself and find the index
        by appending. The path is what the tool is handed."""
        layout = aligners.layout_for(Aligner.BWA_MEM2)
        arg = layout.reference_argument(Path("/w/ref/genome.fna"))
        assert arg == "/w/ref/genome.fna"

    def test_prefix_layout_reference_argument_drops_nothing_from_the_name(self):
        """bowtie2 is handed a basename, and its index files are that basename
        plus a suffix. Since we name the index files after the *full*
        reference filename (genome.fna.1.bt2), the basename is the full path
        -- not the path with .fna stripped. Stripping it would make bowtie2
        look for genome.1.bt2, which does not exist."""
        layout = aligners.layout_for(Aligner.BOWTIE2)
        arg = layout.reference_argument(Path("/w/ref/genome.fna"))
        assert arg == "/w/ref/genome.fna"

    def test_prefix_layout_knows_its_builder_binary(self):
        assert aligners.layout_for(Aligner.BOWTIE2).builder == "bowtie2-build"
        assert aligners.layout_for(Aligner.HISAT2).builder == "hisat2-build"

    def test_suffix_layout_has_no_separate_builder(self):
        """bwa-mem2 indexes through a subcommand and minimap2 through a flag;
        neither has a separate builder binary."""
        assert aligners.layout_for(Aligner.MINIMAP2).builder is None

    def test_every_aligner_has_a_layout(self):
        for aligner in Aligner:
            assert aligners.layout_for(aligner) is not None

    def test_layout_accepts_its_own_sidecars(self):
        layout = aligners.layout_for(Aligner.BOWTIE2)
        assert layout.owns_sidecar("genome.fna", "genome.fna.1.bt2")

    def test_layout_rejects_a_foreign_sidecar(self):
        """The safety check that survives the refactor: an index attached to
        the wrong reference produces a plausible-looking wrong result rather
        than an error, so it must be dropped rather than renamed."""
        layout = aligners.layout_for(Aligner.BOWTIE2)
        assert not layout.owns_sidecar("genome.fna", "other.fna.1.bt2")
