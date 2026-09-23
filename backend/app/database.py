"""SQLite access + schema (CREATE/INDEX/migrations/seed) and small shared helpers."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone

DB_PATH = "/data/networkpulse.db"  # resolved through app.main at call time (see compat note)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db():
    import app.main as main  # DB_PATH is patched on app.main in tests

    connection = sqlite3.connect(main.DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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
    import app.main as main  # seed credentials (ADMIN_EMAIL/...) are patched via app.main in tests

    os.makedirs(os.path.dirname(main.DB_PATH) or ".", exist_ok=True)
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
        admin = connection.execute("SELECT id FROM users WHERE email = ?", (main.ADMIN_EMAIL,)).fetchone()
        if not admin:
            connection.execute("INSERT INTO users(email,password_hash,role,name,created_at) VALUES(?,?,?,?,?)", (main.ADMIN_EMAIL, hash_password(main.ADMIN_PASSWORD), "admin", "Network Administrator", now()))
        regular_user = connection.execute("SELECT id FROM users WHERE email = ?", (main.USER_EMAIL,)).fetchone()
        if not regular_user:
            connection.execute("INSERT INTO users(email,password_hash,role,name,created_at) VALUES(?,?,?,?,?)", (main.USER_EMAIL, hash_password(main.USER_PASSWORD), "user", "Monitoring User", now()))
        if not connection.execute("SELECT id FROM devices LIMIT 1").fetchone():
            connection.execute("INSERT INTO devices(name,host,monitor_type,created_at) VALUES(?,?,?,?)", ("Example HTTPS", "https://example.com", "https", now()))
