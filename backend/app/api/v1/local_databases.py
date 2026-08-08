"""Local-database endpoints: submit and list.

Not owner-scoped, like feedback.py -- a single-user install has no reason to
partition submissions per profile. Append-only: no PATCH or DELETE route
exists. A URL is validated for well-formedness (scheme + host) at the input
model, not fetched -- reachability checking is explicitly out of scope, so a
submission with a typo'd or since-dead URL is still accepted.
"""

from fastapi import APIRouter
from pydantic import AnyUrl, BaseModel, Field

from app.models.local_database import (
    NAME_MAX_LENGTH,
    URL_MAX_LENGTH,
    LocalDatabase,
    LocalDatabaseCategory,
)

router = APIRouter(prefix="/local-databases", tags=["local-databases"])


class LocalDatabaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    url: AnyUrl = Field(max_length=URL_MAX_LENGTH)
    category: LocalDatabaseCategory


class LocalDatabaseOut(BaseModel):
    id: str
    name: str
    url: str
    category: LocalDatabaseCategory
    created_at: str

    @classmethod
    def of(cls, d: LocalDatabase) -> "LocalDatabaseOut":
        return cls(
            id=str(d.id),
            name=d.name,
            url=d.url,
            category=d.category,
            created_at=d.created_at.isoformat(),
        )


@router.post("", status_code=201)
async def submit_local_database(body: LocalDatabaseCreate) -> LocalDatabaseOut:
    db = LocalDatabase(name=body.name, url=str(body.url), category=body.category)
    await db.insert()
    return LocalDatabaseOut.of(db)


@router.get("")
async def list_local_databases() -> list[LocalDatabaseOut]:
    items = await LocalDatabase.find_all().sort("-created_at").to_list()
    return [LocalDatabaseOut.of(d) for d in items]
