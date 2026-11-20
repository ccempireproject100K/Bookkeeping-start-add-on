"""Scheduled integrity pass (safeguard audit 2026-11-19, gap 4).

The audit hash chain (lib/audit.py) makes the trail tamper-evident,
but nothing in production ever VERIFIED it — verify_audit_chain was
called only by its test, so a break would sit undetected forever.
This pass runs the verification, records the outcome in the
``integrity_checks`` collection, and logs any break at ERROR level
with the break point. It runs at app startup (server.py) and daily
from the scheduler (lib/scheduler.py).
"""
import logging

from lib.audit import verify_audit_chain
from lib.base import new_id, now_iso

logger = logging.getLogger("mindatease.integrity")


async def run_integrity_pass(db) -> dict:
    """Verify the audit hash chain and record the outcome.

    Returns the recorded row. Never raises: a failed pass is itself
    recorded and logged, not fatal to the caller — startup must not
    be blocked by an integrity finding, it must be REPORTED."""
    try:
        result = await verify_audit_chain(db)
    except Exception as exc:  # the verifier itself failing is a finding
        logger.exception("Integrity pass: audit chain verification errored")
        result = {"ok": False, "checked": 0, "unchained": 0,
                  "break": {"seq": None,
                            "reason": f"verification errored: {exc}"}}
    row = {"id": new_id(), "kind": "audit_chain", "at": now_iso(),
           "ok": bool(result.get("ok")),
           "checked": result.get("checked", 0),
           "unchained": result.get("unchained", 0),
           "break": result.get("break")}
    try:
        await db.integrity_checks.insert_one(dict(row))
    except Exception:
        logger.exception("Integrity pass: could not record the result")
    if row["ok"]:
        logger.info("Integrity pass: audit chain OK "
                    "(%s chained rows verified, %s pre-chain rows)",
                    row["checked"], row["unchained"])
    else:
        logger.error("Integrity pass: AUDIT CHAIN BREAK detected: %s "
                     "(checked=%s, unchained=%s)",
                     row["break"], row["checked"], row["unchained"])
    return row
