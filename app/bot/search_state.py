_pending: dict[int, str] = {}


def set_pending(telegram_id: int, kind: str) -> None:
    """标记某用户正在等待输入某类型（'github' / 'huggingface'）的搜索关键词"""
    _pending[telegram_id] = kind


def get_pending(telegram_id: int) -> str | None:
    return _pending.get(telegram_id)


def clear_pending(telegram_id: int) -> None:
    _pending.pop(telegram_id, None)


def is_pending(kind: str):
    def _filter(message) -> bool:
        user = getattr(message, "from_user", None)
        if not user:
            return False
        return get_pending(user.id) == kind
    return _filter