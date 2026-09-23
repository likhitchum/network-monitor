async def test_thresholds_defaults(client, auth_headers):
    response = await client.get("/api/settings/thresholds", headers=await auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["cpu_percent"] == 80
    assert body["memory_percent"] == 85
    assert body["disk_percent"] == 90
    assert body["response_ms"] == 2000
    assert body["ssl_days"] == 30


async def test_thresholds_save_and_reload(client, auth_headers):
    headers = await auth_headers()
    response = await client.put(
        "/api/settings/thresholds",
        json={"cpu_percent": 70, "memory_percent": 75, "disk_percent": 95, "response_ms": 1500, "ssl_days": 14},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "saved"

    body = (await client.get("/api/settings/thresholds", headers=headers)).json()
    assert body["cpu_percent"] == 70
    assert body["memory_percent"] == 75
    assert body["disk_percent"] == 95
    assert body["response_ms"] == 1500
    assert body["ssl_days"] == 14


async def test_thresholds_reject_out_of_range(client, auth_headers):
    response = await client.put("/api/settings/thresholds", json={"cpu_percent": 150}, headers=await auth_headers())
    assert response.status_code == 422


async def test_thresholds_require_admin(client, user_token):
    response = await client.put(
        "/api/settings/thresholds",
        json={"cpu_percent": 70},
        headers={"Authorization": f"Bearer {await user_token()}"},
    )
    assert response.status_code == 403


async def test_notification_settings_roundtrip_without_echoing_password(client, auth_headers):
    headers = await auth_headers()
    response = await client.put(
        "/api/settings/notifications",
        json={"smtp_host": "smtp.example.test", "smtp_port": 587, "smtp_username": "monitor", "smtp_password": "s3cret", "smtp_from": "a@b.test", "recipients": ["ops@b.test"]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["smtp_configured"] is True

    body = (await client.get("/api/settings/notifications", headers=headers)).json()
    assert body["smtp_host"] == "smtp.example.test"
    assert "smtp_password" not in body


async def test_notification_password_survives_empty_resubmit(client, auth_headers):
    """Regression: the form always submits an empty password (GET never echoes it),
    so saving other fields must not wipe the stored credential."""
    headers = await auth_headers()
    await client.put(
        "/api/settings/notifications",
        json={"smtp_host": "smtp.example.test", "smtp_port": 587, "smtp_username": "monitor", "smtp_password": "s3cret", "smtp_from": "a@b.test", "recipients": ["ops@b.test"]},
        headers=headers,
    )
    response = await client.put(
        "/api/settings/notifications",
        json={"smtp_host": "smtp2.example.test", "smtp_port": 465, "smtp_username": "monitor", "smtp_password": "", "smtp_from": "a@b.test", "recipients": ["ops2@b.test"]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["smtp_configured"] is True  # password kept -> still configured

    import sqlite3
    import os
    import app.main as m

    with m.db() as connection:
        stored = connection.execute("SELECT value FROM settings WHERE key='notification.smtp_password'").fetchone()["value"]
    assert stored == "s3cret"


async def test_notification_settings_require_admin(client, user_token):
    response = await client.put(
        "/api/settings/notifications",
        json={"smtp_host": "x"},
        headers={"Authorization": f"Bearer {await user_token()}"},
    )
    assert response.status_code == 403


async def test_notification_test_fails_without_config(client, auth_headers):
    import app.main as m

    # The suite shares one database, so drop any config left by other tests.
    with m.db() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'notification.%'")
    response = await client.post("/api/settings/notifications/test", headers=await auth_headers())
    assert response.status_code == 400


async def test_notification_test_sends_when_configured(client, auth_headers, monkeypatch):
    import app.main as m

    headers = await auth_headers()
    await client.put(
        "/api/settings/notifications",
        json={"smtp_host": "smtp.example.test", "smtp_port": 587, "smtp_username": "monitor", "smtp_password": "s3cret", "smtp_from": "a@b.test", "recipients": ["ops@b.test"]},
        headers=headers,
    )
    calls = []

    def fake_send_email(settings, recipients, subject, body):
        calls.append((settings, recipients, subject))

    monkeypatch.setattr(m, "send_email", fake_send_email)
    response = await client.post("/api/settings/notifications/test", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert calls and calls[0][1] == ["ops@b.test"]


async def test_notification_test_endpoint_is_admin_only(client, user_token):
    response = await client.post("/api/settings/notifications/test", headers={"Authorization": f"Bearer {await user_token()}"})
    assert response.status_code == 403


async def test_availability_report(client, create_device, auth_headers, perform_check_result):
    headers = await auth_headers()
    device = await create_device()
    await client.post(f"/api/devices/{device['id']}/check", headers=headers)

    response = await client.get("/api/metrics/availability", headers=headers)
    assert response.status_code == 200
    rows = response.json()
    row = next(item for item in rows if item["id"] == device["id"])
    assert row["checks"] == 1
    assert row["uptime_percent"] == 100.0
