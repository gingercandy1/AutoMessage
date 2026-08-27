from fastapi import Depends, FastAPI, Header, HTTPException, Query

from app.config import settings
from app.health import get_pipeline_health
from app.logger import AppLogger

logger = AppLogger.get(__name__)

if not settings.health_api_key:
    logger.warning("⚠️ HEALTH_API_KEY 未设置，/health /watchdog 接口对外无鉴权，仅建议本机调试使用")

app = FastAPI(title="AI Project Bot — Health", version="1.0")


async def require_api_key(x_api_key: str | None = Header(default=None)):
    if settings.health_api_key and x_api_key != settings.health_api_key:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


@app.get("/health", dependencies=[Depends(require_api_key)])
async def health():
    return get_pipeline_health()


@app.get("/watchdog", dependencies=[Depends(require_api_key)])
async def watchdog(force: bool = Query(False, description="是否立即执行一次全量自检")):
    """看门狗实时自检结果（可追溯）。"""
    from app.watchdog.monitor import get_watchdog_status
    from app.watchdog.alerts import AlertService

    status = await get_watchdog_status(force=force)
    status["recent_events"] = AlertService.recent_events(limit=20)
    return status


@app.get("/watchdog/events", dependencies=[Depends(require_api_key)])
async def watchdog_events(limit: int = Query(20, ge=1, le=100)):
    from app.watchdog.alerts import AlertService
    return {"events": AlertService.recent_events(limit=limit)}


@app.get("/")
async def root():
    return {"service": "ai-project-bot", "status": "ok"}
