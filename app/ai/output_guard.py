import re
from typing import Any, Dict

from app.logger import AppLogger

logger = AppLogger.get(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")

_INJECTION_RE = re.compile(
    "|".join([
        r"ignore (all|any|previous|prior) instructions",
        r"disregard (all|any|previous|prior)",
        r"system prompt",
        r"forget (all|your) (previous |prior )?instructions",
        r"reveal (your|the) (system |hidden )?prompt",
        r"you are now (a|an|in) ",
        r"new instructions?:",
    ]),
    re.IGNORECASE,
)

_TEXT_FIELDS = ("one_liner", "summary", "use_cases", "install_step", "maturity_level")
_LIST_FIELDS = ("pros", "cons", "tags")

_REDACTED = "[内容因疑似 prompt 注入被过滤]"


def _sanitize_str(value: Any, field: str, item_id: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    cleaned = _HTML_TAG_RE.sub("", value)
    if _INJECTION_RE.search(cleaned):
        logger.warning(f"⚠️ 疑似 prompt 注入，已过滤 item={item_id} field={field}: {cleaned[:120]!r}")
        return _REDACTED
    return cleaned


def sanitize_summary_data(summary_data: Dict[str, Any], item_id: Any = None) -> Dict[str, Any]:
    """LLM 输出侧校验（README 安全风险#3）：摘要来自采集内容（README/描述等不可信来源）
    拼进 prompt 生成，输出前剥掉裸 HTML 标签 + 过滤常见注入话术，避免误导性内容
    未经审视直接进频道/私聊推送。不改变原 dict，返回清洗后的副本。"""
    cleaned = dict(summary_data)
    for field in _TEXT_FIELDS:
        if field in cleaned:
            cleaned[field] = _sanitize_str(cleaned[field], field, item_id)
    for field in _LIST_FIELDS:
        values = cleaned.get(field)
        if isinstance(values, list):
            cleaned[field] = [
                _sanitize_str(v, field, item_id) if isinstance(v, str) else v
                for v in values
            ]
    return cleaned
