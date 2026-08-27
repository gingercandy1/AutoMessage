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
            message="GITHUB_TOKEN 未配置，GitHub 采集将受限（匿名 60/h）",
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
                message=f"GitHub Token 有效，剩余配额 {remaining}",
                details={"remaining": remaining, "status": 200},
                suggestion="",
            )
        if r.status_code == 401:
            return CheckResult(
                check_name="W001_GITHUB_TOKEN",
                level="critical",
                message="GitHub Token 无效（401 Bad credentials）— 采集将全部失败",
                details={"status": 401, "body": r.text[:300], "token_prefix": token[:12] + "..."},
                suggestion="Token 已过期/撤销：去 https://github.com/settings/tokens 重新生成并更新 .env 后执行 docker compose up -d --force-recreate bot",
            )
        return CheckResult(
            check_name="W001_GITHUB_TOKEN",
            level="warning",
            message=f"GitHub Token 检查异常：{r.status_code}",
            details={"status": r.status_code, "body": r.text[:300]},
            suggestion="检查网络或 Token 权限（需 public_repo 读权限）",
        )
    except Exception as e:
        return CheckResult(
            check_name="W001_GITHUB_TOKEN",
            level="warning",
            message=f"GitHub Token 检查失败：{e}",
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
            message="HUGGING_FACE_TOKEN 未配置，部分私有/限流模型采集可能 401",
            details={"configured": False},
            suggestion="在 .env 设置 HUGGING_FACE_TOKEN=hf_xxx 后重启",
        )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://huggingface.co/api/whoami",
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code == 200:
            return CheckResult("W002_HF_TOKEN", "ok", "HF Token 有效", {"status": 200}, "")
        if r.status_code == 401:
            return CheckResult("W002_HF_TOKEN", "critical", "HF Token 无效（401）— 模型采集将大量失败", {"status": 401}, "检查 Token 是否过期，去 https://huggingface.co/settings/tokens 重新生成")
        return CheckResult("W002_HF_TOKEN", "warning", f"HF Token 检查异常：{r.status_code}", {"status": r.status_code}, "")
    except Exception as e:
        return CheckResult("W002_HF_TOKEN", "warning", f"HF 检查失败：{e}", {"error": str(e)}, "")

def check_pipeline_health() -> CheckResult:
    """W003: 管线成功率 + backlog（基于 health.py 聚合）。"""
    h = get_pipeline_health()
    if h.get("error"):
        return CheckResult("W003_PIPELINE", "critical", f"健康查询本身失败：{h['error']}", {"error": h["error"]}, "检查数据库连接与日志")
    success = h.get("overall_success_rate_pct", 100)
    total = h.get("total_items", 0)
    # 统计未处理 backlog
    unprocessed = sum(s.get("unprocessed", 0) for s in h.get("per_source", {}).values())
    threshold = settings.watchdog_pipeline_success_threshold
    if success < threshold:
        return CheckResult(
            "W003_PIPELINE",
            "warning",
            f"管线成功率 {success}% 低于阈值 {threshold}%（错误 {sum(s['error'] for s in h['per_source'].values())} 条）",
            {"success_rate": success, "total": total, "unprocessed": unprocessed, "per_source": h["per_source"]},
            "查看 /health 的 per_source 错误分布，或 docker logs ai_project_bot | grep 采集失败",
        )
    if unprocessed > 200:
        return CheckResult(
            "W003_PIPELINE",
            "warning",
            f"未处理积压 {unprocessed} 条（>200），批处理可能卡住",
            {"unprocessed": unprocessed, "total": total},
            "检查 daily_process 是否按时执行，或手动触发 BatchSummary",
        )
    return CheckResult("W003_PIPELINE", "ok", f"管线正常：成功率 {success}% 总条目 {total}", {"success_rate": success, "unprocessed": unprocessed}, "")

def check_job_freshness(heartbeat: Optional[Dict[str, Any]] = None) -> List[CheckResult]:
    """W004: 关键任务是否按时执行（基于 PipelineRun + 心跳）。"""
    results: List[CheckResult] = []
    hb = heartbeat or _job_heartbeat
    db = SessionLocal()
    try:
        # 期望的 run_type -> 最大允许 stale
        expectations = {
            "daily": 25,          # 每天一次
            "news": 25,
            "lightweight": 8,     # 6h 一次
            "process": 25,
        }
        stale_hours = settings.watchdog_stale_hours
        now = datetime.now(timezone.utc)
        for run_type, max_stale in expectations.items():
            row = db.query(PipelineRun).filter(PipelineRun.run_type == run_type).order_by(PipelineRun.started_at.desc()).first()
            if not row:
                results.append(CheckResult(
                    f"W004_FRESH_{run_type.upper()}",
                    "warning",
                    f"类型 {run_type} 暂无 PipelineRun 记录（可能从未执行或表刚迁移）",
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
                    f"{run_type} 上次执行 {age_h:.1f}h 前（阈值 {limit}h），可能调度未触发",
                    {"last_run": row.started_at.isoformat() if row.started_at else None, "age_hours": round(age_h, 1), "failed_by_stage": row.failed_by_stage},
                    f"检查 scheduler 日志 Next wakeup / docker logs ai_project_bot | grep {run_type}；确认容器时区与 Crontab",
                ))
            elif row.failed_by_stage and any(v > 0 for v in (row.failed_by_stage or {}).values()):
                results.append(CheckResult(
                    f"W004_FRESH_{run_type.upper()}",
                    "warning",
                    f"{run_type} 上次执行有失败分段 {row.failed_by_stage}",
                    {"failed_by_stage": row.failed_by_stage, "collected": row.collected},
                    "查看日志 采集失败 [source] 定位具体源的 401/限流",
                ))
            else:
                results.append(CheckResult(f"W004_FRESH_{run_type.upper()}", "ok", f"{run_type} 最近 {age_h:.1f}h 前正常执行", {"age_hours": round(age_h, 1)}, ""))

        # 检查 apscheduler 心跳（若有记录）
        if hb:
            for job_id, info in hb.items():
                if info.get("status") == "failed":
                    results.append(CheckResult(
                        f"W004_JOB_{job_id}",
                        "critical",
                        f"任务 {job_id} 上次执行失败：{info.get('error')}",
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
        return CheckResult("W005_DB", "critical", f"数据库连接失败：{e}", {"error": str(e)}, "检查 postgres 容器健康：docker ps / docker logs ai_postgres")

def check_snapshot_staleness() -> CheckResult:
    """W006: MetricSnapshot 新鲜度（趋势数据是否在更新）。"""
    db = SessionLocal()
    try:
        from sqlalchemy import func
        row = db.query(func.max(MetricSnapshot.captured_at)).scalar()
        if not row:
            return CheckResult("W006_SNAPSHOT", "warning", "暂无 MetricSnapshot（采集可能从未成功）", {}, "触发一次 daily_run 验证")
        age_h = (datetime.now(timezone.utc) - row).total_seconds() / 3600
        if age_h > settings.watchdog_stale_hours:
            return CheckResult("W006_SNAPSHOT", "warning", f"最新快照 {age_h:.1f}h 前，超过 {settings.watchdog_stale_hours}h 阈值", {"last_snapshot": row.isoformat(), "age_hours": round(age_h, 1)}, "检查轻量快照/每日采集是否按时执行")
        return CheckResult("W006_SNAPSHOT", "ok", f"快照新鲜：{age_h:.1f}h 前", {"age_hours": round(age_h, 1)}, "")
    finally:
        db.close()

# 聚合入口
async def run_all_checks() -> List[CheckResult]:
    results: List[CheckResult] = []
    results.append(await check_github_token())
    results.append(await check_hf_token())
    results.append(check_pipeline_health())
    results.extend(check_job_freshness())
    results.append(check_infra_db())
    results.append(check_snapshot_staleness())
    return results
