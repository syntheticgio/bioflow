"""Applying job results to the database.

Thread-mode handlers run off the event loop and so cannot use Beanie, which is
async. Rather than giving handlers a second synchronous database client -- two
connection pools with different transaction semantics -- they return plain
dicts, and the writes happen here on the loop.
"""

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId

from app.config import settings
from app.errors import PermanentError
from app.logging import get_logger
from app.models import (
    DataObject,
    FormatInfo,
    FormatKind,
    IoClass,
    JobClass,
    JobResources,
    JobState,
    ObjectRole,
    ObjectStatus,
    RunJobRole,
    SidecarRole,
)
from app.models.job import Job
from app.pipelines.aligners import Aligner
from app.queue.chunked_align_results import apply_chunked_alignment as _apply_chunked_alignment

log = get_logger(__name__)


async def apply(job_type: str, result: dict, *, owner: str) -> None:
    """Run the applier for a finished job, telling it who launched the job.

    `owner` is the *launching profile*, not necessarily the owner of every
    object an applier writes. Most appliers resolve a parent object first and
    take that parent's owner instead, because a sidecar's owner has to match
    the object it hangs off -- `list_sidecars` and `delete_object`'s cascade
    both key on that relationship, so an inherited owner is the more specific
    fact and the one those appliers must use.

    The download appliers have no parent to inherit from: they create the first
    object in a chain from nothing but a project_id, so the launching profile is
    the only owner that exists. That is the whole reason this parameter is here.
    It is passed to every applier uniformly rather than only to those, so that
    dispatch stays one line and a new applier that *does* need it is not
    silently handed nothing.

    Every applier therefore takes `owner`, including the majority that inherit
    from a parent and never read it. An earlier version had those name it
    `launching_owner`, to mark inheriting as a decision rather than an
    oversight -- but a parameter named differently from the keyword dispatch
    uses is not a signal, it is a `TypeError` at the moment the job finishes.
    `_apply_result` catches it and logs, so the job still reports *succeeded*
    while its output goes unregistered: the tool ran, the file is on disk, and
    nothing tells the user why it never reached the library. Eleven appliers
    were in that state, and it took running a real job to notice. The
    inherit-from-the-parent decision is documented in the appliers that make it
    instead, where a reader is already looking.

    The two should never actually disagree. Every `queue.enqueue` call in the
    services derives its owner from the very object or project the job acts on
    (`owner=bam.owner`, `owner=project.owner`, ...), so the launching profile is
    already the parent's owner by construction. A disagreement would mean one
    profile's job producing another profile's object, which nothing can reach
    today -- but the parent-derived owner is still the one to trust if it ever
    happened, because it is what `list_sidecars` will later query with.
    """
    handler = _APPLIERS.get(job_type)
    if handler is not None:
        await handler(result, owner=owner)


def should_assign_reference_role(
    *, current_role, enrichment: dict | None, user_touched: list[str] | None = None
) -> bool:
    """Whether an ingest should mark this object a reference.

    Only when an assembly accession was found, no role is set, *and* the user
    has never touched the role. A role the user chose is never overruled: they
    may be running something unusual, or know something about the file that its
    name does not say.

    The `user_touched` check is what makes that promise hold across a
    conversion. A role the user *cleared* is `None` -- identical to one never
    set -- so without it, converting a reference back to reads and re-ingesting
    silently restores the role the user just removed.
    """
    if current_role is not None:
        return False
    if "role" in (user_touched or []):
        return False
    return bool((enrichment or {}).get("accession"))


async def _apply_ingest_headers(result: dict, *, owner: str) -> None:
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
    # Accumulated across the branches below, then written as per-key
    # `facts.<key>` paths rather than a whole-dict merge: after an alignment
    # this applier and `_apply_index_bam` finish on the same BAM at nearly the
    # same moment, and a merge computed from a stale `obj.facts` snapshot
    # erased whichever keys the other had just written (#606).
    facts_update: dict = dict(facts)
    # `has_index` from this parse is a sibling-file check that is only
    # meaningful for register-in-place files -- for a managed blob (stored by
    # hash, not next to a `.bai`) it is unconditionally False, and
    # `ingest_headers` can finish after `index_bam` (see `_apply_index_bam`)
    # on a small/fast file. An index, once true, does not become untrue, so a
    # False is written with `$max` below (False < True in BSON) instead of
    # `$set` -- it lands only where no True already exists.
    write_missing_index = "has_index" in facts_update and not facts_update["has_index"]
    if write_missing_index:
        del facts_update["has_index"]

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
        facts_update.update(provenance)
    elif enrichment.get("error"):
        facts_update["sra_error"] = enrichment["error"]

    assembly_enrichment = result.get("assembly_enrichment") or {}
    if assembly_enrichment.get("values"):
        # Already filtered against what the user set, so this cannot clobber.
        merged_metadata = update.get(DataObject.metadata, obj.metadata)
        update[DataObject.metadata] = {
            **merged_metadata,
            **assembly_enrichment["values"],
        }
    # Guessed from the filename, and only ever offered for a key the user has
    # not filled -- `detect_sequence_type` returns None if they have. Re-checked
    # against the object here as well as against the payload there, because the
    # payload was read before parsing and they could have set it since.
    sequence_type = result.get("sequence_type")
    if sequence_type and obj.metadata.get("sequence_type") in (None, ""):
        merged_metadata = update.get(DataObject.metadata, obj.metadata)
        update[DataObject.metadata] = {
            **merged_metadata,
            "sequence_type": sequence_type,
        }

    if assembly_enrichment.get("facts"):
        facts_update.update(assembly_enrichment["facts"])
    if assembly_enrichment.get("accession"):
        provenance = {
            "assembly_accession_source": assembly_enrichment.get("source"),
            "assembly_fields_applied": sorted(assembly_enrichment.get("values", {})),
        }
        if assembly_enrichment.get("conflicts"):
            provenance["assembly_conflicts"] = assembly_enrichment["conflicts"]
        facts_update.update(provenance)
    if assembly_enrichment.get("error"):
        facts_update["assembly_error"] = assembly_enrichment["error"]

    for key, value in facts_update.items():
        update[f"facts.{key}"] = value

    await obj.set(update)
    if write_missing_index:
        await DataObject.find_one(DataObject.id == obj.id).update(
            {"$max": {"facts.has_index": False}}
        )

    # Role is assigned separately, and conditionally, because `obj` was read
    # before a network lookup that can take seconds. A user who converted the
    # file in that window would otherwise be silently overruled by a stale
    # snapshot -- the exact thing "never overrule a person" forbids. Matching
    # on role=None means the write lands only if nobody has decided since.
    if should_assign_reference_role(
        current_role=obj.role,
        enrichment=assembly_enrichment,
        user_touched=obj.user_touched,
    ):
        assigned = await DataObject.find_one(
            DataObject.id == obj.id,
            DataObject.role == None,  # noqa: E711
            # Re-checked here, not just above: a conversion landing between the
            # decision and this write would otherwise be overruled by it.
            {"user_touched": {"$ne": "role"}},
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

    # After the log, so an annotation that cannot be queued still records a
    # completed ingest. `obj` carries the pre-update snapshot, so re-read the
    # fields the eligibility rule needs from what was actually written.
    obj.format = update.get(DataObject.format, obj.format)
    obj.facts = {**obj.facts, **facts_update}
    await _auto_analyze_after_ingest(obj, owner=owner)


async def _auto_analyze_after_ingest(obj: DataObject, *, owner: str) -> None:
    """Analyze annotations without being asked, from either direction.

    Two triggers, because one loses a race. Analyzing an annotation when it
    finishes ingesting is right whenever its reference is already READY --
    but `resolve_annotation_reference` filters candidates to
    `status=READY, role=REFERENCE`, and an NCBI download stages a genome and
    its GFF concurrently with the FASTA's role assigned only after a network
    lookup. An annotation winning that race is analyzed with no contig
    lengths: null coverage on every contig, no track axis, and nothing
    saying why. So a reference landing also sweeps the project for
    annotations that wanted one.

    That sweep is deliberately not restricted to referenceless analyses --
    it also picks up never-analyzed files, which backfills annotations
    ingested before this feature existed. The dedup key makes the redundancy
    free.

    Never raises, and one failure never stops the rest. The object is
    already READY by the time this runs and must stay that way: a malformed
    annotation is a failed recoverable computation, not a failed ingest.
    """
    from app.errors import AppError
    from app.services import pipeline_service

    async def _launch(target: DataObject) -> None:
        try:
            await pipeline_service.launch_annotation_stats(
                object_id=target.id, owner=owner
            )
        except AppError as e:
            log.warning(
                "annotation_autoanalysis_failed",
                object_id=str(target.id),
                error=str(e),
            )

    if pipeline_service.should_auto_analyze_annotation(
        kind=obj.format.kind, sidecar_of=obj.sidecar_of, facts=obj.facts
    ):
        await _launch(obj)
        return

    # Trigger 2. Only a reference gives an annotation the coordinate axis it
    # was missing, so a plain FASTA (protein.faa, cds_from_genomic.fna) is
    # not a reason to re-analyze anything.
    if obj.format.kind is not FormatKind.FASTA or obj.role is not ObjectRole.REFERENCE:
        return

    # One project-scoped query regardless of how many annotations it holds;
    # the eligibility rule then filters in Python so both triggers share
    # exactly one definition of who qualifies.
    candidates = await DataObject.find(
        DataObject.project_id == obj.project_id,
        DataObject.status == ObjectStatus.READY,
        {
            "format.kind": {
                "$in": [k.value for k in pipeline_service._ANNOTATION_STATS_FORMATS]
            }
        },
    ).to_list()

    for ann in candidates:
        if pipeline_service.should_auto_analyze_annotation(
            kind=ann.format.kind, sidecar_of=ann.sidecar_of, facts=ann.facts
        ):
            await _launch(ann)


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

    # "mate" in user_touched covers the case the pointer cannot: a pairing the
    # user *cleared* is None, indistinguishable from one never set, so without
    # this the next re-ingest silently restores what they just removed. Exactly
    # the hole user_touched was introduced to close for role.
    if obj.mate_object_id is not None or "mate" in obj.user_touched:
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
        # Reached from the other side, a file whose pairing was cleared is
        # still an unpaired name match -- so it has to be excluded here too,
        # not just by the early return above.
        {"user_touched": {"$ne": "mate"}},
    ).to_list()

    obj_input = pairing.PairInput(name=obj.name, facts=obj.facts, metadata=obj.metadata)
    matches = []
    for c in candidates:
        c_input = pairing.PairInput(name=c.name, facts=c.facts, metadata=c.metadata)
        v = pairing.verdict(obj_input, c_input)
        if v in (pairing.Verdict.CONFIRMED, pairing.Verdict.NAME_ONLY):
            matches.append(c)
        elif v in (pairing.Verdict.REJECTED_LAYOUT, pairing.Verdict.REJECTED_READ_IDS):
            log.info(
                "mate_rejected",
                object_id=str(obj.id),
                candidate_id=str(c.id),
                name=obj.name,
                candidate_name=c.name,
                verdict=v.value,
            )

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

    # Read numbers come from the same split that matched the pair, so the label
    # and the link cannot disagree. `pairing.OPPOSITE` is the same mapping
    # `is_mate_of` used to establish that these two tokens are opposites, kept
    # to one definition rather than re-encoded here.
    read_number = {"R1": 1, "R2": 2}
    this_read = read_number[split[1]]
    mate_read = read_number[pairing.OPPOSITE[split[1]]]

    # Conditional on both sides still being unpaired, so two ingests finishing
    # at once cannot produce a half-formed link. Whichever write lands first
    # wins; the loser sees a modified_count of zero and stops.
    linked = await DataObject.find_one(
        DataObject.id == mate.id,
        DataObject.mate_object_id == None,  # noqa: E711
        # Re-checked in the query, not just above: a manual pairing landing
        # between the decision and this write would otherwise be overruled by
        # a stale snapshot. Same reasoning as the role assignment.
        {"user_touched": {"$ne": "mate"}},
    ).update(
        {"$set": {DataObject.mate_object_id: obj.id, DataObject.read_number: mate_read}}
    )
    if not getattr(linked, "modified_count", 0):
        log.info("mate_link_skipped_raced", object_id=str(obj.id), mate_id=str(mate.id))
        return

    await DataObject.find_one(
        DataObject.id == obj.id,
        DataObject.mate_object_id == None,  # noqa: E711
        {"user_touched": {"$ne": "mate"}},
    ).update(
        {"$set": {DataObject.mate_object_id: mate.id, DataObject.read_number: this_read}}
    )

    log.info(
        "mate_linked",
        object_id=str(obj.id),
        mate_id=str(mate.id),
        name=obj.name,
        read_number=this_read,
    )


async def _apply_trim_reads(result: dict, *, owner: str) -> None:
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
                owner=parent.owner,
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
            # The owner comes from a file this apply just produced, which
            # object_service stamped with the project's owner. An applier has
            # no request and so no get_current_owner to ask; carrying the owner
            # in the job payload is Task 9's work, and this is the same value
            # that task will arrive at -- an output and the run that produced
            # it always share a project. `created` is non-empty here: the
            # produced-nothing case returned above.
            await run_service.record_outputs(
                run_id, [o.id for o in created], owner=created[0].owner
            )

    log.info(
        "trim_applied",
        object_id=object_id,
        outputs=[str(o.id) for o in created],
        reads_before=report.get("before", {}).get("total_reads"),
        reads_after=report.get("after", {}).get("total_reads"),
    )


async def _apply_filter_long_reads(result: dict, *, owner: str) -> None:
    """Turn a finished filter-long-reads run into objects.

    Mirrors _apply_trim_reads but uses ObjectRole.FILTERED_READS and the
    "filtered_by" provenance key, since length/quality filtering is conceptually
    distinct from adapter trimming -- the resulting file is not "trimmed".
    """
    from app.services import object_service

    object_id = result.get("object_id")
    outputs = result.get("outputs") or []
    if not object_id or not outputs:
        return

    parent = await DataObject.get(PydanticObjectId(object_id))
    if parent is None:
        log.warning("filter_parent_missing", object_id=object_id)
        return

    mate_id = result.get("mate_object_id")
    parents = [parent.id]
    if mate_id:
        parents.append(PydanticObjectId(mate_id))

    job_id = result.get("job_id")
    report = result.get("report") or {}
    params = result.get("params") or {}

    provenance = {
        "filtered_by": result.get("tool", "filtlong"),
        "filter_tool_version": result.get("tool_version"),
        "filter_params": params,
    }

    created: list[DataObject] = []
    for output in outputs:
        tmp_path = Path(output["tmp_path"])
        try:
            obj = await object_service.ingest_local_file(
                owner=parent.owner,
                project_id=parent.project_id,
                path=tmp_path,
                name=output["name"],
                role=ObjectRole.FILTERED_READS,
                derived_from=parents,
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts=dict(provenance),
                metadata=dict(parent.metadata),
            )
        except Exception as e:  # noqa: BLE001 - one bad output must not lose the rest
            log.error(
                "filter_output_ingest_failed",
                object_id=object_id,
                name=output.get("name"),
                error=str(e),
            )
            continue
        created.append(obj)

    if not created:
        log.error("filter_produced_nothing", object_id=object_id)
        return

    filter_facts = {
        "filter_report": report,
        "filter_outputs": [str(o.id) for o in created],
        **provenance,
    }
    for parent_id in parents:
        target = await DataObject.get(parent_id)
        if target is None:
            continue
        await target.set(
            {
                DataObject.facts: {**target.facts, **filter_facts},
                DataObject.updated_at: datetime.now(UTC),
            }
        )

    if job_id:
        from app.services import run_service

        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(
                run_id, [o.id for o in created], owner=created[0].owner
            )

    log.info(
        "filter_applied",
        object_id=object_id,
        outputs=[str(o.id) for o in created],
        reads_before=report.get("before", {}).get("total_reads"),
        reads_after=report.get("after", {}).get("total_reads"),
    )


async def _apply_fetch_remote(result: dict, *, owner: str) -> None:
    """Re-attach fetched bytes to the object that was offloaded.

    Where `_apply_sra_download` creates objects, this restores one. The
    distinction is the entire point of the handler existing separately: the
    object's id is referenced by its children's `derived_from`, by any
    sidecar, by its mate, and by the runs it took part in, so a fetch that
    created a new object would leave every one of those pointing at a file
    with no bytes.

    A run downloaded as a pair stages two files but each offloaded object owns
    exactly one of them, so the staged entry matching this object's name is the
    one restored and the other is discarded. Fetching the mate is its own job,
    enqueued when the mate is itself needed -- fetching both here would
    re-download gigabytes the user may not have asked for.
    """
    from app.services import object_service

    object_id = result.get("object_id")
    staged = result.get("staged") or []
    if not object_id or not staged:
        return

    object_id = PydanticObjectId(object_id)
    obj = await DataObject.get(object_id)
    if obj is None:
        log.warning("fetch_remote_object_gone", object_id=str(object_id))
        return

    from app.storage.detect import strip_compression_suffix

    wanted = strip_compression_suffix(obj.name)
    match = next(
        (e for e in staged if strip_compression_suffix(Path(e["name"]).name) == wanted),
        None,
    )
    # Fall back to the sole staged file: a single-end run names its output
    # after the accession alone, which need not equal the object's name if the
    # user renamed the file after downloading it.
    if match is None and len(staged) == 1:
        match = staged[0]

    for entry in staged:
        if entry is match:
            continue
        path = Path(entry["path"])
        if await asyncio.to_thread(path.exists):
            await asyncio.to_thread(path.unlink)

    if match is None:
        log.error(
            "fetch_remote_no_matching_file",
            object_id=str(object_id),
            name=obj.name,
            staged=[e["name"] for e in staged],
        )
        return

    await object_service.restore_bytes_for_object(
        object_id,
        owner=owner,
        path=Path(match["path"]),
        content_sha256=match.get("content_sha256"),
    )
    log.info("fetch_remote_restored", object_id=str(object_id), name=obj.name)


async def _apply_sra_download(result: dict, *, owner: str) -> None:
    """Take a finished SRA download into the project, and chain its QC.

    The handler ran in a worker thread and could not touch the database, so
    everything persistent happens here: the staged FASTQs become objects, the
    mates are linked to each other, and QC is queued per file.

    One failed file does not lose the rest. A run that produced R1 and R2 where
    only R2 fails to ingest should still yield R1 -- the transfer is the
    expensive part and it already succeeded.
    """
    from app.queue import queue
    from app.services import object_service, run_service

    staged = result.get("staged") or []
    project_id = result.get("project_id")
    if not staged or not project_id:
        return

    project_id = PydanticObjectId(project_id)
    accession = result.get("accession")
    job_id = result.get("job_id")
    platform = result.get("platform") or "UNKNOWN"

    # The job-level dedup_key in sra_service.launch_download only collapses a
    # second request while the first job is still in flight -- once that job
    # has finished, there is no in-flight job left to collide with, and a
    # second request for the same accession reaches this applier and would
    # otherwise create a second, visible DataObject for content already
    # present (bug #81). ERROR/MISSING records are not "already have it": the
    # NCBI dialog deliberately lets a user re-request a run whose file was
    # deleted or came in corrupted, and that recovery path must still ingest.
    if accession and await DataObject.find_one(
        DataObject.project_id == project_id,
        {"metadata.sra_run": accession},
        {"status": {"$nin": [ObjectStatus.ERROR, ObjectStatus.MISSING]}},
    ):
        log.info(
            "sra_download_already_present",
            accession=accession,
            project_id=str(project_id),
        )
        for entry in staged:
            path = Path(entry["path"])
            if await asyncio.to_thread(path.exists):
                await asyncio.to_thread(path.unlink)
        return

    # Provenance, distinct from the sample metadata below: this records where
    # the bytes came from, which is not a searchable property of the biology.
    provenance = {
        "sra_downloaded_from": accession,
        "sra_download_source": "ncbi",
        "sra_platform": platform,
    }
    metadata = dict(result.get("metadata") or {})

    created: dict[str, DataObject] = {}
    for entry in staged:
        try:
            obj = await object_service.ingest_local_file(
                # The launching job's owner, threaded through `apply`. Unlike
                # the appliers that resolve a parent object, a download has only
                # the payload's project_id -- a freshly downloaded FASTQ is
                # derived from nothing, so there is no parent here to read an
                # owner off, and the profile that asked for the download is the
                # only answer.
                owner=owner,
                project_id=project_id,
                path=Path(entry["path"]),
                name=entry["name"],
                # No role. `ObjectRole` marks files that are something *other*
                # than plain reads -- a reference, a trim output, an
                # alignment -- and a freshly downloaded FASTQ is exactly the
                # untransformed input the absence of a role denotes.
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts=dict(provenance),
                metadata=dict(metadata),
                # The handler already compressed this file (it has a
                # JobContext to report progress through; this applier does
                # not) and hashed the plaintext on the way -- passed through
                # so dedup-by-content still applies. None for a format the
                # allowlist left uncompressed.
                content_sha256=entry.get("content_sha256"),
            )
        except Exception as e:  # noqa: BLE001 - one bad file must not lose the rest
            log.error(
                "sra_ingest_failed",
                accession=accession,
                name=entry.get("name"),
                error=str(e),
            )
            continue
        created[entry.get("mate") or "single"] = obj

    if not created:
        log.error("sra_download_ingested_nothing", accession=accession)
        return

    # Linked from what fasterq-dump reported rather than inferred from the
    # filenames. `_link_mate` runs at ingest and would usually reach the same
    # answer -- `<acc>_1.fastq` parses fine under the `_1`/`_2` convention --
    # but here the pairing is known exactly, from fasterq-dump's own labelling,
    # which is more authoritative than an inference and should not be
    # overridden by one.
    r1, r2 = created.get("R1"), created.get("R2")
    if r1 is not None and r2 is not None:
        await r1.set({DataObject.mate_object_id: r2.id, DataObject.read_number: 1})
        await r2.set({DataObject.mate_object_id: r1.id, DataObject.read_number: 2})

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(
            run_id,
            [o.id for o in created.values()],
            owner=next(iter(created.values())).owner,
        )

    log.info(
        "sra_download_applied",
        accession=accession,
        objects=[str(o.id) for o in created.values()],
        paired=bool(r1 and r2),
    )

    if not result.get("run_qc"):
        return

    for obj in created.values():
        qc_job = await queue.enqueue(
            "run_qc",
            owner=obj.owner,
            payload={
                "object_id": str(obj.id),
                "project_id": str(project_id),
                "name": obj.name,
                # Chooses the QC tool: NanoPlot for long reads, fastp+FastQC
                # otherwise. Passed from the resolver's metadata rather than
                # re-derived, since the handler cannot query for it.
                "platform": platform,
                "r1_sha256": obj.blob_sha256,
                "sra_accession": accession,
            },
            job_class=JobClass.COMPUTE,
            resources=JobResources(cpu=2, mem_mb=2048, io=IoClass.HEAVY),
            max_attempts=2,
            # Identical to launch_qc's key, so a manual QC click and this
            # automatic run collapse into one job rather than running twice.
            dedup_key=f"qc:{obj.id}",
            project_id=project_id,
            object_id=obj.id,
            parent_job_id=PydanticObjectId(job_id) if job_id else None,
        )
        # Joins the download's run: caused by it, and part of finishing what
        # the user asked for, though it could not be queued until the file
        # existed. Optional, so a QC failure leaves the run PARTIAL not FAILED.
        if qc_job is not None and run_id is not None:
            await run_service.link_job(run_id, qc_job.id, RunJobRole.QC)


def _role_for_component(entry: dict) -> str | None:
    """The ObjectRole value a staged component becomes.

    Re-derived from `ncbi_assembly_components.COMPONENTS` rather than trusting the
    `role` the handler already attached to `entry`: the handler runs in a
    worker thread and returns a plain dict across a process boundary, and
    re-deriving from the one authoritative table costs nothing while removing
    a class of "what if the dict disagrees with the table" bug.

    Returns None for anything unrecognized: an unroled file is merely
    uncategorized in the explorer, while a wrongly-roled one is actively
    misleading -- a CDS FASTA offered as a reference genome.
    """
    from app.metadata import ncbi_assembly_components

    spec = ncbi_assembly_components.COMPONENTS.get(entry.get("component") or "")
    return spec.role if spec else None


def _component_metadata(base: dict, accession: str, component: str) -> dict:
    """Metadata for one component, from the assembly's shared record.

    `assembly_accession` goes on every component: it is what makes the four
    files recognize each other in search and in the explorer, and what
    "do I already have this genome?" matches on.

    Genome-specific keys are withheld from the others. `reference_build` on a
    protein FASTA would assert that the file is an assembly, which is exactly
    the confusion the PROTEIN role exists to prevent.

    This set mirrors `REFERENCE_FIELDS` in `app.metadata.schemas` minus the
    keys shared with `SEQUENCE_SET_FIELDS` (`organism`, `source`,
    `assembly_accession` -- the last is added back unconditionally below
    anyway). `tax_id`, `assembly_date`, and `paired_accession` come from
    `AssemblyMetadata.to_metadata()` in `app.metadata.ncbi_assembly` just like
    `reference_build` and `assembly_level` do, and are exactly as
    genome-specific.
    """
    genome_only = {
        "reference_build",
        "assembly_level",
        "is_primary_assembly",
        "tax_id",
        "assembly_date",
        "paired_accession",
    }

    out = {
        k: v
        for k, v in (base or {}).items()
        if component == "genome" or k not in genome_only
    }
    out["assembly_accession"] = accession

    # From the component table rather than the filename: `_role_for_component`
    # already trusts that table for the far more consequential role decision,
    # and the two must not be able to disagree about the same file.
    from app.metadata import ncbi_assembly_components

    spec = ncbi_assembly_components.COMPONENTS.get(component)
    if spec is not None and spec.sequence_type:
        out["sequence_type"] = spec.sequence_type
    return out


async def _apply_assembly_download(result: dict, *, owner: str) -> None:
    """Take a finished assembly download into the project.

    Mirrors `_apply_sra_download`: the handler ran in a worker thread and
    could not touch the database, so the ingest happens here. One failed
    component does not lose the rest -- the transfer is the expensive part and
    it already succeeded.

    No QC and no mate linking: a reference genome has no reads to QC and no
    pair.
    """
    from app.services import object_service, run_service

    staged = result.get("staged") or []
    project_id = result.get("project_id")
    if not staged or not project_id:
        return

    project_id = PydanticObjectId(project_id)
    accession = result.get("accession") or ""
    job_id = result.get("job_id")
    base_metadata = dict(result.get("metadata") or {})
    base_facts = dict(result.get("facts") or {})

    created = []
    for entry in staged:
        component = entry.get("component") or ""
        # Provenance, distinct from the biology: where these bytes came from,
        # which is not a searchable property of the organism.
        facts = dict(base_facts)
        facts.update(
            {
                "assembly_downloaded_from": accession,
                "assembly_download_source": "ncbi_datasets",
                "assembly_component": component,
            }
        )
        try:
            obj = await object_service.ingest_local_file(
                # As in `_apply_sra_download`: a download has only the payload's
                # project_id and no parent object to inherit from, so the owner
                # of the job that asked for it is the one that reaches here.
                owner=owner,
                project_id=project_id,
                path=Path(entry["path"]),
                name=entry["name"],
                role=_role_for_component(entry),
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts=facts,
                metadata=_component_metadata(base_metadata, accession, component),
            )
        except Exception as e:  # noqa: BLE001 - one bad file must not lose the rest
            log.error(
                "assembly_ingest_failed",
                accession=accession,
                name=entry.get("name"),
                error=str(e),
            )
            continue
        created.append(obj)

    if not created:
        log.error("assembly_download_ingested_nothing", accession=accession)
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(
            run_id, [o.id for o in created], owner=created[0].owner
        )

    log.info(
        "assembly_download_applied",
        accession=accession,
        objects=[str(o.id) for o in created],
        components=[s.get("component") for s in staged],
    )


async def _apply_uniprot_download(result: dict, *, owner: str) -> None:
    """Take a finished UniProt download into the project.

    Mirrors `_apply_assembly_download`: the handler ran in a worker thread
    and could not touch the database, so the ingest happens here.

    Simpler than the assembly case in one way -- there is exactly one file,
    not up to four -- and the loop is kept anyway so a future multi-file
    shape does not have to reintroduce it.

    No QC and no mate linking: protein sequences have no reads to QC and no
    pair.
    """
    from app.services import object_service, run_service

    staged = result.get("staged") or []
    project_id = result.get("project_id")
    if not staged or not project_id:
        return

    project_id = PydanticObjectId(project_id)
    job_id = result.get("job_id")

    # Provenance, distinct from the biology: which query produced these bytes
    # and which UniProt release they came from. The release is recorded here,
    # per-download, rather than on the `DataSource` entry -- what is true of a
    # source and what is true of one download from it are different claims,
    # and `sources.py` argues at length that a source has no version.
    facts = {
        "uniprot_query": result.get("query"),
        "uniprot_release": result.get("release"),
        "uniprot_proteome": result.get("proteome_id"),
        "uniprot_reviewed_only": bool(result.get("reviewed_only")),
        "uniprot_protein_count": result.get("protein_count"),
        "uniprot_download_source": "uniprot",
    }
    metadata = {}
    if result.get("organism"):
        metadata["organism"] = result["organism"]

    created = []
    for entry in staged:
        try:
            obj = await object_service.ingest_local_file(
                # As in `_apply_assembly_download`: a download has only the
                # payload's project_id and no parent object to inherit from,
                # so the owner of the job that asked for it is the one that
                # reaches here.
                owner=owner,
                project_id=project_id,
                path=Path(entry["path"]),
                name=entry["name"],
                # The role that keeps a protein FASTA out of the aligner's
                # reference picker. Both are FormatKind.FASTA; only this
                # tells them apart.
                role=ObjectRole.PROTEIN,
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts={k: v for k, v in facts.items() if v is not None},
                metadata=metadata,
            )
        except Exception as e:  # noqa: BLE001 - the transfer already succeeded
            log.error(
                "uniprot_ingest_failed",
                query=result.get("query"),
                name=entry.get("name"),
                error=str(e),
            )
            continue
        created.append(obj)

    if not created:
        log.error("uniprot_download_ingested_nothing", query=result.get("query"))
        return

    # Two steps, matching `_apply_assembly_download`: a job does not know its
    # run, so the run is looked up first. There is no single "attach outputs
    # to this job" call.
    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(
            run_id, [o.id for o in created], owner=created[0].owner
        )

    log.info(
        "uniprot_download_applied",
        query=result.get("query"),
        objects=[str(o.id) for o in created],
        proteins=result.get("protein_count"),
    )


async def _apply_run_qc(result: dict, *, owner: str) -> None:
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

    # QC is the moment there is finally something worth narrating, so this is
    # where the optional summary is offered. Best-effort in every direction: it
    # returns None when summaries are disabled or the numbers are unchanged, and
    # a failure to *queue* it must not undo the QC write that just succeeded.
    from app.services import pipeline_service

    try:
        # The *launching* owner, not `obj.owner`: launch_summary now scopes its
        # own lookup, and the QC job that got here was enqueued under the
        # profile that owns this file, so the two agree. Passing the launching
        # profile keeps the chained job in the same partition as the job that
        # spawned it, which is what the activity view groups on.
        await pipeline_service.launch_summary(object_id=obj.id, owner=owner)
    except Exception as e:  # noqa: BLE001 - an additive extra cannot fail QC
        log.warning("summary_launch_failed", object_id=object_id, error=str(e))


async def _apply_summarize_object(result: dict, *, owner: str) -> None:
    """Record a generated narrative summary on the object it describes.

    A no-op when the model server was down or the file had too little to say --
    the handler reports those as a `skipped` reason rather than a failure, and
    the right response to both is to leave whatever summary already exists
    alone. Overwriting a good summary with nothing because the server happened
    to be off would make the feature worse than not having it.
    """
    object_id = result.get("object_id")
    summary = result.get("summary")
    if not object_id or not summary:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("summary_object_missing", object_id=object_id)
        return

    facts = {
        **obj.facts,
        "ai_summary": summary,
        "ai_summary_model": result.get("model"),
        "ai_summary_at": datetime.now(UTC).isoformat(),
    }
    # The fingerprint of the inputs this summary was written from. Present only
    # when the launcher computed one; the UI compares it against the current
    # facts to mark a summary stale after a QC or trim run changes the numbers.
    fingerprint = result.get("facts_fingerprint")
    if fingerprint:
        facts["ai_summary_fingerprint"] = fingerprint

    await obj.set(
        {
            DataObject.facts: facts,
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info("summary_applied", object_id=object_id, model=result.get("model"))


async def _apply_summarize_de_results(result: dict, *, owner: str) -> None:
    """Record a generated DE narrative summary on the results object.

    Same no-op-on-skip contract as _apply_summarize_object: a down model
    server or an unremarkable result must leave whatever summary already
    exists alone rather than clearing it.
    """
    object_id = result.get("object_id")
    summary = result.get("summary")
    if not object_id or not summary:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("de_summary_object_missing", object_id=object_id)
        return

    facts = {
        **obj.facts,
        "ai_de_summary": summary,
        "ai_de_summary_model": result.get("model"),
        "ai_de_summary_at": datetime.now(UTC).isoformat(),
    }
    fingerprint = result.get("facts_fingerprint")
    if fingerprint:
        facts["ai_de_summary_fingerprint"] = fingerprint

    await obj.set(
        {
            DataObject.facts: facts,
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info("de_summary_applied", object_id=object_id, model=result.get("model"))


async def _apply_summarize_variant_results(result: dict, *, owner: str) -> None:
    """Record a generated variant-call narrative summary on the VCF object."""
    object_id = result.get("object_id")
    summary = result.get("summary")
    if not object_id or not summary:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("variant_summary_object_missing", object_id=object_id)
        return

    facts = {
        **obj.facts,
        "ai_variant_summary": summary,
        "ai_variant_summary_model": result.get("model"),
        "ai_variant_summary_at": datetime.now(UTC).isoformat(),
    }
    fingerprint = result.get("facts_fingerprint")
    if fingerprint:
        facts["ai_variant_summary_fingerprint"] = fingerprint

    await obj.set(
        {
            DataObject.facts: facts,
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "variant_summary_applied", object_id=object_id, model=result.get("model")
    )


async def _apply_build_index(result: dict, *, owner: str) -> None:
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
                owner=reference.owner,
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
            await run_service.record_outputs(
                run_id, [o.id for o in created], owner=created[0].owner
            )

    log.info(
        "index_applied",
        reference_id=reference_id,
        aligner=result.get("aligner"),
        sidecars=len(created),
    )

    if len(created) != len(outputs):
        # Loud, because the alternative was measured: when every index file
        # was dropped here, `build_index` still finished green and the failure
        # only surfaced later as the *aligner* reporting a missing index --
        # pointing at the index build, which had apparently succeeded.
        log.error(
            "index_outputs_partially_applied",
            reference_id=reference_id,
            aligner=result.get("aligner"),
            produced=len(outputs),
            stored=len(created),
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


def align_provenance(*, result: dict, reads_facts: dict | None) -> dict:
    """The facts an alignment stamps onto the BAM it produced.

    Chemistry is copied from the reads rather than recorded fresh: aligning
    does not change how accurate the reads are, and QC already answered that
    question on the FASTQ. Without the copy the fact is unreachable from a BAM
    -- `metadata` is carried across but `facts` are not -- and anything
    downstream that picks a tool by chemistry silently gets the short-read
    default instead. Variant calling is the first such consumer.

    Absent stays absent: writing `None` would round-trip as the string "None"
    and parse back as an unrecognized chemistry rather than as "unknown".
    """
    provenance = {
        "aligned_by": result.get("aligner"),
        "aligner_version": result.get("tool_version"),
        "samtools_version": result.get("samtools_version"),
        "align_params": result.get("params") or {},
        "read_group": result.get("read_group") or {},
    }
    chemistry = (reads_facts or {}).get("qc_read_chemistry")
    if chemistry:
        provenance["qc_read_chemistry"] = chemistry
    return provenance


async def _apply_align_reads_chunked(result: dict, *, owner: str) -> None:
    """Resolve per-bucket sub-job BAMs and enqueue merge_chunked_buckets.

    The orchestrator returns sub_job_ids but does not enqueue the merge itself
    (it runs in async mode with no DB access to find output objects by sub-job
    id). This applier resolves each succeeded sub-job, collects its BAM path,
    and enqueues the merge handler.

    Raises `PermanentError` when it cannot account for every bucket, which
    fails the orchestrator job. See the comment on the refusal below.
    """
    from app.queue import queue

    sub_job_ids = result.get("sub_job_ids") or []
    if not sub_job_ids:
        log.warning("chunked_orchestrator_no_sub_jobs")
        raise PermanentError(
            "Chunked alignment recorded no bucket sub-jobs, so there is "
            "nothing to merge."
        )

    bucket_bam_paths: list[str] = []
    unresolved: list[str] = []
    for jid in sub_job_ids:
        try:
            job = await Job.get(PydanticObjectId(jid))
        except Exception:
            log.warning("chunked_orchestrator_sub_job_lookup_failed", job_id=jid)
            unresolved.append(jid)
            continue
        if job is None or job.state != JobState.SUCCEEDED:
            log.warning(
                "chunked_orchestrator_sub_job_not_succeeded",
                job_id=jid,
                state=job.state.value if job else "missing",
            )
            unresolved.append(jid)
            continue
        job_result = job.result or {}
        output = job_result.get("output") or {}
        tmp_path = output.get("tmp_path")
        if not tmp_path:
            log.warning("chunked_orchestrator_sub_job_no_output_path", job_id=jid)
            unresolved.append(jid)
            continue
        bucket_bam_paths.append(tmp_path)

    # Every bucket or none. A bucket is a slice of the reference, so merging a
    # partial set produces a BAM that is structurally valid and missing every
    # read that mapped to the absent contigs -- with nothing downstream to
    # notice, since `bucket_count` in the object's facts would claim a
    # completeness the merge never had. Refusing here costs a rerun; merging
    # anyway costs a wrong alignment that reads as a right one.
    if unresolved or not bucket_bam_paths:
        log.error(
            "chunked_orchestrator_incomplete_buckets",
            sub_job_ids=sub_job_ids,
            expected=len(sub_job_ids),
            resolved=len(bucket_bam_paths),
            unresolved=unresolved,
        )
        # Raise rather than return: `_apply_result` lets `PermanentError`
        # through specifically so an applier can fail the job it is applying.
        # Returning here left the orchestrator reporting succeeded with no
        # alignment object -- refusing to merge, and refusing to say so (#595).
        raise PermanentError(
            f"Resolved {len(bucket_bam_paths)} of {len(sub_job_ids)} bucket "
            "alignments; refusing to merge a partial reference. Re-run the "
            "alignment."
        )

    output_name = result.get("output_name") or "merged.bam"

    merge_payload: dict = {
        "bucket_bam_paths": bucket_bam_paths,
        "output_name": output_name,
        "reference_id": result.get("reference_id"),
        "project_id": result.get("project_id"),
        "reads_object_id": result.get("reads_object_id"),
        "aligner": result.get("aligner"),
        "params": result.get("params"),
        "bucket_count": result.get("bucket_count"),
    }

    await queue.enqueue(
        "merge_chunked_buckets",
        payload=merge_payload,
        owner=owner,
    )

    log.info(
        "chunked_orchestrator_applied",
        sub_job_count=len(sub_job_ids),
        bam_count=len(bucket_bam_paths),
    )


async def _apply_align_reads(result: dict, *, owner: str) -> None:
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
    provenance = align_provenance(result=result, reads_facts=reads.facts)

    try:
        bam = await object_service.ingest_local_file(
            owner=reads.owner,
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
        await run_service.record_outputs(run_id, [bam.id], owner=bam.owner)

    # Chain the follow-on index. Enqueued here rather than at launch because it
    # needs the BAM's digest, which does not exist until the alignment has run.
    if bam.blob_sha256:
        index_job = await queue.enqueue(
            "index_bam",
            owner=bam.owner,
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


def assembly_provenance(result: dict) -> dict:
    """What an assembly's outputs record about how they were made.

    The assembler and its version are half of what a methods section needs;
    the input mode is the other half, since it is the claim about read
    accuracy the whole assembly rests on.
    """
    params = result.get("params") or {}
    provenance = {
        "assembled_by": result.get("assembler"),
        "assembler_version": result.get("tool_version"),
    }
    if params.get("mode"):
        provenance["assembly_mode"] = params["mode"]
    if params.get("genome_size"):
        provenance["assembly_genome_size"] = params["genome_size"]
        # Kept beside the number rather than dropped: a size BioFlow guessed
        # and one the user typed are different claims, and only one of them is
        # evidence about the genome.
        provenance["assembly_genome_size_source"] = params.get(
            "genome_size_source", "unset"
        )
    return provenance


async def _apply_assemble_reads(result: dict, *, owner: str) -> None:
    """Turn a finished assembly into its objects.

    Two DataObjects from one job, which no other pipeline handler does --
    `_apply_assembly_download` is the precedent, not `_apply_align_reads`. The
    contigs become a reference you can align against; the graph is a result in
    its own right.

    The contigs are ingested first and independently: if the graph fails to
    ingest, an assembly that took six hours still produced its FASTA, and
    losing that because a secondary output tripped would be indefensible.
    """
    from app.services import object_service, run_service

    contigs_out = result.get("contigs")
    object_id = result.get("object_id")
    if not contigs_out or not object_id:
        return

    reads = await DataObject.get(PydanticObjectId(object_id))
    if reads is None:
        log.warning("assemble_reads_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")
    produced_by = PydanticObjectId(job_id) if job_id else None
    provenance = assembly_provenance(result)
    facts = {**provenance, **(result.get("assembly_facts") or {})}

    try:
        contigs = await object_service.ingest_local_file(
            owner=reads.owner,
            project_id=reads.project_id,
            path=Path(contigs_out["tmp_path"]),
            name=contigs_out["name"],
            # REFERENCE, so the align card offers aligning these very reads
            # back against their own assembly -- which is also the
            # precondition a polisher needs. The ingest path would not assign
            # it: should_assign_reference_role requires an NCBI accession, and
            # a de novo assembly has none.
            role=ObjectRole.REFERENCE,
            derived_from=[reads.id],
            produced_by_job=produced_by,
            facts=facts,
            metadata=dict(reads.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("assembly_contigs_ingest_failed", object_id=object_id, error=str(e))
        return

    produced = [contigs.id]

    graph_out = result.get("graph")
    if graph_out:
        try:
            graph = await object_service.ingest_local_file(
                owner=reads.owner,
                project_id=reads.project_id,
                path=Path(graph_out["tmp_path"]),
                name=graph_out["name"],
                role=ObjectRole.ASSEMBLY_GRAPH,
                # From the reads *and* the contigs: the graph describes the
                # same assembly the FASTA flattens, so a user deleting the
                # contigs should see the graph as descending from them.
                derived_from=[reads.id, contigs.id],
                produced_by_job=produced_by,
                facts=dict(provenance),
                metadata=dict(reads.metadata),
            )
            produced.append(graph.id)
        except Exception as e:  # noqa: BLE001
            # Logged and swallowed. The assembly succeeded; the graph is a
            # bonus, and failing the whole apply here would strand the FASTA
            # that cost the hours.
            log.error(
                "assembly_graph_ingest_failed", object_id=object_id, error=str(e)
            )

    log.info(
        "assembly_applied",
        object_id=object_id,
        contigs_id=str(contigs.id),
        outputs=len(produced),
        contig_count=facts.get("assembly_contig_count"),
        circular=facts.get("assembly_circular_count"),
    )

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, produced, owner=contigs.owner)


async def _apply_index_bam(result: dict, *, owner: str) -> None:
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
    bai_ingested = False
    try:
        await object_service.ingest_local_file(
            owner=bam.owner,
            project_id=bam.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            derived_from=[bam.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            sidecar_of=bam.id,
            sidecar_role=SidecarRole.BAI,
        )
        bai_ingested = True
    except Exception as e:  # noqa: BLE001
        log.error("bai_ingest_failed", object_id=bam_id, error=str(e))

    facts = result.get("facts") or {}
    if bai_ingested:
        # `has_index` is normally set once at ingest-time parsing (see
        # storage/parsers.py) and never revisited, so a BAM indexed after the
        # fact -- like this one -- needs it stamped here or the Results tab
        # and Provenance panel both keep reporting a missing index forever.
        facts = {**facts, "has_index": True}
    if facts:
        # On the BAM itself: this is where a user looks to decide whether the
        # alignment is worth keeping. Written as per-key `facts.<key>` paths,
        # not a whole-dict merge -- `_apply_ingest_headers` runs on this same
        # BAM at nearly the same moment, and a merge computed from a stale
        # `bam.facts` snapshot erased its header facts (#606).
        await bam.set(
            {
                **{f"facts.{key}": value for key, value in facts.items()},
                DataObject.updated_at: datetime.now(UTC),
            }
        )

    log.info("index_bam_applied", object_id=bam_id, mapped_pct=facts.get("mapped_pct"))

    # This index was built on the Results tab's behalf (see
    # pipeline_service.launch_bam_stats), so finish what the user actually
    # asked for now that the .bai exists.
    if result.get("then_bam_stats"):
        from app.errors import AppError
        from app.services import pipeline_service

        try:
            # Same reasoning as the QC -> summary chain: the index job carried
            # the launching profile, and launch_bam_stats now scopes its own
            # BAM lookup, so handing it that profile is what lets the chained
            # computation resolve the very BAM the index was just built for.
            await pipeline_service.launch_bam_stats(object_id=bam.id, owner=owner)
        except AppError as e:
            log.warning("bam_stats_chain_failed", object_id=bam_id, error=str(e))


async def _apply_run_bam_stats(result: dict, *, owner: str) -> None:
    """Record a Results computation's numbers on the BAM it described.

    Read-only like QC: no files to ingest, just facts merged onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("bam_stats_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "bam_stats_applied",
        object_id=object_id,
        mean_depth=facts.get("bam_stats_summary", {}).get("mean_depth"),
    )


async def _apply_feature_coverage(result: dict, *, owner: str) -> None:
    """Record a per-feature coverage computation's numbers on the BAM it
    described.

    Read-only like bam_stats: no files to ingest, just facts merged onto the
    object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("feature_coverage_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "feature_coverage_applied",
        object_id=object_id,
        feature_count=facts.get("feature_coverage_feature_count"),
        median_breadth=facts.get("feature_coverage_median_breadth"),
    )


async def _apply_variants_in_regions(result: dict, *, owner: str) -> None:
    """Record variants in regions computation numbers on the VCF object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("variants_in_regions_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "variants_in_regions_applied",
        object_id=object_id,
        total_variants=facts.get("variants_in_regions_total_variants"),
        in_features=facts.get("variants_in_regions_in_features"),
    )


async def _apply_annotation_comparison(result: dict, *, owner: str) -> None:
    """Record annotation comparison results on the primary annotation object."""
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("annotation_comparison_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "annotation_comparison_applied",
        object_id=object_id,
        jaccard=facts.get("annotation_comparison_jaccard"),
    )


async def _apply_sequence_extraction(result: dict, *, owner: str) -> None:
    """Ingest extracted sequences FASTA as a new DataObject with provenance."""
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    if not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("sequence_extraction_source_missing", object_id=object_id)
        return

    workdir = result.get("workdir")
    output_name = result.get("output_name", "extracted.fasta")
    if not workdir:
        return

    out_file = Path(workdir) / "extracted.fasta"
    if not out_file.is_file():
        return

    job_id = result.get("job_id")
    derived_from = [source.id]

    anno_id = result.get("source_annotation_id")
    if anno_id:
        try:
            derived_from.append(PydanticObjectId(anno_id))
        except Exception:
            pass

    facts = result.get("facts") or {}

    try:
        derived = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=out_file,
            name=output_name,
            role=ObjectRole.REFERENCE,
            derived_from=derived_from,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=facts,
            metadata=dict(source.metadata),
        )
        if job_id:
            await run_service.record_job_produced_object(
                PydanticObjectId(job_id), derived.id
            )
    except Exception as e:  # noqa: BLE001
        log.error("sequence_extraction_ingest_failed", object_id=object_id, error=str(e))
        return

    log.info("sequence_extraction_applied", object_id=object_id, derived_id=str(derived.id))


async def _apply_coverage(result: dict, *, owner: str) -> None:
    """Record a per-window depth computation's numbers on the BAM it
    described.

    Read-only like feature_coverage: no files to ingest, just facts merged
    onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("coverage_object_missing", object_id=object_id)
        return

    # Facts merge, so the mode-specific keys of a *previous* run survive one in
    # the other mode: re-running a windowed BAM against a target BED otherwise
    # leaves `coverage_window_count` beside the new `coverage_region_count`,
    # and only one of the two describes the report now on disk. Observed on a
    # real BAM before this drop was added. Every other coverage_* key is
    # overwritten by this run, so only these two need clearing.
    merged = {**obj.facts, **facts}
    stale = (
        "coverage_window_count"
        if facts.get("coverage_mode") == "regions"
        else "coverage_region_count"
    )
    merged.pop(stale, None)
    if facts.get("coverage_mode") != "regions":
        # Likewise unattributable once the run it named is no longer the
        # report on disk.
        merged.pop("coverage_regions_id", None)

    await obj.set(
        {
            DataObject.facts: merged,
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "coverage_applied",
        object_id=object_id,
        mode=facts.get("coverage_mode"),
        mean_depth=facts.get("coverage_mean_depth"),
        contig_count=facts.get("coverage_contig_count"),
    )


async def _apply_methylation(result: dict, *, owner: str) -> None:
    """Ingest a finished modkit pileup's bedMethyl file and record its
    summary facts on the BAM it described.

    Unlike coverage/feature_coverage beside it, this produces a real derived
    object -- the bedMethyl track itself, per decision K4 of the design spec
    -- because a person opens it directly (IGV, R, a spreadsheet), not just
    the summary numbers. K3 is enforced by the handler, not here: a pileup
    with zero rows never reaches this applier because `run_methylation`
    raises before returning a result for it.

    Facts are merged onto the BAM by per-key `facts.<key>` paths, never a
    whole-dict merge -- `_apply_ingest_headers` can run on the same BAM at
    nearly the same moment, and a merge computed from a stale `bam.facts`
    snapshot would erase whichever keys the other had just written (#606).
    """
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    facts = result.get("facts") or {}
    if not object_id or not output or not facts:
        return

    bam = await DataObject.get(PydanticObjectId(object_id))
    if bam is None:
        log.warning("methylation_object_missing", object_id=object_id)
        return

    job_id = result.get("job_id")
    try:
        # `output["name"]` ends in `.bed` (see run_methylation), which is
        # what lets the ingest path's own format sniffing classify this as
        # FormatKind.BED without a format override here -- the same route
        # `_apply_call_variants` relies on for its VCF.
        bedmethyl = await object_service.ingest_local_file(
            owner=bam.owner,
            project_id=bam.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            derived_from=[bam.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
        )
    except Exception as e:  # noqa: BLE001
        log.error("methylation_bedmethyl_ingest_failed", object_id=object_id, error=str(e))
        return

    await bam.set(
        {
            **{f"facts.{key}": value for key, value in facts.items()},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "methylation_applied",
        object_id=object_id,
        bedmethyl_id=str(bedmethyl.id),
        site_count=facts.get("methylation_site_count"),
        mean_pct=facts.get("methylation_mean_pct"),
    )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [bedmethyl.id], owner=bedmethyl.owner)


async def _apply_run_transcript_qc(result: dict, *, owner: str) -> None:
    """Record RNA-seq transcript QC on the BAM it described.

    Read-only like BAM stats: no files to ingest, just facts merged onto the
    object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("transcript_qc_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info("transcript_qc_applied", object_id=object_id)


async def _apply_run_vcf_stats(result: dict, *, owner: str) -> None:
    """Record a Variant Results computation on the VCF it described.

    Read-only like QC and BAM stats: no files to ingest, just facts merged
    onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("vcf_stats_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "vcf_stats_applied",
        object_id=object_id,
        variants=facts.get("vcf_stats_summary", {}).get("variants"),
    )

    from app.services import pipeline_service

    try:
        await pipeline_service.launch_variant_summary(object_id=obj.id, owner=owner)
    except Exception as e:  # noqa: BLE001 - an additive extra cannot fail vcf_stats
        log.warning("variant_summary_launch_failed", object_id=object_id, error=str(e))


async def _apply_run_annotation_stats(result: dict, *, owner: str) -> None:
    """Record an Annotation Results computation on the file it described.

    Read-only like QC, BAM stats, and VCF stats: no files to ingest, just
    facts merged onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("annotation_stats_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "annotation_stats_applied",
        object_id=object_id,
        features=facts.get("annotation_feature_count"),
    )


async def _apply_export_annotation_subset(result: dict, *, owner: str) -> None:
    """Register an exported annotation subset as a new object.

    `derived_from` the source annotation, so the subset's lineage is
    inspectable and the explorer can relate the two (AE-19).

    Deliberately carries no annotation results facts (AE-20): the new object
    opens to a "Compute results" button like any other annotation. Whether
    ingestion should trigger analysis automatically is #298's question, and
    answering it here by side effect would pre-empt it.
    """
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("annotation_export_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")

    try:
        subset = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ANNOTATION,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={"annotation_subset_feature_count": result.get("feature_count")},
            # The subset describes the same biology as its source.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "annotation_export_ingest_failed", object_id=object_id, error=str(e)
        )
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [subset.id], owner=subset.owner)

    log.info(
        "annotation_export_applied",
        object_id=object_id,
        subset_id=str(subset.id),
    )


async def _apply_transfer_annotation(result: dict, *, owner: str) -> None:
    """Register a Liftoff-lifted annotation as a new object.

    `derived_from` the target assembly, so the lifted GFF3's lineage points
    at the genome it was transferred onto (and the explorer can relate the
    two). Carries no annotation results facts -- the new object opens to a
    "Compute results" button like any other annotation.
    """
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("transfer_annotation_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")

    try:
        lifted = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ANNOTATION,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={},
            # The lifted annotation describes the target assembly's genes.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "transfer_annotation_ingest_failed", object_id=object_id, error=str(e)
        )
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [lifted.id], owner=lifted.owner)

    log.info(
        "transfer_annotation_applied",
        object_id=object_id,
        lifted_id=str(lifted.id),
    )


async def _apply_materialize_annotation_edits(result: dict, *, owner: str) -> None:
    """Register a materialized annotation edit as a derived object and clear
    the pending edits.

    Same pattern as _apply_export_annotation_subset: the derived object is
    ANNOTATION-role, derived_from the source, and carries no pre-computed
    annotation results (ED-8).

    Clears the source object's pending edits on success — they have been
    consumed into the derived object (ED-6).
    """
    from app.models.annotation_edit import AnnotationEdit
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("annotation_materialize_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")
    edit_summary = result.get("edit_summary", [])
    edit_count = result.get("edit_count", len(edit_summary))

    try:
        derived = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ANNOTATION,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={
                "annotation_edit_count": edit_count,
                "annotation_edit_summary": edit_summary,
            },
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "annotation_materialize_ingest_failed", object_id=object_id, error=str(e)
        )
        return

    # Clear the consumed edits (ED-6: delete on success).
    delete_result = await AnnotationEdit.find(
        AnnotationEdit.object_id == PydanticObjectId(object_id)
    ).delete()
    log.info(
        "annotation_materialize_cleared_edits",
        object_id=object_id,
        deleted=delete_result.deleted_count,
    )

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [derived.id], owner=derived.owner)

    log.info(
        "annotation_materialize_applied",
        object_id=object_id,
        derived_id=str(derived.id),
        edits=edit_count,
    )


async def _apply_extract_genbank_sequence(result: dict, *, owner: str) -> None:
    """Register a GenBank's extracted sequence as a new reference.

    The same shape as `_apply_export_annotation_subset` above, with one
    material difference: `role=ObjectRole.REFERENCE`, not ANNOTATION. That
    role is the whole point -- it is what puts the extracted FASTA in every
    reference picker, which is what makes the GenBank's sequence usable for
    alignment (#348). No sidecar role: this is a first-class object a person
    chooses, not scaffolding the explorer hides.
    """
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("genbank_sequence_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")

    try:
        reference = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.REFERENCE,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={"genbank_source_record_count": result.get("record_count")},
            # The extracted sequence describes the same biology as its source.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "genbank_sequence_ingest_failed", object_id=object_id, error=str(e)
        )
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [reference.id], owner=reference.owner)

    log.info(
        "genbank_sequence_applied",
        object_id=object_id,
        reference_id=str(reference.id),
    )


async def _apply_assess_completeness(result: dict, *, owner: str) -> None:
    """Record compleasm's completeness scores on the assembly it described.

    Read-only like BAM stats and VCF stats: no files to ingest, just facts
    merged onto the object. Unlike those two, the input can be an uploaded
    assembly with no `produced_by_job` of its own, which is fine here -- this
    applier only needs the object to exist, not to have been produced by a
    prior BioFlow run.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("completeness_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "completeness_applied",
        object_id=object_id,
        complete_pct=facts.get("assembly_completeness_complete_pct"),
        lineage=facts.get("assembly_completeness_lineage"),
    )


async def _apply_assess_misassemblies(result: dict, *, owner: str) -> None:
    """Record QUAST's reference-based misassembly facts on the assembly it
    described.

    A near-copy of `_apply_assess_completeness`: read-only, no files to
    ingest, and the object need not have been produced by a prior BioFlow
    run -- an uploaded assembly is scored exactly like one this application
    produced.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("misassembly_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "misassembly_applied",
        object_id=object_id,
        total=facts.get("assembly_misassembly_total"),
        reference_id=facts.get("assembly_reference_id"),
    )

async def _apply_analyze_gc_tracks(result: dict, *, owner: str) -> None:
    """Record per-contig GC content and skew tracks on the assembly."""
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("gc_tracks_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    gc_tracks = facts.get("gc_tracks") or {}
    log.info(
        "gc_tracks_applied",
        object_id=object_id,
        contigs=len(gc_tracks.get("contigs") or []),
        partial=gc_tracks.get("gc_tracks_partial"),
    )


async def _apply_analyze_meryl_tracks(result: dict, *, owner: str) -> None:
    """Record meryl k-mer spectra and repeat-density facts on the assembly.

    Near-copy of ``_apply_analyze_gc_tracks``: read-only, no files to
    ingest. Missing from ``_APPLIERS`` until #612 — the handler's facts
    landed on ``Job.result`` and went nowhere, the exact silent-skip shape
    CLAUDE.md documents from the STAR ``build_index`` failure.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("meryl_tracks_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    density = facts.get("repeat_density") or {}
    log.info(
        "meryl_tracks_applied",
        object_id=object_id,
        spectra=("kmer_spectra" in facts),
        contigs=len(density.get("contigs") or []),
        partial=density.get("repeat_density_partial"),
    )


async def _apply_analyze_synteny(result: dict, *, owner: str) -> None:
    """Record minimap2's synteny alignment on the draft assembly it
    described.

    Near-copy of `_apply_assess_misassemblies`: read-only, no files to
    ingest, and the draft need not have been produced by a prior BioFlow
    run -- an uploaded assembly is aligned exactly like one this application
    produced.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("synteny_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    alignment = facts.get("synteny_alignment") or {}
    log.info(
        "synteny_applied",
        object_id=object_id,
        segments=len(alignment.get("segments") or []),
        reference_id=alignment.get("reference_object_id"),
    )


async def _apply_annotate_genome(result: dict, *, owner: str) -> None:
    """Record Bakta's gene density facts on the assembly it describes.

    Near-copy of ``_apply_analyze_gc_tracks``: read-only, no files to
    ingest, and an uploaded assembly is annotated exactly like one this
    application produced.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("annotation_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    gene_density = facts.get("gene_density") or {}
    log.info(
        "annotation_applied",
        object_id=object_id,
        contigs=len(gene_density.get("contigs") or []),
        partial=gene_density.get("gene_density_partial"),
    )


async def _apply_classify_reads(result: dict, *, owner: str) -> None:
    """Record classification facts on the object they describe.

    Near-copy of ``_apply_annotate_genome``: read-only, no files to
    ingest.  A prior ``taxonomy_mismatch`` is cleared when the new run has
    none -- reclassifying against a better database must be able to
    retract the accusation, not merely restate it. Facts are merged by
    per-key `facts.<key>` path (#606).
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("classification_object_missing", object_id=object_id)
        return

    update: dict = {
        DataObject.updated_at: datetime.now(UTC),
        **{f"facts.{key}": value for key, value in facts.items()},
    }
    await obj.set(update)
    if "taxonomy_mismatch" not in facts and "taxonomy_mismatch" in obj.facts:
        await DataObject.find_one(DataObject.id == obj.id).update(
            {"$unset": {"facts.taxonomy_mismatch": ""}}
        )

    log.info(
        "classification_applied",
        object_id=object_id,
        taxa=len((facts.get("taxonomy") or {}).get("taxa") or []),
        mismatch="taxonomy_mismatch" in facts,
        bin_label=facts.get("bin_taxon_label"),
    )


async def _apply_assess_assembly_continuity(result: dict, *, owner: str) -> None:
    """Record GCI's continuity facts on the assembly they describe.

    Near-copy of `_apply_assess_assembly_errors`: read-only, nothing to
    ingest, and an uploaded assembly is scored exactly like one this
    application produced.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("assembly_continuity_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "assembly_continuity_applied",
        object_id=object_id,
        gci=facts.get("assembly_continuity_gci"),
    )


async def _apply_assess_assembly_errors(result: dict, *, owner: str) -> None:
    """Record CRAQ's error-detection facts on the assembly they describe.

    Near-copy of `_apply_assess_misassemblies`: read-only, nothing to
    ingest, and an uploaded assembly is scored exactly like one this
    application produced.

    The one exception is opt-in chimera breaking (`-b`, never set by the
    Actions card): when the handler produced a corrected FASTA, it is
    ingested below as a brand new REFERENCE object, never as a replacement
    for the assembly scored above -- that assembly's facts and identity are
    untouched by the ingest.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("assembly_errors_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "assembly_errors_applied",
        object_id=object_id,
        cre=facts.get("assembly_error_cre_count"),
        aqi=facts.get("assembly_error_aqi"),
    )

    corrected = result.get("corrected_fasta")
    if not corrected:
        return

    from app.services import object_service, run_service

    job_id = result.get("job_id")
    parents = [obj.id]
    for key in ("ngs_bam_object_id", "sms_bam_object_id"):
        bam_id = result.get(key)
        if bam_id:
            parents.append(PydanticObjectId(bam_id))

    try:
        new_obj = await object_service.ingest_local_file(
            owner=obj.owner,
            project_id=obj.project_id,
            path=Path(corrected),
            name=f"{obj.name}.craq-corrected.fa",
            # REFERENCE for the same reason a de novo assembly gets it
            # (results.py:1271): it is assembly-shaped and alignable. A new
            # object, never a replacement -- the input assembly keeps its
            # facts and its identity.
            role=ObjectRole.REFERENCE,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={"assembly_source": "craq_break"},
            metadata=dict(obj.metadata),
        )
    except Exception as e:  # noqa: BLE001
        # Never destroys the QC result: the facts above are already
        # committed, and a secondary ingest failing must not lose them --
        # the posture _apply_assemble_reads takes for its graph output.
        log.error("craq_corrected_ingest_failed", object_id=str(obj.id), error=str(e))
        return

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(
                run_id, [new_obj.id], owner=new_obj.owner
            )


async def _apply_assess_assembly_qv(result: dict, *, owner: str) -> None:
    """Record Merqury's QV facts on the assembly they describe, and cache
    the read k-mer database as sidecars on the read object.

    Facts half is a near-copy of `_apply_assess_assembly_errors`. The
    sidecar half follows `_apply_build_index`'s pattern for STAR's
    multi-file index: every file inside the meryl database directory
    becomes its own sidecar object on the READ object (not the assembly --
    the database is derived from the reads and has nothing to do with any
    particular assembly), tagged `SidecarRole.MERYL_DB` and carrying the k
    it was built at plus the database's own directory name in its facts,
    since meryl's own qv.sh reads k back out of the database rather than
    taking it as an argument -- a database at a different k cannot serve a
    run wanting this one, and `meryl_db_name` is what lets a later read
    (Task 5) group the flat sidecar list back into one directory per
    database.

    Unlike `_apply_build_index`'s fixed, hand-enumerated STAR outputs, a
    meryl database's file list is only known at runtime -- so this walks
    `read_db_dir` (set by the handler only when it built a fresh database)
    rather than being handed an explicit `outputs` list.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("assembly_qv_object_missing", object_id=object_id)
        return

    await obj.set(
        {
            DataObject.facts: {**obj.facts, **facts},
            DataObject.updated_at: datetime.now(UTC),
        }
    )

    log.info(
        "assembly_qv_applied",
        object_id=object_id,
        qv=facts.get("assembly_qv"),
        completeness=facts.get("assembly_qv_completeness_pct"),
    )

    read_db_dir = result.get("read_db_dir")
    read_object_id = result.get("read_object_id")
    if not read_db_dir or not read_object_id:
        return

    read_obj = await DataObject.get(PydanticObjectId(read_object_id))
    if read_obj is None:
        log.warning("meryl_db_read_object_missing", object_id=read_object_id)
        return

    from app.services import object_service

    db_dir = Path(read_db_dir)
    job_id = result.get("job_id")
    k = result.get("k")
    members = sorted(
        p
        for p in await asyncio.to_thread(lambda: list(db_dir.rglob("*")))
        if p.is_file()
    )
    created = []
    for member in members:
        try:
            obj_out = await object_service.ingest_local_file(
                owner=read_obj.owner,
                project_id=read_obj.project_id,
                path=member,
                # Relative path under the db directory, flattened with a
                # separator that survives as an object name -- mirrors how
                # index files keep their own names in _apply_build_index.
                name=f"{db_dir.name}__{member.relative_to(db_dir).as_posix().replace('/', '__')}",
                derived_from=[read_obj.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                # `meryl_db_expected_count` is the total file count this
                # database was supposed to produce, stamped on every member
                # -- not just the ones that ingested. Without it, a reader
                # grouping sidecars by `meryl_db_name` cannot tell a complete
                # group from one a partial ingest (see meryl_db_partially_
                # applied below) silently shrank; it would see, say, 3 files
                # and have no way to know 5 were expected.
                facts={
                    "meryl_db_k": k,
                    "meryl_db_name": db_dir.name,
                    "meryl_db_expected_count": len(members),
                },
                sidecar_of=read_obj.id,
                sidecar_role=SidecarRole.MERYL_DB,
            )
        except Exception as e:  # noqa: BLE001 - one bad file must not lose the rest
            # A sidecar that fails to ingest costs the next run a rebuild.
            # Must never destroy the QV facts already written above -- same
            # separate-applier discipline every other secondary output here
            # follows.
            log.error(
                "meryl_db_member_ingest_failed",
                read_object_id=read_object_id,
                member=str(member),
                error=str(e),
            )
            continue
        created.append(obj_out)

    if job_id and created:
        from app.services import run_service

        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(
                run_id, [o.id for o in created], owner=created[0].owner
            )

    log.info(
        "meryl_db_cached",
        read_object_id=read_object_id,
        files=len(created),
        k=k,
    )

    if len(created) != len(members):
        # Loud, because the alternative was measured on STAR: when every
        # index file was dropped here, the build job still finished green
        # and the failure only surfaced later as a downstream job reporting
        # a missing index -- pointing at the wrong step. A meryl database
        # that only partially cached is the same shape: the next run would
        # symlink an incomplete directory and fail deep inside merqury.sh
        # rather than here, where the actual cause is known.
        log.error(
            "meryl_db_partially_applied",
            read_object_id=read_object_id,
            produced=len(members),
            stored=len(created),
        )


async def _apply_consensus_from_alignment(result: dict, *, owner: str) -> None:
    """Turn a finished iVar consensus run into a new reference object.

    Mirrors `_apply_call_variants`: the consensus descends from both the
    BAM and the reference it was called against, since a consensus is a
    claim about a specific reference and means nothing without knowing
    which one. Role REFERENCE, not a new role, for the same reason the
    foundation (#21) gives Pilon and RagTag's outputs: a consensus is a
    sequence others will align against, addressable and auditable through
    the same object model de novo assembly already uses.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    bam_id = result.get("bam_object_id")
    reference_id = result.get("reference_object_id")
    if not output or not bam_id:
        return

    bam = await DataObject.get(PydanticObjectId(bam_id))
    if bam is None:
        log.warning("consensus_parent_missing", object_id=bam_id)
        return

    parents = [bam.id]
    if reference_id:
        parents.append(PydanticObjectId(reference_id))

    job_id = result.get("job_id")
    facts = result.get("facts") or {}
    try:
        consensus = await object_service.ingest_local_file(
            owner=bam.owner,
            project_id=bam.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.REFERENCE,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=facts,
        )
    except Exception as e:  # noqa: BLE001
        log.error("consensus_ingest_failed", object_id=bam_id, error=str(e))
        return

    log.info(
        "consensus_applied",
        bam_id=bam_id,
        consensus_id=str(consensus.id),
        n_count=facts.get("consensus_n_count"),
        primers_trimmed=facts.get("consensus_primers_trimmed"),
    )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(
                run_id, [consensus.id], owner=consensus.owner
            )


async def _apply_polish_assembly(result: dict, *, owner: str) -> None:
    """Turn a finished polish run -- Polypolish or Medaka -- into a new assembly object.

    The polished assembly sits *beside* the draft rather than replacing it.
    That is deliberate: the comparison between draft and polished is the
    evidence that polishing helped at all, and overwriting the input
    destroys the only thing that could show it. `polish_changed_positions`
    is the fact that makes the comparison cheap -- a polish that changed
    nothing is either a clean assembly or a broken pipeline, and only that
    number next to the measured depth tells them apart.

    Role REFERENCE, matching the consensus applier and the foundation's rule
    that every generated assembly is addressable, alignable and auditable
    through the same object model.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    draft_id = result.get("draft_object_id")
    if not output or not draft_id:
        return

    draft = await DataObject.get(PydanticObjectId(draft_id))
    if draft is None:
        log.warning("polish_parent_missing", object_id=draft_id)
        return

    # The reads are parents too, not just the draft. A polished assembly is a
    # claim about which reads corrected it, and a provenance graph that
    # showed only the draft would make two polishes of one draft with
    # different samples indistinguishable.
    parents = [draft.id]
    for key in ("reads_object_id", "mate_object_id"):
        value = result.get(key)
        if value:
            parents.append(PydanticObjectId(value))

    job_id = result.get("job_id")
    facts = result.get("facts") or {}
    try:
        polished = await object_service.ingest_local_file(
            owner=draft.owner,
            project_id=draft.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.REFERENCE,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=facts,
        )
    except Exception as e:  # noqa: BLE001
        log.error("polish_ingest_failed", object_id=draft_id, error=str(e))
        return

    log.info(
        "polish_applied",
        draft_id=draft_id,
        polished_id=str(polished.id),
        changed=facts.get("polish_changed_positions"),
        tool=facts.get("polish_tool"),
        careful=facts.get("polish_careful_mode"),
    )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(
                run_id, [polished.id], owner=polished.owner
            )


async def _apply_scaffold_assembly(result: dict, *, owner: str) -> None:
    """Turn a finished RagTag run into a new scaffolded assembly object.

    Two parents, like the polish applier: the draft and the reference. That
    is not optional bookkeeping here the way it might read at a glance --
    RagTag names scaffolds after the *reference's* sequences, so the
    resulting object's structure is partly a claim about which reference
    produced it. `derived_from` omitting the reference would make that claim
    unrecoverable, and `scaffold_reference_name` on the facts is what a
    human reads before trusting the arrangement at all.

    The AGP, when the handler produced one, is ingested as its own visible
    object beside the scaffolded FASTA rather than folded into it or left in
    the workdir -- unlike the intermediates the polish and consensus
    appliers discard, it is the only record of which contig went where and
    in what orientation, and regenerating it means re-running the job. It
    is not a `sidecar_of` scaffolding file (that model exists for aligner
    index files the explorer deliberately hides); a human is meant to open
    this one.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    draft_id = result.get("draft_object_id")
    reference_id = result.get("reference_object_id")
    if not output or not draft_id:
        return

    draft = await DataObject.get(PydanticObjectId(draft_id))
    if draft is None:
        log.warning("scaffold_parent_missing", object_id=draft_id)
        return

    parents = [draft.id]
    if reference_id:
        parents.append(PydanticObjectId(reference_id))

    job_id = result.get("job_id")
    facts = result.get("facts") or {}
    try:
        scaffolded = await object_service.ingest_local_file(
            owner=draft.owner,
            project_id=draft.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.REFERENCE,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=facts,
        )
    except Exception as e:  # noqa: BLE001
        log.error("scaffold_ingest_failed", object_id=draft_id, error=str(e))
        return

    agp = result.get("agp")
    if agp:
        try:
            await object_service.ingest_local_file(
                owner=draft.owner,
                project_id=draft.project_id,
                path=Path(agp["tmp_path"]),
                name=agp["name"],
                derived_from=parents,
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                facts={"scaffold_of_object_id": str(scaffolded.id)},
            )
        except Exception as e:  # noqa: BLE001
            # The AGP is a convenience, not the deliverable -- its own
            # ingest failing must not undo the scaffold that already
            # succeeded above.
            log.warning("scaffold_agp_ingest_failed", object_id=draft_id, error=str(e))

    log.info(
        "scaffold_applied",
        draft_id=draft_id,
        scaffolded_id=str(scaffolded.id),
        placed=facts.get("scaffold_placed_sequences"),
        unplaced=facts.get("scaffold_unplaced_sequences"),
    )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(
                run_id, [scaffolded.id], owner=scaffolded.owner
            )


def variant_provenance(result: dict) -> dict:
    """The facts a variant calling run stamps onto the VCF it produced.

    Which caller ran is not a detail: Clair3 and bcftools disagree about
    marginal sites, and a VCF whose caller is unrecorded cannot be compared
    against another or written up in a methods section.
    """
    return {
        "variants_called_by": result.get("caller"),
        "variant_caller_version": result.get("tool_version"),
        "variant_params": result.get("params") or {},
    }


async def _apply_call_variants(result: dict, *, owner: str) -> None:
    """Turn a finished variant calling run into a VCF object and its index.

    The VCF descends from both the BAM and the reference: a variant call is a
    claim about a position in a particular reference, and the call means
    nothing without knowing which one. The `.tbi` is a sidecar of the VCF,
    exactly as `.bai` is of a BAM.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    bam_id = result.get("bam_object_id")
    if not output or not bam_id:
        return

    bam = await DataObject.get(PydanticObjectId(bam_id))
    if bam is None:
        log.warning("call_variants_parent_missing", object_id=bam_id)
        return

    parents = [bam.id]
    reference_id = result.get("reference_object_id")
    if reference_id:
        parents.append(PydanticObjectId(reference_id))

    job_id = result.get("job_id")
    try:
        vcf = await object_service.ingest_local_file(
            owner=bam.owner,
            project_id=bam.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.VARIANTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=variant_provenance(result),
            # Sample-level metadata describes the biology, which calling does
            # not change -- so the VCF stays findable by the sample it came
            # from, the same reasoning as the BAM's copy.
            metadata=dict(bam.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("variant_vcf_ingest_failed", object_id=bam_id, error=str(e))
        return

    log.info("call_variants_applied", bam_id=bam_id, vcf_id=str(vcf.id))

    # The index is attached after the VCF exists, and its failure is logged
    # rather than raised: the VCF is the deliverable, and an index can be
    # rebuilt from it at any time.
    index = result.get("index")
    if index:
        try:
            await object_service.ingest_local_file(
                owner=vcf.owner,
                project_id=bam.project_id,
                path=Path(index["tmp_path"]),
                name=index["name"],
                derived_from=[vcf.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                sidecar_of=vcf.id,
                sidecar_role=SidecarRole.TBI,
            )
        except Exception as e:  # noqa: BLE001
            log.error("variant_tbi_ingest_failed", vcf_id=str(vcf.id), error=str(e))

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [vcf.id], owner=vcf.owner)


def sv_provenance(result: dict) -> dict:
    """The facts a structural variant calling run stamps onto the VCF it
    produced. Mirrors `variant_provenance`.

    The caller comes from the result rather than a literal: since #620 this
    pipeline runs Sniffles2 for long reads and Delly for short ones, and a
    hardcoded name would mislabel one of them permanently on disk.

    A result with no caller predates #620, when Sniffles2 was the only SV
    caller -- so that is the fallback, and it applies only when the field is
    absent.
    """
    return {
        "variants_called_by": result["caller"] if "caller" in result else "sniffles2",
        "variant_caller_version": result.get("tool_version"),
        "variant_params": result.get("params") or {},
    }


async def _apply_call_structural_variants(result: dict, *, owner: str) -> None:
    """Turn a finished structural variant calling run into a VCF object and
    its index. Mirrors `_apply_call_variants`: the VCF descends from both the
    BAM and the reference, and the `.tbi` is a sidecar of the VCF.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    bam_id = result.get("bam_object_id")
    if not output or not bam_id:
        return

    bam = await DataObject.get(PydanticObjectId(bam_id))
    if bam is None:
        log.warning("call_structural_variants_parent_missing", object_id=bam_id)
        return

    parents = [bam.id]
    reference_id = result.get("reference_object_id")
    if reference_id:
        parents.append(PydanticObjectId(reference_id))

    job_id = result.get("job_id")
    try:
        vcf = await object_service.ingest_local_file(
            owner=bam.owner,
            project_id=bam.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.VARIANTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=sv_provenance(result),
            # Sample-level metadata describes the biology, which calling
            # does not change -- same reasoning as call_variants' copy.
            metadata=dict(bam.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("sv_vcf_ingest_failed", object_id=bam_id, error=str(e))
        return

    log.info("call_structural_variants_applied", bam_id=bam_id, vcf_id=str(vcf.id))

    # The index is attached after the VCF exists, and its failure is logged
    # rather than raised: the VCF is the deliverable, and an index can be
    # rebuilt from it at any time.
    index = result.get("index")
    if index:
        try:
            await object_service.ingest_local_file(
                owner=vcf.owner,
                project_id=bam.project_id,
                path=Path(index["tmp_path"]),
                name=index["name"],
                derived_from=[vcf.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                sidecar_of=vcf.id,
                sidecar_role=SidecarRole.TBI,
            )
        except Exception as e:  # noqa: BLE001
            log.error("sv_tbi_ingest_failed", vcf_id=str(vcf.id), error=str(e))

    snf = result.get("snf")
    if snf:
        try:
            await object_service.ingest_local_file(
                owner=vcf.owner,
                project_id=bam.project_id,
                path=Path(snf["tmp_path"]),
                name=snf["name"],
                role=ObjectRole.VARIANTS,
                derived_from=[vcf.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                sidecar_of=vcf.id,
                sidecar_role=SidecarRole.SNF,
            )
        except Exception as e:  # noqa: BLE001
            log.error("sv_snf_ingest_failed", vcf_id=str(vcf.id), error=str(e))

    # The SV index was built by the handler under a transient path inside
    # the job's own scratch workdir (the VCF had no object id yet -- ingest
    # above is what assigns one). Move it now to sv_stats_dir/<vcf_id>/sv.db,
    # matching the convention every sibling report directory follows:
    # vcf_stats_dir, bam_stats_dir, annotation_stats_dir are all keyed by the
    # object the report is *about*, not by that object's source. Best-effort,
    # like the index above: the VCF is the deliverable, and the SQLite table
    # can be rebuilt from it at any time. The scratch workdir itself (not
    # sv_stats_dir) owns cleanup of whatever this move leaves behind, via
    # ordinary job-lifecycle teardown -- no separate reclaim path is needed
    # the way there would be for a stray directory left inside a permanent
    # report root.
    sv_db_path = result.get("sv_db_path")
    if sv_db_path:
        try:
            src = Path(sv_db_path)
            dest_dir = settings.sv_stats_dir / str(vcf.id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest_dir / "sv.db"))
        except OSError as e:
            log.error("sv_db_move_failed", vcf_id=str(vcf.id), error=str(e))

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [vcf.id], owner=vcf.owner)


async def _apply_merge_structural_variants(result: dict, *, owner: str) -> None:
    """Turn a finished structural variant merging run into a joint VCF object and its index."""
    from app.services import object_service, run_service

    output = result.get("output")
    snf_ids = result.get("snf_object_ids") or []
    if not output or not snf_ids:
        return

    parents = [PydanticObjectId(sid) for sid in snf_ids]
    reference_id = result.get("reference_object_id")
    if reference_id:
        parents.append(PydanticObjectId(reference_id))

    first_snf = await DataObject.get(parents[0])
    if first_snf is None:
        log.warning("merge_structural_variants_parent_missing", object_id=str(parents[0]))
        return

    job_id = result.get("job_id")
    try:
        vcf = await object_service.ingest_local_file(
            owner=first_snf.owner,
            project_id=first_snf.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.VARIANTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=sv_provenance(result),
            metadata=dict(first_snf.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("merge_sv_vcf_ingest_failed", error=str(e))
        return

    log.info("merge_structural_variants_applied", vcf_id=str(vcf.id))

    index = result.get("index")
    if index:
        try:
            await object_service.ingest_local_file(
                owner=vcf.owner,
                project_id=first_snf.project_id,
                path=Path(index["tmp_path"]),
                name=index["name"],
                derived_from=[vcf.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                sidecar_of=vcf.id,
                sidecar_role=SidecarRole.TBI,
            )
        except Exception as e:  # noqa: BLE001
            log.error("merge_sv_tbi_ingest_failed", vcf_id=str(vcf.id), error=str(e))

    sv_db_path = result.get("sv_db_path")
    if sv_db_path:
        try:
            src = Path(sv_db_path)
            dest_dir = settings.sv_stats_dir / str(vcf.id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest_dir / "sv.db"))
        except OSError as e:
            log.error("merge_sv_db_move_failed", vcf_id=str(vcf.id), error=str(e))

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [vcf.id], owner=vcf.owner)



def counts_provenance(result: dict) -> dict:
    """The facts a quantification stamps onto the counts file it produced.

    The annotation digest is carried rather than only its name: the merge in
    `differential_expression` refuses inputs counted against different
    annotations, and it can only do that if each counts file records which one
    it used in a form that cannot be two different files with the same name.
    """
    provenance = {
        "counted_by": "featurecounts",
        "featurecounts_version": result.get("tool_version"),
        "count_params": result.get("params") or {},
        "annotation_name": result.get("annotation_name"),
        "annotation_sha256": result.get("annotation_sha256"),
    }
    provenance.update(result.get("facts") or {})
    return provenance


async def _apply_quantify(result: dict, *, owner: str) -> None:
    """Turn a finished quantification into a counts object.

    The counts descend from both the BAM and the annotation, for the same
    reason a VCF descends from both its BAM and its reference: a count is a
    claim about a gene, and which gene depends entirely on which annotation
    was used.

    Metadata is copied from the BAM, which is what carries `condition` and
    `sample` forward from the reads the user tagged -- so a project tagged
    once at upload arrives at the DE dialog with its design already filled in.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    bam_id = result.get("object_id")
    if not output or not bam_id:
        return

    bam = await DataObject.get(PydanticObjectId(bam_id))
    if bam is None:
        log.warning("quantify_parent_missing", object_id=bam_id)
        return

    parents = [bam.id]
    annotation_id = result.get("annotation_object_id")
    if annotation_id:
        parents.append(PydanticObjectId(annotation_id))

    job_id = result.get("job_id")
    try:
        counts = await object_service.ingest_local_file(
            owner=bam.owner,
            project_id=bam.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.COUNTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=counts_provenance(result),
            metadata=dict(bam.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("counts_ingest_failed", object_id=bam_id, error=str(e))
        return

    log.info("quantify_applied", bam_id=bam_id, counts_id=str(counts.id))

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [counts.id], owner=counts.owner)


def salmon_provenance(result: dict) -> dict:
    """The facts a Salmon quantification stamps onto its counts file.

    `counted_by` distinguishes it from featureCounts output on the object
    itself rather than only by which job produced it, because the two are the
    same role and the same format and a user looking at two counts files has
    no other way to tell them apart.

    `annotation_sha256` holds the transcriptome digest -- see
    `expression_handlers._salmon_result_dict` for why that key rather than a
    new one.
    """
    provenance = {
        "counted_by": "salmon",
        "salmon_version": result.get("tool_version"),
        "annotation_name": result.get("annotation_name"),
        "annotation_sha256": result.get("annotation_sha256"),
    }
    provenance.update(result.get("facts") or {})
    return provenance


async def _apply_salmon_quantify(result: dict, *, owner: str) -> None:
    """Turn a finished Salmon run into a counts object.

    The counts descend from both the reads and the transcriptome, for the same
    reason featureCounts output descends from its BAM and its annotation: a
    count is a claim about a gene, and which gene depends entirely on which
    reference was used.

    Metadata is copied from the reads object, which is where `condition` and
    `sample` live. `_apply_quantify` takes them from the BAM instead only
    because a BAM is what it has; this path has no BAM and the reads are the
    parent, so it reads them from the source directly.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    reads_id = result.get("object_id")
    if not output or not reads_id:
        return

    reads = await DataObject.get(PydanticObjectId(reads_id))
    if reads is None:
        log.warning("salmon_quantify_parent_missing", object_id=reads_id)
        return

    parents = [reads.id]
    transcriptome_id = result.get("transcriptome_object_id")
    if transcriptome_id:
        parents.append(PydanticObjectId(transcriptome_id))

    job_id = result.get("job_id")
    try:
        counts = await object_service.ingest_local_file(
            owner=reads.owner,
            project_id=reads.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.COUNTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=salmon_provenance(result),
            metadata=dict(reads.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("salmon_counts_ingest_failed", object_id=reads_id, error=str(e))
        return

    log.info(
        "salmon_quantify_applied", reads_id=reads_id, counts_id=str(counts.id)
    )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [counts.id], owner=counts.owner)


async def _apply_transcript_assembly(result: dict, *, owner: str) -> None:
    """Register a StringTie assembly as a derived object.

    Role is ASSEMBLED_TRANSCRIPTS rather than ANNOTATION, unlike the two
    other appliers in this file that produce GFF/GTF. Those both derive from
    an authoritative annotation and stay authoritative; this one proposes
    transcript models from one sample's alignment, and conflating the two
    would let a hypothesis become the reference for the next run.
    """
    from app.services import object_service, run_service

    object_id = result.get("object_id")
    output = result.get("output")
    if not output or not object_id:
        return

    source = await DataObject.get(PydanticObjectId(object_id))
    if source is None:
        log.warning("transcript_assembly_parent_missing", object_id=object_id)
        return

    job_id = result.get("job_id")

    try:
        assembled = await object_service.ingest_local_file(
            owner=source.owner,
            project_id=source.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ASSEMBLED_TRANSCRIPTS,
            derived_from=[source.id],
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts={
                "assembled_by": result.get("assembled_by"),
                "transcript_count": result.get("transcript_count"),
                "novel_transcript_count": result.get("novel_transcript_count"),
                "gene_count": result.get("gene_count"),
            },
            # The assembly describes the same sample as its alignment.
            metadata=dict(source.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "transcript_assembly_ingest_failed",
            object_id=object_id,
            error=str(e),
        )
        return

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [assembled.id], owner=assembled.owner)

    log.info(
        "transcript_assembly_applied",
        object_id=object_id,
        assembled_id=str(assembled.id),
    )


async def _apply_differential_expression(result: dict, *, owner: str) -> None:
    """Turn a finished DE run into a results object.

    Unlike every other applier here, the parent is a list of N objects rather
    than one or two, and there is no single "the input" to copy metadata from
    -- the samples deliberately disagree about `condition`, which is the whole
    point of the run. So metadata is left empty and the design is recorded in
    facts instead, where it describes the run rather than claiming to describe
    the file.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    project_id = result.get("project_id")
    if not output or not project_id:
        return

    counts_ids = [
        PydanticObjectId(cid) for cid in (result.get("counts_object_ids") or [])
    ]

    job_id = result.get("job_id")
    facts = {
        "tested_by": "pydeseq2",
        "pydeseq2_version": result.get("tool_version"),
        "de_params": result.get("params") or {},
        **(result.get("facts") or {}),
    }

    try:
        de = await object_service.ingest_local_file(
            owner=owner,
            project_id=PydanticObjectId(project_id),
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.DE_RESULTS,
            derived_from=counts_ids,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=facts,
        )
    except Exception as e:  # noqa: BLE001
        log.error("de_results_ingest_failed", project_id=str(project_id), error=str(e))
        return

    log.info(
        "differential_expression_applied",
        de_id=str(de.id),
        samples=len(counts_ids),
    )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [de.id], owner=de.owner)

    from app.services import pipeline_service

    try:
        # Additive extra, same reasoning as QC's chained launch_summary call:
        # a failure to queue the DE summary must not undo the DE results
        # ingest that just succeeded.
        await pipeline_service.launch_de_summary(object_id=de.id, owner=owner)
    except Exception as e:  # noqa: BLE001 - an additive extra cannot fail DE
        log.warning("de_summary_launch_failed", object_id=str(de.id), error=str(e))


async def _apply_merge_transcripts(result: dict, *, owner: str) -> None:
    """Turn a finished transcript merge into a merged annotation object.

    Role is ANNOTATION, not ASSEMBLED_TRANSCRIPTS (S3 in the design doc): the
    merge output is a thing you quantify *against*, no longer a per-sample
    hypothesis. Giving it the ANNOTATION role is what stops it being offered
    back into another merge as though it were one more per-sample assembly.
    `derived_from` is every input GTF, plus the reference annotation when `-G`
    was used -- the merged annotation is a function of all of them.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    project_id = result.get("project_id")
    if not output or not project_id:
        return

    parent_ids = [PydanticObjectId(i) for i in (result.get("object_ids") or [])]
    reference_id = result.get("reference_object_id")
    if reference_id:
        parent_ids.append(PydanticObjectId(reference_id))

    job_id = result.get("job_id")
    facts = {
        "merged_by": result.get("merged_by"),
        # The number of inputs, so a later reader can tell a 2-sample merge
        # from a 12-sample one without counting provenance edges.
        "input_count": len(parent_ids),
        "transcript_count": result.get("transcript_count"),
        "novel_transcript_count": result.get("novel_transcript_count"),
        "gene_count": result.get("gene_count"),
    }

    try:
        merged = await object_service.ingest_local_file(
            owner=owner,
            project_id=PydanticObjectId(project_id),
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ANNOTATION,
            derived_from=parent_ids,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=facts,
        )
    except Exception as e:  # noqa: BLE001
        log.error("merge_transcripts_ingest_failed", project_id=str(project_id), error=str(e))
        return

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [merged.id], owner=merged.owner)

    log.info(
        "merge_transcripts_applied",
        merged_id=str(merged.id),
        inputs=len(parent_ids),
    )


def annotation_provenance(result: dict) -> dict:
    """The facts an annotation run stamps onto the VCF it produced."""
    return {
        "variants_annotated_by": result.get("tool"),
        "variant_annotation_tool_version": result.get("tool_version"),
    }


def phase_provenance(result: dict) -> dict:
    """The facts a phasing run stamps onto the VCF it produced.

    Like annotation, the input VCF survives as the caller's output and phasing
    is a derivation of it -- so the mode and tool version are the whole of what
    distinguishes one phased copy from another.
    """
    return {
        "variants_phased_by": "whatshap",
        "variant_phase_tool_version": result.get("tool_version"),
        "variant_phase_mode": result.get("mode"),
    }


async def _apply_phase_variants(result: dict, *, owner: str) -> None:
    """Turn a finished phasing run into a new VCF object and its index.

    Mirrors `_apply_annotate_variants`: the phased VCF descends from the source
    VCF, the reference it was phased against, and every alignment that informed
    it, since the phase sets mean nothing without knowing which reads supported
    them. The `.tbi` is a sidecar of the new VCF, exactly as the other variant
    appliers attach one.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    vcf_id = result.get("object_id")
    if not output or not vcf_id:
        return

    vcf = await DataObject.get(PydanticObjectId(vcf_id))
    if vcf is None:
        log.warning("phase_variants_parent_missing", object_id=vcf_id)
        return

    parents = [vcf.id]
    for key in ("reference_object_id", "alignment_object_ids"):
        if key == "alignment_object_ids":
            for parent_id in result.get(key) or []:
                parents.append(PydanticObjectId(parent_id))
        else:
            parent_id = result.get(key)
            if parent_id:
                parents.append(PydanticObjectId(parent_id))

    job_id = result.get("job_id")
    try:
        phased = await object_service.ingest_local_file(
            owner=vcf.owner,
            project_id=vcf.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.VARIANTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=phase_provenance(result),
            # Sample-level metadata describes the biology, which phasing
            # does not change -- same reasoning as call_variants' copy.
            metadata=dict(vcf.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("phase_variants_ingest_failed", object_id=vcf_id, error=str(e))
        return

    log.info("phase_variants_applied", vcf_id=vcf_id, phased_id=str(phased.id))

    index = result.get("index")
    if index:
        try:
            await object_service.ingest_local_file(
                owner=phased.owner,
                project_id=vcf.project_id,
                path=Path(index["tmp_path"]),
                name=index["name"],
                derived_from=[phased.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                sidecar_of=phased.id,
                sidecar_role=SidecarRole.TBI,
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "phase_variants_tbi_ingest_failed",
                vcf_id=str(phased.id),
                error=str(e),
            )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [phased.id], owner=phased.owner)


def haplotag_provenance(result: dict) -> dict:
    """The facts a haplotag run stamps onto the BAM it produced."""
    facts = {
        "haplotagged_by": "whatshap",
        "haplotag_tool_version": result.get("tool_version"),
    }
    sample = result.get("sample")
    if sample:
        facts["haplotag_sample"] = sample
    if result.get("ignore_read_groups"):
        facts["haplotag_ignore_read_groups"] = True
    return facts


async def _apply_haplotag(result: dict, *, owner: str) -> None:
    """Turn a finished haplotag run into a BAM object, and chain its indexing.

    The haplotagged BAM descends from the phased VCF it read phase sets from,
    the source alignment it tagged, and the reference -- all three are
    biologically meaningful, so `derived_from` rather than a sidecar. Sample
    metadata follows the source BAM, as in `_apply_align_reads`.
    """
    from app.queue import queue
    from app.services import object_service

    output = result.get("output")
    object_id = result.get("object_id")
    if not output or not object_id:
        return

    vcf = await DataObject.get(PydanticObjectId(object_id))
    if vcf is None:
        log.warning("haplotag_parent_missing", object_id=object_id)
        return

    parents = [vcf.id]
    for key in ("alignment_object_ids", "reference_object_id"):
        value = result.get(key)
        if isinstance(value, list):
            parents.extend(PydanticObjectId(v) for v in value)
        elif value:
            parents.append(PydanticObjectId(value))

    source_bam = None
    alignment_ids = result.get("alignment_object_ids") or []
    if alignment_ids:
        source_bam = await DataObject.get(PydanticObjectId(alignment_ids[0]))

    job_id = result.get("job_id")
    provenance = haplotag_provenance(result=result)

    try:
        bam = await object_service.ingest_local_file(
            owner=vcf.owner,
            project_id=vcf.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.ALIGNMENT,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=dict(provenance),
            # The source BAM carries the sample-level metadata the haplotagged
            # copy should keep, so it stays findable by the same sample.
            metadata=dict(source_bam.metadata) if source_bam else None,
        )
    except Exception as e:  # noqa: BLE001
        log.error("haplotag_output_ingest_failed", object_id=object_id, error=str(e))
        return

    log.info("haplotag_applied", object_id=object_id, bam_id=str(bam.id))

    from app.services import run_service

    run_id = await run_service.run_for_job(PydanticObjectId(job_id)) if job_id else None
    if run_id is not None:
        await run_service.record_outputs(run_id, [bam.id], owner=bam.owner)

    # Chain the follow-on index. Enqueued here rather than at launch because it
    # needs the BAM's digest, which does not exist until the haplotag has run.
    if bam.blob_sha256:
        index_job = await queue.enqueue(
            "index_bam",
            owner=bam.owner,
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
        # Joins the haplotag's run: it was caused by this run and finishes the
        # work the user asked for, even though it could not be enqueued until
        # the BAM existed.
        if index_job is not None and run_id is not None:
            await run_service.link_job(run_id, index_job.id, RunJobRole.INDEX_BAM)


async def _apply_annotate_variants(result: dict, *, owner: str) -> None:
    """Turn a finished annotation run into a new VCF object and its index.

    Mirrors `_apply_call_variants`: the annotated VCF descends from the
    source VCF, the reference and the annotation (GFF3) it was called
    against, since the annotations mean nothing without knowing which genes
    they were read from. The `.tbi` is a sidecar of the new VCF, exactly as
    `_apply_call_variants` attaches one to its own output.
    """
    from app.services import object_service, run_service

    output = result.get("output")
    vcf_id = result.get("object_id")
    if not output or not vcf_id:
        return

    vcf = await DataObject.get(PydanticObjectId(vcf_id))
    if vcf is None:
        log.warning("annotate_variants_parent_missing", object_id=vcf_id)
        return

    parents = [vcf.id]
    for key in ("reference_object_id", "annotation_object_id"):
        parent_id = result.get(key)
        if parent_id:
            parents.append(PydanticObjectId(parent_id))

    job_id = result.get("job_id")
    try:
        annotated = await object_service.ingest_local_file(
            owner=vcf.owner,
            project_id=vcf.project_id,
            path=Path(output["tmp_path"]),
            name=output["name"],
            role=ObjectRole.VARIANTS,
            derived_from=parents,
            produced_by_job=PydanticObjectId(job_id) if job_id else None,
            facts=annotation_provenance(result),
            # Sample-level metadata describes the biology, which annotating
            # does not change -- same reasoning as call_variants' copy.
            metadata=dict(vcf.metadata),
        )
    except Exception as e:  # noqa: BLE001
        log.error("annotate_variants_ingest_failed", object_id=vcf_id, error=str(e))
        return

    log.info("annotate_variants_applied", vcf_id=vcf_id, annotated_id=str(annotated.id))

    # The index is attached after the VCF exists, and its failure is logged
    # rather than raised: the VCF is the deliverable, and an index can be
    # rebuilt from it at any time.
    index = result.get("index")
    if index:
        try:
            await object_service.ingest_local_file(
                owner=annotated.owner,
                project_id=vcf.project_id,
                path=Path(index["tmp_path"]),
                name=index["name"],
                derived_from=[annotated.id],
                produced_by_job=PydanticObjectId(job_id) if job_id else None,
                sidecar_of=annotated.id,
                sidecar_role=SidecarRole.TBI,
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "annotate_variants_tbi_ingest_failed",
                vcf_id=str(annotated.id),
                error=str(e),
            )

    if job_id:
        run_id = await run_service.run_for_job(PydanticObjectId(job_id))
        if run_id is not None:
            await run_service.record_outputs(run_id, [annotated.id], owner=annotated.owner)


# Derived from the enum rather than hand-listed, which is what this used to
# be. A handler names a sidecar role as a string and this is what turns it
# back into a `SidecarRole`; a role missing from the mapping is skipped with a
# warning, so the index files were silently dropped while `build_index` still
# reported success -- a green job, a reference with no index, and an alignment
# that then failed with STAR complaining its genome directory did not exist.
#
# STAR was the first role added after this dict was written, and it went
# wrong immediately. Deriving it means the next one cannot: every enum member
# is accepted by construction, and the string keys here were always exactly
# the enum values anyway.
_SIDECAR_ROLES = {role.value: role for role in SidecarRole}


async def _apply_gc_bias(result: dict, *, owner: str) -> None:
    """Record a GC-vs-coverage bias curve on the BAM it was computed for.

    Read-only like _apply_coverage: no files to ingest, just facts merged
    onto the object.
    """
    object_id = result.get("object_id")
    facts = result.get("facts") or {}
    if not object_id or not facts:
        return

    obj = await DataObject.get(PydanticObjectId(object_id))
    if obj is None:
        log.warning("gc_bias_object_missing", object_id=object_id)
        return

    merged = {**obj.facts, **facts}
    await obj.set(
        {
            DataObject.facts: merged,
            DataObject.updated_at: datetime.now(UTC),
        }
    )


_APPLIERS = {
    "ingest_headers": _apply_ingest_headers,
    "filter_long_reads": _apply_filter_long_reads,
    "trim_reads": _apply_trim_reads,
    "run_qc": _apply_run_qc,
    "summarize_object": _apply_summarize_object,
    "summarize_de_results": _apply_summarize_de_results,
    "summarize_variant_results": _apply_summarize_variant_results,
    "download_sra_run": _apply_sra_download,
    "fetch_remote": _apply_fetch_remote,
    "download_assembly": _apply_assembly_download,
    "download_uniprot": _apply_uniprot_download,
    "build_index": _apply_build_index,
    "align_reads_chunked": _apply_align_reads_chunked,
    "merge_chunked_buckets": _apply_chunked_alignment,
    "align_reads": _apply_align_reads,
    "index_bam": _apply_index_bam,
    "call_variants": _apply_call_variants,
    "call_structural_variants": _apply_call_structural_variants,
    "merge_structural_variants": _apply_merge_structural_variants,
    "run_bam_stats": _apply_run_bam_stats,
    "feature_coverage": _apply_feature_coverage,
    "variants_in_regions": _apply_variants_in_regions,
    "annotation_comparison": _apply_annotation_comparison,
    "sequence_extraction": _apply_sequence_extraction,
    "coverage": _apply_coverage,
    "methylation": _apply_methylation,
    "gc_bias": _apply_gc_bias,
    "run_transcript_qc": _apply_run_transcript_qc,
    "run_vcf_stats": _apply_run_vcf_stats,
    "run_annotation_stats": _apply_run_annotation_stats,
    "export_annotation_subset": _apply_export_annotation_subset,
    "materialize_annotation_edits": _apply_materialize_annotation_edits,
    "extract_genbank_sequence": _apply_extract_genbank_sequence,
    "annotate_variants": _apply_annotate_variants,
    "phase_variants": _apply_phase_variants,
    "haplotag": _apply_haplotag,
    "quantify": _apply_quantify,
    "salmon_quantify": _apply_salmon_quantify,
    "transcript_assembly": _apply_transcript_assembly,
    "merge_transcripts": _apply_merge_transcripts,
    "differential_expression": _apply_differential_expression,
    "assemble_reads": _apply_assemble_reads,
    "assess_completeness": _apply_assess_completeness,
    "assess_misassemblies": _apply_assess_misassemblies,
    "analyze_gc_tracks": _apply_analyze_gc_tracks,
    "analyze_meryl_tracks": _apply_analyze_meryl_tracks,
    "analyze_synteny": _apply_analyze_synteny,
    "annotate_genome": _apply_annotate_genome,
    "classify_reads": _apply_classify_reads,
    "assess_assembly_errors": _apply_assess_assembly_errors,
    "assess_assembly_qv": _apply_assess_assembly_qv,
    "assess_assembly_continuity": _apply_assess_assembly_continuity,
    "consensus_from_alignment": _apply_consensus_from_alignment,
    "polish_assembly": _apply_polish_assembly,
    "polish_long_assembly": _apply_polish_assembly,
    "scaffold_assembly": _apply_scaffold_assembly,
    "transfer_annotation": _apply_transfer_annotation,
}
