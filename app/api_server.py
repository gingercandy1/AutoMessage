from fastapi import FastAPI

from app.health import get_pipeline_health
from app.logger import AppLogger

logger = AppLogger.get(__name__)

app = FastAPI(title="AI Project Bot — Health", version="1.0")


@app.get("/health")
async def health():
    return get_pipeline_health()


@app.get("/")
async def root():
    return {"service": "ai-project-bot", "status": "ok"}
