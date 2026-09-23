import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.main as m


def _insert_metric(device_id, checked_at):
    with m.db() as connection:
        connection.execute(
            "INSERT INTO metrics(device_id,checked_at,status) VALUES(?,?,?)",
            (device_id, checked_at, "up"),
        )


def _count_old(days=30):
    with m.db() as connection:
        return connection.execute("SELECT COUNT(*) FROM metrics WHERE checked_at < datetime('now', ?)", (f"-{days} days",)).fetchone()[0]


async def test_sweep_removes_only_old_rows(client, create_device, auth_headers, perform_check_result):
    device = await create_device()
    headers = await auth_headers()
    perform_check_result.update({"status": "up", "response_ms": 5.0})
    await client.post(f"/api/devices/{device['id']}/check", headers=headers)  # fresh row via real persist path
    _insert_metric(device["id"], "2020-01-01T00:00:00+00:00")  # ancient row

    removed = m.sweep_old_metrics()
    assert removed == 1
    assert _count_old() == 0

    metrics = (await client.get(f"/api/metrics/device/{device['id']}", headers=headers)).json()
    assert len(metrics) == 1  # the fresh check survived


async def test_sweep_respects_custom_retention_days(client, create_device):
    device = await create_device()
    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    _insert_metric(device["id"], ten_days_ago)

    assert m.sweep_old_metrics(retention_days=365) == 0  # 10-day-old row is inside the window
    assert m.sweep_old_metrics(retention_days=5) == 1  # now it is outside


async def test_sweep_returns_zero_when_nothing_to_remove(client, create_device):
    device = await create_device()
    _insert_metric(device["id"], m.now())
    assert m.sweep_old_metrics(retention_days=30) == 0


async def test_retention_loop_cancels_cleanly():
    task = asyncio.create_task(m.metrics_retention_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_retention_loop_survives_db_errors(monkeypatch):
    """A failing sweep must be logged and swallowed; the loop reaches its next sleep."""
    sweep_calls = {"count": 0}

    def broken_sweep():
        sweep_calls["count"] += 1
        raise RuntimeError("db on fire")

    monkeypatch.setattr(m, "sweep_old_metrics", broken_sweep)

    second_sleep_reached = False
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        nonlocal second_sleep_reached
        if seconds >= m.RETENTION_SWEEP_HOURS * 3600:
            second_sleep_reached = True  # got past the broken sweep
            raise asyncio.CancelledError()  # end the loop deterministically
        await real_sleep(0)  # let the initial startup delay pass instantly

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        with pytest.raises(asyncio.CancelledError):
            await m.metrics_retention_loop()
    finally:
        monkeypatch.setattr(asyncio, "sleep", real_sleep)

    assert sweep_calls["count"] == 1
    assert second_sleep_reached
