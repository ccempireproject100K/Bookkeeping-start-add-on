"""Full export / restore / scheduled backups (Top-20 #20 — Backups & data-out).

The CPA package (routers/cpa.py) is a strong *preparer* export but partial:
no attachments, no audit trail, no settings, and no way back in. The legacy
backup (routers/backup.py) dumps the main collections as JSON but excludes
the audit log deliberately and has no manifest, no validation, no schedule.

This module is the owner-facing data-out layer, built on the same machinery:

- FULL EXPORT — one ZIP holding every business collection (JSON for exact
  restore + CSV for humans), receipt attachment files, workspace settings,
  the audit trail, a README that documents the format so the data is usable
  WITHOUT this app, and a manifest whose counts/totals anchor verification.
- RESTORE — validate first (manifest, counts, journal totals, tenant,
  format version). A failed validation restores NOTHING. A verified restore
  recounts after writing and reports itself failed (and rolls back) if the
  numbers don't match the manifest.
- SCHEDULED BACKUPS — the same ZIP, produced on a cadence into a retained
  list (keep last N, pruned visibly). Default OFF: enabling is a deliberate
  owner act.
- RESTORE TEST — restore a backup into a throwaway scratch workspace,
  verify, report PASS/FAIL, discard the scratch.

Storage: backup ZIPs live on disk under uploads/backups/<tenant_id>/ (the
receipts pattern); metadata lives in the `full_backups` collection and the
schedule in `backup_settings`.
"""
import csv
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.base import new_id, now_iso

FORMAT = "bookkeeping-full-export"
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = {1}

BACKEND_DIR = Path(__file__).resolve().parent.parent
# receipts.py resolves its upload root from its OWN folder
# (routers/uploads/receipts/<tenant_id>/) — match it exactly.
RECEIPTS_UPLOAD_ROOT = BACKEND_DIR / "routers" / "uploads" / "receipts"
BACKUPS_ROOT = BACKEND_DIR / "uploads" / "backups"

# Every tenant-scoped business collection that belongs in a complete export.
# Deliberately EXCLUDED (same reasoning as routers/backup.py): users, login
# attempts, counters, webauthn challenges/credentials, stripe/bank webhook
# inboxes, devices, one-time action tokens — restoring those could lock the
# owner out or replay secrets. The audit log IS included here (read-only
# history; on restore it lands in the target workspace's audit trail).
FULL_COLLECTIONS = [
    # ledger core
    "accounts", "journal_entries", "journal_lines", "fiscal_periods",
    "book_closes",
    # sales / purchasing
    "invoices", "customer_credits", "estimates", "bills", "vendor_credits",
    "expenses", "purchase_orders", "sales", "payment_transactions",
    "payment_reminders", "recurring_invoices", "recurring",
    # people & catalogue
    "customers", "vendors", "contractors", "contractor_payments", "products",
    # jobs & time
    "jobs", "job_costs", "time_entries", "movements",
    # banking
    "bank_accounts", "bank_transactions", "bank_rules", "reconciliations",
    "cash_closes",
    # receipts & mileage (attachment files ride along for receipts)
    "receipts", "mileage_trips", "mileage_vehicles",
    # planning / payroll / tax
    "budgets", "monthly_budgets", "payroll_runs", "payroll_settings",
    "tax_jurisdictions", "tax_payments",
    # misc business records
    "giftcards", "gift_cards", "commission_payouts", "booth_rent_payments",
    "portal_links", "collections_config",
    # history
    "audit_log",
]

DEFAULT_KEEP = 8
DEFAULT_CADENCE_DAYS = 1


# --------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------

def _jsonable(doc):
    """Mongo docs -> JSON-safe (ObjectId/datetime/etc. via str, like backup.py)."""
    return json.loads(json.dumps(doc, default=str))


def _line_amounts(ln):
    """(debit, credit) integer cents from one journal line record.

    Production lines (lib/ledger._write_lines) carry plain ``debit`` /
    ``credit`` fields; early exports and fixtures used ``debit_cents`` /
    ``credit_cents``. Both namings are honored so old archives verify."""
    d = ln.get("debit")
    if d is None:
        d = ln.get("debit_cents")
    c = ln.get("credit")
    if c is None:
        c = ln.get("credit_cents")
    return int(d or 0), int(c or 0)


def _lines_by_entry(lines):
    by_entry = {}
    for ln in lines or []:
        by_entry.setdefault(ln.get("entry_id"), []).append(ln)
    return by_entry


def _effective_lines(entry, by_entry):
    """The lines that count for one entry, amounts normalized to
    debit_cents/credit_cents: its journal_lines records when they exist
    (the production shape), else lines embedded in the entry document
    (archives written before journal_lines existed). Never both — an
    entry is counted from exactly one shape."""
    flat = by_entry.get(entry.get("id")) or []
    if flat:
        out = []
        for ln in flat:
            if ln.get("status") not in (None, "posted"):
                continue
            d, c = _line_amounts(ln)
            out.append({"account_code": ln.get("account_code"),
                        "debit_cents": d, "credit_cents": c})
        return out
    return [{"account_code": ln.get("account_code"),
             "debit_cents": _line_amounts(ln)[0],
             "credit_cents": _line_amounts(ln)[1]}
            for ln in entry.get("lines", []) or []]


def _journal_totals(entries, lines=None):
    """Verification anchor: sums over POSTED journal entries, recomputed
    from the journal_lines store.

    Production journal entries carry NO embedded lines — the lines live
    in the journal_lines collection keyed by entry_id (see
    lib/ledger._write_lines). The previous version of this function
    summed lines embedded in the entry document, a shape production
    never writes, so on real books it compared 0 == 0 and proved
    nothing. Entries with no flattened lines (pre-journal_lines
    archives) fall back to their embedded lines so old exports still
    verify."""
    by_entry = _lines_by_entry(lines)
    debit = credit = 0
    posted = 0
    for e in entries or []:
        if e.get("status") != "posted":
            continue
        posted += 1
        for ln in _effective_lines(e, by_entry):
            debit += ln["debit_cents"]
            credit += ln["credit_cents"]
    return {"posted_entries": posted, "debit_cents": debit,
            "credit_cents": credit, "balanced": debit == credit}


def _csv_for(docs):
    """Flat CSV for humans: scalar fields only; nested values are JSON-only
    (the README says so). Columns = union of scalar keys, id first."""
    keys = []
    for d in docs:
        for k, v in d.items():
            if k == "_id" or isinstance(v, (dict, list)):
                continue
            if k not in keys:
                keys.append(k)
    keys.sort(key=lambda k: (k != "id", k))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(keys)
    for d in docs:
        w.writerow(["" if d.get(k) is None else d.get(k) for k in keys])
    return buf.getvalue()


README = """# Your Bookkeeping data — full export

This ZIP is a complete copy of one workspace's Bookkeeping data. It is
yours: every file below is a plain, documented format (JSON, CSV, and the
original attachment files). You do NOT need Bookkeeping — or us — to read
it. Any spreadsheet opens the CSVs; any programmer can work with the JSON.

## What's inside

- `manifest.json` — the table of contents and the verification anchor:
  the export time, the workspace it came from, the format version, how
  many records are in each collection, the journal totals (posted debit
  and credit cents — they must be equal), and the attachment list.
- `README.md` — this file.
- `settings.json` — the workspace record itself (name, trade/niche,
  currency, and the workspace's own settings fields, exactly as stored).
- `data/<collection>.json` — the complete records of each collection,
  exactly as stored, one JSON array per collection. This is the fidelity
  copy: it is what the restore reads. Money fields ending in `_cents`
  are integer cents (divide by 100 for dollars). Dates are ISO strings.
- `csv/<collection>.csv` — the same records flattened for humans:
  scalar fields only (nested objects/arrays appear in the JSON copy).
  Money is still integer cents here, same as the JSON.
- `reports/trial_balance.csv`, `reports/chart_of_accounts.csv`,
  `reports/general_ledger.csv` — accountant-shaped views of the ledger
  (the same shapes the one-click CPA package produces).
- `attachments/receipts/<file>` — the original receipt files, named
  exactly as the matching record in `data/receipts.json` says in its
  `stored` field.
- `data/audit_log.json` — the workspace's audit trail: who did what,
  when. History, included so nothing about the books is hidden.

## Collections (data/*.json)

accounts (chart of accounts), journal_entries (ledger entries),
journal_lines (the entries' lines, flattened — the manifest's journal
totals are recomputed from these records), fiscal_periods,
book_closes, invoices, customer_credits, estimates, bills,
vendor_credits, expenses, purchase_orders, sales, payment_transactions,
payment_reminders, recurring_invoices, recurring, customers, vendors,
contractors, contractor_payments, products, jobs, job_costs,
time_entries, movements, bank_accounts, bank_transactions, bank_rules,
reconciliations, cash_closes, receipts, mileage_trips, mileage_vehicles,
budgets, monthly_budgets, payroll_runs, payroll_settings,
tax_jurisdictions, tax_payments, giftcards, gift_cards,
commission_payouts, booth_rent_payments, portal_links,
collections_config, audit_log.

Deliberately NOT included: logins/passwords, sessions, passkeys, payment
processor webhook inboxes, and device records. A backup must never become
a way into the account — restoring data must never lock the owner out.

## Restoring

In Bookkeeping: Backup & Restore -> "Restore a full export". The file is
validated against the manifest before anything is written; a file that
fails validation restores nothing and says why in plain words.
"""


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

async def collect_workspace(db, tenant_id):
    """Read the whole workspace: {collection: [docs]}, settings, attachments."""
    data = {}
    for name in FULL_COLLECTIONS:
        docs = await db[name].find({"tenant_id": tenant_id},
                                   {"_id": 0}).to_list(length=500000)
        data[name] = [_jsonable(d) for d in docs]
    tenant = await db.tenants.find_one({"id": tenant_id}, {"_id": 0}) or {}
    attachments = {}  # zip relpath -> bytes
    missing = []
    rdir = RECEIPTS_UPLOAD_ROOT / tenant_id
    for rec in data.get("receipts", []):
        stored = rec.get("stored")
        if not stored:
            continue
        p = rdir / stored
        if p.exists():
            attachments[f"attachments/receipts/{stored}"] = p.read_bytes()
        else:
            missing.append(stored)
    return data, _jsonable(tenant), attachments, missing


def build_manifest(tenant_id, tenant, data, attachments, missing):
    counts = {name: len(docs) for name, docs in data.items()}
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "tenant_id": tenant_id,
        "tenant_name": tenant.get("name"),
        "exported_at": now_iso(),
        "generator": "Bookkeeping full export (Top-20 #20)",
        "collections": counts,
        "documents": sum(counts.values()),
        "journal": _journal_totals(data.get("journal_entries", []),
                                   data.get("journal_lines", [])),
        "attachments": {
            "count": len(attachments),
            "bytes": sum(len(b) for b in attachments.values()),
            "files": sorted(attachments.keys()),
            "missing_files": missing,
        },
    }


def _reports_csvs(data):
    from lib.coa import NAME_OF
    out = {}
    tb_rows = []
    bals = {}
    by_entry = _lines_by_entry(data.get("journal_lines", []))
    for e in data.get("journal_entries", []):
        if e.get("status") != "posted":
            continue
        for ln in _effective_lines(e, by_entry):
            b = bals.setdefault(ln.get("account_code"), [0, 0])
            b[0] += int(ln.get("debit_cents") or 0)
            b[1] += int(ln.get("credit_cents") or 0)
    for code in sorted(bals, key=lambda c: str(c)):
        d, c = bals[code]
        net = d - c
        tb_rows.append([code, NAME_OF.get(code, code),
                        f"{(net if net > 0 else 0) / 100:.2f}",
                        f"{(-net if net < 0 else 0) / 100:.2f}"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Code", "Account", "Debit", "Credit"])
    w.writerows(tb_rows)
    out["reports/trial_balance.csv"] = buf.getvalue()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Entry#", "Memo", "Code", "Account", "Debit", "Credit"])
    for e in sorted(data.get("journal_entries", []),
                    key=lambda x: str(x.get("entry_date") or "")):
        if e.get("status") != "posted":
            continue
        for ln in _effective_lines(e, by_entry):
            w.writerow([e.get("entry_date"), e.get("number"), e.get("memo", ""),
                        ln.get("account_code"),
                        NAME_OF.get(ln.get("account_code"), ""),
                        f"{(ln.get('debit_cents') or 0) / 100:.2f}",
                        f"{(ln.get('credit_cents') or 0) / 100:.2f}"])
    out["reports/general_ledger.csv"] = buf.getvalue()
    return out


async def build_full_export(db, tenant_id):
    """Return (zip_bytes, manifest) for the complete workspace export."""
    data, tenant, attachments, missing = await collect_workspace(db, tenant_id)
    manifest = build_manifest(tenant_id, tenant, data, attachments, missing)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", README)
        z.writestr("settings.json", json.dumps(tenant, indent=2, default=str))
        for name in FULL_COLLECTIONS:
            z.writestr(f"data/{name}.json",
                       json.dumps(data[name], indent=1, default=str))
            z.writestr(f"csv/{name}.csv", _csv_for(data[name]))
        for rel, content in _reports_csvs(data).items():
            z.writestr(rel, content)
        for rel, blob in attachments.items():
            z.writestr(rel, blob)
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue(), manifest


async def export_summary(db, tenant_id):
    """Honest pre-download summary: what's inside, without building the ZIP."""
    counts = {}
    total = 0
    for name in FULL_COLLECTIONS:
        n = await db[name].count_documents({"tenant_id": tenant_id})
        if n:
            counts[name] = n
        total += n
    att_count = att_bytes = 0
    rdir = RECEIPTS_UPLOAD_ROOT / tenant_id
    if rdir.exists():
        for p in rdir.iterdir():
            if p.is_file():
                att_count += 1
                att_bytes += p.stat().st_size
    return {
        "collections": counts,
        "documents": total,
        "attachments": {"count": att_count, "bytes": att_bytes},
        # Rough, plainly labeled estimate: JSON+CSV of the records plus the
        # attachment bytes, before ZIP compression.
        "approx_size_note": "Rough estimate before compression",
        "approx_size_bytes": att_bytes + total * 400,
    }


# --------------------------------------------------------------------------
# Validation (pure: bytes in, verdict out — restores NOTHING on failure)
# --------------------------------------------------------------------------

def parse_and_validate(zip_bytes, expect_tenant_id=None):
    """Return (ok, errors, manifest, data, attachments). Pure — no DB, no disk."""
    errors = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        bad = zf.testzip()
        if bad:
            errors.append(f"The ZIP file is damaged (bad member: {bad}).")
            return False, errors, None, None, None
        names = set(zf.namelist())
    except Exception:
        return False, ["That file is not a readable ZIP archive."], None, None, None

    if "manifest.json" not in names:
        return False, ["This ZIP has no manifest.json — it is not a Bookkeeping "
                       "full export (the CPA package and the old encrypted "
                       "backup cannot be restored here)."], None, None, None
    try:
        manifest = json.loads(zf.read("manifest.json"))
    except Exception:
        return False, ["The manifest.json inside the ZIP cannot be read."] , None, None, None
    if manifest.get("format") != FORMAT:
        errors.append("This ZIP's manifest is not a Bookkeeping full export.")
    version = manifest.get("version")
    if version not in SUPPORTED_VERSIONS:
        errors.append(
            f"This export uses format version {version}, which this "
            f"Bookkeeping does not understand yet (supported: "
            f"{sorted(SUPPORTED_VERSIONS)}). Update Bookkeeping, then retry.")
    if expect_tenant_id is not None and manifest.get("tenant_id") != expect_tenant_id:
        errors.append(
            "This export belongs to a different workspace than the one "
            "selected. Restoring another workspace's data is refused.")
    if errors:
        return False, errors, manifest, None, None

    data = {}
    for name in FULL_COLLECTIONS:
        rel = f"data/{name}.json"
        if rel not in names:
            errors.append(f"The export is missing {rel}.")
            continue
        try:
            docs = json.loads(zf.read(rel))
        except Exception:
            errors.append(f"{rel} cannot be read.")
            continue
        if not isinstance(docs, list):
            errors.append(f"{rel} is not a record list.")
            continue
        data[name] = docs
        want = (manifest.get("collections") or {}).get(name)
        if want is not None and want != len(docs):
            errors.append(
                f"{name}: the manifest says {want} record(s) but the file "
                f"holds {len(docs)}. The export is damaged or was changed.")
    if not errors:
        jt = _journal_totals(data.get("journal_entries", []),
                             data.get("journal_lines", []))
        mj = manifest.get("journal") or {}
        if (jt["debit_cents"] != mj.get("debit_cents")
                or jt["credit_cents"] != mj.get("credit_cents")
                or jt["posted_entries"] != mj.get("posted_entries")):
            errors.append(
                "The journal totals inside the export do not match its "
                "manifest (debits/credits/entries differ). The export is "
                "damaged or was changed — nothing was restored.")
    attachments = {}
    att = manifest.get("attachments") or {}
    for rel in att.get("files", []):
        if rel not in names:
            errors.append(f"An attachment listed in the manifest is missing: {rel}.")
        else:
            attachments[rel] = zf.read(rel)
    if not errors and sum(len(b) for b in attachments.values()) != att.get("bytes"):
        errors.append("The attachment bytes do not add up to the manifest total.")
    return (not errors), errors, manifest, data, attachments


# --------------------------------------------------------------------------
# Restore + verification
# --------------------------------------------------------------------------

async def _tenant_doc_count(db, tenant_id):
    total = 0
    for name in FULL_COLLECTIONS:
        total += await db[name].count_documents({"tenant_id": tenant_id})
    return total


async def restore_into(db, target_tenant_id, data, attachments, uploads_root=None):
    """Replace the target workspace's exported collections with the export's
    records and write its attachment files back. Returns inserted count."""
    root = uploads_root or RECEIPTS_UPLOAD_ROOT
    inserted = 0
    for name in FULL_COLLECTIONS:
        docs = data.get(name, [])
        await db[name].delete_many({"tenant_id": target_tenant_id})
        clean = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            d = dict(d)
            d.pop("_id", None)
            d["tenant_id"] = target_tenant_id
            clean.append(d)
        if clean:
            await db[name].insert_many(clean)
            inserted += len(clean)
    tdir = root / target_tenant_id
    for rel, blob in (attachments or {}).items():
        fname = rel.split("/")[-1]
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / fname).write_bytes(blob)
    return inserted


async def verify_against_manifest(db, tenant_id, manifest):
    """Recount the workspace and compare with the manifest anchor."""
    counts = {}
    ok = True
    problems = []
    for name in FULL_COLLECTIONS:
        n = await db[name].count_documents({"tenant_id": tenant_id})
        counts[name] = n
        want = (manifest.get("collections") or {}).get(name)
        # The audit trail is append-only history: restoring adds its own
        # event, so it verifies as "at least the manifest count", never less.
        if want is not None and (n < want if name == "audit_log" else n != want):
            ok = False
            problems.append(f"{name}: restored {n}, manifest says {want}")
    entries = await db.journal_entries.find(
        {"tenant_id": tenant_id}, {"_id": 0}).to_list(length=500000)
    lines = await db.journal_lines.find(
        {"tenant_id": tenant_id}, {"_id": 0}).to_list(length=500000)
    jt = _journal_totals([_jsonable(e) for e in entries],
                         [_jsonable(ln) for ln in lines])
    mj = manifest.get("journal") or {}
    if (jt["debit_cents"] != mj.get("debit_cents")
            or jt["credit_cents"] != mj.get("credit_cents")):
        ok = False
        problems.append(
            f"journal totals: restored dr {jt['debit_cents']} / cr "
            f"{jt['credit_cents']}, manifest dr {mj.get('debit_cents')} / "
            f"cr {mj.get('credit_cents')}")
    return {"ok": ok, "problems": problems, "counts": counts,
            "documents": sum(counts.values()), "journal": jt}


async def wipe_tenant(db, tenant_id, uploads_root=None):
    for name in FULL_COLLECTIONS:
        await db[name].delete_many({"tenant_id": tenant_id})
    root = uploads_root or RECEIPTS_UPLOAD_ROOT
    tdir = root / tenant_id
    if tdir.exists():
        for p in tdir.iterdir():
            if p.is_file():
                p.unlink()


# --------------------------------------------------------------------------
# Stored backups: schedule, retention, restore test
# --------------------------------------------------------------------------

def _backup_dir(tenant_id):
    d = BACKUPS_ROOT / tenant_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def get_settings(db, tenant_id):
    doc = await db.backup_settings.find_one({"tenant_id": tenant_id},
                                            {"_id": 0})
    if not doc:
        # Default ON, daily (safeguard audit 2026-11-19, gap 3): a
        # workspace that never touches backup settings is protected
        # from day one; opting out is an explicit settings write.
        doc = {"tenant_id": tenant_id, "enabled": True,
               "cadence_days": DEFAULT_CADENCE_DAYS, "keep": DEFAULT_KEEP,
               "last_run_at": None}
    return doc


async def create_stored_backup(db, tenant_id, kind):
    """Build the full export and retain it in the backup list. Returns the
    backup metadata doc; pruning to the keep-limit happens here."""
    zip_bytes, manifest = await build_full_export(db, tenant_id)
    bid = new_id()
    path = _backup_dir(tenant_id) / f"{bid}.zip"
    path.write_bytes(zip_bytes)
    doc = {"id": bid, "tenant_id": tenant_id, "kind": kind,
           "created_at": now_iso(), "size_bytes": len(zip_bytes),
           "documents": manifest["documents"],
           "attachments": manifest["attachments"]["count"],
           "last_test": None}
    await db.full_backups.insert_one(dict(doc))
    pruned = await prune_backups(db, tenant_id)
    doc["pruned"] = pruned
    return doc


async def prune_backups(db, tenant_id):
    """Keep the newest N backups; older ones are deleted (file + record)."""
    settings = await get_settings(db, tenant_id)
    keep = max(1, int(settings.get("keep") or DEFAULT_KEEP))
    docs = await db.full_backups.find({"tenant_id": tenant_id},
                                      {"_id": 0}).to_list(length=10000)
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    pruned = 0
    for old in docs[keep:]:
        p = _backup_dir(tenant_id) / f"{old['id']}.zip"
        if p.exists():
            p.unlink()
        await db.full_backups.delete_one({"id": old["id"],
                                          "tenant_id": tenant_id})
        pruned += 1
    return pruned


async def maybe_run_scheduled(db, tenant_id, now=None):
    """If scheduled backups are ON and one is due, make it. Returns the new
    backup doc, or None. The real scheduler (lib/scheduler.py, daily job)
    calls this for every tenant; backup list reads and the run-due
    endpoint call it too, as a catch-up. This function is the single
    decision point for enabled/due, whichever caller asks."""
    settings = await get_settings(db, tenant_id)
    if not settings.get("enabled"):
        return None
    now = now or datetime.now(timezone.utc)
    last = settings.get("last_run_at")
    due = True
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            due = now >= last_dt + timedelta(
                days=int(settings.get("cadence_days") or DEFAULT_CADENCE_DAYS))
        except Exception:
            due = True
    if not due:
        return None
    doc = await create_stored_backup(db, tenant_id, kind="scheduled")
    # Persist the EFFECTIVE settings alongside last_run_at: with the
    # default-ON shape there may be no settings doc yet, and a doc
    # holding only last_run_at would read back as enabled=None (off)
    # on the next check — the default would silently cancel itself.
    await db.backup_settings.update_one(
        {"tenant_id": tenant_id},
        {"$set": {"tenant_id": tenant_id,
                  "enabled": bool(settings.get("enabled")),
                  "cadence_days": int(settings.get("cadence_days")
                                      or DEFAULT_CADENCE_DAYS),
                  "keep": int(settings.get("keep") or DEFAULT_KEEP),
                  "last_run_at": now_iso()}},
        upsert=True)
    return doc


async def test_stored_backup(db, tenant_id, backup_id):
    """Restore a stored backup into a throwaway scratch workspace, verify
    against its manifest, discard the scratch, and record the outcome."""
    meta = await db.full_backups.find_one(
        {"id": backup_id, "tenant_id": tenant_id}, {"_id": 0})
    if not meta:
        return None
    path = _backup_dir(tenant_id) / f"{backup_id}.zip"
    if not path.exists():
        result = {"passed": False, "at": now_iso(),
                  "detail": "The backup file itself is missing from storage."}
        await db.full_backups.update_one(
            {"id": backup_id, "tenant_id": tenant_id},
            {"$set": {"last_test": result}})
        return result
    ok, errors, manifest, data, attachments = parse_and_validate(
        path.read_bytes(), expect_tenant_id=tenant_id)
    if not ok:
        result = {"passed": False, "at": now_iso(),
                  "detail": "Validation failed: " + " ".join(errors)}
        await db.full_backups.update_one(
            {"id": backup_id, "tenant_id": tenant_id},
            {"$set": {"last_test": result}})
        return result
    scratch = f"__restore_test__{new_id()}"
    try:
        await restore_into(db, scratch, data, attachments)
        # Verification compares the scratch against the manifest; the
        # manifest names the source tenant, so verify counts/totals only.
        ver = await verify_against_manifest(db, scratch, manifest)
        result = {"passed": bool(ver["ok"]), "at": now_iso(),
                  "documents": ver["documents"], "journal": ver["journal"],
                  "detail": ("Restored into a throwaway workspace and every "
                             "count and journal total matched the manifest."
                             if ver["ok"] else
                             "Mismatch: " + "; ".join(ver["problems"]))}
    finally:
        await wipe_tenant(db, scratch)
    await db.full_backups.update_one(
        {"id": backup_id, "tenant_id": tenant_id},
        {"$set": {"last_test": result}})
    return result
