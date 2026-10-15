"""Outbound email via SMTP. Used for security alerts (new-device logins,
sentinel findings). Configuration comes from the environment:

  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
  SMTP_FROM (default SMTP_USER), SMTP_USE_TLS (default true)

When SMTP_HOST is unset, send_email is a graceful no-op returning
("skipped", "SMTP not configured") — the app keeps working and in-app
security alerts still fire.
"""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _config():
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from": os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "")),
        "tls": os.environ.get("SMTP_USE_TLS", "true").lower() not in ("0", "false", "no"),
    }


async def send_email(to: str, subject: str, body: str) -> tuple[str, str | None]:
    """Returns (status, error). status is 'sent' or 'skipped'."""
    cfg = _config()
    if not cfg or not to:
        return ("skipped", "SMTP not configured")
    try:
        msg = EmailMessage()
        msg["From"] = cfg["from"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
            if cfg["tls"]:
                s.starttls()
            if cfg["user"]:
                s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        return ("sent", None)
    except Exception as e:
        log.warning("send_email failed: %s", e)
        return ("skipped", str(e))
