"""Backwards-compatible import shim.

Q11 placed `BaseProjectRenderer` in `app/bot/renderers/__init__.py`, but the
source handlers import `from app.bot.renderers.base import BaseProjectRenderer`.
Re-export here so both paths resolve.
"""
from app.bot.renderers import BaseProjectRenderer

__all__ = ["BaseProjectRenderer"]
