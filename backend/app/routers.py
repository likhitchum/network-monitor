"""HTTP API routers: health, auth, devices, metrics, summary, maps/topology, alerts, settings."""
import json
import sqlite3
import time

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import app.main as main
from app.database import db, now, verify_password
from app.notifications import notify
from app.pipeline import persist_result
from app.probes import perform_check
from app.schemas import (DeviceCreate, DeviceUpdate, LoginRequest, MapCreate,
                         NotificationSettings, ThresholdSettings, TopologyLayout,
                         TopologyPosition)

router = APIRouter()


def validate_config():
    import app.main as main  # every value is resolved via app.main so tests can patch it

    if main.APP_ENV != "production":
        return
    insecure_secrets = {"dev-only-change-me", "change-this-secret-before-production", "admin123", "user123", "change-me"}
    if main.SECRET in insecure_secrets or len(main.SECRET) < 32:
        raise RuntimeError("APP_SECRET must be a unique value of at least 32 characters in production")
    if main.ADMIN_PASSWORD in insecure_secrets or len(main.ADMIN_PASSWORD) < 12:
        raise RuntimeError("ADMIN_PASSWORD must be changed and contain at least 12 characters in production")
    if main.USER_PASSWORD in insecure_secrets or len(main.USER_PASSWORD) < 12:
        raise RuntimeError("USER_PASSWORD must be changed and contain at least 12 characters in production")
    if not main.ALLOWED_ORIGINS or "*" in main.ALLOWED_ORIGINS:
        raise RuntimeError("ALLOWED_ORIGINS must explicitly list trusted frontend origins in production")


def current_user(request: Request):
    import app.main as main  # SECRET + db are patched via app.main in tests

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = jwt.decode(token, main.SECRET, algorithms=["HS256"])
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


@router.get("/api/health")
def health():
    return {"status": "ok", "service": "wwnm-api", "name": "WinsWiew Network Monitor", "short_name": "WWNM", "time": now()}


@router.post("/api/auth/login")
def login(payload: LoginRequest):
    import app.main as main

    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (payload.email.lower(),)).fetchone()
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = jwt.encode({"sub": str(user["id"]), "exp": int(time.time()) + 28800}, main.SECRET, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "user": {"email": user["email"], "role": user["role"], "name": user["name"]}}


@router.get("/api/me")
def me(user=Depends(current_user)):
    return user


@router.get("/api/devices")
def list_devices(user=Depends(current_user)):
    with db() as connection:
        rows = connection.execute("SELECT id,name,host,monitor_type,interval_seconds,enabled,status,last_check,last_response_ms,ssl_days_left,created_at,snmp_port,(SELECT sensor_json FROM metrics m WHERE m.device_id=devices.id ORDER BY m.id DESC LIMIT 1) AS sensor_json FROM devices ORDER BY id DESC").fetchall()
    devices = []
    for row in rows:
        item = dict(row)
        item["sensors"] = json.loads(item.pop("sensor_json") or "{}")
        devices.append(item)
    return devices


@router.get("/api/summary")
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


@router.get("/api/metrics/device/{device_id}")
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


@router.get("/api/metrics/availability")
def availability(user=Depends(current_user)):
    with db() as connection:
        rows = connection.execute("SELECT d.id, d.name, COUNT(m.id) AS checks, SUM(CASE WHEN m.status='up' THEN 1 ELSE 0 END) AS successful_checks FROM devices d LEFT JOIN metrics m ON m.device_id=d.id AND m.checked_at >= datetime('now', '-30 days') GROUP BY d.id ORDER BY d.id DESC").fetchall()
    return [{"id": row["id"], "name": row["name"], "checks": row["checks"], "uptime_percent": round((row["successful_checks"] or 0) * 100 / row["checks"], 2) if row["checks"] else None} for row in rows]


@router.get("/api/maps")
def list_maps(user=Depends(current_user)):
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT m.*, COUNT(md.device_id) AS device_count FROM topology_maps m LEFT JOIN topology_map_devices md ON md.map_id=m.id AND md.visible=1 GROUP BY m.id ORDER BY m.id")]


@router.post("/api/maps")
def create_map(payload: MapCreate, user=Depends(require_admin)):
    with db() as connection:
        try:
            cursor = connection.execute("INSERT INTO topology_maps(name,description,created_at) VALUES(?,?,?)", (payload.name.strip(), payload.description.strip(), now()))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="A map with this name already exists")
        return dict(connection.execute("SELECT *, 0 AS device_count FROM topology_maps WHERE id=?", (cursor.lastrowid,)).fetchone())


@router.delete("/api/maps/{map_id}")
def delete_map(map_id: int, user=Depends(require_admin)):
    with db() as connection:
        count = connection.execute("SELECT COUNT(*) AS value FROM topology_maps").fetchone()["value"]
        if count <= 1:
            raise HTTPException(status_code=400, detail="At least one map must remain")
        cursor = connection.execute("DELETE FROM topology_maps WHERE id=?", (map_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Map not found")
    return {"status": "deleted", "id": map_id}


@router.get("/api/maps/{map_id}/topology")
def get_map_topology(map_id: int, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM topology_maps WHERE id=?", (map_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Map not found")
        rows = connection.execute("SELECT d.id AS device_id, d.name, d.host, d.monitor_type, d.status, d.enabled, md.x, md.y FROM topology_map_devices md JOIN devices d ON d.id=md.device_id WHERE md.map_id=? AND md.visible=1 ORDER BY d.id", (map_id,)).fetchall()
    return [dict(row) for row in rows]


@router.put("/api/maps/{map_id}/layout")
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


@router.delete("/api/maps/{map_id}/nodes/{device_id}")
def hide_map_node(map_id: int, device_id: int, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM topology_maps WHERE id=?", (map_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Map not found")
        if not connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("INSERT INTO topology_map_devices(map_id,device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(map_id,device_id) DO UPDATE SET visible=0,updated_at=excluded.updated_at", (map_id, device_id, 20, 20, 0, now()))
    return {"status": "hidden", "map_id": map_id, "device_id": device_id}


@router.get("/api/topology")
def get_topology(user=Depends(current_user)):
    with db() as connection:
        rows = connection.execute("SELECT d.id AS device_id, d.name, d.host, d.monitor_type, d.status, d.enabled, COALESCE(t.x, 20) AS x, COALESCE(t.y, 20) AS y FROM devices d LEFT JOIN topology_nodes t ON t.device_id=d.id WHERE COALESCE(t.visible, 1)=1 ORDER BY d.id").fetchall()
    return [dict(row) for row in rows]


@router.put("/api/topology/nodes/{device_id}")
def save_topology_position(device_id: int, payload: TopologyPosition, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("INSERT INTO topology_nodes(device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET x=excluded.x,y=excluded.y,visible=1,updated_at=excluded.updated_at", (device_id, payload.x, payload.y, 1, now()))
    return {"device_id": device_id, "x": payload.x, "y": payload.y}


@router.put("/api/topology/layout")
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


@router.delete("/api/topology/nodes/{device_id}")
def hide_topology_node(device_id: int, user=Depends(current_user)):
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("INSERT INTO topology_nodes(device_id,x,y,visible,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET visible=0,updated_at=excluded.updated_at", (device_id, 20, 20, 0, now()))
    return {"status": "hidden", "device_id": device_id}


@router.post("/api/devices")
def create_device(payload: DeviceCreate, user=Depends(require_admin)):
    with db() as connection:
        cursor = connection.execute("INSERT INTO devices(name,host,monitor_type,interval_seconds,snmp_community,snmp_port,created_at) VALUES(?,?,?,?,?,?,?)", (payload.name, payload.host, payload.monitor_type, payload.interval_seconds, payload.snmp_community, payload.snmp_port, now()))
        device = connection.execute("SELECT id,name,host,monitor_type,interval_seconds,enabled,status,last_check,last_response_ms,ssl_days_left,created_at,snmp_port FROM devices WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(device)


@router.put("/api/devices/{device_id}")
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


@router.delete("/api/devices/{device_id}")
def delete_device(device_id: int, user=Depends(require_admin)):
    with db() as connection:
        if not connection.execute("SELECT id FROM devices WHERE id = ?", (device_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Device not found")
        connection.execute("DELETE FROM metrics WHERE device_id = ?", (device_id,))
        connection.execute("DELETE FROM alerts WHERE device_id = ?", (device_id,))
        connection.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    return {"status": "deleted", "id": device_id}


@router.post("/api/devices/{device_id}/enable")
def enable_device(device_id: int, user=Depends(require_admin)):
    return set_device_enabled(device_id, True)


@router.post("/api/devices/{device_id}/disable")
def disable_device(device_id: int, user=Depends(require_admin)):
    return set_device_enabled(device_id, False)


def set_device_enabled(device_id: int, enabled: bool):
    with db() as connection:
        cursor = connection.execute("UPDATE devices SET enabled = ? WHERE id = ?", (int(enabled), device_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Device not found")
        return dict(connection.execute("SELECT id,name,host,monitor_type,interval_seconds,enabled,status,last_check,last_response_ms,ssl_days_left,created_at,snmp_port FROM devices WHERE id = ?", (device_id,)).fetchone())


@router.post("/api/devices/{device_id}/check")
async def check_device(device_id: int, user=Depends(current_user)):
    import app.main as main  # perform_check/persist_result are patched via app.main in tests

    with db() as connection:
        device = connection.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    result = await main.perform_check(dict(device))
    await main.persist_result(dict(device), result)
    return result


@router.get("/api/alerts")
def list_alerts(user=Depends(current_user)):
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT a.*, d.name AS device_name FROM alerts a LEFT JOIN devices d ON d.id=a.device_id ORDER BY a.id DESC LIMIT 100")]


@router.post("/api/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, user=Depends(current_user)):
    with db() as connection:
        cursor = connection.execute("UPDATE alerts SET active=0,resolved_at=? WHERE id=? AND active=1", (now(), alert_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Active alert not found")
    return {"status": "resolved", "id": alert_id}


@router.get("/api/settings/thresholds")
def get_thresholds(user=Depends(current_user)):
    defaults = ThresholdSettings().model_dump()
    with db() as connection:
        rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'threshold.%'").fetchall()
    for row in rows:
        key = row["key"].removeprefix("threshold.")
        if key in defaults:
            defaults[key] = float(row["value"]) if key != "ssl_days" else int(row["value"])
    return defaults


@router.put("/api/settings/thresholds")
def save_thresholds(payload: ThresholdSettings, user=Depends(require_admin)):
    with db() as connection:
        for key, value in payload.model_dump().items():
            connection.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"threshold.{key}", str(value)))
    return {"status": "saved", **payload.model_dump()}


@router.get("/api/settings/notifications")
def get_notification_settings(user=Depends(require_admin)):
    with db() as connection:
        rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'notification.%'").fetchall()
    values = {row["key"].removeprefix("notification."): row["value"] for row in rows}
    values.pop("smtp_password", None)
    return values


@router.put("/api/settings/notifications")
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


@router.post("/api/settings/notifications/test")
def test_notification(user=Depends(require_admin)):
    with db() as connection:
        rows = connection.execute("SELECT key,value FROM settings WHERE key LIKE 'notification.%'").fetchall()
    values = {row["key"].removeprefix("notification."): row["value"] for row in rows}
    recipients = json.loads(values.get("recipients", "[]"))
    if not values.get("smtp_host") or not recipients:
        raise HTTPException(status_code=400, detail="SMTP host and recipient must be configured first")
    main.send_email(values, recipients, "WWNM test notification", "This is a real test message from WinsWiew Network Monitor.")
    return {"status": "sent"}
