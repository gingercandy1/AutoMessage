from fastapi import FastAPI, Query

from app.health import get_pipeline_health
from app.logger import AppLogger

logger = AppLogger.get(__name__)

app = FastAPI(title="AI Project Bot — Health", version="1.0")


@app.get("/health")
async def health():
    return get_pipeline_health()


@app.get("/watchdog")
async def watchdog(force: bool = Query(False, description="是否立即执行一次全量自检")):
    """看门狗实时自检结果（可追溯）。"""
    from app.watchdog.monitor import get_watchdog_status
    from app.watchdog.alerts import AlertService

    status = await get_watchdog_status(force=force)
    status["recent_events"] = AlertService.recent_events(limit=20)
    return status


@app.get("/watchdog/events")
async def watchdog_events(limit: int = Query(20, ge=1, le=100)):
    from app.watchdog.alerts import AlertService
    return {"events": AlertService.recent_events(limit=limit)}


@app.get("/")
async def root():
    return {"service": "ai-project-bot", "status": "ok"}
