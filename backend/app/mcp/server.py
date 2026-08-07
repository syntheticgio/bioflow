"""Builds the MCP server and its mountable ASGI app.

The installed `mcp` package (2.0.0) does not match the API an earlier draft
of this plan assumed. There is no `mcp.server.fastmcp.FastMCP` and no
`mcp.get_context()`. The real surface, verified by running live probes
against the actual installed package rather than trusted from memory:

- The class is `mcp.server.mcpserver.MCPServer`, constructed the same way
  (`MCPServer("bioflow")`).
- `.tool()` and `.resource()` are decorators with the shape you'd expect.
- Context is *parameter-injected*, not fetched via a module-level
  `get_context()`. A tool or resource function that declares a parameter
  annotated `Context` (name doesn't matter, only the annotation) receives one
  automatically; a function that doesn't need it just omits the parameter.
- `Context.request_context.request` is the real inbound Starlette `Request`
  for an HTTP-transport call, which is how a tool wrapper reaches the
  `?profile=` query string -- this is the direct equivalent of what the
  original plan wanted from `mcp.get_context().request_context.request`.

Every wrapper below takes `ctx: Context` because every one of the 16
functions in `app.mcp.tools` takes `owner` as a required keyword argument
(see that module's own docstring on why): there is no tool here that can
skip resolving a profile.
"""

from contextlib import asynccontextmanager, AsyncExitStack
from typing import AsyncIterator, Callable

from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.mcp import context, resources, tools

MOUNT_PATH = "/api/v1/mcp"


async def _owner(ctx: Context) -> str:
    """Resolve the calling profile from the live request's `?profile=`.

    `ctx.request_context.request` is typed `Request | None` because a stdio
    transport has no HTTP request to carry it. This server is only ever
    mounted via `streamable_http_app()` (see `build_mcp_app` below) -- no
    stdio transport is wired up anywhere in this plan -- so `request` being
    `None` here would mean the mcp library changed how the HTTP transport
    populates request context, not a case this codebase's own deployment can
    reach. Letting that raise naturally (`AttributeError` on `None`) rather
    than adding a defensive branch means it fails loudly if that assumption
    ever stops holding, instead of silently resolving profile=None the way a
    swallowed-None branch would.
    """
    request = ctx.request_context.request
    profile_param = request.query_params.get("profile")
    return await context.owner_for(profile_param)


def _register_tools(srv: MCPServer) -> None:
    """Wire all 16 tools in app.mcp.tools to MCPServer, each resolving its
    own `owner` from the live request before calling straight through.

    Kept as one flat block of thin wrappers rather than a generated loop:
    each tool's positional parameters differ (see tools.py), so a wrapper
    that just forwards `**kwargs` would swallow a mismatched-argument bug
    that a caller would otherwise see immediately as a clear TypeError.
    """

    @srv.tool(name="bioflow_whoami")
    async def bioflow_whoami(ctx: Context) -> dict:
        return await tools.whoami(owner=await _owner(ctx))

    @srv.tool(name="bioflow_list_projects")
    async def bioflow_list_projects(ctx: Context, parent_id: str | None = None) -> dict:
        return await tools.list_projects(owner=await _owner(ctx), parent_id=parent_id)

    @srv.tool(name="bioflow_get_project")
    async def bioflow_get_project(project_id: str, ctx: Context) -> dict:
        return await tools.get_project(project_id, owner=await _owner(ctx))

    @srv.tool(name="bioflow_create_project")
    async def bioflow_create_project(
        name: str,
        ctx: Context,
        description: str = "",
        parent_id: str | None = None,
    ) -> dict:
        return await tools.create_project(
            name,
            owner=await _owner(ctx),
            description=description,
            parent_id=parent_id,
        )

    @srv.tool(name="bioflow_list_objects")
    async def bioflow_list_objects(project_id: str, ctx: Context) -> dict:
        return await tools.list_objects(project_id, owner=await _owner(ctx))

    @srv.tool(name="bioflow_get_object")
    async def bioflow_get_object(object_id: str, ctx: Context) -> dict:
        return await tools.get_object(object_id, owner=await _owner(ctx))

    @srv.tool(name="bioflow_suggest_next")
    async def bioflow_suggest_next(object_id: str, ctx: Context) -> dict:
        return await tools.suggest_next(object_id, owner=await _owner(ctx))

    @srv.tool(name="bioflow_run_pipeline")
    async def bioflow_run_pipeline(kind: str, params: dict, ctx: Context) -> dict:
        return await tools.run_pipeline(kind, params, owner=await _owner(ctx))

    @srv.tool(name="bioflow_get_job")
    async def bioflow_get_job(job_id: str, ctx: Context) -> dict:
        return await tools.get_job(job_id, owner=await _owner(ctx))

    @srv.tool(name="bioflow_list_jobs")
    async def bioflow_list_jobs(ctx: Context, limit: int = 50) -> dict:
        return await tools.list_jobs(owner=await _owner(ctx), limit=limit)

    @srv.tool(name="bioflow_cancel_job")
    async def bioflow_cancel_job(job_id: str, ctx: Context) -> dict:
        return await tools.cancel_job(job_id, owner=await _owner(ctx))

    @srv.tool(name="bioflow_search_objects")
    async def bioflow_search_objects(query: str, ctx: Context, limit: int = 50) -> dict:
        return await tools.search_objects(query, owner=await _owner(ctx), limit=limit)

    @srv.tool(name="bioflow_search_ncbi")
    async def bioflow_search_ncbi(term: str, ctx: Context) -> dict:
        return await tools.search_ncbi(term, owner=await _owner(ctx))

    @srv.tool(name="bioflow_download_reference")
    async def bioflow_download_reference(accession: str, project_id: str, ctx: Context) -> dict:
        return await tools.download_reference(accession, project_id, owner=await _owner(ctx))

    @srv.tool(name="bioflow_list_tools")
    async def bioflow_list_tools(ctx: Context) -> dict:
        return await tools.list_tools(owner=await _owner(ctx))

    @srv.tool(name="bioflow_get_guide")
    async def bioflow_get_guide(topic: str, ctx: Context) -> dict:
        return await tools.get_guide(topic, owner=await _owner(ctx))


def _register_resources(srv: MCPServer) -> None:
    """The derived, always-fresh resources from app.mcp.resources.

    No `ctx`/owner here -- unlike the tools, none of these read anything
    owner-scoped. `resources.py`'s own docstring is the source of truth for
    why each is safe to generate rather than hand-write.
    """

    @srv.resource("bioflow://jobs/types", mime_type="application/json")
    def jobs_types() -> dict:
        return resources.job_types()

    @srv.resource("bioflow://tools/installed", mime_type="application/json")
    def tools_installed() -> dict:
        return resources.installed_tools()

    @srv.resource("bioflow://sources", mime_type="application/json")
    def sources() -> dict:
        return resources.data_sources()

    # One resource per guide topic. Registered in a loop (unlike the tools
    # above) because every guide resource has the exact same shape --
    # read_guide(topic) -- with nothing per-topic to get subtly wrong the
    # way the tools' differing argument lists would.
    def _make_guide_reader(topic: resources.GuideTopic) -> Callable[[], str]:
        def _read() -> str:
            return resources.load_guide(topic)

        return _read

    for topic in resources.GuideTopic:
        srv.resource(f"bioflow://guides/{topic.value}", mime_type="text/markdown")(
            _make_guide_reader(topic)
        )


def build_mcp_app() -> Starlette:
    """The MCP server as a mountable Starlette ASGI app.

    Callers must chain this app's lifespan into their own -- see
    `mount_mcp_app` below, which does this for `app.main`'s FastAPI app. A
    plain `app.mount(...)` is not enough: Starlette does not propagate a
    mounted sub-app's lifespan into the parent's automatically, and without
    it the streamable-HTTP session manager never starts, so every request
    fails with "Task group is not initialized" (confirmed by direct
    reproduction against this exact version).

    transport_security: DNS-rebinding protection is disabled outright rather
    than narrowed to an allowlist. BioFlow is a single-user, local-only tool
    (CLAUDE.md), the REST API it sits beside is already unauthenticated, and
    the docker-compose port binding only ever exposes this on
    `${BIND_ADDRESS:-127.0.0.1}`. An allowlist would need to name every
    origin/host the settings panel might hand out (5173 and 8000 today, per
    the connection-panel design, but also whatever host:port a user's own
    reverse proxy or remote-access setup uses) and silently 421 anything
    that isn't on it -- a worse failure mode for a local single-user tool
    than the DNS-rebinding risk this protection exists for, which assumes a
    browser-reachable target worth protecting from a hostile page. If this
    server is ever exposed beyond localhost, this is the line to revisit.
    """
    srv = MCPServer("bioflow")
    _register_tools(srv)
    _register_resources(srv)

    return srv.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    )


def mount_mcp_app(app: FastAPI) -> None:
    """Mount the MCP server on `app` at MOUNT_PATH, chaining its lifespan.

    `/api/v1/mcp` rather than a bare `/mcp`: `vite.config.ts` proxies `/api`,
    so this versioned path is reachable with no new proxy rule from either
    the dev-server origin (5173) or the API's own origin (8000). See
    tests/mcp/test_mount.py for the regression test.

    `build_mcp_app` passes `streamable_http_path="/"` so the sub-app serves
    its endpoint at its own root -- otherwise `MCPServer.streamable_http_app`
    defaults that path to `/mcp` too, and the externally-reachable URL ends
    up as `/api/v1/mcp/mcp` instead of `/api/v1/mcp`. Starlette's `Mount`
    still 307-redirects a bare `/api/v1/mcp` (no trailing slash) request to
    `/api/v1/mcp/`, since the sub-app's only route is `/` -- callers should
    request the slashed form directly (see `SettingsMcp.tsx`) rather than
    rely on redirect-following.
    """
    mcp_app = build_mcp_app()
    app.mount(MOUNT_PATH, mcp_app)

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(existing_lifespan(app))
            await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
            yield

    app.router.lifespan_context = combined_lifespan
