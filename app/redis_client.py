from redis.asyncio import Redis

from app.config import settings

_client: Redis | None = None


def get_redis() -> Redis:
    """Shared async Redis client, lazily created from settings.redis_url."""
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _client
