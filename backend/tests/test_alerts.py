async def _run_check(client, device_id, headers):
    return (await client.post(f"/api/devices/{device_id}/check", headers=headers)).json()


async def test_down_check_creates_critical_alert(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    perform_check_result.update({"status": "down", "error": "Ping failed"})
    device = await create_device()

    await _run_check(client, device["id"], headers)
    alerts = (await client.get("/api/alerts", headers=headers)).json()
    down_alerts = [a for a in alerts if a["device_id"] == device["id"] and a["alert_type"] == "device_down"]
    assert len(down_alerts) == 1
    assert down_alerts[0]["severity"] == "critical"
    assert down_alerts[0]["active"] == 1
    assert down_alerts[0]["device_name"] == device["name"]


async def test_same_condition_does_not_duplicate_alerts(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    perform_check_result.update({"status": "down", "error": "Ping failed"})
    device = await create_device()

    await _run_check(client, device["id"], headers)
    await _run_check(client, device["id"], headers)
    await _run_check(client, device["id"], headers)

    alerts = (await client.get("/api/alerts", headers=headers)).json()
    assert len([a for a in alerts if a["device_id"] == device["id"]]) == 1


async def test_recovery_resolves_alert(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    device = await create_device()

    perform_check_result.update({"status": "down", "error": "Ping failed"})
    await _run_check(client, device["id"], headers)

    perform_check_result.update({"status": "up", "error": None})
    await _run_check(client, device["id"], headers)

    alerts = (await client.get("/api/alerts", headers=headers)).json()
    assert all(a["active"] == 0 for a in alerts if a["device_id"] == device["id"])


async def test_warning_http_status_creates_warning_alert(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    perform_check_result.update({"status": "warning", "status_code": 404, "response_ms": 120.0})
    device = await create_device(monitor_type="http", host="http://example.test")

    await _run_check(client, device["id"], headers)
    alerts = (await client.get("/api/alerts", headers=headers)).json()
    http_alerts = [a for a in alerts if a["device_id"] == device["id"] and a["alert_type"] == "http_status"]
    assert len(http_alerts) == 1
    assert http_alerts[0]["severity"] == "warning"
    assert "404" in http_alerts[0]["message"]


async def test_ssl_expiring_soon_creates_critical_alert(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    perform_check_result.update({"status": "up", "ssl_days_left": 5})
    device = await create_device(monitor_type="https", host="https://example.test")

    await _run_check(client, device["id"], headers)
    alerts = (await client.get("/api/alerts", headers=headers)).json()
    ssl_alerts = [a for a in alerts if a["device_id"] == device["id"] and a["alert_type"] == "ssl_expiring"]
    assert len(ssl_alerts) == 1
    assert ssl_alerts[0]["severity"] == "critical"


async def test_ssl_far_from_expiry_no_alert(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    perform_check_result.update({"status": "up", "ssl_days_left": 120})
    device = await create_device(monitor_type="https", host="https://example.test")

    await _run_check(client, device["id"], headers)
    alerts = (await client.get("/api/alerts", headers=headers)).json()
    assert not [a for a in alerts if a["device_id"] == device["id"]]


async def test_snmp_threshold_alerts(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    perform_check_result.update({"status": "up", "sensors": {"cpu_percent": 95.0, "disk_percent": 96.0}})
    device = await create_device(monitor_type="snmp", host="10.0.0.5")

    await _run_check(client, device["id"], headers)
    alerts = (await client.get("/api/alerts", headers=headers)).json()
    types = {a["alert_type"]: a["severity"] for a in alerts if a["device_id"] == device["id"]}
    assert types.get("snmp_cpu_percent") == "warning"
    assert types.get("snmp_disk_percent") == "critical"


async def test_manual_resolve_alert(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    perform_check_result.update({"status": "down", "error": "Ping failed"})
    device = await create_device()
    await _run_check(client, device["id"], headers)

    alerts = (await client.get("/api/alerts", headers=headers)).json()
    alert_id = next(a["id"] for a in alerts if a["device_id"] == device["id"])

    response = await client.post(f"/api/alerts/{alert_id}/resolve", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"

    # Resolving an already-resolved alert fails.
    response = await client.post(f"/api/alerts/{alert_id}/resolve", headers=headers)
    assert response.status_code == 404


async def test_resolve_unknown_alert_returns_404(client, auth_headers):
    response = await client.post("/api/alerts/999999/resolve", headers=await auth_headers())
    assert response.status_code == 404


async def test_alerts_require_auth(client):
    response = await client.get("/api/alerts")
    assert response.status_code == 401


async def test_summary_reflects_devices_and_alerts(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    up_device = await create_device(name="UpDev")
    down_device = await create_device(name="DownDev")
    perform_check_result.update({"status": "up"})
    await _run_check(client, up_device["id"], headers)
    perform_check_result.update({"status": "down", "error": "Ping failed"})
    await _run_check(client, down_device["id"], headers)

    summary = (await client.get("/api/summary", headers=headers)).json()
    assert summary["total_devices"] >= 2
    assert summary["online"] >= 1
    assert summary["critical"] >= 1
    assert summary["active_alerts"] >= 1
