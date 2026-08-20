from datetime import datetime, timezone
from html import escape
from typing import Any


class BaseProjectRenderer:
    """渲染契约：子类必须实现 card()；page()/empty() 提供默认实现。"""

    @staticmethod
    def _format_count(n: int | None) -> str:
        n = n or 0
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    @staticmethod
    def _time_ago(value: Any) -> str | None:
        if not value:
            return None
        dt = value
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(dt, datetime):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        seconds = (now - dt).total_seconds()
        if seconds < 0:
            return "刚刚"
        if seconds < 3600:
            return f"{int(seconds // 60)}分钟前"
        if seconds < 86400:
            return f"{int(seconds // 3600)}小时前"
        days = int(seconds // 86400)
        if days < 30:
            return f"{days}天前"
        if days < 365:
            return f"{days // 30}个月前"
        return f"{days // 365}年前"

    @staticmethod
    def _license_icon(license_name: str | None) -> str:
        """分级匹配开源协议图标（GitHub 与 HuggingFace 共用同一套协议命名）。"""
        if not license_name:
            return "📄"
        name = license_name.lower()
        exact_map = {
            "mit": "🔓",
            "apache-2.0": "📜",
            "apache license 2.0": "📜",
            "gpl-3.0": "©️",
            "gnu general public license v3.0": "©️",
            "gpl-2.0": "©️",
            "gnu general public license v2.0": "©️",
            "bsd-3-clause": "🅱️",
            "bsd 3-clause \"new\" or \"revised\" license": "🅱️",
            "mpl-2.0": "🦊",
            "mozilla public license 2.0": "🦊",
        }
        if name in exact_map:
            return exact_map[name]
        keyword_map = {
            "mit": "🔓", "apache": "📜", "gpl": "©️", "bsd": "🅱️",
            "mozilla": "🦊", "creative commons": "🎨", "cc-": "🎨",
        }
        for kw, icon in keyword_map.items():
            if kw in name:
                return icon
        return "📄"

    @staticmethod
    def card(item, index: int) -> str:
        raise NotImplementedError

    @staticmethod
    def page(items, all_count: int, page: int, total_pages: int, header: str) -> str:
        lines = [header, ""]
        start_index = page * 6
        for i, item in enumerate(items, start=start_index + 1):
            lines.append(BaseProjectRenderer.card(item, i))
            lines.append("")
        lines.append(f"📊 共 <b>{all_count}</b> 个 · 第 <b>{page + 1}/{total_pages}</b> 页")
        return "\n".join(lines)

    @staticmethod
    def empty(message: str) -> str:
        return f"📊 {escape(message)}\n\n请稍后再试"
