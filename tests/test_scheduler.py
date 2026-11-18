import asyncio

from even_auth_gov import scheduler as sch, reconcile


def _teardown():
    sch.stop_scheduler()
    assert sch._scheduler is None


def test_start_scheduler_registers_reconcile_job_with_cron(monkeypatch):
    monkeypatch.setenv("SSO_RECONCILE_CRON", "30 3 * * *")
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")

    async def _go():
        s = sch.start_scheduler()
        try:
            jobs = {j.id: j for j in s.get_jobs()}
            assert set(jobs) == {sch.RECONCILE_JOB_ID, sch.PENDING_JOB_ID}
            job = jobs[sch.RECONCILE_JOB_ID]
            assert job.id == sch.RECONCILE_JOB_ID
            assert job.func is sch._run_reconcile_job

            fields = {f.name: str(f) for f in job.trigger.fields}
            assert fields["hour"] == "3"
            assert fields["minute"] == "30"
            assert str(job.trigger.timezone) == "Asia/Shanghai"

            # idempotent: calling again while running returns the same instance
            s2 = sch.start_scheduler()
            assert s2 is s
        finally:
            _teardown()

    asyncio.run(_go())


def test_start_scheduler_uses_default_cron_and_timezone(monkeypatch):
    monkeypatch.delenv("SSO_RECONCILE_CRON", raising=False)
    monkeypatch.delenv("SCHEDULER_TIMEZONE", raising=False)

    async def _go():
        s = sch.start_scheduler()
        try:
            job = s.get_job(sch.RECONCILE_JOB_ID)
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert fields["hour"] == "8"
            assert fields["minute"] == "0"
            assert str(job.trigger.timezone) == "Asia/Shanghai"
        finally:
            _teardown()

    asyncio.run(_go())


def test_reconcile_job_calls_reconcile_run_with_fresh_client(monkeypatch):
    calls = []

    async def fake_run(client):
        calls.append(client)

    async def fake_alert(client):
        return 0

    monkeypatch.setattr(reconcile, "run", fake_run)
    monkeypatch.setattr("even_auth_gov.access_report.alert", fake_alert)

    asyncio.run(sch._run_reconcile_job())

    assert len(calls) == 1
    # a real httpx.AsyncClient was constructed and passed through
    assert calls[0].__class__.__name__ == "AsyncClient"


def test_stop_scheduler_without_start_is_noop():
    sch.stop_scheduler()
    assert sch._scheduler is None


def test_pending_sweep_job_runs_hourly_by_default_and_is_configurable(monkeypatch):
    """待批补发/催办/超时升级每小时扫一次(原来只跟每日对账一起跑,发失败的卡片要等到第二天)。"""
    async def _go(cron, expect_minute, expect_hour):
        if cron is None:
            monkeypatch.delenv("SSO_PENDING_SWEEP_CRON", raising=False)
        else:
            monkeypatch.setenv("SSO_PENDING_SWEEP_CRON", cron)
        s = sch.start_scheduler()
        try:
            job = s.get_job(sch.PENDING_JOB_ID)
            assert job.func is sch._run_pending_job
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert fields["minute"] == expect_minute and fields["hour"] == expect_hour
        finally:
            _teardown()
    asyncio.run(_go(None, "30", "*"))
    asyncio.run(_go("*/10 9-18 * * *", "*/10", "9-18"))


def test_pending_job_runs_pending_sweep_then_account_sweep(monkeypatch):
    from even_auth_gov import account_lifecycle
    order = []
    async def fake_pending(): order.append("pending")
    async def fake_accounts(client): order.append("accounts")
    monkeypatch.setattr(reconcile, "sweep_pending", fake_pending)
    monkeypatch.setattr(account_lifecycle, "sweep", fake_accounts)
    asyncio.run(sch._run_pending_job())
    assert order == ["pending", "accounts"]


def test_pending_job_survives_account_sweep_failure(monkeypatch):
    from even_auth_gov import account_lifecycle
    async def fake_pending(): pass
    async def boom(client): raise RuntimeError("casdoor down")
    monkeypatch.setattr(reconcile, "sweep_pending", fake_pending)
    monkeypatch.setattr(account_lifecycle, "sweep", boom)
    asyncio.run(sch._run_pending_job())   # 不抛


def test_reconcile_job_pushes_access_report_after_reconcile(monkeypatch):
    """对账跑完再核对一遍权限(对账刚禁用的人不会被误报);核对失败不影响对账。"""
    order = []

    async def fake_run(client):
        order.append("reconcile")

    async def fake_alert(client):
        order.append("alert")
        raise RuntimeError("casdoor down")

    monkeypatch.setattr(reconcile, "run", fake_run)
    monkeypatch.setattr("even_auth_gov.access_report.alert", fake_alert)
    asyncio.run(sch._run_reconcile_job())          # 不抛
    assert order == ["reconcile", "alert"]

    order.clear()
    monkeypatch.setenv("ACCESS_REPORT_ALERT", "false")
    asyncio.run(sch._run_reconcile_job())
    assert order == ["reconcile"]
