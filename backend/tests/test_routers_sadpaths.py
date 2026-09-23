"""Sad-path tests for app.routers error branches reported by coverage.

Covers the eight uncovered raises: the 401 "User not found" branch, five
404 branches (map delete / map-node hide / topology save / topology hide /
device delete), the 422 topology-coordinate branch, and the 400
"No changes supplied" branch.

Uses non-existent IDs (999999) so no cleanup of devices is needed; maps
created here are cleaned up directly in the database to avoid depending on
seed state or the "at least one map must remain" guard.
"""
import time

import jwt

import app.main as m


async def _headers(auth_headers):
    return await auth_headers()


# --------------------------------------------------------------------- 401


async def test_token_for_deleted_user_returns_401_user_not_found(client):
    """A syntactically valid token whose subject no longer exists must hit the
    "User not found" branch, not the "Invalid or expired token" one."""
    token = jwt.encode({"sub": "999999", "exp": int(time.time()) + 3600}, m.SECRET, algorithm="HS256")
    response = await client.get("/api/devices", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "User not found"


# --------------------------------------------------------- maps (404 x2)


async def test_delete_missing_map_returns_404(client, auth_headers):
    headers = await _headers(auth_headers)
    # Create two maps so the "at least one map must remain" guard (400) can
    # never mask the 404 regardless of what earlier tests did to the seed.
    created = []
    for name in ("SadPath A", "SadPath B"):
        response = await client.post("/api/maps", json={"name": name}, headers=headers)
        assert response.status_code == 200, response.text
        created.append(response.json()["id"])

    response = await client.delete("/api/maps/999999", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Map not found"

    # Cleanup directly in DB: deleting via the API could trip the last-map guard.
    with m.db() as connection:
        connection.execute("DELETE FROM topology_maps WHERE name LIKE 'SadPath %'")


async def test_hide_node_on_existing_map_with_missing_device_returns_404(client, auth_headers):
    headers = await _headers(auth_headers)
    response = await client.post("/api/maps", json={"name": "SadPath C"}, headers=headers)
    assert response.status_code == 200, response.text
    map_id = response.json()["id"]

    response = await client.delete(f"/api/maps/{map_id}/nodes/999999", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Device not found"

    with m.db() as connection:
        connection.execute("DELETE FROM topology_maps WHERE name LIKE 'SadPath %'")


# --------------------------------------------------- topology (404 x2, 422)


async def test_save_topology_position_for_missing_device_returns_404(client, auth_headers):
    response = await client.put(
        "/api/topology/nodes/999999",
        json={"x": 10, "y": 10},
        headers=await _headers(auth_headers),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Device not found"


async def test_hide_topology_node_for_missing_device_returns_404(client, auth_headers):
    response = await client.delete("/api/topology/nodes/999999", headers=await _headers(auth_headers))
    assert response.status_code == 404
    assert response.json()["detail"] == "Device not found"


async def test_save_topology_layout_with_out_of_range_coordinates_returns_422(client, auth_headers):
    response = await client.put(
        "/api/topology/layout",
        json={"nodes": [{"device_id": 999999, "x": 150, "y": 50}]},
        headers=await _headers(auth_headers),
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Topology coordinates must be between 0 and 100"


# ------------------------------------------------------- devices (400, 404)


async def test_update_device_with_empty_payload_returns_400(client, auth_headers):
    response = await client.put(
        "/api/devices/999999",
        json={},
        headers=await _headers(auth_headers),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "No changes supplied"


async def test_delete_missing_device_returns_404(client, auth_headers):
    response = await client.delete("/api/devices/999999", headers=await _headers(auth_headers))
    assert response.status_code == 404
    assert response.json()["detail"] == "Device not found"


async def test_enable_missing_device_returns_404(client, auth_headers):
    """The shared set_device_enabled() raise behind /enable and /disable."""
    response = await client.post("/api/devices/999999/enable", headers=await _headers(auth_headers))
    assert response.status_code == 404
    assert response.json()["detail"] == "Device not found"
