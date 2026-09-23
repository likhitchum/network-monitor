async def test_check_device_requires_auth(client, create_device):
    device = await create_device()
    response = await client.post(f"/api/devices/{device['id']}/check")
    assert response.status_code == 401


async def test_check_device_allowed_for_regular_user(client, create_device, user_token):
    device = await create_device()
    response = await client.post(
        f"/api/devices/{device['id']}/check",
        headers={"Authorization": f"Bearer {await user_token()}"},
    )
    assert response.status_code == 200


async def test_check_device_up_updates_status(client, create_device, auth_headers, perform_check_result):
    perform_check_result.update({"status": "up", "response_ms": 42.5})
    device = await create_device()

    response = await client.post(f"/api/devices/{device['id']}/check", headers=await auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "up"
    assert body["response_ms"] == 42.5

    devices = (await client.get("/api/devices", headers=await auth_headers())).json()
    stored = next(item for item in devices if item["id"] == device["id"])
    assert stored["status"] == "up"
    assert stored["last_response_ms"] == 42.5
    assert stored["last_check"]


async def test_check_device_down_records_error(client, create_device, auth_headers, perform_check_result):
    perform_check_result.update({"status": "down", "response_ms": 3000.0, "error": "Ping failed"})
    device = await create_device()

    response = await client.post(f"/api/devices/{device['id']}/check", headers=await auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "down"
    assert body["error"] == "Ping failed"

    devices = (await client.get("/api/devices", headers=await auth_headers())).json()
    stored = next(item for item in devices if item["id"] == device["id"])
    assert stored["status"] == "down"


async def test_check_nonexistent_device_returns_404(client, auth_headers):
    response = await client.post("/api/devices/999999/check", headers=await auth_headers())
    assert response.status_code == 404


async def test_check_persists_metric_row(client, create_device, auth_headers, perform_check_result):
    perform_check_result.update({"status": "up", "response_ms": 42.5})
    device = await create_device()
    await client.post(f"/api/devices/{device['id']}/check", headers=await auth_headers())

    response = await client.get(f"/api/metrics/device/{device['id']}", headers=await auth_headers())
    assert response.status_code == 200
    metrics = response.json()
    assert len(metrics) == 1
    assert metrics[0]["status"] == "up"
    assert metrics[0]["response_ms"] == 42.5


async def test_device_metrics_requires_auth(client, create_device):
    device = await create_device()
    response = await client.get(f"/api/metrics/device/{device['id']}")
    assert response.status_code == 401


async def test_device_metrics_unknown_device_returns_404(client, auth_headers):
    response = await client.get("/api/metrics/device/999999", headers=await auth_headers())
    assert response.status_code == 404
