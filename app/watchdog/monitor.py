"""WatchdogMonitor — 统一调度自检 + 触发告警，供 scheduler 定时调用。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.logger import AppLogger
from app.watchdog.checks import run_all_checks, CheckResult
from app.watchdog.alerts import AlertService

logger = AppLogger.get(__name__)

class WatchdogMonitor:
    """每 N 分钟被 scheduler 调用的看门狗。"""

    async def run_once(self, force_alert: bool = False) -> Dict[str, Any]:
        logger.info("🔍 看门狗开始自检...")
        results = await run_all_checks()
        # 统计
        summary = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total": len(results),
            "ok": sum(1 for r in results if r.level == "ok"),
            "warning": sum(1 for r in results if r.level == "warning"),
            "critical": sum(1 for r in results if r.level == "critical"),
            "results": [r.to_dict() for r in results],
        }
        # 触发告警通道
        sent = await AlertService.dispatch(results, force=force_alert)
        summary["alerts_sent"] = sent
        if sent:
            logger.warning(f"看门狗自检完成：{summary['ok']} ok / {summary['warning']} warning / {summary['critical']} critical，已告警 {sent} 项")
        else:
            logger.info(f"看门狗自检完成：{summary['ok']} ok / {summary['warning']} warning / {summary['critical']} critical")
        return summary

    async def run_and_alert(self):
        """供 APScheduler 直接调用的无参包装。"""
        try:
            await self.run_once()
        except Exception as e:
            logger.error(f"看门狗执行失败: {e}", exc_info=True)

# 单例便捷函数
_monitor = WatchdogMonitor()

async def get_watchdog_status(force: bool = False) -> Dict[str, Any]:
    """供 /health 或 Bot 命令即时查询（force=True 立即跑一次全量检查）。"""
    if force:
        return await _monitor.run_once(force_alert=False)
    # 非 force 时复用最近逻辑：直接跑一次轻量检查（避免缓存过期）
    return await _monitor.run_once(force_alert=False)
