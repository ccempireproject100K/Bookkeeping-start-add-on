"""Outbound email via Resend (the empire's email lane for invoice delivery).

Configuration comes from the environment:

  RESEND_API_KEY   — owner's Resend key (server-side only, never in code)
  RESEND_FROM      — default From, e.g. "Mind At Ease Books <books@mindateasesolutions.com>"
  RESEND_API_URL   — default https://api.resend.com/emails

House pattern (mirrors lib/mailer.py): when the key is unset, send_email is
a graceful no-op returning ("skipped", "Resend not configured") so the app
keeps working and the send is still RECORDED by the caller.

Test/proof capture: when RESEND_CAPTURE_FILE points at a path, every send is
appended to that file as one JSON line (and no HTTP call is made when the
key is also unset — the capture IS the delivery proof in stub mode).
"""
import asyncio
import json
import logging
import os

import requests

log = logging.getLogger(__name__)


def _config():
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        return None
    return {
        "key": key,
        "from": os.environ.get("RESEND_FROM", "").strip(),
        "url": os.environ.get("RESEND_API_URL", "https://api.resend.com/emails"),
    }


def configured() -> bool:
    return _config() is not None or bool(os.environ.get("RESEND_CAPTURE_FILE"))


def _capture(record: dict):
    path = os.environ.get("RESEND_CAPTURE_FILE", "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:  # capture must never break a send
        log.warning("resend capture failed: %s", e)


def _post(cfg: dict, payload: dict) -> tuple[str, str | None]:
    try:
        r = requests.post(cfg["url"], json=payload, timeout=20, headers={
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
        })
        if r.status_code in (200, 201):
            return ("sent", None)
        return ("failed", f"Resend HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        return ("failed", str(e)[:300])


async def send_email(to: str, subject: str, html: str, text: str = "",
                     from_addr: str = "", reply_to: str = "",
                     attachments: list | None = None) -> tuple[str, str | None]:
    """Returns (status, error). status: 'sent' | 'skipped' | 'failed'.

    attachments: optional list of {"filename": str, "content_b64": str}
    (used by the dispute evidence packet export)."""
    if not to:
        return ("skipped", "No recipient address")
    cfg = _config()
    payload = {
        "from": from_addr or (cfg or {}).get("from") or os.environ.get("RESEND_FROM", ""),
        "to": [to], "subject": subject, "html": html, "text": text or "",
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if attachments:
        payload["attachments"] = [
            {"filename": a["filename"], "content": a["content_b64"]} for a in attachments]
    if cfg is None:
        _capture({"to": to, "subject": subject, "status": "skipped",
                  "reason": "Resend not configured", "html": html})
        return ("skipped", "Resend not configured")
    status, err = await asyncio.to_thread(_post, cfg, payload)
    _capture({"to": to, "subject": subject, "status": status, "error": err, "html": html})
    return (status, err)
