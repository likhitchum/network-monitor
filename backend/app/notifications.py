"""Alert delivery: email (SMTP 587/STARTTLS or 465/implicit TLS) and webhooks."""
import asyncio
import json
import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.database import db

logger = logging.getLogger("wwnm")


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
