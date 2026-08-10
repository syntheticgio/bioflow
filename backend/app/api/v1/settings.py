"""Configuration the user edits: AI providers and task routing.

Deliberately **not owner-scoped**, matching the precedent in `pipelines.py`'s
`/summary/status`: there is one machine and one set of providers here, so a
profile header cannot change the answer, and gating these behind one would hide
the settings page from a client that has not resolved a profile yet.

**No response from this module ever contains an API key.** Keys go in via
`api_key` on create and update; they come back only as `key_hint` and
`has_key`. `tests/api/test_settings_ai.py::TestKeysNeverLeak` asserts it across
every shape here.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.models.ai import AiRouting, ProviderKind, TaskSlot
from app.models.app_settings import AppSettings
from app.services import resource_limit_service
from app.services.ai import presets as presets_mod
from app.services.ai import provider_service
from app.services.ai.adapters import Failure

router = APIRouter(prefix="/settings", tags=["settings"])


class PresetOut(BaseModel):
    id: str
    label: str
    kind: ProviderKind
    base_url: str
    needs_key: bool


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str
    model: str
    key_hint: str | None
    has_key: bool
    models_cache: list[str]
    status: str
    status_reason: str | None
    checked_at: str | None
    # Human labels of the slots routed here, so the detail pane can say what
    # depends on this provider without a second request.
    used_by: list[str]


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1)
    kind: ProviderKind
    base_url: str = Field(min_length=1)
    model: str = ""
    api_key: str | None = None


class ProviderUpdate(BaseModel):
    """Every field optional, and `api_key` has three-way semantics.

    Absent preserves the stored key, explicit null clears it, a string replaces
    it. `model_fields_set` is what distinguishes absent from null -- which is
    why this cannot be a plain dict with defaults.
    """

    name: str | None = None
    kind: ProviderKind | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


class SlotOut(BaseModel):
    name: str
    label: str


class RoutingOut(BaseModel):
    default: str | None
    slots: dict[str, str]
    catalog: list[SlotOut]


class RoutingIn(BaseModel):
    default: str | None = None
    slots: dict[str, str] = Field(default_factory=dict)


class FetchModelsOut(BaseModel):
    status: str
    models: list[str]
    reason: str | None = None
    detail: str | None = None


class GeneralSettingsOut(BaseModel):
    feedback_enabled: bool


class GeneralSettingsIn(BaseModel):
    feedback_enabled: bool


class ResourceLimitsOut(BaseModel):
    """The stored budget, plus what the machine actually has.

    The machine numbers are reported alongside so the UI can render a range
    and say what "no limit" resolves to right now, without a second request.
    """

    max_mem_mb: int | None
    max_cpu: float | None
    max_threads: int | None
    machine_mem_mb: int
    machine_cpu: float
    # The kernel-enforced ceiling, when hard limits are on. None means the
    # soft budget is the only ceiling and nothing is ever killed.
    hard_mem_mb: int | None


class ResourceLimitsIn(BaseModel):
    """Every field is written on every save, including None.

    Absent means "no limit", not "leave unchanged": the UI's "No limit" option
    has to be able to clear a ceiling that was set earlier. Deliberately
    simpler than ProviderUpdate's three-way api_key semantics -- there is no
    secret here to accidentally erase.
    """

    max_mem_mb: int | None = Field(default=None, gt=0)
    max_cpu: float | None = Field(default=None, gt=0)
    max_threads: int | None = Field(default=None, gt=0)


async def _used_by_map() -> dict[str, list[str]]:
    """provider id -> labels of the slots routed to it."""
    routing = await AiRouting.load()
    out: dict[str, list[str]] = {}
    for slot_name, provider_id in routing.slots.items():
        try:
            label = TaskSlot(slot_name).label
        except ValueError:
            continue  # a slot removed from the enum; ignore rather than 500
        out.setdefault(provider_id, []).append(label)
    if routing.default:
        out.setdefault(routing.default, []).append("Default")
    return out


def _to_out(provider, used_by: list[str]) -> ProviderOut:
    return ProviderOut(
        id=str(provider.id),
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        model=provider.model,
        key_hint=provider.key_hint,
        has_key=provider.api_key_enc is not None,
        models_cache=provider.models_cache,
        status=provider.status,
        status_reason=provider.status_reason,
        checked_at=provider.checked_at.isoformat() if provider.checked_at else None,
        used_by=used_by,
    )


@router.get("/ai/presets", response_model=list[PresetOut])
async def list_presets() -> list[PresetOut]:
    return [
        PresetOut(
            id=p.id, label=p.label, kind=p.kind, base_url=p.base_url, needs_key=p.needs_key
        )
        for p in presets_mod.ALL
    ]


@router.get("/ai/providers", response_model=list[ProviderOut])
async def list_providers() -> list[ProviderOut]:
    used = await _used_by_map()
    return [_to_out(p, used.get(str(p.id), [])) for p in await provider_service.list_all()]


@router.post(
    "/ai/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED
)
async def create_provider(body: ProviderCreate) -> ProviderOut:
    from pymongo.errors import DuplicateKeyError

    try:
        provider = await provider_service.create(
            name=body.name,
            kind=body.kind,
            base_url=body.base_url,
            model=body.model,
            api_key=body.api_key,
        )
    except DuplicateKeyError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A provider named {body.name!r} already exists"
        ) from None
    return _to_out(provider, [])


@router.patch("/ai/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: str, body: ProviderUpdate) -> ProviderOut:
    # exclude_unset is what preserves an omitted key: without it every field
    # arrives as None and the key is wiped on any edit.
    changes = body.model_dump(exclude_unset=True)
    provider = await provider_service.update(provider_id, changes)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such provider")
    used = await _used_by_map()
    return _to_out(provider, used.get(str(provider.id), []))


@router.delete("/ai/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: str) -> None:
    if not await provider_service.delete(provider_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such provider")


@router.post("/ai/providers/{provider_id}/fetch-models", response_model=FetchModelsOut)
async def fetch_models(provider_id: str) -> FetchModelsOut:
    """Fetch the model list, which doubles as the connection test.

    A provider failure is a 200 with `status: failed`, not a 502: the request
    itself succeeded, and the UI renders the outcome as a badge rather than an
    error toast.
    """
    result = await provider_service.fetch_models(provider_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such provider")
    if isinstance(result, Failure):
        provider = await provider_service.get(provider_id)
        return FetchModelsOut(
            status="failed",
            models=provider.models_cache if provider else [],
            reason=result.reason,
            detail=result.detail,
        )
    return FetchModelsOut(status="ok", models=result)


@router.get("/ai/routing", response_model=RoutingOut)
async def get_routing() -> RoutingOut:
    routing = await AiRouting.load()
    return RoutingOut(
        default=routing.default,
        slots=routing.slots,
        catalog=[SlotOut(name=s.value, label=s.label) for s in TaskSlot],
    )


@router.put("/ai/routing", response_model=RoutingOut)
async def set_routing(body: RoutingIn) -> RoutingOut:
    valid_slots = {s.value for s in TaskSlot}
    unknown = set(body.slots) - valid_slots
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown task slots: {sorted(unknown)}"
        )

    # Every referenced provider must exist. Writing a dangling id would give a
    # silently non-functional route that resolve() reports only in the log.
    for provider_id in {*body.slots.values(), *( [body.default] if body.default else [] )}:
        if await provider_service.get(provider_id) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"No such provider: {provider_id}"
            )

    routing = await AiRouting.load()
    routing.default = body.default
    routing.slots = dict(body.slots)
    await routing.save()
    return RoutingOut(
        default=routing.default,
        slots=routing.slots,
        catalog=[SlotOut(name=s.value, label=s.label) for s in TaskSlot],
    )


@router.get("/general", response_model=GeneralSettingsOut)
async def get_general_settings() -> GeneralSettingsOut:
    settings = await AppSettings.load()
    return GeneralSettingsOut(feedback_enabled=settings.feedback_enabled)


@router.put("/general", response_model=GeneralSettingsOut)
async def set_general_settings(body: GeneralSettingsIn) -> GeneralSettingsOut:
    settings = await AppSettings.load()
    settings.feedback_enabled = body.feedback_enabled
    await settings.save()
    return GeneralSettingsOut(feedback_enabled=settings.feedback_enabled)


def _machine_budget() -> tuple[int, float]:
    """What this host actually has, via the governor's cgroup-aware readers.

    Uses the governor rather than psutil directly: inside Docker the cgroup
    limit is the number that binds, and psutil reports the Linux VM's
    resources rather than the container's.
    """
    from app.queue.governor import LoadGovernor

    governor = LoadGovernor()
    return int(governor.mem_budget_bytes() / (1024 * 1024)), governor.cpu_budget()


def _limits_out(limits) -> ResourceLimitsOut:
    machine_mem_mb, machine_cpu = _machine_budget()
    return ResourceLimitsOut(
        max_mem_mb=limits.max_mem_mb,
        max_cpu=limits.max_cpu,
        max_threads=limits.max_threads,
        machine_mem_mb=machine_mem_mb,
        machine_cpu=machine_cpu,
        hard_mem_mb=resource_limit_service.hard_mem_mb(),
    )


@router.get("/resources", response_model=ResourceLimitsOut)
async def get_resource_limits() -> ResourceLimitsOut:
    return _limits_out(await resource_limit_service.load())


@router.put("/resources", response_model=ResourceLimitsOut)
async def set_resource_limits(body: ResourceLimitsIn) -> ResourceLimitsOut:
    # Refused rather than silently clamped: a budget that saves as a number
    # the user did not type is worse than an error saying why.
    hard = resource_limit_service.hard_mem_mb()
    if hard is not None and body.max_mem_mb is not None and body.max_mem_mb > hard:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A hard limit of {hard} MB is enforced on this machine. "
                f"The memory budget cannot exceed it. Change the hard limit "
                f"in the BioFlow launcher's settings."
            ),
        )
    limits = await resource_limit_service.save(
        max_mem_mb=body.max_mem_mb,
        max_cpu=body.max_cpu,
        max_threads=body.max_threads,
    )
    return _limits_out(limits)
