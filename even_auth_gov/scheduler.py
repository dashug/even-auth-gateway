"""定时任务:
- 每日对账(reconcile.run,SSO_RECONCILE_CRON,默认每天 08:00):兜底飞书离职事件遗漏;
  跑完再做一遍权限核对(access_report.alert),高/中级异常推给全局审批人(ACCESS_REPORT_ALERT=false 关);
- 待批扫描(reconcile.sweep_pending,SSO_PENDING_SWEEP_CRON,默认每小时第 30 分):
  补发没送达的审批卡片、催办、超时升级 —— 不必等到第二天对账。

Module-level singleton AsyncIOScheduler; timezone from SCHEDULER_TIMEZONE (default Asia/Shanghai).
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
PENDING_JOB_ID = "sso_pending_sweep"


def _default_cron() -> str:
    return os.getenv("SSO_RECONCILE_CRON", "0 8 * * *")


def _pending_cron() -> str:
    return os.getenv("SSO_PENDING_SWEEP_CRON", "30 * * * *")


def _default_timezone() -> str:
    return os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")


def _alert_enabled() -> bool:
    return os.getenv("ACCESS_REPORT_ALERT", "true").strip().lower() not in ("", "0", "false", "no")


async def _run_reconcile_job() -> None:
    """Job body: open a fresh Casdoor client, run reconcile, then the access check; always close it.
    权限核对放在对账之后(对账刚禁用的人不会被误报),失败只记日志,不影响对账本身。"""
    import httpx
    from even_auth_gov import access_report, reconcile, settings

    async with httpx.AsyncClient(base_url=settings.casdoor_endpoint()) as client:
        await reconcile.run(client)
        if _alert_enabled():
            try:
                await access_report.alert(client)
            except Exception as e:
                logger.warning("每日权限核对失败: %s", e)


async def _run_pending_job() -> None:
    """Job body: 待批补发/催办/超时升级(不碰 Casdoor,只读写审批库 + 发飞书卡片)。"""
    from even_auth_gov import reconcile

    await reconcile.sweep_pending()


def is_running() -> bool:
    """对账调度器是否在跑(/healthz 用)。"""
    return _scheduler is not None and bool(_scheduler.running)


def start_scheduler() -> AsyncIOScheduler:
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
    pending_cron = _pending_cron()
    scheduler.add_job(
        _run_pending_job,
        trigger=CronTrigger.from_crontab(pending_cron, timezone=tz),
        id=PENDING_JOB_ID,
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Scheduler started: reconcile cron=%r, pending sweep cron=%r, tz=%r", cron, pending_cron, tz)
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
