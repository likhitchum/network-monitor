"""Pydantic request models shared across routers."""
from pydantic import BaseModel, Field


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
