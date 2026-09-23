"""Tests for the background scheduler: run_device_check, check_due_devices and scheduler_loop.

The scheduler tests use a fake monotonic clock: every asyncio.sleep() call advances
the fake clock instead of waiting, so per-device intervals are verified precisely
without real waiting.
"""

import asyncio
import logging

import pytest

import app.main as m


def make_device(**overrides):
    device = {
        "id": 1,
        "name": "Probe",
        "host": "203.0.113.10",
        "monitor_type": "ping",
        "interval_seconds": 60,
        "enabled": 1,
        "status": "unknown",
        "last_check": None,
        "last_response_ms": None,
        "ssl_days_left": None,
        "created_at": m.now(),
        "snmp_community": "public",
        "snmp_port": 161,
    }
    device.update(overrides)
    return device


def _insert_device(name, interval, host="203.0.113.55"):
    with m.db() as connection:
        cursor = connection.execute(
            "INSERT INTO devices(name,host,monitor_type,interval_seconds,enabled,created_at) VALUES(?,?,?,?,1,?)",
            (name, host, "ping", interval, m.now()),
        )
        return cursor.lastrowid


def _delete_devices(*device_ids):
    with m.db() as connection:
        connection.execute("DELETE FROM devices WHERE id IN (%s)" % ",".join("?" * len(device_ids)), device_ids)


# ------------------------------------------------------------- run_device_check


async def test_run_device_check_persists_result(monkeypatch):
    calls = []

    async def fake_persist(device, result):
        calls.append((device["id"], result["status"]))

    monkeypatch.setattr(m, "persist_result", fake_persist)
    await m.run_device_check(make_device(id=7), asyncio.Semaphore(1))
    assert calls == [(7, "up")]


async def test_run_device_check_down_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(m.logger, "propagate", True)
    monkeypatch.setattr(m, "persist_result", lambda d, r: asyncio.sleep(0))
    with caplog.at_level(logging.WARNING, logger="wwnm"):
        perform_check_result = {"status": "down", "response_ms": None, "status_code": None, "ssl_days_left": None, "error": "Ping failed", "sensors": {}}

        async def down_result(device):
            return perform_check_result

        monkeypatch.setattr(m, "perform_check", down_result)
        await m.run_device_check(make_device(id=3), asyncio.Semaphore(1))
    assert "down" in caplog.text
    assert "Ping failed" in caplog.text


async def test_run_device_check_up_logs_info(monkeypatch, caplog):
    monkeypatch.setattr(m.logger, "propagate", True)
    with caplog.at_level(logging.INFO, logger="wwnm"):
        await m.run_device_check(make_device(id=4), asyncio.Semaphore(1))
    assert "up in" in caplog.text


async def test_run_device_check_crash_is_swallowed_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(m.logger, "propagate", True)

    async def exploding(device):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(m, "perform_check", exploding)
    with caplog.at_level(logging.ERROR, logger="wwnm"):
        await m.run_device_check(make_device(id=5), asyncio.Semaphore(1))  # must not raise
    assert "check crashed" in caplog.text


async def test_run_device_check_reraises_cancellation(monkeypatch):
    async def cancelled(device):
        raise asyncio.CancelledError()

    monkeypatch.setattr(m, "perform_check", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await m.run_device_check(make_device(id=6), asyncio.Semaphore(1))


# ----------------------------------------------------------- check_due_devices


async def test_check_due_devices_empty_is_noop(monkeypatch, caplog):
    monkeypatch.setattr(m.logger, "propagate", True)
    with caplog.at_level(logging.INFO, logger="wwnm"):
        await m.check_due_devices([])
    assert "running checks" not in caplog.text


async def test_check_due_devices_logs_device_count(monkeypatch, caplog):
    monkeypatch.setattr(m.logger, "propagate", True)
    with caplog.at_level(logging.INFO, logger="wwnm"):
        await m.check_due_devices([make_device(id=1), make_device(id=2)])
    assert "running checks for 2 due device(s)" in caplog.text


async def test_check_due_devices_semaphore_limits_concurrency(monkeypatch):
    """Three devices with a 2-slot semaphore: at most 2 probes may overlap."""
    monkeypatch.setattr(m, "MAX_CONCURRENT_CHECKS", 2)
    counter = {"current": 0, "max_seen": 0}

    async def slow_perform(device):
        counter["current"] += 1
        counter["max_seen"] = max(counter["max_seen"], counter["current"])
        await asyncio.sleep(0.05)
        counter["current"] -= 1
        return {"status": "up", "response_ms": 1.0, "status_code": None, "ssl_days_left": None, "error": None, "sensors": {}}

    async def noop_persist(device, result):
        pass

    monkeypatch.setattr(m, "perform_check", slow_perform)
    monkeypatch.setattr(m, "persist_result", noop_persist)

    devices = [make_device(id=i, name=f"D{i}") for i in (1, 2, 3)]
    await m.check_due_devices(devices)

    assert counter["max_seen"] == 2  # overlapping probes capped at the semaphore size
    assert counter["current"] == 0  # and everything finished


async def test_check_due_devices_isolates_failing_device(monkeypatch, caplog):
    monkeypatch.setattr(m.logger, "propagate", True)

    async def flaky_persist(device, result):
        if device["id"] == 2:
            raise RuntimeError("persist exploded")

    monkeypatch.setattr(m, "persist_result", flaky_persist)
    devices = [make_device(id=i, name=f"D{i}") for i in (1, 2, 3)]
    with caplog.at_level(logging.ERROR, logger="wwnm"):
        await m.check_due_devices(devices)  # must not raise
    assert "check crashed" in caplog.text


# -------------------------------------------------------------- scheduler_loop


async def test_scheduler_respects_per_device_intervals(monkeypatch):
    """A(60s), B(15s->floored to 30s) and C(24h) on a fake clock.

    Expected timeline from t=1000: both A and B run immediately, B runs alone after
    its floored interval, then A and B coincide again at 60s. C never runs again.
    """
    ticks = []

    async def fake_check_due(devices):
        ticks.append([d["id"] for d in devices])

    monkeypatch.setattr(m, "check_due_devices", fake_check_due)
    monkeypatch.setattr(m, "POLL_SECONDS", 30)
    monkeypatch.setattr(m, "CHECK_TICK_SECONDS", 5)

    a = _insert_device("A", 60)
    b = _insert_device("B", 15)  # below the floor -> effectively 30s
    c = _insert_device("C", 86400)
    try:
        clock = {"t": 1000.0}
        monkeypatch.setattr(m.time, "monotonic", lambda: clock["t"])

        real_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            clock["t"] += seconds
            await real_sleep(0)  # let the scheduled check_due task run before deciding to stop
            if len([t for t in ticks if a in t]) >= 2:
                raise asyncio.CancelledError()  # deterministic stop

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        with pytest.raises(asyncio.CancelledError):
            await m.scheduler_loop()

        assert set(ticks[0]) >= {a, b, c}, ticks  # everything is due on the first tick
        assert [t for t in ticks if t == [b]] == [[b]], ticks  # exactly one solo B run
        a_indices = [i for i, t in enumerate(ticks) if a in t]
        assert a_indices == [0, len(ticks) - 1], ticks  # A only at the start and at 60s
        assert all(c not in t for t in ticks[1:]), ticks  # C never repeats
        assert len(ticks) >= 10  # several empty ticks between the three real ones
    finally:
        _delete_devices(a, b, c)


async def test_scheduler_survives_database_errors(monkeypatch, caplog):
    device_id = _insert_device("Flaky", 30)
    ticks = []

    async def fake_check_due(devices):
        ticks.append([d["id"] for d in devices])

    monkeypatch.setattr(m, "check_due_devices", fake_check_due)
    monkeypatch.setattr(m.logger, "propagate", True)

    real_db = m.db
    attempts = {"n": 0}

    def flaky_db():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("db temporarily down")
        return real_db()

    monkeypatch.setattr(m, "db", flaky_db)

    clock = {"t": 500.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: clock["t"])

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        clock["t"] += seconds
        await real_sleep(0)
        if ticks:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    try:
        with pytest.raises(asyncio.CancelledError):
            await m.scheduler_loop()
    finally:
        _delete_devices(device_id)

    assert attempts["n"] >= 3  # failed twice, then the loop kept going
    assert set(ticks[0]) >= {device_id}  # and still picked up the device
    assert caplog.text.count("scheduler loop iteration failed") >= 2
