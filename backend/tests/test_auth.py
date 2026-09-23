async def test_health(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["short_name"] == "WWNM"


async def test_login_success(client):
    response = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "test-admin-pass-123"})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["role"] == "admin"
    assert body["access_token"]


async def test_login_wrong_password(client):
    response = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "nope"})
    assert response.status_code == 401


async def test_login_unknown_email(client):
    response = await client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "nope"})
    assert response.status_code == 401


async def test_me_requires_token(client):
    response = await client.get("/api/me")
    assert response.status_code == 401


async def test_me_with_token(client, admin_token):
    response = await client.get("/api/me", headers={"Authorization": f"Bearer {await admin_token()}"})
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "admin@example.com"
    assert body["role"] == "admin"


async def test_me_rejects_garbage_token(client):
    response = await client.get("/api/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


async def test_protected_routes_require_token(client):
    for method, path in (
        ("GET", "/api/devices"),
        ("GET", "/api/summary"),
        ("GET", "/api/alerts"),
        ("GET", "/api/maps"),
        ("GET", "/api/settings/thresholds"),
    ):
        response = await getattr(client, method.lower())(path)
        assert response.status_code == 401, f"{method} {path}"
