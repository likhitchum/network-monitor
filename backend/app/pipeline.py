"""Check pipeline: persistence + alert engine + background loops.

Monkeypatch seam: tests patch ``perform_check``/``persist_result``/``notify`` and the
tuning constants (``POLL_SECONDS``/``CHECK_TICK_SECONDS``/``MAX_CONCURRENT_CHECKS``/
``METRICS_RETENTION_DAYS``/``RETENTION_SWEEP_HOURS``) on ``app.main``; every lookup
below goes through the main module at call time so patches stay effective.
"""
import asyncio
import json
import logging
import time
from typing import Any

import app.main as main
from app.database import db, now
from app.notifications import notify
from app.schemas import ThresholdSettings

logger = logging.getLogger("wwnm")


async def persist_result(device: dict[str, Any], result: dict[str, Any]):
    alerts = []
    if result["status"] == "down":
        alerts.append(("critical", "device_down", result.get("error") or "Device is unreachable"))
    elif result["status"] == "warning":
        alerts.append(("warning", "http_status", f"HTTP status {result['status_code']}"))
    if result.get("ssl_days_left") is not None and result["ssl_days_left"] <= 30:
        alerts.append(("warning" if result["ssl_days_left"] > 7 else "critical", "ssl_expiring", f"SSL certificate expires in {result['ssl_days_left']} days"))
    sensors = result.get("sensors", {})
    thresholds = ThresholdSettings().model_dump()
    with db() as connection:
        threshold_rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'threshold.%'").fetchall()
    for row in threshold_rows:
        key = row["key"].removeprefix("threshold.")
        if key in thresholds:
            thresholds[key] = float(row["value"])
    for key, severity in (("cpu_percent", "warning"), ("memory_percent", "warning"), ("disk_percent", "critical")):
        threshold = thresholds[key]
        value = sensors.get(key)
        if isinstance(value, (int, float)) and value >= threshold:
            label = key.replace("_percent", "").upper()
            alerts.append((severity, f"snmp_{key}", f"SNMP {label} usage is {value}% (threshold {threshold}%)"))
    active_types = {alert[1] for alert in alerts}
    with db() as connection:
        connection.execute("UPDATE devices SET status=?,last_check=?,last_response_ms=?,ssl_days_left=? WHERE id=?", (result["status"], now(), result["response_ms"], result.get("ssl_days_left"), device["id"]))
        connection.execute("INSERT INTO metrics(device_id,checked_at,status,response_ms,status_code,ssl_days_left,error,sensor_json) VALUES(?,?,?,?,?,?,?,?)", (device["id"], now(), result["status"], result["response_ms"], result.get("status_code"), result.get("ssl_days_left"), result.get("error"), json.dumps(sensors)))
        if active_types:
            placeholders = ",".join("?" for _ in active_types)
            connection.execute(f"UPDATE alerts SET active=0,resolved_at=? WHERE device_id=? AND active=1 AND alert_type NOT IN ({placeholders})", (now(), device["id"], *active_types))
        else:
            connection.execute("UPDATE alerts SET active=0,resolved_at=? WHERE device_id=? AND active=1", (now(), device["id"]))
        for severity, alert_type, message in alerts:
            active = connection.execute("SELECT id FROM alerts WHERE device_id=? AND alert_type=? AND active=1", (device["id"], alert_type)).fetchone()
            if not active:
                connection.execute("INSERT INTO alerts(device_id,severity,alert_type,message,created_at) VALUES(?,?,?,?,?)", (device["id"], severity, alert_type, message, now()))
                await main.notify(device, severity, message)


async def run_device_check(device: dict[str, Any], semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        try:
            result = await main.perform_check(device)
            await main.persist_result(device, result)
            if result["status"] == "up":
                logger.info("device %s (%s) up in %sms", device["id"], device["name"], result["response_ms"])
            else:
                logger.warning("device %s (%s) %s: %s", device["id"], device["name"], result["status"], result.get("error") or "check reported degraded state")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device %s (%s) check crashed", device["id"], device["name"])


async def check_due_devices(devices: list[dict[str, Any]]) -> None:
    if not devices:
        return
    semaphore = asyncio.Semaphore(main.MAX_CONCURRENT_CHECKS)
    logger.info("running checks for %d due device(s)", len(devices))
    results = await asyncio.gather(*(main.run_device_check(device, semaphore) for device in devices), return_exceptions=True)
    for device, outcome in zip(devices, results):
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            logger.error("device %s (%s) check task failed: %r", device["id"], device["name"], outcome)


async def scheduler_loop() -> None:
    """Run each enabled device on its own interval_seconds schedule."""
    await asyncio.sleep(2)
    next_run: dict[int, float] = {}
    while True:
        try:
            with main.db() as connection:
                devices = [dict(row) for row in connection.execute("SELECT * FROM devices WHERE enabled=1")]
            current = main.time.monotonic()
            due: list[dict[str, Any]] = []
            seen: set[int] = set()
            for device in devices:
                device_id = device["id"]
                seen.add(device_id)
                interval = max(main.POLL_SECONDS, int(device.get("interval_seconds") or main.POLL_SECONDS))
                if current >= next_run.get(device_id, 0):
                    due.append(device)
                    next_run[device_id] = current + interval
            for stale_id in set(next_run) - seen:
                next_run.pop(stale_id, None)
            asyncio.create_task(main.check_due_devices(due))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler loop iteration failed")
        await asyncio.sleep(main.CHECK_TICK_SECONDS)


# Backwards-compatible alias for the original serial loop name.
monitor_loop = scheduler_loop


def sweep_old_metrics(retention_days: int | None = None) -> int:
    """Delete metric rows older than the given number of days; return row count."""
    if retention_days is None:
        retention_days = main.METRICS_RETENTION_DAYS
    with db() as connection:
        cursor = connection.execute("DELETE FROM metrics WHERE checked_at < datetime('now', ?)", (f"-{retention_days} days",))
    if cursor.rowcount:
        logger.info("metrics retention: removed %d row(s) older than %d days", cursor.rowcount, retention_days)
    return cursor.rowcount


async def metrics_retention_loop() -> None:
    """Periodically delete metric rows older than METRICS_RETENTION_DAYS."""
    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.to_thread(main.sweep_old_metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("metrics retention sweep failed")
        await asyncio.sleep(main.RETENTION_SWEEP_HOURS * 3600)
