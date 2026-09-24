"""Watchdog checks — 每个 check 返回 CheckResult，带问题定位与修复建议。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.database import SessionLocal
from app.database.models import PipelineRun, MetricSnapshot, ContentItem
from app.health import get_pipeline_health
from app.logger import AppLogger

logger = AppLogger.get(__name__)

@dataclass
class CheckResult:
    check_name: str          # 唯一 code，如 W001
    level: str               # ok / warning / critical
    message: str             # 人类可读问题
    details: Dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""     # 修复建议
    # 用于可追溯：关联的 job/源
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.level == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check_name,
            "level": self.level,
            "message": self.message,
            "details": self.details,
            "suggestion": self.suggestion,
            "meta": self.meta,
        }

# 用于记录 job 心跳（由 scheduler wrapper 更新）
_job_heartbeat: Dict[str, Dict[str, Any]] = {}

def record_job_heartbeat(job_id: str, status: str, error: str | None = None):
    """由 scheduler 的 job wrapper 调用，记录最近一次执行状态。"""
    _job_heartbeat[job_id] = {
        "last_run": datetime.now(timezone.utc),
        "status": status,  # ok / failed
        "error": error,
    }

def get_job_heartbeat() -> Dict[str, Dict[str, Any]]:
    return dict(_job_heartbeat)

# ---------- 单项检查 ----------

async def check_github_token() -> CheckResult:
    """W001: GitHub Token 是否有效（命中你刚遇到的 401）。"""
    token = settings.github_token
    if not token:
        return CheckResult(
            check_name="W001_GITHUB_TOKEN",
            level="warning",
            message="GitHub 采集没配访问令牌，速度会很慢",
            details={"configured": False},
            suggestion="在 .env 设置 GITHUB_TOKEN=ghp_xxx 或 github_pat_xxx 后重启 bot",
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.github.com/rate_limit",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if r.status_code == 200:
            data = r.json()
            remaining = data.get("resources", {}).get("core", {}).get("remaining", "?")
            return CheckResult(
                check_name="W001_GITHUB_TOKEN",
                level="ok",
                message=f"GitHub 采集正常，剩余额度 {remaining}",
                details={"remaining": remaining, "status": 200},
                suggestion="",
            )
        if r.status_code == 401:
            return CheckResult(
                check_name="W001_GITHUB_TOKEN",
                level="critical",
                message="GitHub 访问令牌已失效，GitHub 相关内容会全部采集不到",
                details={"status": 401, "body": r.text[:300], "token_prefix": token[:12] + "..."},
                suggestion="Token 已过期/撤销：去 https://github.com/settings/tokens 重新生成并更新 .env 后执行 docker compose up -d --force-recreate bot",
            )
        return CheckResult(
            check_name="W001_GITHUB_TOKEN",
            level="warning",
            message=f"GitHub 令牌检查异常（状态码 {r.status_code}）",
            details={"status": r.status_code, "body": r.text[:300]},
            suggestion="检查网络或 Token 权限（需 public_repo 读权限）",
        )
    except Exception as e:
        return CheckResult(
            check_name="W001_GITHUB_TOKEN",
            level="warning",
            message="GitHub 令牌检查失败，可能是网络问题",
            details={"error": str(e)},
            suggestion="检查容器网络连通性，或稍后重试",
        )

async def check_hf_token() -> CheckResult:
    """W002: HuggingFace Token 有效性（可选，未配置仅 warning）。"""
    token = (settings.hugging_face_token or "").strip()
    if not token:
        return CheckResult(
            check_name="W002_HF_TOKEN",
            level="warning",
            message="HuggingFace 采集没配访问令牌，部分模型可能采集不到",
            details={"configured": False},
            suggestion="在 .env 设置 HUGGING_FACE_TOKEN=hf_xxx 后重启",
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://huggingface.co/api/whoami-v2",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code == 200:
            return CheckResult("W002_HF_TOKEN", "ok", "HuggingFace 采集正常", {"status": 200}, "")
        if r.status_code == 401:
            return CheckResult("W002_HF_TOKEN", "critical", "HuggingFace 访问令牌已失效，模型采集会大量失败", {"status": 401}, "检查 Token 是否过期，去 https://huggingface.co/settings/tokens 重新生成")
        return CheckResult("W002_HF_TOKEN", "warning", f"HuggingFace 令牌检查异常（状态码 {r.status_code}）", {"status": r.status_code}, "")
    except Exception as e:
        return CheckResult("W002_HF_TOKEN", "warning", "HuggingFace 令牌检查失败，可能是网络问题", {"error": str(e)}, "")

def check_pipeline_health() -> CheckResult:
    """W003: 管线成功率 + backlog（基于 health.py 聚合）。"""
    h = get_pipeline_health()
    if h.get("error"):
        return CheckResult("W003_PIPELINE", "critical", f"健康查询本身失败：{h['error']}", {"error": h["error"]}, "检查数据库连接与日志")
    success = h.get("overall_success_rate_pct", 100)
    total = h.get("total_items", 0)
    unprocessed = sum(s.get("unprocessed", 0) for s in h.get("per_source", {}).values())
    threshold = settings.watchdog_pipeline_success_threshold
    if success < threshold:
        error_count = sum(s["error"] for s in h["per_source"].values())
        return CheckResult(
            "W003_PIPELINE",
            "warning",
            f"最近收集的内容里，只有 {success}% 成功生成了摘要，{error_count} 条失败了——"
            f"通常是 AI 摘要服务暂时不稳定，或采集到了没法总结的内容",
            {"success_rate": success, "total": total, "unprocessed": unprocessed, "per_source": h["per_source"]},
            "查看 /health 的 per_source 错误分布，或 docker logs ai_project_bot | grep 采集失败",
        )
    if unprocessed > 200:
        return CheckResult(
            "W003_PIPELINE",
            "warning",
            f"有 {unprocessed} 条内容还堆在那没处理，处理速度可能跟不上",
            {"unprocessed": unprocessed, "total": total},
            "检查 daily_process 是否按时执行，或手动触发 BatchSummary",
        )
    return CheckResult("W003_PIPELINE", "ok", f"内容处理正常：成功率 {success}%，共 {total} 条", {"success_rate": success, "unprocessed": unprocessed}, "")

def check_job_freshness(heartbeat: Optional[Dict[str, Any]] = None) -> List[CheckResult]:
    """W004: 关键任务是否按时执行（基于 PipelineRun + 心跳）。"""
    results: List[CheckResult] = []
    hb = heartbeat or _job_heartbeat
    db = SessionLocal()
    try:
        # 期望的 run_type -> (中文名, 最大允许 stale)
        expectations = {
            "daily": ("每日采集", 25),          # 每天一次
            "news": ("新闻采集", 25),
            "lightweight": ("轻量快照", 8),     # 6h 一次
            "process": ("内容处理", 25),
        }
        stale_hours = settings.watchdog_stale_hours
        now = datetime.now(timezone.utc)
        for run_type, (label, max_stale) in expectations.items():
            row = db.query(PipelineRun).filter(PipelineRun.run_type == run_type).order_by(PipelineRun.started_at.desc()).first()
            if not row:
                results.append(CheckResult(
                    f"W004_FRESH_{run_type.upper()}",
                    "warning",
                    f"「{label}」还没有过运行记录，可能是从没成功跑过",
                    {"run_type": run_type},
                    "等待下一次调度，或手动触发对应采集",
                ))
                continue
            age_h = (now - row.started_at).total_seconds() / 3600 if row.started_at else 999
            # 使用 stale_hours 覆盖更严格的阈值
            limit = min(max_stale, stale_hours)
            if age_h > limit:
                level = "critical" if age_h > limit * 1.5 else "warning"
                results.append(CheckResult(
                    f"W004_FRESH_{run_type.upper()}",
                    level,
                    f"「{label}」已经 {age_h:.0f} 小时没有成功运行了，本该每隔一段时间自动跑一次，"
                    f"可能是没被正常触发",
                    {"last_run": row.started_at.isoformat() if row.started_at else None, "age_hours": round(age_h, 1), "failed_by_stage": row.failed_by_stage},
                    f"检查 scheduler 日志 Next wakeup / docker logs ai_project_bot | grep {run_type}；确认容器时区与 Crontab",
                ))
            elif row.failed_by_stage and any(v > 0 for v in (row.failed_by_stage or {}).values()):
                results.append(CheckResult(
                    f"W004_FRESH_{run_type.upper()}",
                    "warning",
                    f"「{label}」上次运行时有部分内容没采集成功",
                    {"failed_by_stage": row.failed_by_stage, "collected": row.collected},
                    "查看日志 采集失败 [source] 定位具体源的 401/限流",
                ))
            else:
                results.append(CheckResult(f"W004_FRESH_{run_type.upper()}", "ok", f"「{label}」运行正常，{age_h:.0f} 小时前刚跑过", {"age_hours": round(age_h, 1)}, ""))

        if hb:
            for job_id, info in hb.items():
                if info.get("status") == "failed":
                    results.append(CheckResult(
                        f"W004_JOB_{job_id}",
                        "critical",
                        f"「{job_id}」这个任务上次执行失败了：{info.get('error')}",
                        {"job_id": job_id, "last_run": info.get("last_run").isoformat() if info.get("last_run") else None},
                        "查看日志 ❌ 采集失败 / 批处理失败 栈",
                    ))
        return results
    finally:
        db.close()

def check_infra_db() -> CheckResult:
    """W005: 数据库连通性。"""
    try:
        db = SessionLocal()
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db.close()
        return CheckResult("W005_DB", "ok", "数据库连接正常", {}, "")
    except Exception as e:
        return CheckResult("W005_DB", "critical", "数据库连不上了，机器人基本无法正常工作", {"error": str(e)}, "检查 postgres 容器健康：docker ps / docker logs ai_postgres")

async def check_redis() -> CheckResult:
    """W007: Redis 连通性。FSM 状态 + 限流中间件现在都靠它，是单点依赖，
    中间件本身对 Redis 故障是 fail-open（放行不限流），所以不会直接炸掉 bot，
    但会话状态和限流会静默失效，需要能在这里发现。"""
    try:
        from app.redis_client import get_redis
        redis = get_redis()
        await redis.ping()
        return CheckResult("W007_REDIS", "ok", "Redis 连接正常", {}, "")
    except Exception as e:
        return CheckResult(
            "W007_REDIS", "critical",
            "Redis 连不上了，用户的登录状态和防刷限制会暂时失效（其他功能不受影响）",
            {"error": str(e)},
            "检查 redis 容器健康、REDIS_URL 是否带上了和 REDIS_PASSWORD 一致的密码："
            "docker ps / docker logs ai_redis；期间 FSM 状态和限流会静默失效（fail-open）",
        )

def check_snapshot_staleness() -> CheckResult:
    """W006: MetricSnapshot 新鲜度（趋势数据是否在更新）。"""
    db = SessionLocal()
    try:
        from sqlalchemy import func
        row = db.query(func.max(MetricSnapshot.captured_at)).scalar()
        if not row:
            return CheckResult("W006_SNAPSHOT", "warning", "还没有采集到任何数据", {}, "触发一次 daily_run 验证")
        age_h = (datetime.now(timezone.utc) - row).total_seconds() / 3600
        if age_h > settings.watchdog_stale_hours:
            return CheckResult("W006_SNAPSHOT", "warning", f"数据已经 {age_h:.0f} 小时没更新了，趋势数据可能停止刷新了", {"last_snapshot": row.isoformat(), "age_hours": round(age_h, 1)}, "检查轻量快照/每日采集是否按时执行")
        return CheckResult("W006_SNAPSHOT", "ok", f"数据更新正常，{age_h:.0f} 小时前刚更新过", {"age_hours": round(age_h, 1)}, "")
    finally:
        db.close()

def _news_feed_urls() -> List[str]:
    return [f.strip() for f in settings.news_rss_feeds.split(",") if f.strip()]


def check_news_source_freshness(now: Optional[datetime] = None) -> CheckResult:
    """W007: 按新闻来源、按 RSS feed 的产出新鲜度。W006 只看全局最新快照，
    单个源失效（被反爬 429、feed 停更、改版解析为空）会被其他源掩盖；采集器
    又会吞掉单个 feed 的失败，所以这里按 feed 逐个看最近一次产出时间。"""
    from sqlalchemy import func
    from app.collectors.news.news_base import NEWS_SOURCES

    now = now or datetime.now(timezone.utc)
    threshold = settings.watchdog_feed_stale_hours
    db = SessionLocal()
    try:
        feed_col = MetricSnapshot.metrics["feed_url"].astext
        last_by_feed = dict(
            db.query(feed_col, func.max(MetricSnapshot.captured_at))
            .filter(MetricSnapshot.source == "news")
            .group_by(feed_col)
            .all()
        )
        last_by_source = dict(
            db.query(MetricSnapshot.source, func.max(MetricSnapshot.captured_at))
            .filter(MetricSnapshot.source.in_([s for s in NEWS_SOURCES if s != "news"]))
            .group_by(MetricSnapshot.source)
            .all()
        )
    finally:
        db.close()

    disabled = set() if (settings.product_hunt_token or "").strip() else {"producthunt"}
    targets = {f: last_by_feed.get(f) for f in _news_feed_urls()}
    targets.update({s: last_by_source.get(s) for s in NEWS_SOURCES if s != "news" and s not in disabled})

    never, stale = [], {}
    for name, last in targets.items():
        if last is None:
            never.append(name)
            continue
        age_h = (now - last).total_seconds() / 3600
        if age_h > threshold:
            stale[name] = round(age_h, 1)

    if not never and not stale:
        return CheckResult("W007_NEWS_SOURCES", "ok", f"{len(targets)} 个新闻源都在 {threshold} 小时内有产出", {}, "")

    parts = []
    if never:
        parts.append(f"{len(never)} 个源从没采到过内容")
    if stale:
        parts.append(f"{len(stale)} 个源超过 {threshold} 小时没有新内容")
    return CheckResult(
        "W007_NEWS_SOURCES",
        "warning",
        "；".join(parts) + "——可能是源站反爬、feed 停更或地址失效",
        {"never": never, "stale_hours": stale},
        "用 curl -A 'AIProjectBot/1.0' <feed> 看状态码和内容：429/403 加 HTML 页面多为反爬；"
        "200 但最新条目很旧是 feed 停更，可从 NEWS_RSS_FEEDS 移除或换地址",
    )


async def run_all_checks() -> List[CheckResult]:
    results: List[CheckResult] = []
    results.append(await check_github_token())
    results.append(await check_hf_token())
    results.append(check_pipeline_health())
    results.extend(check_job_freshness())
    results.append(check_infra_db())
    results.append(await check_redis())
    results.append(check_snapshot_staleness())
    results.append(check_news_source_freshness())
    return results
