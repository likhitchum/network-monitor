"""Central configuration: every value is read from the environment once at import.

Tests patch these names on ``app.main`` (e.g. ``monkeypatch.setattr(m, "SECRET", ...)``),
so consumers must resolve them lazily via the main module — see app/compat.py.
"""
import os

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
