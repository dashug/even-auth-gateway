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
            jobs = s.get_jobs()
            assert len(jobs) == 1
            job = jobs[0]
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
            job = s.get_jobs()[0]
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

    monkeypatch.setattr(reconcile, "run", fake_run)

    asyncio.run(sch._run_reconcile_job())

    assert len(calls) == 1
    # a real httpx.AsyncClient was constructed and passed through
    assert calls[0].__class__.__name__ == "AsyncClient"


def test_stop_scheduler_without_start_is_noop():
    sch.stop_scheduler()
    assert sch._scheduler is None
