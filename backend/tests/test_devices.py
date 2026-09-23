async def test_create_and_list_device(client, create_device, auth_headers):
    device = await create_device(name="Core Switch", host="10.0.0.1", monitor_type="snmp")
    assert device["id"] > 0
    assert device["status"] == "unknown"

    response = await client.get("/api/devices", headers=await auth_headers())
    devices = response.json()
    assert any(item["id"] == device["id"] and item["name"] == "Core Switch" for item in devices)


async def test_create_device_requires_admin(client, user_token, auth_headers):
    response = await client.post(
        "/api/devices",
        json={"name": "Nope", "host": "10.0.0.9", "monitor_type": "ping"},
        headers={"Authorization": f"Bearer {await user_token()}"},
    )
    assert response.status_code == 403


async def test_create_device_rejects_bad_monitor_type(client, auth_headers):
    response = await client.post(
        "/api/devices",
        json={"name": "Bad", "host": "10.0.0.2", "monitor_type": "carrier-pigeon"},
        headers=await auth_headers(),
    )
    assert response.status_code == 422


async def test_create_device_rejects_short_interval(client, auth_headers):
    response = await client.post(
        "/api/devices",
        json={"name": "Fast", "host": "10.0.0.3", "monitor_type": "ping", "interval_seconds": 5},
        headers=await auth_headers(),
    )
    assert response.status_code == 422


async def test_create_device_rejects_empty_name(client, auth_headers):
    response = await client.post("/api/devices", json={"name": "", "host": "10.0.0.4", "monitor_type": "ping"}, headers=await auth_headers())
    assert response.status_code == 422


async def test_update_device(client, create_device, auth_headers):
    device = await create_device()
    response = await client.put(
        f"/api/devices/{device['id']}",
        json={"name": "Renamed", "interval_seconds": 300},
        headers=await auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["interval_seconds"] == 300


async def test_update_device_requires_admin(client, create_device, user_token):
    device = await create_device()
    response = await client.put(
        f"/api/devices/{device['id']}",
        json={"name": "Hacked"},
        headers={"Authorization": f"Bearer {await user_token()}"},
    )
    assert response.status_code == 403


async def test_update_nonexistent_device_returns_404(client, auth_headers):
    response = await client.put("/api/devices/999999", json={"name": "Ghost"}, headers=await auth_headers())
    assert response.status_code == 404


async def test_enable_disable_device(client, create_device, auth_headers):
    device = await create_device()
    response = await client.post(f"/api/devices/{device['id']}/disable", headers=await auth_headers())
    assert response.status_code == 200
    assert response.json()["enabled"] == 0

    response = await client.post(f"/api/devices/{device['id']}/enable", headers=await auth_headers())
    assert response.status_code == 200
    assert response.json()["enabled"] == 1


async def test_delete_device(client, create_device, auth_headers):
    device = await create_device()
    response = await client.delete(f"/api/devices/{device['id']}", headers=await auth_headers())
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"

    devices = (await client.get("/api/devices", headers=await auth_headers())).json()
    assert all(item["id"] != device["id"] for item in devices)


async def test_delete_device_requires_admin(client, create_device, user_token):
    device = await create_device()
    response = await client.delete(f"/api/devices/{device['id']}", headers={"Authorization": f"Bearer {await user_token()}"})
    assert response.status_code == 403


async def test_delete_nonexistent_device_returns_404(client, auth_headers):
    response = await client.delete("/api/devices/999999", headers=await auth_headers())
    assert response.status_code == 404


async def test_snmp_defaults_applied(client, create_device, auth_headers):
    device = await create_device(monitor_type="snmp", host="10.1.1.1")
    assert device["snmp_port"] == 161
    response = await client.get("/api/devices", headers=await auth_headers())
    stored = next(item for item in response.json() if item["id"] == device["id"])
    assert stored["snmp_port"] == 161
