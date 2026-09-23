"""Hermetic tests for the real perform_check() network probes.

The conftest autouse fixture stubs ``m.perform_check`` for the whole suite, so we
capture the real function at import time (before fixtures run) and use that.
httpx, subprocess and the SSL layer are mocked; no packet ever leaves the machine.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import app.main as m

real_perform_check = m.perform_check


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


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class FakeAsyncClient:
    """Async context manager mimicking httpx.AsyncClient for the probe path."""

    last_request = None

    def __init__(self, handler=None, **kwargs):
        self.handler = handler
        type(self).last_request = {"kwargs": kwargs, "url": None}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        type(self).last_request["url"] = url
        if self.handler is not None:
            return self.handler(url)
        return FakeResponse(200)


def fake_subprocess(monkeypatch, commands):
    """commands: mapping from (tool, last-arg) -> (returncode, stdout, stderr).
    Tool is 'ping' or 'snmpget'; last-arg is the host for ping, the OID for snmpget."""

    def fake_run(command, **kwargs):
        tool, key = command[0], command[-1]
        entry = commands.get((tool, key), (1, "", "no such command entry"))
        return SimpleNamespace(returncode=entry[0], stdout=entry[1], stderr=entry[2])

    monkeypatch.setattr(m, "subprocess", SimpleNamespace(run=fake_run))


SNMP_OIDS = {
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "cpu_percent": "1.3.6.1.2.1.25.3.3.1.2.1",
    "memory_available_kb": "1.3.6.1.4.1.2021.4.6.0",
    "memory_total_kb": "1.3.6.1.4.1.2021.4.5.0",
    "disk_percent": "1.3.6.1.4.1.2021.9.1.9.1",
}


def snmp_commands(overrides=None):
    base = {
        ("snmpget", SNMP_OIDS["sys_uptime"]): (0, "13:02:10.55", ""),
        ("snmpget", SNMP_OIDS["cpu_percent"]): (0, "42", ""),
        ("snmpget", SNMP_OIDS["memory_available_kb"]): (0, "2000000", ""),
        ("snmpget", SNMP_OIDS["memory_total_kb"]): (0, "8000000", ""),
        ("snmpget", SNMP_OIDS["disk_percent"]): (0, "37", ""),
    }
    if overrides:
        base.update(overrides)
    return base


# ---------------------------------------------------------------- HTTP / HTTPS


@pytest.mark.parametrize("code,expected", [(200, "up"), (204, "up"), (404, "warning"), (503, "warning")])
async def test_http_status_mapping(monkeypatch, code, expected):
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: FakeAsyncClient(handler=lambda url: FakeResponse(code)))
    result = await real_perform_check(make_device(monitor_type="http", host="http://example.test"))
    assert result["status"] == expected
    assert result["status_code"] == code
    assert result["response_ms"] > 0


async def test_http_target_gets_scheme_from_monitor_type(monkeypatch):
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())
    await real_perform_check(make_device(monitor_type="http", host="203.0.113.10"))
    assert FakeAsyncClient.last_request["url"] == "http://203.0.113.10"


async def test_http_connection_error_is_down(monkeypatch):
    def refused(url):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: FakeAsyncClient(handler=refused))
    result = await real_perform_check(make_device(monitor_type="http", host="http://example.test"))
    assert result["status"] == "down"
    assert "Connection refused" in result["error"]


async def test_https_success_reports_ssl_days(monkeypatch):
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())
    monkeypatch.setattr(m, "get_ssl_days_left", lambda host, port: 90)
    result = await real_perform_check(make_device(monitor_type="https", host="https://example.test"))
    assert result["status"] == "up"
    assert result["ssl_days_left"] == 90


async def test_http_never_probes_ssl(monkeypatch):
    def boom(host, port):
        raise AssertionError("SSL probe must not run for plain http")

    monkeypatch.setattr(m.httpx, "AsyncClient", lambda **kw: FakeAsyncClient())
    monkeypatch.setattr(m, "get_ssl_days_left", boom)
    result = await real_perform_check(make_device(monitor_type="http", host="http://example.test"))
    assert result["ssl_days_left"] is None


# ------------------------------------------------------------------------ PING


async def test_ping_success(monkeypatch):
    fake_subprocess(monkeypatch, {("ping", "203.0.113.10"): (0, "", "")})
    result = await real_perform_check(make_device(monitor_type="ping", host="203.0.113.10"))
    assert result["status"] == "up"
    assert result["error"] is None


async def test_ping_failure_is_down(monkeypatch):
    fake_subprocess(monkeypatch, {("ping", "203.0.113.10"): (1, "", "")})
    result = await real_perform_check(make_device(monitor_type="ping", host="203.0.113.10"))
    assert result["status"] == "down"
    assert result["error"] == "Ping failed"


async def test_vpn_uses_ping_probe(monkeypatch):
    fake_subprocess(monkeypatch, {("ping", "10.8.0.1"): (0, "", "")})
    result = await real_perform_check(make_device(monitor_type="vpn", host="10.8.0.1"))
    assert result["status"] == "up"


async def test_ping_probe_missing_binary_is_down(monkeypatch):
    def no_ping(command, **kwargs):
        raise FileNotFoundError("ping binary missing")

    monkeypatch.setattr(m, "subprocess", SimpleNamespace(run=no_ping))
    result = await real_perform_check(make_device(monitor_type="ping", host="203.0.113.10"))
    assert result["status"] == "down"
    assert result["error"]


# ------------------------------------------------------------------------ SNMP


async def test_snmp_success_collects_all_sensors(monkeypatch):
    fake_subprocess(monkeypatch, snmp_commands())
    result = await real_perform_check(make_device(monitor_type="snmp", host="10.0.0.5"))
    assert result["status"] == "up"
    sensors = result["sensors"]
    assert sensors["sys_uptime"] == "13:02:10.55"
    assert sensors["cpu_percent"] == 42.0
    assert sensors["disk_percent"] == 37.0
    assert sensors["memory_percent"] == 75.0  # (8_000_000-2_000_000)/8_000_000


async def test_snmp_memory_rounding(monkeypatch):
    commands = snmp_commands({
        ("snmpget", SNMP_OIDS["memory_available_kb"]): (0, "3333333", ""),
        ("snmpget", SNMP_OIDS["memory_total_kb"]): (0, "7000000", ""),
    })
    fake_subprocess(monkeypatch, commands)
    result = await real_perform_check(make_device(monitor_type="snmp", host="10.0.0.5"))
    expected = round((7000000 - 3333333) * 100 / 7000000, 2)
    assert result["sensors"]["memory_percent"] == expected


async def test_snmp_main_probe_failure_is_down_with_detail(monkeypatch):
    commands = snmp_commands({("snmpget", SNMP_OIDS["sys_uptime"]): (1, "", "Timeout\nNo Response from 10.0.0.5")})
    fake_subprocess(monkeypatch, commands)
    result = await real_perform_check(make_device(monitor_type="snmp", host="10.0.0.5"))
    assert result["status"] == "down"
    assert result["error"] == "No Response from 10.0.0.5"


async def test_snmp_partial_sensor_failure_keeps_rest(monkeypatch):
    commands = snmp_commands({("snmpget", SNMP_OIDS["cpu_percent"]): (1, "", "No Such Object")})
    fake_subprocess(monkeypatch, commands)
    result = await real_perform_check(make_device(monitor_type="snmp", host="10.0.0.5"))
    assert result["status"] == "up"
    assert "cpu_percent" not in result["sensors"]
    assert result["sensors"]["disk_percent"] == 37.0


async def test_snmp_threshold_alert_end_to_end(client, create_device, auth_headers, monkeypatch):
    """Full path: real perform_check (mocked snmpget) -> persist_result -> alert row."""
    commands = snmp_commands({
        ("snmpget", SNMP_OIDS["cpu_percent"]): (0, "95", ""),
        ("snmpget", SNMP_OIDS["disk_percent"]): (0, "96", ""),
    })
    fake_subprocess(monkeypatch, commands)
    # The autouse conftest fixture stubs m.perform_check for the suite; restore the
    # real probe so the HTTP route exercises the full mocked-snmp pipeline.
    monkeypatch.setattr(m, "perform_check", real_perform_check)
    device = await create_device(monitor_type="snmp", host="10.0.0.5")
    headers = await auth_headers()

    response = await client.post(f"/api/devices/{device['id']}/check", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "up"
    assert body["sensors"]["cpu_percent"] == 95.0

    alerts = (await client.get("/api/alerts", headers=headers)).json()
    types = {a["alert_type"]: a["severity"] for a in alerts if a["device_id"] == device["id"]}
    assert types.get("snmp_cpu_percent") == "warning"
    assert types.get("snmp_disk_percent") == "critical"


# ------------------------------------------------------------------ SSL helper


class FakeSecureSocket:
    def __init__(self, not_after):
        self.not_after = not_after

    def getpeercert(self):
        return {"notAfter": self.not_after}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeSSLContext:
    def __init__(self, not_after):
        self.not_after = not_after

    def wrap_socket(self, raw, server_hostname=None):
        return FakeSecureSocket(self.not_after)


def install_fake_ssl(monkeypatch, not_after):
    monkeypatch.setattr(m, "ssl", SimpleNamespace(create_default_context=lambda: FakeSSLContext(not_after)))
    monkeypatch.setattr(m, "socket_create_connection", lambda host, port, timeout: FakeSecureSocket(not_after))


async def test_get_ssl_days_left_future_certificate(monkeypatch):
    install_fake_ssl(monkeypatch, "Jun 30 12:00:00 2028 GMT")
    days = await asyncio.to_thread(m.get_ssl_days_left, "example.test", 443)
    assert days > 500


async def test_get_ssl_days_left_expired_certificate_clamps_to_zero(monkeypatch):
    install_fake_ssl(monkeypatch, "Jan 01 00:00:00 2020 GMT")
    days = await asyncio.to_thread(m.get_ssl_days_left, "example.test", 443)
    assert days == 0


# ------------------------------------------------------------- unknown monitor


async def test_unsupported_monitor_type_reports_unknown():
    with m.db() as connection:
        cursor = connection.execute(
            "INSERT INTO devices(name,host,monitor_type,created_at) VALUES('weird','10.9.9.9','carrier-pigeon',?)",
            (m.now(),),
        )
        device = dict(connection.execute("SELECT * FROM devices WHERE id=?", (cursor.lastrowid,)).fetchone())
    result = await real_perform_check(device)
    assert result["status"] == "unknown"
    assert result["error"] == "Unsupported monitor type"
    # clean up the odd row so later tests are not affected
    with m.db() as connection:
        connection.execute("DELETE FROM devices WHERE id=?", (device["id"],))
