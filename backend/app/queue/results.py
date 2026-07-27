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
from app.models import DataObject, FormatInfo, ObjectRole, ObjectStatus

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

    log.info(
        "trim_applied",
        object_id=object_id,
        outputs=[str(o.id) for o in created],
        reads_before=report.get("before", {}).get("total_reads"),
        reads_after=report.get("after", {}).get("total_reads"),
    )


_APPLIERS = {
    "ingest_headers": _apply_ingest_headers,
    "trim_reads": _apply_trim_reads,
}
