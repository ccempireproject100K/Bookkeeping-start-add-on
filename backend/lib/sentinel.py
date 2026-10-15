"""Sentinel: the always-on security watcher.

Runs on a schedule (cron/systemd every 15 minutes — see docs/sentinel.md)
and scans for signs of intrusion or misuse:

  - brute-force spikes that slipped past per-request limits
  - admin actions in the dead of night (off-hours)
  - bursts of PII viewing (possible data exfiltration)
  - stale backups (no successful export in 7 days, when exports were routine)
  - email digest of unacknowledged critical alerts to workspace owners

Every finding becomes a row in `security_alerts` (visible in Settings →
Security alerts) and, when SMTP is configured, an email. Findings are
de-duplicated so a long attack produces one alert per hour, not hundreds.

This is application-layer monitoring — a smoke detector, not a staffed fire
station. Network/host intrusion detection belongs to the hosting provider.
"""
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from db import db
from lib.alerts import raise_alert
from lib.base import now_iso
from lib.mailer import send_email

log = logging.getLogger(__name__)

BRUTE_FORCE_WINDOW = timedelta(minutes=15)
BRUTE_FORCE_THRESHOLD = 10          # failed logins from one IP in the window
PII_BURST_WINDOW = timedelta(hours=1)
PII_BURST_THRESHOLD = 10            # payout/TIN views per tenant in the window
BACKUP_STALE_AFTER = timedelta(days=7)
DEDUP_WINDOW = timedelta(hours=1)

ADMIN_ACTIONS = {
    "books.close", "books.reopen", "settings.inventory_toggled",
    "team.payout.updated", "auth.sessions.revoke_all", "backup.import",
}
PII_ACTIONS = {"team.payout.viewed"}
CRITICAL_TYPES = {"brute_force", "passkey_clone", "pii_burst", "off_hours_admin",
                  "new_device"}


def _tz():
    try:
        return ZoneInfo(os.environ.get("SENTINEL_TZ", "America/Chicago"))
    except Exception:
        return timezone.utc


def _doc_time(doc) -> datetime | None:
    """Audit rows come from two writers: lib/ledger.audit uses `at`,
    routers/auth._audit uses `created_at`. Both are ISO strings."""
    raw = doc.get("at") or doc.get("created_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _time_gte(iso: str) -> dict:
    return {"$or": [{"at": {"$gte": iso}}, {"created_at": {"$gte": iso}}]}


def _off_hours(dt: datetime) -> bool:
    return dt.astimezone(_tz()).hour < 5


async def _deduped(tenant_id: str, atype: str) -> bool:
    """True if an identical alert fired within the dedup window."""
    cutoff = (datetime.now(timezone.utc) - DEDUP_WINDOW).isoformat()
    return await db.security_alerts.count_documents({
        "tenant_id": tenant_id, "type": atype,
        "created_at": {"$gte": cutoff}}) > 0


async def _tenant_for_email(email: str) -> str | None:
    u = await db.users.find_one({"email": email}, {"tenant_id": 1})
    return u.get("tenant_id") if u else None


async def check_brute_force() -> int:
    """IPs with a burst of failed logins in the window."""
    cutoff = datetime.now(timezone.utc) - BRUTE_FORCE_WINDOW
    cur = db.login_attempts.find({"at": {"$gte": cutoff}}, {"ip": 1, "email": 1})
    by_ip: dict[str, Counter] = {}
    async for doc in cur:
        by_ip.setdefault(doc.get("ip", "?"), Counter())[doc.get("email", "?")] += 1
    raised = 0
    for ip, emails in by_ip.items():
        total = sum(emails.values())
        if total < BRUTE_FORCE_THRESHOLD:
            continue
        tenant_id = await _tenant_for_email(emails.most_common(1)[0][0])
        if not tenant_id or await _deduped(tenant_id, "brute_force"):
            continue
        await raise_alert(
            tenant_id, None, "brute_force",
            "Possible brute-force attack",
            f"{total} failed logins from {ip} in the last 15 minutes. "
            f"Rate limiting is holding; review the attempts if it continues.")
        raised += 1
    return raised


async def check_off_hours() -> int:
    """Admin actions taken between midnight and 5am local time."""
    state = await db.sentinel_state.find_one({"_id": "audit_cursor"})
    since = state["last"] if state else (
        datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    now = datetime.now(timezone.utc)
    cur = db.audit_log.find(_time_gte(since), {"_id": 0})
    raised = 0
    async for doc in cur:
        if doc.get("action") not in ADMIN_ACTIONS:
            continue
        at = _doc_time(doc)
        if not at or not _off_hours(at):
            continue
        tenant_id = doc.get("tenant_id") or ""
        if not tenant_id or await _deduped(tenant_id, "off_hours_admin"):
            continue
        await raise_alert(
            tenant_id, doc.get("actor_id") or doc.get("user_id"), "off_hours_admin",
            "Admin action outside business hours",
            f"{doc.get('action')} was performed at "
            f"{at.astimezone(_tz()).strftime('%-I:%M %p')} local time.")
        raised += 1
    await db.sentinel_state.update_one(
        {"_id": "audit_cursor"}, {"$set": {"last": now.isoformat()}}, upsert=True)
    return raised


async def check_pii_burst() -> int:
    """Many PII views in a short window — possible exfiltration."""
    cutoff = (datetime.now(timezone.utc) - PII_BURST_WINDOW).isoformat()
    cur = db.audit_log.find(
        {**_time_gte(cutoff), "action": {"$in": list(PII_ACTIONS)}},
        {"tenant_id": 1})
    counts: Counter = Counter()
    async for doc in cur:
        if doc.get("tenant_id"):
            counts[doc["tenant_id"]] += 1
    raised = 0
    for tenant_id, n in counts.items():
        if n < PII_BURST_THRESHOLD or await _deduped(tenant_id, "pii_burst"):
            continue
        await raise_alert(
            tenant_id, None, "pii_burst",
            "Unusual volume of sensitive-data views",
            f"{n} payout-account/TIN views in the last hour. If this wasn't "
            f"legitimate payroll or tax work, review who accessed what.")
        raised += 1
    return raised


async def check_backup_stale() -> int:
    """No backup export in 7 days, for tenants that used to export."""
    raised = 0
    tenants = await db.tenants.find({}, {"id": 1}).to_list(1000)
    for t in tenants:
        tid = t["id"]
        rows = await db.audit_log.find(
            {"tenant_id": tid, "action": "backup.export"},
            {"_id": 0, "at": 1, "created_at": 1}).to_list(10)
        times = [_doc_time(r) for r in rows]
        times = [x for x in times if x]
        if not times:
            continue
        last_at = max(times)
        if datetime.now(timezone.utc) - last_at < BACKUP_STALE_AFTER:
            continue
        if await _deduped(tid, "backup_stale"):
            continue
        await raise_alert(
            tid, None, "backup_stale",
            "No backup in over 7 days",
            "This workspace used to export backups but hasn't in a week. "
            "Export a fresh encrypted backup from Backup.")
        raised += 1
    return raised


async def send_digest() -> int:
    """Email owners about unacknowledged critical alerts (SMTP optional)."""
    sent = 0
    tenants = await db.security_alerts.distinct(
        "tenant_id", {"acknowledged_at": None,
                      "type": {"$in": list(CRITICAL_TYPES)}})
    for tid in tenants:
        rows = await db.security_alerts.find(
            {"tenant_id": tid, "acknowledged_at": None,
             "type": {"$in": list(CRITICAL_TYPES)}},
            {"_id": 0, "title": 1, "detail": 1, "created_at": 1,
             "type": 1}).sort("created_at", -1).to_list(20)
        if not rows:
            continue
        owners = await db.users.find(
            {"tenant_id": tid, "role": "owner"}, {"email": 1}).to_list(10)
        body = ("Unacknowledged security alerts for your workspace:\n\n" +
                "\n".join(f"- [{r['type']}] {r['title']}: {r.get('detail', '')}"
                          for r in rows) +
                "\n\nReview them in Settings → Security alerts.")
        for o in owners:
            status, _err = await send_email(
                o.get("email", ""), "[Security] Unacknowledged alerts", body)
            if status == "sent":
                sent += 1
    return sent


async def run_sentinel() -> dict:
    """Run every check. Returns counts per check."""
    results = {}
    for name, fn in (("brute_force", check_brute_force),
                     ("off_hours", check_off_hours),
                     ("pii_burst", check_pii_burst),
                     ("backup_stale", check_backup_stale)):
        try:
            results[name] = await fn()
        except Exception as e:
            log.warning("sentinel check %s failed: %s", name, e)
            results[name] = f"error: {e}"
    try:
        results["digest_emails"] = await send_digest()
    except Exception as e:
        log.warning("sentinel digest failed: %s", e)
        results["digest_emails"] = f"error: {e}"
    await db.sentinel_state.update_one(
        {"_id": "last_run"},
        {"$set": {"at": now_iso(), "results": str(results)}}, upsert=True)
    return results
