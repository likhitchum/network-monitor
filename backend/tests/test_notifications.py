"""Hermetic tests for app.notifications (email + webhook delivery).

No real network access: SMTP is faked at the smtplib.SMTP/SMTP_SSL layer,
the email worker is faked at module level, and the webhook client is faked
at the httpx.AsyncClient layer.
"""
import asyncio
import logging

import pytest

import app.main as m
import app.notifications as notif

DEVICE = {"id": 1, "name": "Router-1", "host": "10.0.0.1"}


def seed_notification_settings(**values):
    with m.db() as connection:
        for key, value in values.items():
            connection.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"notification.{key}", value),
            )


@pytest.fixture(autouse=True)
def clean_notification_settings():
    """The suite shares one database; keep notification config isolated."""
    with m.db() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'notification.%'")
    yield
    with m.db() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'notification.%'")


# ---------------------------------------------------------------- notify()


async def test_notify_sends_email_with_loaded_settings(monkeypatch):
    seed_notification_settings(smtp_host="smtp.test", recipients='["ops@b.test"]')
    calls = []

    def fake_send_email(settings, recipients, subject, body):
        calls.append((settings, recipients, subject, body))

    monkeypatch.setattr(notif, "send_email", fake_send_email)
    await notif.notify(DEVICE, "critical", "cpu high")

    assert len(calls) == 1
    settings, recipients, subject, body = calls[0]
    assert recipients == ["ops@b.test"]
    assert subject == "[CRITICAL] Router-1"
    assert body == "cpu high"
    assert settings["smtp_host"] == "smtp.test"


async def test_notify_skips_email_when_unconfigured(monkeypatch):
    calls = []
    monkeypatch.setattr(notif, "send_email", lambda *args: calls.append(args))

    # Host set but no recipients.
    seed_notification_settings(smtp_host="smtp.test")
    await notif.notify(DEVICE, "warning", "msg")

    # Recipients set but no host.
    with m.db() as connection:
        connection.execute("DELETE FROM settings WHERE key='notification.smtp_host'")
    seed_notification_settings(recipients='["ops@b.test"]')
    await notif.notify(DEVICE, "warning", "msg")

    assert calls == []


async def test_notify_posts_webhook_payload(monkeypatch):
    seed_notification_settings(webhook_url="https://hooks.test/abc")
    created, posts = [], []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            created.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            posts.append((url, json))

    monkeypatch.setattr(notif.httpx, "AsyncClient", FakeAsyncClient)
    await notif.notify(DEVICE, "critical", "device down")

    assert created and created[0]["timeout"] == 10
    assert posts == [
        (
            "https://hooks.test/abc",
            {"severity": "critical", "device": "Router-1", "message": "device down"},
        )
    ]


async def test_notify_email_failure_is_logged_not_raised(monkeypatch, caplog):
    seed_notification_settings(smtp_host="smtp.test", recipients='["ops@b.test"]')

    def boom(settings, recipients, subject, body):
        raise RuntimeError("smtp refused connection")

    monkeypatch.setattr(notif, "send_email", boom)
    # The shared "wwnm" logger has propagate=False (set in app.main), so caplog
    # sees nothing unless we re-enable propagation for this test only.
    monkeypatch.setattr(m.logger, "propagate", True)
    with caplog.at_level(logging.ERROR, logger="wwnm"):
        await notif.notify(DEVICE, "critical", "msg")

    assert "email notification failed" in caplog.text
    assert "Router-1" in caplog.text


async def test_notify_webhook_failure_is_logged_not_raised(monkeypatch, caplog):
    seed_notification_settings(webhook_url="https://hooks.test/abc")

    class FailingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            raise RuntimeError("dns failure")

    monkeypatch.setattr(notif.httpx, "AsyncClient", FailingClient)
    monkeypatch.setattr(m.logger, "propagate", True)
    with caplog.at_level(logging.WARNING, logger="wwnm"):
        await notif.notify(DEVICE, "critical", "msg")

    assert "webhook notification failed" in caplog.text


async def test_notify_reraises_cancellation_from_email(monkeypatch):
    seed_notification_settings(smtp_host="smtp.test", recipients='["ops@b.test"]')

    async def cancelled_to_thread(func, *args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(notif.asyncio, "to_thread", cancelled_to_thread)
    with pytest.raises(asyncio.CancelledError):
        await notif.notify(DEVICE, "critical", "msg")


async def test_notify_reraises_cancellation_from_webhook(monkeypatch):
    seed_notification_settings(webhook_url="https://hooks.test/abc")

    class CancellingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            raise asyncio.CancelledError()

    monkeypatch.setattr(notif.httpx, "AsyncClient", CancellingClient)
    with pytest.raises(asyncio.CancelledError):
        await notif.notify(DEVICE, "critical", "msg")


# ------------------------------------------------------------- send_email()


def test_send_email_uses_starttls_on_587(monkeypatch):
    created, ssl_created = [], []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port, self.ops = host, port, []
            created.append(self)

        def starttls(self):
            self.ops.append("starttls")

        def login(self, user, password):
            self.ops.append(("login", user, password))

        def send_message(self, message):
            self.ops.append(("send", message["To"], message["Subject"]))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeSMTPSSL:
        def __init__(self, *args, **kwargs):
            ssl_created.append(args)

    monkeypatch.setattr(notif.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notif.smtplib, "SMTP_SSL", FakeSMTPSSL)

    settings = {
        "smtp_host": "smtp.test",
        "smtp_port": "587",
        "smtp_from": "monitor@test",
        "smtp_username": "user1",
        "smtp_password": "pass1",
    }
    notif.send_email(settings, ["ops@b.test"], "Subject line", "Body text")

    assert ssl_created == []
    assert len(created) == 1
    server = created[0]
    assert (server.host, server.port) == ("smtp.test", 587)
    assert server.ops[0] == "starttls"
    assert server.ops[1] == ("login", "user1", "pass1")
    assert server.ops[2] == ("send", "ops@b.test", "Subject line")


def test_send_email_uses_implicit_tls_on_465(monkeypatch):
    created, plain_created = [], []

    class FakeSMTPSSL:
        def __init__(self, host, port, timeout=None):
            self.host, self.port, self.ops = host, port, []
            created.append(self)

        def login(self, user, password):
            self.ops.append(("login", user, password))

        def send_message(self, message):
            self.ops.append(("send", message["From"], message["To"]))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            plain_created.append(args)

    monkeypatch.setattr(notif.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notif.smtplib, "SMTP_SSL", FakeSMTPSSL)

    settings = {
        "smtp_host": "smtp.test",
        "smtp_port": 465,
        "smtp_from": "monitor@test",
        "smtp_username": "user1",
        "smtp_password": "pass1",
    }
    notif.send_email(settings, ["ops@b.test"], "Subject line", "Body text")

    assert plain_created == []  # 465 must never touch the STARTTLS path
    assert len(created) == 1
    server = created[0]
    assert (server.host, server.port) == ("smtp.test", 465)
    assert server.ops == [("login", "user1", "pass1"), ("send", "monitor@test", "ops@b.test")]


def test_send_email_defaults_to_587_when_port_missing(monkeypatch):
    created = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port
            created.append(self)

        def starttls(self):
            pass

        def login(self, user, password):
            pass

        def send_message(self, message):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(notif.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notif.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("SMTP_SSL must not be used without port 465"))

    settings = {"smtp_host": "smtp.test", "smtp_from": "monitor@test", "smtp_username": "u", "smtp_password": "p"}
    notif.send_email(settings, ["ops@b.test"], "s", "b")

    assert created and created[0].port == 587
