import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import smtplib
import sqlite3
import ssl
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl

DB_PATH = os.getenv("DATABASE_PATH", "/data/networkpulse.db")
APP_ENV = os.getenv("APP_ENV", "development").lower()
SECRET = os.getenv("APP_SECRET", "dev-only-change-me")
ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8080").split(",") if origin.strip()]
POLL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
CHECK_TICK_SECONDS = max(5, int(os.getenv("CHECK_TICK_SECONDS", "5")))
MAX_CONCURRENT_CHECKS = max(1, int(os.getenv("MAX_CONCURRENT_CHECKS", "10")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
METRICS_RETENTION_DAYS = max(1, int(os.getenv("METRICS_RETENTION_DAYS", "30")))
RETENTION_SWEEP_HOURS = max(1, int(os.getenv("RETENTION_SWEEP_HOURS", "24")))
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com").lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
USER_EMAIL = os.getenv("USER_EMAIL", "user@example.com").lower()
USER_PASSWORD = os.getenv("USER_PASSWORD", "user123")

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    init_db()
    app.state.monitor_task = asyncio.create_task(monitor_loop())
    app.state.retention_task = asyncio.create_task(metrics_retention_loop())
    try:
        yield
    finally:
        for task in (app.state.monitor_task, app.state.retention_task):
            task.cancel()
        # Wait for the loops to actually finish so shutdown is deterministic.
        await asyncio.gather(app.state.monitor_task, app.state.retention_task, return_exceptions=True)


app = FastAPI(title="WinsWiew Network Monitor API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])

logger = logging.getLogger("wwnm")
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False


def validate_config():
    if APP_ENV != "production":
        return
    insecure_secrets = {"dev-only-change-me", "change-this-secret-before-production", "admin123", "user123", "change-me"}
    if SECRET in insecure_secrets or len(SECRET) < 32:
        raise RuntimeError("APP_SECRET must be a unique value of at least 32 characters in production")
    if ADMIN_PASSWORD in insecure_secrets or len(ADMIN_PASSWORD) < 12:
        raise RuntimeError("ADMIN_PASSWORD must be changed and contain at least 12 characters in production")
    if USER_PASSWORD in insecure_secrets or len(USER_PASSWORD) < 12:
        raise RuntimeError("USER_PASSWORD must be changed and contain at least 12 characters in production")
    if not ALLOWED_ORIGINS or "*" in ALLOWED_ORIGINS:
        raise RuntimeError("ALLOWED_ORIGINS must explicitly list trusted frontend origins in production")


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def now():
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest = encoded.split("$", 1)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310_000).hex()
        return hmac.compare_digest(expected, digest)
    except ValueError:
        return False


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', name TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, monitor_type TEXT NOT NULL, interval_seconds INTEGER NOT NULL DEFAULT 60, enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'unknown', last_check TEXT, last_response_ms REAL, ssl_days_left INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL, checked_at TEXT NOT NULL, status TEXT NOT NULL, response_ms REAL, status_code INTEGER, ssl_days_left INTEGER, error TEXT, sensor_json TEXT, FOREIGN KEY(device_id) REFERENCES devices(id));
        CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, device_id INTEGER, severity TEXT NOT NULL, alert_type TEXT NOT NULL, message TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, resolved_at TEXT, FOREIGN KEY(device_id) REFERENCES devices(id));
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topology_nodes (device_id INTEGER PRIMARY KEY, x REAL NOT NULL DEFAULT 20, y REAL NOT NULL DEFAULT 20, visible INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS topology_maps (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topology_map_devices (map_id INTEGER NOT NULL, device_id INTEGER NOT NULL, x REAL NOT NULL DEFAULT 20, y REAL NOT NULL DEFAULT 20, visible INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, PRIMARY KEY(map_id, device_id), FOREIGN KEY(map_id) REFERENCES topology_maps(id) ON DELETE CASCADE, FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE);
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_metrics_device_time ON metrics(device_id, checked_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_metrics_checked_at ON metrics(checked_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_alerts_device_active ON alerts(device_id, active)")
        # Rows created before PRAGMA foreign_keys was enabled; topology tables use ON DELETE CASCADE.
        # This must run before the topology_map_devices backfill below.
        connection.execute("DELETE FROM topology_nodes WHERE device_id NOT IN (SELECT id FROM devices)")
        connection.execute("DELETE FROM topology_map_devices WHERE device_id NOT IN (SELECT id FROM devices) OR map_id NOT IN (SELECT id FROM topology_maps)")
        connection.execute("DELETE FROM metrics WHERE device_id NOT IN (SELECT id FROM devices)")
        connection.execute("DELETE FROM alerts WHERE device_id IS NOT NULL AND device_id NOT IN (SELECT id FROM devices)")
        metric_columns = {row["name"] for row in connection.execute("PRAGMA table_info(metrics)").fetchall()}
        if "sensor_json" not in metric_columns:
            connection.execute("ALTER TABLE metrics ADD COLUMN sensor_json TEXT")
        device_columns = {row["name"] for row in connection.execute("PRAGMA table_info(devices)").fetchall()}
        if "snmp_community" not in device_columns:
            connection.execute("ALTER TABLE devices ADD COLUMN snmp_community TEXT NOT NULL DEFAULT 'public'")
        if "snmp_port" not in device_columns:
            connection.execute("ALTER TABLE devices ADD COLUMN snmp_port INTEGER NOT NULL DEFAULT 161")
        topology_columns = {row["name"] for row in connection.execute("PRAGMA table_info(topology_nodes)").fetchall()}
        if "visible" not in topology_columns:
            connection.execute("ALTER TABLE topology_nodes ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
        default_map = connection.execute("SELECT id FROM topology_maps WHERE name = ?", ("Main site",)).fetchone()
        if not default_map:
            connection.execute("INSERT INTO topology_maps(name,description,created_at) VALUES(?,?,?)", ("Main site", "Default network topology", now()))
            default_map = connection.execute("SELECT id FROM topology_maps WHERE name = ?", ("Main site",)).fetchone()
        connection.execute("INSERT OR IGNORE INTO topology_map_devices(map_id,device_id,x,y,visible,updated_at) SELECT ?,t.device_id,t.x,t.y,t.visible,t.updated_at FROM topology_nodes t JOIN devices d ON d.id=t.device_id", (default_map["id"],))
        admin = connection.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
        if not admin:
            connection.execute("INSERT INTO users(email,password_hash,role,name,created_at) VALUES(?,?,?,?,?)", (ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), "admin", "Network Administrator", now()))
        regular_user = connection.execute("SELECT id FROM users WHERE email = ?", (USER_EMAIL,)).fetchone()
        if not regular_user:
            connection.execute("INSERT INTO users(email,password_hash,role,name,created_at) VALUES(?,?,?,?,?)", (USER_EMAIL, hash_password(USER_PASSWORD), "user", "Monitoring User", now()))
        if not connection.execute("SELECT id FROM devices LIMIT 1").fetchone():
            connection.execute("INSERT INTO devices(name,host,monitor_type,created_at) VALUES(?,?,?,?)", ("Example HTTPS", "https://example.com", "https", now()))


class LoginRequest(BaseModel):
    email: str
    password: str


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=3, max_length=500)
    monitor_type: str = Field(pattern="^(http|https|ping|vpn|snmp)$")
    interval_seconds: int = Field(default=60, ge=15, le=86400)
    snmp_community: str = Field(default="public", min_length=1, max_length=100)
    snmp_port: int = Field(default=161, ge=1, le=65535)


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = Field(default=None, min_length=3, max_length=500)
    monitor_type: str | None = Field(default=None, pattern="^(http|https|ping|vpn|snmp)$")
    interval_seconds: int | None = Field(default=None, ge=15, le=86400)
    snmp_community: str | None = Field(default=None, min_length=1, max_length=100)
    snmp_port: int | None = Field(default=None, ge=1, le=65535)
    enabled: bool | None = None


class MapCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=250)


class TopologyPosition(BaseModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)


class TopologyLayout(BaseModel):
    nodes: list[dict[str, float | int]]


class ThresholdSettings(BaseModel):
    cpu_percent: float = Field(default=80, ge=1, le=100)
    memory_percent: float = Field(default=85, ge=1, le=100)
    disk_percent: float = Field(default=90, ge=1, le=100)
    response_ms: float = Field(default=2000, ge=1, le=120000)
    ssl_days: int = Field(default=30, ge=1, le=365)


class NotificationSettings(BaseModel):
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    recipients: list[str] = []
    webhook_url: str = ""


def current_user(request: Request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        with db() as connection:
            user = connection.execute("SELECT id,email,role,name FROM users WHERE id = ?", (int(payload["sub"]),)).fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return dict(user)
    except (jwt.InvalidTokenError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "wwnm-api", "name": "WinsWiew Network Monitor", "short_name": "WWNM", "time": now()}


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (payload.email.lower(),)).fetchone()
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = jwt.encode({"sub": str(user["id"]), "exp": int(time.time()) + 28800}, SECRET, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "user": {"email": user["email"], "role": user["role"], "name": user["name"]}}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


@app.get("/api/devices")
def list_devices(user=Depends(current_user)):
    with db() as connection:
        rows = connection.execute("SELECT id,name,host,monitor_type,interval_seconds,enabled,status,last_check,last_response_ms,ssl_days_left,created_at,snmp_port,(SELECT sensor_json FROM metrics m WHERE m.device_id=devices.id ORDER BY m.id DESC LIMIT 1) AS sensor_json FROM devices ORDER BY id DESC").fetchall()
    devices = []
    for row in rows:
        item = dict(row)
        item["sensors"] = json.loads(item.pop("sensor_json") or "{}")
        devices.append(item)
    return devices


@app.get("/api/summary")
def summary(user=Depends(current_user)):
    with db() as connection:
        total = connection.execute("SELECT COUNT(*) AS value FROM devices").fetchone()["value"]
        enabled = connection.execute("SELECT COUNT(*) AS value FROM devices WHERE enabled=1").fetchone()["value"]
        status_rows = connection.execute("SELECT status, COUNT(*) AS value FROM devices WHERE enabled=1 GROUP BY status").fetchall()
        active_alerts = connection.execute("SELECT COUNT(*) AS value FROM alerts WHERE active=1").fetchone()["value"]
        metric = connection.execute("SELECT AVG(response_ms) AS response_ms, SUM(CASE WHEN status='up' THEN 1 ELSE 0 END) AS up_count, COUNT(*) AS total_count FROM metrics WHERE checked_at >= datetime('now', '-30 days')").fetchone()
    statuses = {row["status"]: row["value"] for row in status_rows}
    metric_total = metric["total_count"] or 0
    return {"total_devices": total, "enabled_devices": enabled, "online": statuses.get("up", 0), "warning": statuses.get("warning", 0), "critical": statuses.get("down", 0), "unknown": statuses.get("unknown", 0), "active_alerts": active_alerts, "avg_response_ms": round(metric["response_ms"], 2) if metric["response_ms"] is not None else None, "uptime_percent": round((metric["up_count"] or 0) * 100 / metric_total, 2) if metric_total else None}


@app.get("/api/metrics/device/{device_id}")
def device_metrics(device_id: int, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        rows = connection.execute("SELECT id,checked_at,status,response_ms,status_code,ssl_days_left,error,sensor_json FROM metrics WHERE device_id=? ORDER BY id DESC LIMIT 200", (device_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["sensors"] = json.loads(item.pop("sensor_json") or "{}")
        result.append(item)
    return result


@app.get("/api/metrics/availability")
def availability(user=Depends(current_user)): 
    with db() as connection:
        rows = connection.execute("SELECT d.id, d.name, COUNT(m.id) AS checks, SUM(CASE WHEN m.status='up' THEN 1 ELSE 0 END) AS successful_checks FROM devices d LEFT JOIN metrics m ON m.device_id=d.id AND m.checked_at >= datetime('now', '-30 days') GROUP BY d.id ORDER BY d.id DESC").fetchall()
    return [{"id": row["id"], "name": row["name"], "checks": row["checks"], "uptime_percent": round((row["successful_checks"] or 0) * 100 / row["checks"], 2) if row["checks"] else None} for row in rows]


@app.get("/api/maps")
def list_maps(user=Depends(current_user)):
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT m.*, COUNT(md.device_id) AS device_count FROM topology_maps m LEFT JOIN topology_map_devices md ON md.map_id=m.id AND md.visible=1 GROUP BY m.id ORDER BY m.id")]


@app.post("/api/maps")
def create_map(payload: MapCreate, user=Depends(require_admin)):
    with db() as connection:
        try:
            cursor = connection.execute("INSERT INTO topology_maps(name,description,created_at) VALUES(?,?,?)", (payload.name.strip(), payload.description.strip(), now()))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="A map with this name already exists")
        return dict(connection.execute("SELECT *, 0 AS device_count FROM topology_maps WHERE id=?", (cursor.lastrowid,)).fetchone())


@app.delete("/api/maps/{map_id}")
def delete_map(map_id: int, user=Depends(require_admin)):
    with db() as connection:
        count = connection.execute("SELECT COUNT(*) AS value FROM topology_maps").fetchone()["value"]
        if count <= 1:
            raise HTTPException(status_code=400, detail="At least one map must remain")
        cursor = connection.execute("DELETE FROM topology_maps WHERE id=?", (map_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Map not found")
    return {"status": "deleted", "id": map_id}


@app.get("/api/maps/{map_id}/topology")
def get_map_topology(map_id: int, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM topology_maps WHERE id=?", (map_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Map not found")
        rows = connection.execute("SELECT d.id AS device_id, d.name, d.host, d.monitor_type, d.status, d.enabled, md.x, md.y FROM topology_map_devices md JOIN devices d ON d.id=md.device_id WHERE md.map_id=? AND md.visible=1 ORDER BY d.id", (map_id,)).fetchall()
    return [dict(row) for row in rows]


@app.put("/api/maps/{map_id}/layout")
def save_map_layout(map_id: int, payload: TopologyLayout, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM topology_maps WHERE id=?", (map_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Map not found")
        for node in payload.nodes:
            device_id, x, y = int(node.get("device_id", 0)), float(node.get("x", 20)), float(node.get("y", 20))
            if not 0 <= x <= 100 or not 0 <= y <= 100:
                raise HTTPException(status_code=422, detail="Topology coordinates must be between 0 and 100")
            if connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
                connection.execute("INSERT INTO topology_map_devices(map_id,device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(map_id,device_id) DO UPDATE SET x=excluded.x,y=excluded.y,visible=1,updated_at=excluded.updated_at", (map_id, device_id, x, y, 1, now()))
    return {"status": "saved", "count": len(payload.nodes)}


@app.delete("/api/maps/{map_id}/nodes/{device_id}")
def hide_map_node(map_id: int, device_id: int, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM topology_maps WHERE id=?", (map_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Map not found")
        if not connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("INSERT INTO topology_map_devices(map_id,device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(map_id,device_id) DO UPDATE SET visible=0,updated_at=excluded.updated_at", (map_id, device_id, 20, 20, 0, now()))
    return {"status": "hidden", "map_id": map_id, "device_id": device_id}


@app.get("/api/topology")
def get_topology(user=Depends(current_user)):
    with db() as connection:
        rows = connection.execute("SELECT d.id AS device_id, d.name, d.host, d.monitor_type, d.status, d.enabled, COALESCE(t.x, 20) AS x, COALESCE(t.y, 20) AS y FROM devices d LEFT JOIN topology_nodes t ON t.device_id=d.id WHERE COALESCE(t.visible, 1)=1 ORDER BY d.id").fetchall()
    return [dict(row) for row in rows]


@app.put("/api/topology/nodes/{device_id}")
def save_topology_position(device_id: int, payload: TopologyPosition, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("INSERT INTO topology_nodes(device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET x=excluded.x,y=excluded.y,visible=1,updated_at=excluded.updated_at", (device_id, payload.x, payload.y, 1, now()))
    return {"device_id": device_id, "x": payload.x, "y": payload.y}


@app.put("/api/topology/layout")
def save_topology_layout(payload: TopologyLayout, user=Depends(current_user)):
    with db() as connection:
        for node in payload.nodes:
            device_id = int(node.get("device_id", 0))
            x, y = float(node.get("x", 20)), float(node.get("y", 20))
            if not 0 <= x <= 100 or not 0 <= y <= 100:
                raise HTTPException(status_code=422, detail="Topology coordinates must be between 0 and 100")
            if connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
                connection.execute("INSERT INTO topology_nodes(device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET x=excluded.x,y=excluded.y,visible=1,updated_at=excluded.updated_at", (device_id, x, y, 1, now()))
    return {"status": "saved", "count": len(payload.nodes)}


@app.delete("/api/topology/nodes/{device_id}")
def hide_topology_node(device_id: int, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("INSERT INTO topology_nodes(device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET visible=0,updated_at=excluded.updated_at", (device_id, 20, 20, 0, now()))
    return {"status": "hidden", "device_id": device_id}


@app.post("/api/devices")
def create_device(payload: DeviceCreate, user=Depends(require_admin)):
    with db() as connection:
        cursor = connection.execute("INSERT INTO devices(name,host,monitor_type,interval_seconds,snmp_community,snmp_port,created_at) VALUES(?,?,?,?,?,?,?)", (payload.name, payload.host, payload.monitor_type, payload.interval_seconds, payload.snmp_community, payload.snmp_port, now()))
        device = connection.execute("SELECT id,name,host,monitor_type,interval_seconds,enabled,status,last_check,last_response_ms,ssl_days_left,created_at,snmp_port FROM devices WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(device)


@app.put("/api/devices/{device_id}")
def update_device(device_id: int, payload: DeviceUpdate, user=Depends(require_admin)):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id = ?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = [int(value) if isinstance(value, bool) else value for value in changes.values()]
        connection.execute(f"UPDATE devices SET {assignments} WHERE id = ?", (*values, device_id))
        device = connection.execute("SELECT id,name,host,monitor_type,interval_seconds,enabled,status,last_check,last_response_ms,ssl_days_left,created_at,snmp_port FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(device)


@app.delete("/api/devices/{device_id}")
def delete_device(device_id: int, user=Depends(require_admin)):
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id = ?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("DELETE FROM metrics WHERE device_id = ?", (device_id,))
        connection.execute("DELETE FROM alerts WHERE device_id = ?", (device_id,))
        connection.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    return {"status": "deleted", "id": device_id}


@app.post("/api/devices/{device_id}/enable")
def enable_device(device_id: int, user=Depends(require_admin)):
    return set_device_enabled(device_id, True)


@app.post("/api/devices/{device_id}/disable")
def disable_device(device_id: int, user=Depends(require_admin)):
    return set_device_enabled(device_id, False)


def set_device_enabled(device_id: int, enabled: bool):
    with db() as connection:
        cursor = connection.execute("UPDATE devices SET enabled = ? WHERE id = ?", (int(enabled), device_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Device not found")
        return dict(connection.execute("SELECT id,name,host,monitor_type,interval_seconds,enabled,status,last_check,last_response_ms,ssl_days_left,created_at,snmp_port FROM devices WHERE id = ?", (device_id,)).fetchone())


@app.post("/api/devices/{device_id}/check")
async def check_device(device_id: int, user=Depends(current_user)):
    with db() as connection:
        device = connection.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    result = await perform_check(dict(device))
    await persist_result(dict(device), result)
    return result


@app.get("/api/alerts")
def list_alerts(user=Depends(current_user)):
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT a.*, d.name AS device_name FROM alerts a LEFT JOIN devices d ON d.id=a.device_id ORDER BY a.id DESC LIMIT 100")]


@app.post("/api/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, user=Depends(current_user)):
    with db() as connection:
        cursor = connection.execute("UPDATE alerts SET active=0,resolved_at=? WHERE id=? AND active=1", (now(), alert_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Active alert not found")
    return {"status": "resolved", "id": alert_id}


@app.get("/api/settings/thresholds")
def get_thresholds(user=Depends(current_user)):
    defaults = ThresholdSettings().model_dump()
    with db() as connection:
        rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'threshold.%'").fetchall()
    for row in rows:
        key = row["key"].removeprefix("threshold.")
        if key in defaults:
            defaults[key] = float(row["value"]) if key != "ssl_days" else int(row["value"])
    return defaults


@app.put("/api/settings/thresholds")
def save_thresholds(payload: ThresholdSettings, user=Depends(require_admin)):
    with db() as connection:
        for key, value in payload.model_dump().items():
            connection.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"threshold.{key}", str(value)))
    return {"status": "saved", **payload.model_dump()}


@app.get("/api/settings/notifications")
def get_notification_settings(user=Depends(require_admin)):
    with db() as connection:
        rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'notification.%'").fetchall()
    values = {row["key"].removeprefix("notification."): row["value"] for row in rows}
    values.pop("smtp_password", None)
    return values


@app.put("/api/settings/notifications")
def save_notification_settings(payload: NotificationSettings, user=Depends(require_admin)):
    values = payload.model_dump()
    values["recipients"] = json.dumps(values["recipients"])
    with db() as connection:
        # An empty password means "keep the stored one" — the GET endpoint never echoes it back,
        # so the form resubmits an empty field and must not wipe the saved credential.
        if not values.get("smtp_password"):
            stored = connection.execute("SELECT value FROM settings WHERE key = 'notification.smtp_password'").fetchone()
            if stored and stored["value"]:
                values["smtp_password"] = stored["value"]
        for key, value in values.items():
            connection.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"notification.{key}", str(value)))
    return {"status": "saved", "smtp_configured": bool(values["smtp_host"] and values["smtp_username"] and values["smtp_password"] and values["smtp_from"] and json.loads(values["recipients"]))}


@app.post("/api/settings/notifications/test")
def test_notification(user=Depends(require_admin)):
    with db() as connection:
        rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'notification.%'").fetchall()
    values = {row["key"].removeprefix("notification."): row["value"] for row in rows}
    recipients = json.loads(values.get("recipients", "[]"))
    if not values.get("smtp_host") or not recipients:
        raise HTTPException(status_code=400, detail="SMTP host and recipient must be configured first")
    send_email(values, recipients, "WWNM test notification", "This is a real test message from WinsWiew Network Monitor.")
    return {"status": "sent"}


async def snmp_get(device: dict[str, Any], oid: str):
    community = device.get("snmp_community") or "public"
    target = f"{device['host']}:{int(device.get('snmp_port') or 161)}"
    command = ["snmpget", "-v", "2c", "-c", community, "-t", "2", "-r", "0", "-Oqv", target, oid]
    completed = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=6)
    if completed.returncode != 0:
        return None
    value = (completed.stdout or "").strip().splitlines()
    return value[-1].strip().strip('"') if value else None


def numeric_snmp(value):
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError):
        return None


async def perform_check(device: dict[str, Any]):
    started = time.perf_counter()
    host = device["host"]
    target = host if host.startswith(("http://", "https://")) else f"{device['monitor_type']}://{host}"
    parsed = urlparse(target)
    result = {"status": "down", "response_ms": None, "status_code": None, "ssl_days_left": None, "error": None, "sensors": {}}
    try:
        if device["monitor_type"] in ("http", "https"):
            async with httpx.AsyncClient(timeout=15, follow_redirects=True, verify=True) as client:
                response = await client.get(target)
            result["status_code"] = response.status_code
            result["status"] = "up" if response.is_success else "warning"
            if parsed.scheme == "https":
                result["ssl_days_left"] = await asyncio.to_thread(get_ssl_days_left, parsed.hostname, parsed.port or 443)
        elif device["monitor_type"] in ("ping", "vpn"):
            command = ["ping", "-c", "1", "-W", "3", host]
            completed = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=5)
            if completed.returncode != 0:
                raise RuntimeError("Ping failed")
            result["status"] = "up"
        elif device["monitor_type"] == "snmp":
            community = device.get("snmp_community") or "public"
            target = f"{host}:{int(device.get('snmp_port') or 161)}"
            command = ["snmpget", "-v", "2c", "-c", community, "-t", "3", "-r", "0", "-Oqv", target, "1.3.6.1.2.1.1.3.0"]
            completed = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=8)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "SNMP request failed").strip().splitlines()[-1]
                raise RuntimeError(detail[:250])
            result["status"] = "up"
            sensor_oids = {"sys_uptime": "1.3.6.1.2.1.1.3.0", "cpu_percent": "1.3.6.1.2.1.25.3.3.1.2.1", "memory_available_kb": "1.3.6.1.4.1.2021.4.6.0", "memory_total_kb": "1.3.6.1.4.1.2021.4.5.0", "disk_percent": "1.3.6.1.4.1.2021.9.1.9.1"}
            sensor_results = await asyncio.gather(*(snmp_get(device, oid) for oid in sensor_oids.values()), return_exceptions=True)
            sensor_values = {key: (None if isinstance(value, BaseException) else value) for key, value in zip(sensor_oids.keys(), sensor_results)}
            if sensor_values.get("sys_uptime"):
                result["sensors"]["sys_uptime"] = sensor_values["sys_uptime"]
            cpu = numeric_snmp(sensor_values.get("cpu_percent"))
            if cpu is not None:
                result["sensors"]["cpu_percent"] = cpu
            disk = numeric_snmp(sensor_values.get("disk_percent"))
            if disk is not None:
                result["sensors"]["disk_percent"] = disk
            memory_total = numeric_snmp(sensor_values.get("memory_total_kb")); memory_available = numeric_snmp(sensor_values.get("memory_available_kb"))
            if memory_total and memory_available is not None and memory_total > 0:
                result["sensors"]["memory_percent"] = round((memory_total - memory_available) * 100 / memory_total, 2)
        else:
            result["status"] = "unknown"
            result["error"] = "Unsupported monitor type"
    except Exception as exc:
        result["error"] = str(exc)[:300]
    result["response_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def get_ssl_days_left(host: str, port: int) -> int:
    context = ssl.create_default_context()
    with socket_create_connection(host, port, timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secure:
            certificate = secure.getpeercert()
    expires = datetime.strptime(certificate["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return max(0, (expires - datetime.now(timezone.utc)).days)


def socket_create_connection(host: str, port: int, timeout: int):
    import socket
    return socket.create_connection((host, port), timeout=timeout)


async def persist_result(device: dict[str, Any], result: dict[str, Any]):
    alerts = []
    if result["status"] == "down":
        alerts.append(("critical", "device_down", result.get("error") or "Device is unreachable"))
    elif result["status"] == "warning":
        alerts.append(("warning", "http_status", f"HTTP status {result['status_code']}"))
    if result.get("ssl_days_left") is not None and result["ssl_days_left"] <= 30:
        alerts.append(("warning" if result["ssl_days_left"] > 7 else "critical", "ssl_expiring", f"SSL certificate expires in {result['ssl_days_left']} days"))
    sensors = result.get("sensors", {})
    thresholds = ThresholdSettings().model_dump()
    with db() as connection:
        threshold_rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'threshold.%'").fetchall()
    for row in threshold_rows:
        key = row["key"].removeprefix("threshold.")
        if key in thresholds:
            thresholds[key] = float(row["value"])
    for key, severity in (("cpu_percent", "warning"), ("memory_percent", "warning"), ("disk_percent", "critical")):
        threshold = thresholds[key]
        value = sensors.get(key)
        if isinstance(value, (int, float)) and value >= threshold:
            label = key.replace("_percent", "").upper()
            alerts.append((severity, f"snmp_{key}", f"SNMP {label} usage is {value}% (threshold {threshold}%)"))
    active_types = {alert[1] for alert in alerts}
    with db() as connection:
        connection.execute("UPDATE devices SET status=?,last_check=?,last_response_ms=?,ssl_days_left=? WHERE id=?", (result["status"], now(), result["response_ms"], result.get("ssl_days_left"), device["id"]))
        connection.execute("INSERT INTO metrics(device_id,checked_at,status,response_ms,status_code,ssl_days_left,error,sensor_json) VALUES(?,?,?,?,?,?,?,?)", (device["id"], now(), result["status"], result["response_ms"], result.get("status_code"), result.get("ssl_days_left"), result.get("error"), json.dumps(sensors)))
        if active_types:
            placeholders = ",".join("?" for _ in active_types)
            connection.execute(f"UPDATE alerts SET active=0,resolved_at=? WHERE device_id=? AND active=1 AND alert_type NOT IN ({placeholders})", (now(), device["id"], *active_types))
        else:
            connection.execute("UPDATE alerts SET active=0,resolved_at=? WHERE device_id=? AND active=1", (now(), device["id"]))
        for severity, alert_type, message in alerts:
            active = connection.execute("SELECT id FROM alerts WHERE device_id=? AND alert_type=? AND active=1", (device["id"], alert_type)).fetchone()
            if not active:
                connection.execute("INSERT INTO alerts(device_id,severity,alert_type,message,created_at) VALUES(?,?,?,?,?)", (device["id"], severity, alert_type, message, now()))
                await notify(device, severity, message)


async def notify(device: dict, severity: str, message: str):
    with db() as connection:
        rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'notification.%'").fetchall()
    values = {row["key"].removeprefix("notification."): row["value"] for row in rows}
    subject = f"[{severity.upper()}] {device['name']}"
    recipients = json.loads(values.get("recipients", "[]"))
    if values.get("smtp_host") and recipients:
        try:
            await asyncio.to_thread(send_email, values, recipients, subject, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("email notification failed for device %s (recipients: %s)", device["name"], recipients)
    webhook = values.get("webhook_url")
    if webhook:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(webhook, json={"severity": severity, "device": device["name"], "message": message})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("webhook notification failed for device %s: %s", device["name"], exc)


def send_email(settings: dict, recipients: list[str], subject: str, body: str):
    email = EmailMessage()
    email["Subject"], email["From"], email["To"] = subject, settings["smtp_from"], ", ".join(recipients)
    email.set_content(body)
    port = int(settings.get("smtp_port") or 587)
    if port == 465:
        # Implicit TLS from the first byte (Gmail, Outlook and most providers on 465).
        with smtplib.SMTP_SSL(settings["smtp_host"], port, timeout=20) as server:
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.send_message(email)
        return
    with smtplib.SMTP(settings["smtp_host"], port, timeout=20) as server:
        server.starttls()
        server.login(settings["smtp_username"], settings["smtp_password"])
        server.send_message(email)


async def run_device_check(device: dict[str, Any], semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        try:
            result = await perform_check(device)
            await persist_result(device, result)
            if result["status"] == "up":
                logger.info("device %s (%s) up in %sms", device["id"], device["name"], result["response_ms"])
            else:
                logger.warning("device %s (%s) %s: %s", device["id"], device["name"], result["status"], result.get("error") or "check reported degraded state")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("device %s (%s) check crashed", device["id"], device["name"])


async def check_due_devices(devices: list[dict[str, Any]]) -> None:
    if not devices:
        return
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    logger.info("running checks for %d due device(s)", len(devices))
    results = await asyncio.gather(*(run_device_check(device, semaphore) for device in devices), return_exceptions=True)
    for device, outcome in zip(devices, results):
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            logger.error("device %s (%s) check task failed: %r", device["id"], device["name"], outcome)


async def scheduler_loop() -> None:
    """Run each enabled device on its own interval_seconds schedule."""
    await asyncio.sleep(2)
    next_run: dict[int, float] = {}
    while True:
        try:
            with db() as connection:
                devices = [dict(row) for row in connection.execute("SELECT * FROM devices WHERE enabled=1")]
            current = time.monotonic()
            due: list[dict[str, Any]] = []
            seen: set[int] = set()
            for device in devices:
                device_id = device["id"]
                seen.add(device_id)
                interval = max(POLL_SECONDS, int(device.get("interval_seconds") or POLL_SECONDS))
                if current >= next_run.get(device_id, 0):
                    due.append(device)
                    next_run[device_id] = current + interval
            for stale_id in set(next_run) - seen:
                next_run.pop(stale_id, None)
            asyncio.create_task(check_due_devices(due))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler loop iteration failed")
        await asyncio.sleep(CHECK_TICK_SECONDS)


# Backwards-compatible alias for the original serial loop name.
monitor_loop = scheduler_loop


def sweep_old_metrics(retention_days: int = METRICS_RETENTION_DAYS) -> int:
    """Delete metric rows older than the given number of days; return row count."""
    with db() as connection:
        cursor = connection.execute("DELETE FROM metrics WHERE checked_at < datetime('now', ?)", (f"-{retention_days} days",))
    if cursor.rowcount:
        logger.info("metrics retention: removed %d row(s) older than %d days", cursor.rowcount, retention_days)
    return cursor.rowcount


async def metrics_retention_loop() -> None:
    """Periodically delete metric rows older than METRICS_RETENTION_DAYS."""
    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.to_thread(sweep_old_metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("metrics retention sweep failed")
        await asyncio.sleep(RETENTION_SWEEP_HOURS * 3600)
