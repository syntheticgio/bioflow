"""Applying job results to the database.

Thread-mode handlers run off the event loop and so cannot use Beanie, which is
async. Rather than giving handlers a second synchronous database client -- two
connection pools with different transaction semantics -- they return plain
dicts, and the writes happen here on the loop.
"""

from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId

from app.logging import get_logger
from app.models import (
    DataObject,
    FormatInfo,
    IoClass,
    JobClass,
    JobResources,
    JobState,
    ObjectRole,
    ObjectStatus,
    RunJobRole,
    SidecarRole,
)
from app.pipelines.aligners import Aligner

log = get_logger(__name__)


async def apply(job_type: str, result: dict) -> None:
    handler = _APPLIERS.get(job_type)
    if handler is not None:
        await handler(result)


def should_assign_reference_role(*, current_role, enrichment: dict | None) -> bool:
    """Whether an ingest should mark this object a reference.

    Only when an assembly accession was found *and* no role is set. A role the
    user chose is never overruled: they may be running something unusual, or
    know something about the file that its name does not say.
    """
    if current_role is not None:
        return False
    return bool((enrichment or {}).get("accession"))


async def _apply_ingest_headers(result: dict) -> None:
    object_id = result.get("object_id")
    if not object_id:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        return

    fmt = result.get("format") or {}
    facts = result.get("facts") or {}

    update: dict = {
        DataObject.status: ObjectStatus.READY,
        DataObject.updated_at: datetime.now(UTC),
    }
    if fmt:
        update[DataObject.format] = FormatInfo(
            kind=fmt.get("kind", "unknown"),
            compression=fmt.get("compression", "none"),
            confidence=fmt.get("confidence", "none"),
            extension_says=fmt.get("extension_says"),
            magic_says=fmt.get("magic_says"),
            detected_at=datetime.now(UTC),
        )
    if facts:
        # Merged, not replaced: a re-ingest should not discard facts an earlier
        # pass established (or that a future parser added).
        update[DataObject.facts] = {**obj.facts, **facts}

    enrichment = result.get("enrichment") or {}
    if enrichment.get("values"):
        # enrich_from_sra already excluded anything the user had set, so this
        # merge cannot clobber a manual edit.
        update[DataObject.metadata] = {**obj.metadata, **enrichment["values"]}

    # Provenance lives in facts rather than metadata: it describes where the
    # values came from, and should not itself become searchable metadata.
    if enrichment.get("accession"):
        provenance = {
            "sra_accession": enrichment["accession"],
            "sra_source": enrichment.get("source"),
            "sra_fields_applied": sorted(enrichment.get("values", {})),
        }
        if enrichment.get("conflicts"):
            provenance["sra_conflicts"] = enrichment["conflicts"]
        if enrichment.get("error"):
            provenance["sra_error"] = enrichment["error"]
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {**merged_facts, **provenance}
    elif enrichment.get("error"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {**merged_facts, "sra_error": enrichment["error"]}

    assembly_enrichment = result.get("assembly_enrichment") or {}
    if assembly_enrichment.get("values"):
        # Already filtered against what the user set, so this cannot clobber.
        merged_metadata = update.get(DataObject.metadata, obj.metadata)
        update[DataObject.metadata] = {
            **merged_metadata,
            **assembly_enrichment["values"],
        }
    if assembly_enrichment.get("facts"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {
            **merged_facts,
            **assembly_enrichment["facts"],
        }
    if assembly_enrichment.get("accession"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        provenance = {
            "assembly_accession_source": assembly_enrichment.get("source"),
            "assembly_fields_applied": sorted(assembly_enrichment.get("values", {})),
        }
        if assembly_enrichment.get("conflicts"):
            provenance["assembly_conflicts"] = assembly_enrichment["conflicts"]
        update[DataObject.facts] = {**merged_facts, **provenance}
    if assembly_enrichment.get("error"):
        merged_facts = update.get(DataObject.facts, obj.facts)
        update[DataObject.facts] = {
            **merged_facts,
            "assembly_error": assembly_enrichment["error"],
        }

    await obj.set(update)

    # Role is assigned separately, and conditionally, because `obj` was read
    # before a network lookup that can take seconds. A user who converted the
    # file in that window would otherwise be silently overruled by a stale
    # snapshot -- the exact thing "never overrule a person" forbids. Matching
    # on role=None means the write lands only if nobody has decided since.
    if should_assign_reference_role(
        current_role=obj.role, enrichment=assembly_enrichment
    ):
        assigned = await DataObject.find_one(
            DataObject.id == obj.id, DataObject.role == None  # noqa: E711
        ).update({"$set": {DataObject.role: ObjectRole.REFERENCE}})
        if not getattr(assigned, "modified_count", 0):
            log.info("assembly_role_skipped_raced", object_id=object_id)
    await _link_mate(obj)

    log.info(
        "ingest_applied",
        object_id=object_id,
        kind=fmt.get("kind"),
        sra=enrichment.get("accession"),
        sra_fields=len(enrichment.get("values", {})),
        assembly=assembly_enrichment.get("accession"),
        assembly_fields=len(assembly_enrichment.get("values", {})),
    )


async def _link_mate(obj: DataObject) -> None:
    """Find this file's paired-end partner and link them to each other.

    Runs after every ingest because the second half of a pair usually arrives
    after the first: whichever lands last is the one that finds the match, and
    it links both sides.

    A link the user set is never overwritten -- same principle as role. The
    filename convention is a strong hint, not a fact, and someone who corrected
    it knows something the name does not say.
    """
    from app.pipelines import pairing

    if obj.mate_object_id is not None:
        return

    split = pairing.split_mate(obj.name)
    if split is None or not split[0]:
        return

    # Narrowed to the project, and to files that are not already paired. The
    # candidate set is small enough that filtering names in Python beats
    # encoding the convention as a database query.
    candidates = await DataObject.find(
        DataObject.project_id == obj.project_id,
        DataObject.id != obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).to_list()

    matches = [c for c in candidates if pairing.is_mate_of(obj.name, c.name)]
    if len(matches) != 1:
        # Zero is the common case (the mate has not been uploaded, or the file
        # is single-end). More than one is genuinely ambiguous -- two files
        # with the same name in one project -- and guessing would be worse than
        # leaving it for the launch dialog to ask about.
        if len(matches) > 1:
            log.info(
                "mate_ambiguous",
                object_id=str(obj.id),
                name=obj.name,
                candidates=[str(m.id) for m in matches],
            )
        return

    mate = matches[0]

    # Conditional on both sides still being unpaired, so two ingests finishing
    # at once cannot produce a half-formed link. Whichever write lands first
    # wins; the loser sees a modified_count of zero and stops.
    linked = await DataObject.find_one(
        DataObject.id == mate.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update({"$set": {DataObject.mate_object_id: obj.id}})
    if not getattr(linked, "modified_count", 0):
        log.info("mate_link_skipped_raced", object_id=str(obj.id), mate_id=str(mate.id))
        return

    await DataObject.find_one(
        DataObject.id == obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
    ).update({"$set": {DataObject.mate_object_id: mate.id}})

    log.info("mate_linked", object_id=str(obj.id), mate_id=str(mate.id), name=obj.name)


async def _apply_trim_reads(result: dict) -> None:
    """Turn a finished trim run into objects.

    The handler ran in a worker thread and could not touch the database, so
    everything persistent happens here: the produced files are taken into the
    object store, linked to the reads they came from, and the before/after
    report is recorded on the source so the comparison is visible from the file
    the user started with.

    Ordering matters. Outputs are ingested first and the parent is updated
    afterwards, because the parent's report is what the UI keys on to show that
    a trim happened. Writing it before the outputs exist would offer a
    comparison with nothing to compare against.
    """
    from app.services import object_service

    object_id = result.get("object_id")
    outputs = result.get("outputs") or []
    if not object_id or not outputs:
        return

    parent = await DataObject.get(PydanticObjectId(object_id))
    if parent is None:
        log.warning("trim_parent_missing", object_id=object_id)
        return

    mate_id = result.get("mate_object_id")
    parents = [parent.id]
    if mate_id:
        parents.append(PydanticObjectId(mate_id))

    job_id = result.get("job_id")
    report = result.get("report") or {}
    params = result.get("params") or {}

    # Recorded on every output: the parameters alone do not describe a run
    # without the version of the tool that applied them.
    provenance = {
        "trimmed_by": result.get("tool", "fastp"),
        "trim_tool_version": result.get("tool_version"),
        "trim_params": params,
    }

    created: list[DataObject] = []
    for output in outputs:
        tmp_path = Path(output["tmp_path"])
        try:
            obj = await object_service.ingest_local_file(
                project_id=parent.project_id,
                path=tmp_path,
                name=output["name"],
                role=ObjectRole.TRIMMED_READS,
                derived_from=parents,
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts=dict(provenance),
                # Sample-level metadata describes the biology, which trimming
                # does not change. Carrying it over means a trimmed file is
                # still findable by the sample it came from.
                metadata=dict(parent.metadata),
            )
        except Exception as e:  # noqa: BLE001 - one bad output must not lose the rest
            log.error(
                "trim_output_ingest_failed",
                object_id=object_id,
                name=output.get("name"),
                error=str(e),
            )
            continue
        created.append(obj)

    # Link the produced mates to each other, mirroring how the inputs are
    # paired. Done here rather than at ingest because the pairing is known
    # exactly -- there is no need to infer it from the filenames.
    if len(created) == 2:
        await created[0].set({DataObject.mate_object_id: created[1].id})
        await created[1].set({DataObject.mate_object_id: created[0].id})

    if not created:
        log.error("trim_produced_nothing", object_id=object_id)
        return

    # The report goes on both inputs: either half of a pair is a reasonable
    # place for a user to look for what trimming did.
    trim_facts = {
        "trim_report": report,
        "trim_outputs": [str(o.id) for o in created],
        **provenance,
    }
    for parent_id in parents:
        target = await DataObject.get(parent_id)
        if target is None:
            continue
        await target.set(
            {
                DataObject.facts: {**target.facts, **trim_facts},
                DataObject.updated_at: datetime.now(UTC),
            }
        )

    if job_id:
        from app.services import run_service

        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [o.id for o in created])

    log.info(
        "trim_applied",
        object_id=object_id,
        outputs=[str(o.id) for o in created],
        reads_before=report.get("before", {}).get("total_reads"),
        reads_after=report.get("after", {}).get("total_reads"),
    )


async def _apply_run_qc(result: dict) -> None:
    """Record a QC run's numbers on the object it described.

    QC derives no files, so unlike trim or align there is nothing to ingest --
    the whole result is facts merged onto the object the user ran it against.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("qc_object_missing", object_id=object_id)
        return

    # Merged rather than replaced, like every other fact write: a QC run must
    # not discard what ingest or a trim established about the same file.
    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "qc_applied",
        object_id=object_id,
        tool=facts.get("qc_tool"),
        q30=facts.get("qc_before_filtering", {}).get("q30_rate"),
    )


async def _apply_build_index(result: dict) -> None:
    """Turn a finished index build into sidecar objects on the reference.

    Every produced file becomes an object in its own right -- verified,
    refcounted and garbage-collected like anything else -- but attached via
    `sidecar_of` rather than `derived_from`, so the explorer can keep
    scaffolding out of the listing.
    """
    from app.services import object_service

    reference_id = result.get("reference_object_id")
    outputs = result.get("outputs") or []
    if not reference_id or not outputs:
        return

    reference = await DataObject.get(PydanticObjectId(reference_id))
    if reference is None:
        log.warning("index_reference_missing", object_id=reference_id)
        return

    job_id = result.get("job_id")
    provenance = {
        "index_built_by": result.get("aligner"),
        "index_tool_version": result.get("tool_version"),
    }

    created = []
    for output in outputs:
        role = _SIDECAR_ROLES.get(output.get("role"))
        if role is None:
            log.warning("unknown_sidecar_role", role=output.get("role"))
            continue
        try:
            obj = await object_service.ingest_local_file(
                project_id=reference.project_id,
                path=Path(output["tmp_path"]),
                name=output["name"],
                derived_from=[reference.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts=dict(provenance),
                sidecar_of=reference.id,
                sidecar_role=role,
            )
        except Exception as e:  # noqa: BLE001 - one bad file must not lose the rest
            log.error(
                "index_output_ingest_failed",
                reference_id=reference_id,
                name=output.get("name"),
                error=str(e),
            )
            continue
        created.append(obj)

    if job_id and created:
        from app.services import run_service

        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [o.id for o in created])

    log.info(
        "index_applied",
        reference_id=reference_id,
        aligner=result.get("aligner"),
        sidecars=len(created),
    )

    await _supply_sidecars_to_blocked_alignments(reference, result.get("aligner"))


async def _supply_sidecars_to_blocked_alignments(
    reference: DataObject, aligner: str | None
) -> None:
    """Fill in the sidecar paths for alignments waiting on this index build.

    An alignment queued against an unindexed reference is enqueued *before* the
    index exists, so its payload cannot name sidecars that have not been built
    yet. The handler runs in a worker thread and cannot query the database, so
    the paths have to be written into the payload from here -- on the event
    loop, after the sidecar objects exist and before the dependency gate
    releases the job.

    Without this the alignment materializes a reference with no index beside it
    and fails with "Reference has no index available to this job".
    """
    from app.db.client import get_db
    from app.services import pipeline_service

    if not aligner:
        return

    try:
        sidecars = await pipeline_service.sidecar_payload(
            reference, Aligner(aligner)
        )
    except Exception as e:  # noqa: BLE001 - a bad lookup must not fail the index
        log.error("sidecar_payload_failed", reference_id=str(reference.id), error=str(e))
        return

    if not sidecars:
        return

    # Every blocked alignment against this reference, whichever job it is
    # waiting on: a second alignment queued behind the same build needs the
    # same paths, and neither knows about the other.
    result = await get_db().jobs.update_many(
        {
            "type": "align_reads",
            "state": JobState.BLOCKED.value,
            "payload.reference_object_id": str(reference.id),
            "payload.aligner": aligner,
        },
        {"$set": {"payload.sidecars": sidecars}},
    )
    if result.modified_count:
        log.info(
            "sidecars_supplied",
            reference_id=str(reference.id),
            aligner=aligner,
            jobs=result.modified_count,
        )


async def _apply_align_reads(result: dict) -> None:
    """Turn a finished alignment into a BAM object, and chain its indexing.

    The BAM descends from both the reads and the reference: all three are
    biologically meaningful, so this is `derived_from` rather than a sidecar
    relationship.
    """
    from app.queue import queue
    from app.services import object_service

    output = result.get("output")
    object_id = result.get("object_id")
    if not output or not object_id:
        return

    reads = await DataObject.get(PydanticObjectId(object_id))
    if reads is None:
        log.warning("align_reads_parent_missing", object_id=object_id)
        return

    parents = [reads.id]
    for key in ("mate_object_id", "reference_object_id"):
        value = result.get(key)
        if value:
            parents.append(PydanticObjectId(value))

    job_id = result.get("job_id")
    provenance = {
        "aligned_by": result.get("aligner"),
        "aligner_version": result.get("tool_version"),
        "samtools_version": result.get("samtools_version"),
        "align_params": result.get("params") or {},
        "read_group": result.get("read_group") or {},
    }

    try:
        bam = await object_service.ingest_local_file(
            project_id=reads.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ALIGNMENT,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=dict(provenance),
            # Sample-level metadata describes the biology, which aligning does
            # not change -- so the BAM stays findable by the sample it came from.
            metadata=dict(reads.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("align_output_ingest_failed", object_id=object_id, error=str(e))
        return

    log.info("align_applied", object_id=object_id, bam_id=str(bam.id))

    from app.services import run_service

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [bam.id])

    # Chain the follow-on index. Enqueued here rather than at launch because it
    # needs the BAM's digest, which does not exist until the alignment has run.
    if bam.blob_sha256:
        index_job = await queue.enqueue(
            "index_bam",
            payload={
                "bam_object_id": str(bam.id),
                "bam_sha256": bam.blob_sha256,
                "bam_name": bam.name,
                "project_id": str(bam.project_id),
            },
            job_class=JobClass.COMPUTE,
            resources=JobResources(cpu=1, mem_mb=1024, io=IoClass.LIGHT),
            max_attempts=2,
            dedup_key=f"index_bam:{bam.blob_sha256}",
            project_id=bam.project_id,
            object_id=bam.id,
            parent_job_id=PydanticObjectId(job_id) if job_id else None,
        )
        # Joins the alignment's run: it was caused by this run and finishes the
        # work the user asked for, even though it could not be enqueued until
        # the BAM existed.
        if index_job is not None and run_id is not None:
            await run_service.link_job(run_id, index_job.id, RunJobRole.INDEX_BAM)


async def _apply_index_bam(result: dict) -> None:
    """Attach a `.bai` to its BAM and record the flagstat numbers."""
    from app.services import object_service

    bam_id = result.get("bam_object_id")
    output = result.get("output")
    if not bam_id or not output:
        return

    bam = await DataObject.get(PydanticObjectId(bam_id))
    if bam is None:
        log.warning("index_bam_parent_missing", object_id=bam_id)
        return

    job_id = result.get("job_id")
    try:
        await object_service.ingest_local_file(
            project_id=bam.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            derived_from=[bam.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            sidecar_of=bam.id,
            sidecar_role=SidecarRole.BAI,
        )
    except Exception as e:  # noqa: BLE001
        log.error("bai_ingest_failed", object_id=bam_id, error=str(e))

    facts = result.get("facts") or {}
    if facts:
        # On the BAM itself: this is where a user looks to decide whether the
        # alignment is worth keeping.
        await bam.set(
            {
                DataObject.facts: {**bam.facts, **facts},
                DataObject.updated_at: datetime.now(UTC),
            }
        )

    log.info("index_bam_applied", object_id=bam_id, mapped_pct=facts.get("mapped_pct"))


_SIDECAR_ROLES = {
    "fai": SidecarRole.FAI,
    "bai": SidecarRole.BAI,
    SidecarRole.BWA_MEM2_INDEX.value: SidecarRole.BWA_MEM2_INDEX,
    SidecarRole.MINIMAP2_INDEX.value: SidecarRole.MINIMAP2_INDEX,
}


_APPLIERS = {
    "ingest_headers": _apply_ingest_headers,
    "trim_reads": _apply_trim_reads,
    "run_qc": _apply_run_qc,
    "build_index": _apply_build_index,
    "align_reads": _apply_align_reads,
    "index_bam": _apply_index_bam,
}
