"""Pipeline health / observability (ADR-0001 §7).

Exposes a plain callable `get_pipeline_health()` so it can be consumed by:
  - a future FastAPI `/health` endpoint,
  - the bot `/status` command,
  - unit tests (no network / no HTTP server needed).
"""
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import func, select, case

from app.database import SessionLocal
from app.database.models import ContentItem, MetricSnapshot
from app.logger import AppLogger

logger = AppLogger.get(__name__)


# ContentItem.processed state machine (ADR-0001 §7)
PROCESSED_UNPROCESSED = 0   # freshly collected, not yet analyzed
PROCESSED_OK = 1            # analyzed successfully, summary exists
PROCESSED_ERROR = 2         # analysis/collection failed
PROCESSED_REVIEW = 3        # news awaiting/reviewing in human-review gate (reserved)


def get_pipeline_health() -> Dict[str, Any]:
    """Return per-source health metrics. Safe to call anytime; never raises."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        per_source: Dict[str, Any] = {}

        # ContentItem counts by (source, processed-state)
        rows = (
            db.query(
                ContentItem.source,
                ContentItem.processed,
                func.count(),
            )
            .group_by(ContentItem.source, ContentItem.processed)
            .all()
        )
        for source, state, count in rows:
            per_source.setdefault(source, {
                "total": 0, "unprocessed": 0, "ok": 0, "error": 0, "review": 0,
                "last_snapshot_at": None,
            })
            per_source[source]["total"] += count
            if state == PROCESSED_UNPROCESSED:
                per_source[source]["unprocessed"] += count
            elif state == PROCESSED_OK:
                per_source[source]["ok"] += count
            elif state == PROCESSED_ERROR:
                per_source[source]["error"] += count
            elif state == PROCESSED_REVIEW:
                per_source[source]["review"] += count

        # Last metric snapshot per source (freshness signal)
        snap_rows = (
            db.query(
                MetricSnapshot.source,
                func.max(MetricSnapshot.captured_at),
            )
            .group_by(MetricSnapshot.source)
            .all()
        )
        for source, last_at in snap_rows:
            per_source.setdefault(source, {
                "total": 0, "unprocessed": 0, "ok": 0, "error": 0, "review": 0,
                "last_snapshot_at": None,
            })
            per_source[source]["last_snapshot_at"] = (
                last_at.isoformat() if last_at else None
            )

        total_error = sum(s["error"] for s in per_source.values())
        total_items = sum(s["total"] for s in per_source.values())
        success_rate = (
            (1.0 - total_error / total_items) * 100.0 if total_items else 100.0
        )

        return {
            "generated_at": now.isoformat(),
            "total_items": total_items,
            "overall_success_rate_pct": round(success_rate, 2),
            "per_source": per_source,
        }
    except Exception as e:  # never let health check itself crash the caller
        logger.error(f"get_pipeline_health failed: {e}")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
            "total_items": 0,
            "overall_success_rate_pct": 0.0,
            "per_source": {},
        }
    finally:
        db.close()
