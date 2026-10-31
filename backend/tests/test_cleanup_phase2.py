"""Cleanup Console Phase 2 tests (2026-10-30).

Pins the four Phase 2 behaviors:
1. Import runs are persisted and the dashboard's files-cleaned metric
   is counted from those rows (blocked runs are evidence, not files).
2. Dedupe scans persist run rows (manual AND scheduled), scheduled
   scans are per-tenant opt-in and only ever create SUGGESTIONS.
3. Saved import profiles hold learned per-rule decisions: held changes
   are reported, never applied; per-run overrides win; learning happens
   only when explicitly requested.
4. A dismissed suggestion can be restored (dismiss undo) — tenant-scoped.

Same fake-db idiom as test_import_cleanup.py, plus one emulation the
scheduler needs: inserting a scheduler_runs row whose deterministic
``id`` already exists raises DuplicateKeyError (the unique index the
production claim path relies on).
"""
import asyncio
import hashlib
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ------------------------------------------------------------------ fakes
class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, spec):
        for key, direction in reversed(spec or []):
            self._docs.sort(key=lambda d: (d.get(key) is None, d.get(key)),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n=None, length=None):
        return self._docs[:(n if n is not None else length)]


class FakeColl:
    def __init__(self):
        self.docs = {}
        self._seq = 0
        self.unique_ids = False  # only scheduler_runs emulates the index

    def _match(self, doc, q):
        for k, v in (q or {}).items():
            dv = doc.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$ne" in v and dv == v["$ne"]:
                    return False
                if "$exists" in v and ((k in doc) != bool(v["$exists"])):
                    return False
            elif dv != v:
                return False
        return True

    async def insert_one(self, doc):
        if self.unique_ids and doc.get("id") is not None and any(
                d.get("id") == doc["id"] for d in self.docs.values()):
            raise DuplicateKeyError("duplicate id")
        self._seq += 1
        doc.setdefault("_id", f"oid{self._seq}")
        self.docs[doc["_id"]] = dict(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)

    def find(self, q=None, proj=None):
        return FakeCursor([dict(d) for d in self.docs.values()
                           if self._match(d, q)])

    async def find_one(self, q=None, proj=None):
        for d in self.docs.values():
            if self._match(d, q or {}):
                return dict(d)
        return None

    async def update_one(self, q, upd, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                for k, v in (upd.get("$set") or {}).items():
                    d[k] = v
                for k, v in (upd.get("$addToSet") or {}).items():
                    cur = d.setdefault(k, [])
                    if v not in cur:
                        cur.append(v)
                return type("R", (), {"modified_count": 1})()
        return type("R", (), {"modified_count": 0})()

    async def delete_one(self, q):
        for k, d in list(self.docs.items()):
            if self._match(d, q):
                del self.docs[k]
                return type("R", (), {"deleted_count": 1})()
        return type("R", (), {"deleted_count": 0})()

    async def count_documents(self, q=None):
        return sum(1 for d in self.docs.values() if self._match(d, q or {}))


COLL_NAMES = ("customers", "vendors", "products", "import_profiles",
              "dedupe_suggestions", "entity_merges", "audit_log",
              "import_runs", "dedupe_scan_runs", "dedupe_schedule_configs",
              "tenants", "scheduler_runs")


class FakeDB:
    def __init__(self):
        for name in COLL_NAMES:
            setattr(self, name, FakeColl())
        self.scheduler_runs.unique_ids = True


USER1 = {"id": "u1", "tenant_id": "t1", "email": "owner@shop.example",
         "role": "owner", "permissions": ["*"]}
USER2 = {"id": "u2", "tenant_id": "t2", "email": "other@shop.example",
         "role": "owner", "permissions": ["*"]}

PHONE_RULE = "phone:libphonenumber-e164"
EMAIL_RULE = "email:trim-and-lowercase"

CSV_TWO = ("Client,E-mail,Phone,Address\n"
           "Acme Supply Inc,ops@acme.example,(512) 555-0134,"
           "\"9 Oak Ave, Dallas, TX 75001\"\n"
           "Lone Star Plumbing,,512.555.0142,"
           "\"77 Ranch Rd, Austin, TX 78701\"\n")
BAD_EMAIL_CSV = "Client,E-mail\nA Co,x\nB Co,y\nC Co,z\nD Co,w\n"

DUPE_ROWS = [
    {"id": "c1", "name": "Acme Supply Inc", "phone": "(512) 555-0111",
     "email": "a@acme.example", "address": ""},
    {"id": "c2", "name": "ACME SUPPLY, INC.", "phone": "512.555.0111",
     "email": "a@acme.example", "address": ""},
    {"id": "c3", "name": "Unrelated Co", "phone": "(512) 555-0999",
     "email": "u@u.example", "address": ""},
]


def _patch_all(fake):
    import routers.dashboard as DASH
    import routers.dedupe as DD
    import routers.importer as IMP
    IMP.db = fake
    DD.db = fake
    DASH.db = fake
    return IMP, DD, DASH


def _seed(fake, coll_name, user, rows):
    from lib.base import Scoped
    coll = Scoped(getattr(fake, coll_name), user["tenant_id"])
    for r in rows:
        run(coll.insert_one(dict(r)))


def _expect_http(status, fn, *args, **kwargs):
    try:
        run(fn(*args, **kwargs))
    except HTTPException as e:
        assert e.status_code == status, e.status_code
        return e
    raise AssertionError(f"expected HTTP {status}")


# ------------------------------------------------------- 1. import runs
def test_import_run_is_persisted_and_counted():
    fake = FakeDB()
    IMP, _DD, DASH = _patch_all(fake)
    out = run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=CSV_TWO, email_summary=False,
        file_name="clients.csv"), USER1))
    assert out["created"] == 2 and out["import_run_id"]
    rows = [d for d in fake.import_runs.docs.values()
            if d["tenant_id"] == "t1"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == out["import_run_id"]
    assert row["status"] == "completed" and row["cleanup_applied"] is True
    assert row["file_name"] == "clients.csv"
    assert row["file_hash"] == hashlib.sha256(
        CSV_TWO.encode("utf-8")).hexdigest()
    assert row["rows_total"] == 2 and row["rows_created"] == 2
    assert row["changes"] > 0 and row["changes_by_rule"]
    assert row["schema_override"] is False
    # The dashboard metric is now REAL, counted from the persisted run.
    dq = run(DASH.data_quality(USER1))
    assert dq["files_cleaned_available"] is True
    assert dq["files_cleaned_this_month"] == 1
    assert dq["files_cleaned_tracking_since"] == row["created_at"]
    assert dq["import_runs_by_week"][-1]["files"] == 1
    assert sum(b["files"] for b in dq["import_runs_by_week"]) == 1
    # Tenant isolation: the other tenant has no runs and no count.
    dq2 = run(DASH.data_quality(USER2))
    assert dq2["files_cleaned_this_month"] == 0
    assert dq2["files_cleaned_tracking_since"] is None
    assert run(IMP.list_import_runs(USER2))["runs"] == []


def test_blocked_run_is_recorded_but_never_counted():
    fake = FakeDB()
    IMP, _DD, DASH = _patch_all(fake)
    _expect_http(422, IMP.run_import, IMP.RunIn(
        kind="customers", csv_text=BAD_EMAIL_CSV, email_summary=False),
        USER1)
    rows = list(fake.import_runs.docs.values())
    assert len(rows) == 1 and rows[0]["status"] == "blocked"
    assert rows[0]["schema_blocking"]
    dq = run(DASH.data_quality(USER1))
    assert dq["files_cleaned_this_month"] == 0
    # ...but tracking DID begin (the blocked attempt is on record).
    assert dq["files_cleaned_tracking_since"] == rows[0]["created_at"]
    # The override path completes and the run row carries the reason.
    out = run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=BAD_EMAIL_CSV, email_summary=False,
        override_schema=True, override_reason="legacy export, verified"),
        USER1))
    assert out["cleanup"]["schema_override"] is True
    done = [d for d in fake.import_runs.docs.values()
            if d["status"] == "completed"]
    assert len(done) == 1 and done[0]["schema_override"] is True
    assert done[0]["override_reason"] == "legacy export, verified"
    assert run(DASH.data_quality(USER1))["files_cleaned_this_month"] == 1


def test_run_without_cleanup_is_persisted_but_not_a_cleaned_file():
    fake = FakeDB()
    IMP, _DD, DASH = _patch_all(fake)
    run(IMP.run_import(IMP.RunIn(kind="customers", csv_text=CSV_TWO,
                                 cleanup=False, email_summary=False), USER1))
    rows = list(fake.import_runs.docs.values())
    assert len(rows) == 1 and rows[0]["cleanup_applied"] is False
    assert run(DASH.data_quality(USER1))["files_cleaned_this_month"] == 0


# ------------------------------------------------- 3. profiles and rules
def _make_profile(fake, IMP):
    run(IMP.run_import(IMP.RunIn(kind="customers", csv_text=CSV_TWO,
                                 save_profile_name="Client Sheet",
                                 email_summary=False), USER1))
    profiles = run(IMP.list_profiles(USER1))["profiles"]
    assert len(profiles) == 1
    return profiles[0]


# A row whose name needs the whitespace rule while its phone needs the
# phone rule — proves one rule can be held while another still applies.
CSV_MESSY_NAME = ("Client,E-mail,Phone,Address\n"
                  "  Acme   Supply Inc ,ops@acme.example,(512) 555-0134,"
                  "\"9 Oak Ave, Dallas, TX 75001\"\n")


def test_profile_rules_hold_changes_and_stay_inspectable():
    fake = FakeDB()
    IMP, _DD, _DASH = _patch_all(fake)
    _seed(fake, "import_profiles", USER1, [{
        "id": "prof1", "name": "Client Sheet", "kind": "customers",
        "column_map": {"Client": "name", "E-mail": "email",
                       "Phone": "phone", "Address": "address"},
        "created_at": "2026-10-01T00:00:00+00:00"}])
    pid = "prof1"
    updated = run(IMP.set_profile_cleanup_rules(
        pid, IMP.CleanupRulesIn(cleanup_rules={PHONE_RULE: False}), USER1))
    assert updated["cleanup_rules"] == {PHONE_RULE: False}
    # Preview with the profile: the phone normalization is HELD (shown,
    # not applied) and rule_states names the profile as the decider,
    # while the name rule still applies normally.
    prev = run(IMP.cleanup_preview(IMP.CleanupPreviewIn(
        kind="customers", csv_text=CSV_MESSY_NAME, profile_id=pid), USER1))
    assert prev["profile"] == {"id": pid, "name": "Client Sheet"}
    assert prev["rule_states"][PHONE_RULE] == {
        "enabled": False, "source": "profile"}
    assert prev["held_in_first_50_rows"] == 1
    held_fields = {c["field"] for rows in prev["held_changes"].values()
                   for c in rows}
    assert held_fields == {"phone"}
    applied_fields = {c["field"]
                      for rows in prev["normalization_preview"].values()
                      for c in rows}
    assert applied_fields == {"name"}
    # Run with the profile: phones are stored verbatim, names cleaned.
    out = run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=CSV_MESSY_NAME, profile_id=pid,
        email_summary=False), USER1))
    assert out["created"] == 1
    assert out["cleanup"]["held_changes"] == 1
    assert out["cleanup"]["rules_held"] == [PHONE_RULE]
    imported = [d for d in fake.customers.docs.values()
                if d.get("tenant_id") == "t1"
                and d.get("imported_via") == "guided_importer"]
    assert len(imported) == 1
    assert imported[0]["name"] == "Acme Supply Inc"      # name rule applied
    assert imported[0]["phone"] == "(512) 555-0134"      # phone rule held


def test_run_override_wins_over_profile_and_learning_is_explicit():
    fake = FakeDB()
    IMP, _DD, _DASH = _patch_all(fake)
    pid = _make_profile(fake, IMP)["id"]
    run(IMP.set_profile_cleanup_rules(
        pid, IMP.CleanupRulesIn(cleanup_rules={PHONE_RULE: False}), USER1))
    # A per-run override re-enables the phone rule for this run only.
    prev = run(IMP.cleanup_preview(IMP.CleanupPreviewIn(
        kind="customers", csv_text=CSV_TWO, profile_id=pid,
        rule_overrides={PHONE_RULE: True}), USER1))
    assert prev["rule_states"][PHONE_RULE] == {
        "enabled": True, "source": "run"}
    assert prev["held_in_first_50_rows"] == 0
    # Overrides WITHOUT learn_rules change nothing on the profile.
    run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=CSV_TWO, profile_id=pid,
        rule_overrides={EMAIL_RULE: False}, email_summary=False), USER1))
    prof = run(IMP.list_profiles(USER1))["profiles"][0]
    assert prof["cleanup_rules"] == {PHONE_RULE: False}
    # WITH learn_rules the explicit choice is written back, inspectably.
    run(IMP.run_import(IMP.RunIn(
        kind="customers", csv_text=CSV_TWO, profile_id=pid,
        rule_overrides={EMAIL_RULE: False}, learn_rules=True,
        email_summary=False), USER1))
    prof = run(IMP.list_profiles(USER1))["profiles"][0]
    assert prof["cleanup_rules"] == {PHONE_RULE: False, EMAIL_RULE: False}
    prev = run(IMP.cleanup_preview(IMP.CleanupPreviewIn(
        kind="customers", csv_text=CSV_TWO, profile_id=pid), USER1))
    assert prev["rule_states"][EMAIL_RULE] == {
        "enabled": False, "source": "profile"}


def test_profile_rule_validation_and_tenant_isolation():
    fake = FakeDB()
    IMP, _DD, _DASH = _patch_all(fake)
    pid = _make_profile(fake, IMP)["id"]
    # Unknown rule names are rejected, never silently learned.
    _expect_http(422, IMP.set_profile_cleanup_rules, pid,
                 IMP.CleanupRulesIn(cleanup_rules={"bogus:rule": False}),
                 USER1)
    _expect_http(422, IMP.cleanup_preview, IMP.CleanupPreviewIn(
        kind="customers", csv_text=CSV_TWO,
        rule_overrides={"bogus:rule": True}), USER1)
    # Another tenant cannot see or use the profile at all.
    assert run(IMP.list_profiles(USER2))["profiles"] == []
    _expect_http(404, IMP.set_profile_cleanup_rules, pid,
                 IMP.CleanupRulesIn(cleanup_rules={PHONE_RULE: False}),
                 USER2)
    _expect_http(404, IMP.cleanup_preview, IMP.CleanupPreviewIn(
        kind="customers", csv_text=CSV_TWO, profile_id=pid), USER2)
    # A bank-importer profile (same collection, no `kind`) is invisible
    # to the guided importer and cannot be given cleanup rules.
    _seed(fake, "import_profiles", USER1, [
        {"id": "bankp", "name": "Bank CSV",
         "column_map": {"Date": "date"}, "dedupe_mode": "exact"}])
    names = [p["name"] for p in run(IMP.list_profiles(USER1))["profiles"]]
    assert names == ["Client Sheet"]
    _expect_http(404, IMP.set_profile_cleanup_rules, "bankp",
                 IMP.CleanupRulesIn(cleanup_rules={PHONE_RULE: False}),
                 USER1)


def test_clean_row_full_holds_without_applying():
    from lib.dataclean.cleaning import clean_row, clean_row_full
    doc = {"name": "  Acme   Supply ", "phone": "(512) 555-0134",
           "email": " OPS@ACME.EXAMPLE "}
    out, applied, held = clean_row_full(doc, "customers", {PHONE_RULE})
    assert out["phone"] == "(512) 555-0134"          # held → verbatim
    assert [c["rule"] for c in held] == [PHONE_RULE]
    assert {c["rule"] for c in applied} == {
        "name:whitespace", EMAIL_RULE}
    assert out["name_key"]  # matching key survives a held name rule
    # Default behavior is byte-identical to clean_row.
    out2, applied2, held2 = clean_row_full(doc, "customers")
    legacy_out, legacy_changes = clean_row(doc, "customers")
    assert held2 == [] and out2 == legacy_out and applied2 == legacy_changes


# ------------------------------------------- 2 & 4. scans, schedule, undo
def test_scan_persists_exact_run_rows_including_zero_results():
    fake = FakeDB()
    _IMP, DD, DASH = _patch_all(fake)
    _seed(fake, "customers", USER1, DUPE_ROWS)
    out = run(DD.scan(DD.ScanIn(entity="customers"), USER1))
    runs = [d for d in fake.dedupe_scan_runs.docs.values()
            if d["tenant_id"] == "t1"]
    assert len(runs) == 1
    assert runs[0]["id"] == out["scan_run_id"]
    assert runs[0]["trigger"] == "manual" and runs[0]["status"] == "done"
    assert runs[0]["suggestions_new"] == 1 and runs[0]["finished_at"]
    dq = run(DASH.data_quality(USER1))
    assert dq["last_scan_at"] == runs[0]["finished_at"]
    # Zero-result scan: no suggestions exist for t2, yet "last scan" is
    # still an exact persisted fact, not an inference.
    out2 = run(DD.scan(DD.ScanIn(entity="vendors"), USER2))
    assert out2["suggestions"] == []
    runs2 = [d for d in fake.dedupe_scan_runs.docs.values()
             if d["tenant_id"] == "t2"]
    assert len(runs2) == 1 and runs2[0]["suggestions_new"] == 0
    assert run(DASH.data_quality(USER2))["last_scan_at"] == \
        runs2[0]["finished_at"]


def test_schedule_config_defaults_validation_and_isolation():
    fake = FakeDB()
    _IMP, DD, _DASH = _patch_all(fake)
    cfg = run(DD.read_schedule(USER1))
    assert cfg["enabled"] is False and cfg["frequency"] == "weekly"
    assert cfg["last_scan_at"] is None and cfg["next_scan_at"] is None
    _expect_http(422, DD.write_schedule,
                 DD.ScheduleIn(enabled=True, frequency="hourly"), USER1)
    _expect_http(422, DD.write_schedule,
                 DD.ScheduleIn(enabled=True, frequency="daily",
                               entities=[]), USER1)
    saved = run(DD.write_schedule(
        DD.ScheduleIn(enabled=True, frequency="daily",
                      entities=["customers"]), USER1))
    assert saved["enabled"] is True and saved["entities"] == ["customers"]
    # Tenant isolation: t2 still sees the defaults.
    cfg2 = run(DD.read_schedule(USER2))
    assert cfg2["enabled"] is False and cfg2["frequency"] == "weekly"
    audits = [d for d in fake.audit_log.docs.values()
              if d.get("action") == "dedupe.schedule_update"]
    assert len(audits) == 1 and audits[0]["tenant_id"] == "t1"


def test_next_scan_at_math():
    from routers.dedupe import next_scan_at
    stamp = "2026-10-30T06:00:00+00:00"
    assert next_scan_at({"enabled": False, "frequency": "daily"},
                        stamp) is None
    assert next_scan_at({"enabled": True, "frequency": "daily"},
                        None) is None
    assert next_scan_at({"enabled": True, "frequency": "daily"},
                        stamp) == "2026-10-31T06:00:00+00:00"
    assert next_scan_at({"enabled": True, "frequency": "weekly"},
                        stamp) == "2026-11-06T06:00:00+00:00"


def test_scheduled_sweep_scans_opted_in_tenants_only_and_never_merges():
    import lib.scheduler as SCH
    fake = FakeDB()
    _IMP, DD, _DASH = _patch_all(fake)
    SCH.db = fake
    run(fake.tenants.insert_one({"id": "t1"}))
    run(fake.tenants.insert_one({"id": "t2"}))
    _seed(fake, "customers", USER1, DUPE_ROWS)
    _seed(fake, "customers", USER2, DUPE_ROWS)
    run(DD.write_schedule(DD.ScheduleIn(
        enabled=True, frequency="daily", entities=["customers"]), USER1))
    outcome = run(SCH.run_dedupe_scans())
    assert outcome["tenants"] == 1 and outcome["errors"] == 0
    runs = [d for d in fake.dedupe_scan_runs.docs.values()]
    assert len(runs) == 1 and runs[0]["tenant_id"] == "t1"
    assert runs[0]["trigger"] == "scheduled"
    # Suggestions only — the sweep merged nothing, anywhere.
    assert len(fake.entity_merges.docs) == 0
    assert all("merged_into" not in d for d in fake.customers.docs.values())
    pend = [d for d in fake.dedupe_suggestions.docs.values()
            if d["tenant_id"] == "t1" and d["status"] == "pending"]
    assert len(pend) == 1
    assert not [d for d in fake.dedupe_suggestions.docs.values()
                if d["tenant_id"] == "t2"]
    # Same window again: the claim blocks a second scan.
    outcome2 = run(SCH.run_dedupe_scans())
    assert outcome2["tenants"] == 0
    assert outcome2["skipped"] == [
        {"tenant_id": "t1", "reason": "already_scanned_this_window"}]
    assert len(fake.dedupe_scan_runs.docs) == 1


def test_dismiss_restore_round_trip():
    fake = FakeDB()
    _IMP, DD, _DASH = _patch_all(fake)
    _seed(fake, "customers", USER1, DUPE_ROWS)
    out = run(DD.scan(DD.ScanIn(entity="customers"), USER1))
    sid = out["suggestions"][0]["id"]
    run(DD.dismiss(sid, USER1))
    doc = [d for d in fake.dedupe_suggestions.docs.values()
           if d["id"] == sid][0]
    assert doc["status"] == "dismissed" and doc["dismissed_at"]
    dismissed = run(DD.list_suggestions(
        entity="customers", status="dismissed", user=USER1))
    assert [s["id"] for s in dismissed["suggestions"]] == [sid]
    # Restore is tenant-scoped: the other tenant cannot even see it.
    _expect_http(404, DD.restore, sid, USER2)
    back = run(DD.restore(sid, USER1))
    assert back == {"id": sid, "status": "pending"}
    doc = [d for d in fake.dedupe_suggestions.docs.values()
           if d["id"] == sid][0]
    assert doc["status"] == "pending" and doc["restored_at"]
    # Restoring a non-dismissed suggestion is a conflict, and the audit
    # trail carries both halves of the undo story.
    _expect_http(409, DD.restore, sid, USER1)
    actions = {d.get("action") for d in fake.audit_log.docs.values()}
    assert "dedupe.dismiss" in actions
    assert "dedupe.dismiss_restore" in actions


def test_dismissed_pair_survives_a_rescan():
    fake = FakeDB()
    _IMP, DD, _DASH = _patch_all(fake)
    _seed(fake, "customers", USER1, DUPE_ROWS)
    out = run(DD.scan(DD.ScanIn(entity="customers"), USER1))
    sid = out["suggestions"][0]["id"]
    run(DD.dismiss(sid, USER1))
    run(DD.scan(DD.ScanIn(entity="customers"), USER1))
    doc = [d for d in fake.dedupe_suggestions.docs.values()
           if d["id"] == sid][0]
    assert doc["status"] == "dismissed"  # refresh never rewrites history
