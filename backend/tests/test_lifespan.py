import asyncio

from starlette.testclient import TestClient

import app.main as m


def test_lifespan_starts_and_cancels_background_loops(monkeypatch):
    events = []

    async def fake_monitor_loop():
        events.append("monitor_start")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("monitor_cancel")
            raise

    async def fake_retention_loop():
        events.append("retention_start")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("retention_cancel")
            raise

    monkeypatch.setattr(m, "monitor_loop", fake_monitor_loop)
    monkeypatch.setattr(m, "metrics_retention_loop", fake_retention_loop)

    with TestClient(m.app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert "monitor_start" in events
        assert "retention_start" in events

    # Exiting the context manager must cancel and reap both background loops.
    assert "monitor_cancel" in events
    assert "retention_cancel" in events
    assert m.app.state.monitor_task.cancelled()
    assert m.app.state.retention_task.cancelled()


def test_lifespan_initialises_database(monkeypatch):
    # init_db() runs on lifespan entry; the settings table must exist afterwards.
    monkeypatch.setattr(m, "monitor_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(m, "metrics_retention_loop", lambda: asyncio.sleep(3600))

    with TestClient(m.app):
        with m.db() as connection:
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"devices", "metrics", "alerts", "settings", "topology_maps"} <= tables


def test_background_loops_cancelled_even_if_one_already_finished(monkeypatch):
    """A loop that exits on its own must not break the shutdown gather."""
    async def short_lived_loop():
        await asyncio.sleep(0)

    async def hanging_loop():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(m, "monitor_loop", short_lived_loop)
    monkeypatch.setattr(m, "metrics_retention_loop", hanging_loop)

    with TestClient(m.app):
        pass
    assert m.app.state.monitor_task.done()
    assert m.app.state.retention_task.cancelled()
