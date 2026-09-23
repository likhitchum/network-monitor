import os
import tempfile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Isolated SQLite file per test session, set BEFORE app.main is imported.
_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_PATH"] = _TMPDB.name
os.environ["APP_SECRET"] = "test-secret-" + "0" * 40  # 32+ bytes: silences PyJWT InsecureKeyLengthWarning
os.environ["ADMIN_PASSWORD"] = "test-admin-pass-123"
os.environ["USER_PASSWORD"] = "test-user-pass-1234"
os.environ["APP_ENV"] = "test"

import app.main as m  # noqa: E402

# httpx ASGITransport does not run FastAPI startup events, so initialise the
# schema explicitly once per test session.
m.init_db()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=m.app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
def admin_token(client):
    async def _login():
        response = await client.post("/api/auth/login", json={"email": "admin@example.com", "password": "test-admin-pass-123"})
        return response.json()["access_token"]

    return _login


@pytest.fixture
def user_token(client):
    async def _login():
        response = await client.post("/api/auth/login", json={"email": "user@example.com", "password": "test-user-pass-1234"})
        return response.json()["access_token"]

    return _login


@pytest.fixture
def auth_headers(admin_token):
    async def _headers():
        return {"Authorization": f"Bearer {await admin_token()}"}

    return _headers


@pytest.fixture
def create_device(client, auth_headers):
    """Return a helper that creates a device and returns the parsed JSON payload."""

    async def _create(**overrides):
        payload = {"name": "Test Device", "host": "127.0.0.1", "monitor_type": "ping"}
        payload.update(overrides)
        response = await client.post("/api/devices", json=payload, headers=await auth_headers())
        assert response.status_code == 200, response.text
        return response.json()

    return _create


@pytest.fixture(autouse=True)
def perform_check_result(monkeypatch):
    """Replace the real network check with a stub, so tests never touch the network.

    Tests mutate the returned dict to control what each check reports:
        perform_check_result.update({"status": "down", "error": "..."})
    """

    state = {"status": "up", "response_ms": 10.0, "status_code": None, "ssl_days_left": None, "error": None, "sensors": {}}

    async def fake_perform_check(device):
        return dict(state)

    monkeypatch.setattr(m, "perform_check", fake_perform_check)
    return state
