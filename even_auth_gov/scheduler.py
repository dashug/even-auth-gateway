"""每日对账调度:定时触发 reconcile.run,兜底飞书事件遗漏。

Module-level singleton AsyncIOScheduler. Cron from SSO_RECONCILE_CRON env
(default daily 08:00), timezone from SCHEDULER_TIMEZONE env (default
Asia/Shanghai).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None

RECONCILE_JOB_ID = "sso_daily_reconcile"


def _default_cron() -> str:
    return os.getenv("SSO_RECONCILE_CRON", "0 8 * * *")


def _default_timezone() -> str:
    return os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")


async def _run_reconcile_job() -> None:
    """Job body: open a fresh Casdoor client, run reconcile, always close it."""
    import httpx
    from even_auth_gov import reconcile, settings

    async with httpx.AsyncClient(base_url=settings.casdoor_endpoint()) as client:
        await reconcile.run(client)


def start_scheduler(db=None, kb=None) -> AsyncIOScheduler:
    """Start the daily reconcile scheduler (idempotent — returns existing instance if running)."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.info("Reconcile scheduler already running")
        return _scheduler

    cron = _default_cron()
    tz = _default_timezone()

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        _run_reconcile_job,
        trigger=CronTrigger.from_crontab(cron, timezone=tz),
        id=RECONCILE_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Reconcile scheduler started: cron=%r tz=%r", cron, tz)
    return scheduler


def stop_scheduler() -> None:
    """Stop the scheduler if running."""
    global _scheduler
    if _scheduler is not None:
        try:
            if _scheduler.running:
                _scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Error shutting down reconcile scheduler")
        _scheduler = None
        logger.info("Reconcile scheduler stopped")
