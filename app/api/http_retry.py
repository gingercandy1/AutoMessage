import asyncio
import random
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.logger import AppLogger

logger = AppLogger.get(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    delay = min(max_delay, base_delay * 2 ** (attempt - 1))
    return delay * random.uniform(0.5, 1.0)


async def send_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_statuses: frozenset = RETRY_STATUSES,
    **kwargs,
) -> httpx.Response:
    """发请求，对网络错误和 429/5xx 做指数退避+抖动重试，429/503 优先遵守 Retry-After。

    不调用 raise_for_status：最终响应（含非重试类错误码）原样返回，由调用方按各自语义处理。
    重试耗尽仍是网络错误时抛出最后一次的 httpx.TransportError。
    """
    attempts = max(1, max_retries)
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as e:
            if attempt == attempts:
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            logger.warning(f"请求失败，{delay:.1f}s 后重试 ({attempt}/{attempts}) {method} {url}: {e!r}")
            await asyncio.sleep(delay)
            continue

        if resp.status_code not in retry_statuses or attempt == attempts:
            return resp

        retry_after = _retry_after_seconds(resp)
        delay = min(max_delay, retry_after) if retry_after is not None else backoff_delay(attempt, base_delay, max_delay)
        logger.warning(f"HTTP {resp.status_code}，{delay:.1f}s 后重试 ({attempt}/{attempts}) {method} {url}")
        await asyncio.sleep(delay)

    raise AssertionError("unreachable")
