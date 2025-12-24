from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None


def get_redis() -> Redis | None:
    """Shared client, or None when Redis isn't configured (features degrade to database reads)."""
    global _client
    url = get_settings().redis_url
    if not url:
        return None
    if _client is None:
        _client = Redis.from_url(url, decode_responses=True, health_check_interval=30)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
