"""AlertService — 告警通道：Telegram + 日志 + 持久化 WatchdogEvent。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from aiogram import Bot

from app.config import settings
from app.database import SessionLocal
from app.database.models import WatchdogEvent
from app.logger import AppLogger
from app.watchdog.checks import CheckResult

logger = AppLogger.get(__name__)

# 内存级冷却：check_name -> 上次告警时间
_last_alert_at: Dict[str, datetime] = {}

def _alert_target() -> int | None:
    """优先级：watchdog_alert_chat_id > review_chat_id > review_admin_ids[0]"""
    if settings.watchdog_alert_chat_id:
        return settings.watchdog_alert_chat_id
    if settings.review_chat_id:
        return settings.review_chat_id
    if settings.review_admin_ids:
        return int(settings.review_admin_ids[0])
    return None

def _should_alert(check_name: str) -> bool:
    now = datetime.now(timezone.utc)
    last = _last_alert_at.get(check_name)
    if not last:
        return True
    cooldown = timedelta(minutes=settings.watchdog_cooldown_minutes)
    return (now - last) >= cooldown

def _mark_alerted(check_name: str):
    _last_alert_at[check_name] = datetime.now(timezone.utc)

def _persist_event(r: CheckResult):
    db = SessionLocal()
    try:
        # 去重：若同 check 最近 1h 内已有同 level 未解决事件，则不重复落库（避免刷屏）
        recent = db.query(WatchdogEvent).filter(
            WatchdogEvent.check_name == r.check_name,
            WatchdogEvent.created_at > datetime.now(timezone.utc) - timedelta(minutes=settings.watchdog_cooldown_minutes),
            WatchdogEvent.resolved == 0,
        ).first()
        if recent and recent.level == r.level:
            return recent
        evt = WatchdogEvent(
            check_name=r.check_name,
            level=r.level,
            message=r.message,
            details=r.details,
            suggestion=r.suggestion,
            resolved=0 if r.level != "ok" else 1,
            created_at=datetime.now(timezone.utc),
        )
        db.add(evt)
        db.commit()
        return evt
    except Exception as e:
        logger.error(f"WatchdogEvent 落库失败: {e}")
    finally:
        db.close()

def _format_telegram(results: List[CheckResult]) -> str:
    """生成告警卡片：说清楚"出了什么问题、要不要紧、怎么处理"，不堆技术
    术语和原始数据——那些留给 /watchdog 和日志，推送只给看得懂的结论。"""
    bad = [r for r in results if r.level != "ok"]
    if not bad:
        return ""
    critical_n = sum(1 for r in bad if r.level == "critical")
    warning_n = len(bad) - critical_n
    parts = []
    if critical_n:
        parts.append(f"{critical_n} 个严重")
    if warning_n:
        parts.append(f"{warning_n} 个提醒")
    lines = [
        f"🚨 <b>系统检查提醒</b> · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"发现 " + "、".join(parts),
        "",
    ]
    for r in sorted(bad, key=lambda x: x.level != "critical"):
        icon = "🔴" if r.level == "critical" else "🟡"
        lines.append(f"{icon} {r.message}")
        if r.suggestion:
            lines.append(f"  怎么办：{r.suggestion}")
        lines.append("")
    lines.append("完整详情：<code>/watchdog</code>")
    return "\n".join(lines).strip()

class AlertService:
    @staticmethod
    async def dispatch(results: List[CheckResult], force: bool = False) -> int:
        """发送告警并持久化。返回发送的告警数。force=True 忽略冷却。"""
        bad = [r for r in results if r.level != "ok"]
        # 持久化所有结果（包括 ok，用于 resolved 标记）
        for r in results:
            _persist_event(r)
            if r.level == "ok":
                # 若之前有未解决的同 check 事件，标记为已恢复
                db = SessionLocal()
                try:
                    pending = db.query(WatchdogEvent).filter(
                        WatchdogEvent.check_name == r.check_name,
                        WatchdogEvent.resolved == 0,
                    ).all()
                    for evt in pending:
                        evt.resolved = 1
                        evt.resolved_at = datetime.now(timezone.utc)
                    if pending:
                        db.commit()
                finally:
                    db.close()

        if not bad:
            logger.info("✅ 看门狗自检全部通过")
            return 0

        to_send = []
        for r in bad:
            if force or _should_alert(r.check_name):
                to_send.append(r)
            else:
                logger.info(f"⏭️ 看门狗 {r.check_name} 冷却期内跳过推送")

        if not to_send:
            return 0

        # 日志通道（始终）
        for r in to_send:
            log_fn = logger.error if r.level == "critical" else logger.warning
            log_fn(f"看门狗告警 [{r.check_name}] {r.level}: {r.message} | 建议: {r.suggestion} | 详情: {r.details}")

        # Telegram 通道
        target = _alert_target()
        if not target:
            logger.warning("看门狗无告警目标（watchdog_alert_chat_id / review_chat_id / review_admin_ids 均未配置），仅日志告警")
            for r in to_send:
                _mark_alerted(r.check_name)
            return len(to_send)

        text = _format_telegram(to_send)
        try:
            bot = Bot(token=settings.telegram_bot_token)
            await bot.send_message(target, text, parse_mode="HTML", disable_web_page_preview=True)
            await bot.session.close()
            logger.info(f"📣 看门狗告警已推送到 {target} ({len(to_send)} 项)")
        except Exception as e:
            logger.error(f"看门狗 Telegram 推送失败 target={target}: {e}")

        for r in to_send:
            _mark_alerted(r.check_name)
        return len(to_send)

    @staticmethod
    def recent_events(limit: int = 20) -> List[Dict[str, Any]]:
        db = SessionLocal()
        try:
            rows = db.query(WatchdogEvent).order_by(WatchdogEvent.created_at.desc()).limit(limit).all()
            return [
                {
                    "id": r.id,
                    "check": r.check_name,
                    "level": r.level,
                    "message": r.message,
                    "details": r.details,
                    "suggestion": r.suggestion,
                    "resolved": bool(r.resolved),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
