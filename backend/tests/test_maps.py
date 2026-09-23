async def _create_map(client, headers, name="Branch A"):
    response = await client.post("/api/maps", json={"name": name, "description": "test map"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_default_map_exists(client, auth_headers):
    maps = (await client.get("/api/maps", headers=await auth_headers())).json()
    assert any(m["name"] == "Main site" for m in maps)


async def test_create_map_requires_admin(client, user_token):
    response = await client.post("/api/maps", json={"name": "X"}, headers={"Authorization": f"Bearer {await user_token()}"})
    assert response.status_code == 403


async def test_create_map_and_duplicate_name(client, auth_headers):
    headers = await auth_headers()
    first = await _create_map(client, headers, "Site X")
    assert first["device_count"] == 0

    duplicate = await client.post("/api/maps", json={"name": "Site X"}, headers=headers)
    assert duplicate.status_code == 409


async def test_delete_map_requires_admin(client, auth_headers, user_token):
    headers = await auth_headers()
    created = await _create_map(client, headers, "Site D")
    response = await client.delete(f"/api/maps/{created['id']}", headers={"Authorization": f"Bearer {await user_token()}"})
    assert response.status_code == 403
    assert (await client.delete(f"/api/maps/{created['id']}", headers=headers)).status_code == 200


async def test_cannot_delete_last_remaining_map(client, auth_headers):
    headers = await auth_headers()
    maps = (await client.get("/api/maps", headers=headers)).json()
    for m in maps[1:]:
        assert (await client.delete(f"/api/maps/{m['id']}", headers=headers)).status_code == 200
    remaining = (await client.get("/api/maps", headers=headers)).json()
    assert len(remaining) == 1
    response = await client.delete(f"/api/maps/{remaining[0]['id']}", headers=headers)
    assert response.status_code == 400


async def test_topology_only_shows_devices_on_map(client, create_device, auth_headers):
    headers = await auth_headers()
    on_map = await create_device(name="OnMap")
    not_on_map = await create_device(name="OffMap")

    map_id = (await client.get("/api/maps", headers=headers)).json()[0]["id"]
    response = await client.put(
        f"/api/maps/{map_id}/layout",
        json={"nodes": [{"device_id": on_map["id"], "x": 30, "y": 40}]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1

    topology = (await client.get(f"/api/maps/{map_id}/topology", headers=headers)).json()
    ids = {node["device_id"] for node in topology}
    assert on_map["id"] in ids
    assert not_on_map["id"] not in ids


async def test_layout_rejects_out_of_range_coordinates(client, create_device, auth_headers):
    headers = await auth_headers()
    device = await create_device()
    map_id = (await client.get("/api/maps", headers=headers)).json()[0]["id"]
    response = await client.put(
        f"/api/maps/{map_id}/layout",
        json={"nodes": [{"device_id": device["id"], "x": 130, "y": 40}]},
        headers=headers,
    )
    assert response.status_code == 422


async def test_layout_ignores_unknown_device_ids(client, auth_headers):
    headers = await auth_headers()
    map_id = (await client.get("/api/maps", headers=headers)).json()[0]["id"]
    response = await client.put(
        f"/api/maps/{map_id}/layout",
        json={"nodes": [{"device_id": 999999, "x": 30, "y": 40}]},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1  # counted but skipped silently


async def test_layout_persists_positions(client, create_device, auth_headers):
    headers = await auth_headers()
    device = await create_device()
    map_id = (await client.get("/api/maps", headers=headers)).json()[0]["id"]
    await client.put(
        f"/api/maps/{map_id}/layout",
        json={"nodes": [{"device_id": device["id"], "x": 66.5, "y": 22.5}]},
        headers=headers,
    )
    topology = (await client.get(f"/api/maps/{map_id}/topology", headers=headers)).json()
    node = next(n for n in topology if n["device_id"] == device["id"])
    assert node["x"] == 66.5
    assert node["y"] == 22.5


async def test_hide_node_then_readd_via_layout(client, create_device, auth_headers):
    headers = await auth_headers()
    device = await create_device()
    map_id = (await client.get("/api/maps", headers=headers)).json()[0]["id"]

    await client.put(f"/api/maps/{map_id}/layout", json={"nodes": [{"device_id": device["id"], "x": 10, "y": 10}]}, headers=headers)
    response = await client.delete(f"/api/maps/{map_id}/nodes/{device['id']}", headers=headers)
    assert response.status_code == 200
    topology = (await client.get(f"/api/maps/{map_id}/topology", headers=headers)).json()
    assert not any(n["device_id"] == device["id"] for n in topology)

    # Re-adding through a layout save makes it visible again.
    await client.put(f"/api/maps/{map_id}/layout", json={"nodes": [{"device_id": device["id"], "x": 55, "y": 66}]}, headers=headers)
    topology = (await client.get(f"/api/maps/{map_id}/topology", headers=headers)).json()
    node = next(n for n in topology if n["device_id"] == device["id"])
    assert node["x"] == 55
    assert node["y"] == 66


async def test_hide_node_unknown_map_or_device_returns_404(client, create_device, auth_headers):
    """Regression: with foreign_keys ON, hiding a node for a non-existent map/device
    used to violate the FK constraint and return HTTP 500."""
    headers = await auth_headers()
    device = await create_device()
    response = await client.delete("/api/maps/999999/nodes/" + str(device["id"]), headers=headers)
    assert response.status_code == 404
    response = await client.delete(f"/api/maps/999999/nodes/999999", headers=headers)
    assert response.status_code == 404


async def test_layout_unknown_map_returns_404(client, auth_headers):
    response = await client.put("/api/maps/999999/layout", json={"nodes": []}, headers=await auth_headers())
    assert response.status_code == 404


async def test_topology_unknown_map_returns_404(client, auth_headers):
    response = await client.get("/api/maps/999999/topology", headers=await auth_headers())
    assert response.status_code == 404


async def test_maps_require_auth(client):
    for method, path, kwargs in (
        ("get", "/api/maps", {}),
        ("post", "/api/maps", {"json": {"name": "X"}}),
        ("get", "/api/maps/1/topology", {}),
        ("put", "/api/maps/1/layout", {"json": {"nodes": []}}),
        ("delete", "/api/maps/1/nodes/1", {}),
    ):
        response = await getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, f"{method} {path}"


async def test_legacy_topology_endpoints(client, create_device, auth_headers):
    headers = await auth_headers()
    device = await create_device()

    response = await client.get("/api/topology", headers=headers)
    assert response.status_code == 200
    assert any(n["device_id"] == device["id"] for n in response.json())

    position = await client.put(f"/api/topology/nodes/{device['id']}", json={"x": 42, "y": 24}, headers=headers)
    assert position.status_code == 200
    assert position.json() == {"device_id": device["id"], "x": 42, "y": 24}

    layout = await client.put("/api/topology/layout", json={"nodes": [{"device_id": device["id"], "x": 10, "y": 20}]}, headers=headers)
    assert layout.status_code == 200
    assert layout.json()["count"] == 1

    hidden = await client.delete(f"/api/topology/nodes/{device['id']}", headers=headers)
    assert hidden.status_code == 200
    assert hidden.json()["status"] == "hidden"


async def test_device_deletion_cascades_to_map_nodes(client, create_device, auth_headers):
    headers = await auth_headers()
    device = await create_device()
    map_id = (await client.get("/api/maps", headers=headers)).json()[0]["id"]
    await client.put(f"/api/maps/{map_id}/layout", json={"nodes": [{"device_id": device["id"], "x": 10, "y": 10}]}, headers=headers)

    assert (await client.delete(f"/api/devices/{device['id']}", headers=headers)).status_code == 200
    topology = (await client.get(f"/api/maps/{map_id}/topology", headers=headers)).json()
    assert not any(n["device_id"] == device["id"] for n in topology)
