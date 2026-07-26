"""Redis connection and Lua script registry."""

from pathlib import Path
from typing import Any

import redis.asyncio as redis

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "queue" / "scripts"

_redis: redis.Redis | None = None
# Registered Lua scripts. Typed as Any because the concrete class moved between
# redis-py versions (redis.asyncio.client.Script -> redis.commands.core.
# AsyncScript in 8.0); the annotation is not worth a version dependency.
_scripts: dict[str, Any] = {}


def get_redis() -> redis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized; call connect_to_redis() first")
    return _redis


async def connect_to_redis() -> redis.Redis:
    global _redis
    if _redis is not None:
        return _redis
    _redis = redis.from_url(settings.redis_url, decode_responses=True)
    await _redis.ping()
    _load_scripts(_redis)
    log.info("redis_connected", url=settings.redis_url)
    return _redis


def _load_scripts(client: redis.Redis) -> None:
    """Register every .lua file under queue/scripts by stem name."""
    if not SCRIPT_DIR.exists():
        return
    for path in sorted(SCRIPT_DIR.glob("*.lua")):
        _scripts[path.stem] = client.register_script(path.read_text())
    if _scripts:
        log.info("lua_scripts_loaded", names=sorted(_scripts))


def get_script(name: str) -> Any:
    if name not in _scripts:
        raise KeyError(f"Lua script not registered: {name}")
    return _scripts[name]


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        _scripts.clear()
        log.info("redis_closed")


async def ping() -> bool:
    try:
        await get_redis().ping()
        return True
    except Exception as e:  # noqa: BLE001 - health check reports, never raises
        log.warning("redis_ping_failed", error=str(e))
        return False
