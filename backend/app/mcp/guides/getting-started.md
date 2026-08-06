# Getting started with BioFlow

BioFlow manages bioinformatics data and runs pipelines over it. This guide
covers the shape of the system; the other guides cover specific workflows.

## Profiles

Every piece of data belongs to a **profile**. The MCP connection is already
acting as one -- call `bioflow_whoami` to see which. You cannot switch
profiles from here; that is chosen by the human when they configure this
server.

## Projects

A **project** holds data objects and can nest inside another project. Create
one with `bioflow_create_project` before adding data. List them with
`bioflow_list_projects`.

## Objects

An **object** is a file BioFlow knows something about -- reads, a reference
genome, an alignment, a variant call set. Its `format` and `role` are detected
on ingest and drive what can be run against it.

Find them with `bioflow_list_objects` (by project) or `bioflow_search_objects`
(across the library).

## The most useful call

`bioflow_suggest_next(object_id)` asks BioFlow itself what can be run against
an object right now. It returns each candidate with a status -- `available`,
`unavailable`, or `needs_install` -- a ready-made launch payload, and the
honest reason anything unavailable cannot run.

Prefer it over reasoning from these guides. It is computed from the actual
object, so it accounts for what is installed on this machine, whether a
reference has an index, and what has already been run.

## Running something

`bioflow_run_pipeline(kind, params)` starts a job. The `kind` values are the
registered job types -- read them from the `bioflow://jobs/types` resource, or
take the payload straight from `bioflow_suggest_next`.

Jobs are asynchronous. `bioflow_run_pipeline` returns immediately with a job
id; poll `bioflow_get_job` for progress. Long pipelines can run for hours.
`bioflow_cancel_job` stops one.

## What this server will not do

There are no delete tools. Removing a project or an object is done by the
human in the BioFlow UI.
