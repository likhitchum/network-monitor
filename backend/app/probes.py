"""Network probes used by the check pipeline.

Monkeypatch seam: tests replace ``subprocess``/``ssl``/``socket_create_connection``/
``get_ssl_days_left`` as attributes of ``app.main``; the helpers below resolve them
through ``app.main`` at call time so those patches keep working after the split.
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

import app.main as main
from app.database import db


def socket_create_connection(host: str, port: int, timeout: int):
    import socket
    return socket.create_connection((host, port), timeout=timeout)


def get_ssl_days_left(host: str, port: int) -> int:
    context = main.ssl.create_default_context()
    with main.socket_create_connection(host, port, timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secure:
            certificate = secure.getpeercert()
    expires = datetime.strptime(certificate["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return max(0, (expires - datetime.now(timezone.utc)).days)


async def snmp_get(device: dict[str, Any], oid: str):
    community = device.get("snmp_community") or "public"
    target = f"{device['host']}:{int(device.get('snmp_port') or 161)}"
    command = ["snmpget", "-v", "2c", "-c", community, "-t", "2", "-r", "0", "-Oqv", target, oid]
    completed = await asyncio.to_thread(main.subprocess.run, command, capture_output=True, text=True, timeout=6)
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip().splitlines()
    return value[-1].strip().strip('"') if value else None


def numeric_snmp(value):
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError):
        return None


async def perform_check(device: dict[str, Any]):
    started = time.perf_counter()
    host = device["host"]
    target = host if host.startswith(("http://", "https://")) else f"{device['monitor_type']}://{host}"
    parsed = urlparse(target)
    result = {"status": "down", "response_ms": None, "status_code": None, "ssl_days_left": None, "error": None, "sensors": {}}
    try:
        if device["monitor_type"] in ("http", "https"):
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, verify=True) as client:
                response = await client.get(target)
            result["status_code"] = response.status_code
            result["status"] = "up" if response.is_success else "warning"
            if parsed.scheme == "https":
                result["ssl_days_left"] = await asyncio.to_thread(main.get_ssl_days_left, parsed.hostname, parsed.port or 443)
        elif device["monitor_type"] in ("ping", "vpn"):
            command = ["ping", "-c", "1", "-W", "3", host]
            completed = await asyncio.to_thread(main.subprocess.run, command, capture_output=True, text=True, timeout=5)
            if completed.returncode != 0:
                raise RuntimeError("Ping failed")
            result["status"] = "up"
        elif device["monitor_type"] == "snmp":
            community = device.get("snmp_community") or "public"
            target = f"{host}:{int(device.get('snmp_port') or 161)}"
            command = ["snmpget", "-v", "2c", "-c", community, "-t", "3", "-r", "0", "-Oqv", target, "1.3.6.1.2.1.1.3.0"]
            completed = await asyncio.to_thread(main.subprocess.run, command, capture_output=True, text=True, timeout=8)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "SNMP request failed").strip().splitlines()[-1]
                raise RuntimeError(detail[:250])
            result["status"] = "up"
            sensor_oids = {"sys_uptime": "1.3.6.1.2.1.1.3.0", "cpu_percent": "1.3.6.1.2.1.25.3.3.1.2.1", "memory_available_kb": "1.3.6.1.4.1.2021.4.6.0", "memory_total_kb": "1.3.6.1.4.1.2021.4.5.0", "disk_percent": "1.3.6.1.4.1.2021.9.1.9.1"}
            sensor_results = await asyncio.gather(*(snmp_get(device, oid) for oid in sensor_oids.values()), return_exceptions=True)
            sensor_values = {key: (None if isinstance(value, BaseException) else value) for key, value in zip(sensor_oids.keys(), sensor_results)}
            if sensor_values.get("sys_uptime"):
                result["sensors"]["sys_uptime"] = sensor_values["sys_uptime"]
            cpu = numeric_snmp(sensor_values.get("cpu_percent"))
            if cpu is not None:
                result["sensors"]["cpu_percent"] = cpu
            disk = numeric_snmp(sensor_values.get("disk_percent"))
            if disk is not None:
                result["sensors"]["disk_percent"] = disk
            memory_total = numeric_snmp(sensor_values.get("memory_total_kb")); memory_available = numeric_snmp(sensor_values.get("memory_available_kb"))
            if memory_total and memory_available is not None and memory_total > 0:
                result["sensors"]["memory_percent"] = round((memory_total - memory_available) * 100 / memory_total, 2)
        else:
            result["status"] = "unknown"
            result["error"] = "Unsupported monitor type"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    result["response_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result
