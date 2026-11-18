"""Invoice Assistant — rundown greeting + scheduled in-app notifications
(owner addition 2026-11-14). Reuses the FakeDB fixtures from
test_invoice_delivery.py; adds the invoice_assistant module to the patch.
"""
import pytest

from test_invoice_delivery import (env, run, make_customer, make_invoice,  # noqa: F401
                                   stored_invoice, USER)
from lib.base import Scoped, today_str

from routers import invoice_assistant as IA

TID = USER["tenant_id"]
TODAY = today_str()


@pytest.fixture()
def aenv(env, monkeypatch):
    monkeypatch.setattr(IA, "db", env.db)
    return env


def _set_invoice(env, inv_id, **fields):
    run(Scoped(env.db.invoices, TID).update_one({"id": inv_id},
                                               {"$set": fields}))


def test_rundown_counts_from_live_data(aenv):
    cust = make_customer(aenv)
    inv_open = make_invoice(aenv, amount=100.0, customer_id=cust["id"])  # pending, issued today
    inv_sent = make_invoice(aenv, amount=200.0, customer_id=cust["id"], due="2000-01-01")
    _set_invoice(aenv, inv_sent["id"],
                 sends=[{"channel": "email", "status": "sent", "at": TODAY}],
                 first_viewed_at=f"{TODAY}T10:00:00+00:00")
    inv_paid = make_invoice(aenv, amount=50.0, customer_id=cust["id"])
    _set_invoice(aenv, inv_paid["id"], status="paid", paid_cents=5000,
                 payments=[{"date": TODAY, "amount_cents": 5000,
                            "method": "bank"}])
    inv_old = make_invoice(aenv, amount=70.0, customer_id=cust["id"])
    _set_invoice(aenv, inv_old["id"], created_at="2020-01-01T00:00:00+00:00")

    r = run(IA.compute_rundown(TID, f"{TODAY}T12:00:00+00:00"))
    assert r["open"] == 3               # open, sent, old (paid excluded)
    assert r["pending"] == 2            # open + old never sent
    assert r["sent_waiting"] == 1
    assert r["viewed"] == 1
    assert r["overdue"] == 1            # sent one, due 2000-01-01
    assert r["issued_today"] == 3       # old one excluded
    assert r["paid_today_cents"] == 5000
    assert r["paid_today_count"] == 1
    assert r["outstanding_cents"] == 10000 + 20000 + 7000
    text = IA.rundown_text(r)
    assert "3 invoices open" in text and "$370.00 outstanding" in text
    assert "$50.00 paid today" in text


def test_settings_default_off_and_roundtrip(aenv):
    s = run(IA.get_settings(TID, USER["id"]))
    assert s["frequency"] == "off"  # sane default: no spam
    out = run(IA.write_settings(
        IA.SettingsBody(frequency="twice_daily", times=["16:00", "08:30"]),
        USER))
    assert out["frequency"] == "twice_daily"
    assert out["times"] == ["08:30", "16:00"]  # sorted
    again = run(IA.get_settings(TID, USER["id"]))
    assert again["frequency"] == "twice_daily"


def test_settings_validation(aenv):
    with pytest.raises(Exception) as ei:
        run(IA.write_settings(
            IA.SettingsBody(frequency="daily", times=["08:00", "09:00"]),
            USER))
    assert getattr(ei.value, "status_code", None) == 422
    with pytest.raises(Exception) as ei2:
        run(IA.write_settings(
            IA.SettingsBody(frequency="daily", times=["9am"]), USER))
    assert getattr(ei2.value, "status_code", None) == 422


def test_run_due_off_creates_nothing(aenv):
    make_invoice(aenv)
    out = run(IA.generate_due_rundowns(TID, USER["id"], f"{TODAY}T23:00:00+00:00"))
    assert out["created"] == []
    lst = run(IA.list_notifications(USER))
    assert lst["notifications"] == [] and lst["unread"] == 0


def test_run_due_respects_slot_time_and_dedupes(aenv):
    make_invoice(aenv, amount=120.0)
    run(IA.write_settings(IA.SettingsBody(frequency="daily", times=["09:00"]),
                          USER))
    early = run(IA.generate_due_rundowns(TID, USER["id"],
                                         f"{TODAY}T08:00:00+00:00"))
    assert early["created"] == []  # slot not due yet
    due = run(IA.generate_due_rundowns(TID, USER["id"],
                                       f"{TODAY}T09:30:00+00:00"))
    assert len(due["created"]) == 1
    again = run(IA.generate_due_rundowns(TID, USER["id"],
                                         f"{TODAY}T10:00:00+00:00"))
    assert again["created"] == []  # dedupe: same slot never fires twice
    lst = run(IA.list_notifications(USER))
    assert len(lst["notifications"]) == 1 and lst["unread"] == 1
    n = lst["notifications"][0]
    assert n["kind"] == "invoice_rundown" and n["link"] == "/invoices"
    assert "invoice" in n["body"].lower()
    run(IA.mark_read(n["id"], USER))
    lst2 = run(IA.list_notifications(USER))
    assert lst2["unread"] == 0


def test_run_due_twice_daily_fires_both_slots_once(aenv):
    make_invoice(aenv)
    run(IA.write_settings(
        IA.SettingsBody(frequency="twice_daily", times=["08:00", "17:00"]),
        USER))
    morning = run(IA.generate_due_rundowns(TID, USER["id"],
                                           f"{TODAY}T09:00:00+00:00"))
    assert len(morning["created"]) == 1  # only the 08:00 slot so far
    evening = run(IA.generate_due_rundowns(TID, USER["id"],
                                           f"{TODAY}T18:00:00+00:00"))
    assert len(evening["created"]) == 1  # 17:00 slot; 08:00 deduped
    lst = run(IA.list_notifications(USER))
    assert len(lst["notifications"]) == 2


def test_rundown_helps_are_data_backed(aenv):
    cust = make_customer(aenv)
    # #12 follow-up: viewed 3 times, still unpaid
    inv_hot = make_invoice(aenv, amount=300.0, customer_id=cust["id"])
    _set_invoice(aenv, inv_hot["id"], timeline=[
        {"event": "viewed", "at": f"{TODAY}T09:00:00+00:00"},
        {"event": "viewed", "at": f"{TODAY}T10:00:00+00:00"},
        {"event": "viewed", "at": f"{TODAY}T11:00:00+00:00"}])
    inv_once = make_invoice(aenv, amount=10.0, customer_id=cust["id"])
    _set_invoice(aenv, inv_once["id"], timeline=[
        {"event": "viewed", "at": f"{TODAY}T09:00:00+00:00"}])
    # #7 autopay alert with recorded reason
    inv_ap = make_invoice(aenv, amount=80.0, customer_id=cust["id"])
    _set_invoice(aenv, inv_ap["id"],
                 autopay_last_error="Your card was declined.",
                 autopay_failed_at=f"{TODAY}T08:00:00+00:00")
    # #8 overdue installment (split part, real invoice)
    inv_part = make_invoice(aenv, amount=250.0, customer_id=cust["id"],
                            due="2000-06-01")
    _set_invoice(aenv, inv_part["id"], split_parent_id="parent-1",
                 split_label="Deposit")
    # #3 + #10 money flag and open dispute with packet ready
    inv_disp = make_invoice(aenv, amount=400.0, customer_id=cust["id"])
    _set_invoice(aenv, inv_disp["id"], status="paid", paid_cents=40000,
                 timeline=[
                     {"event": "payment_failed",
                      "at": f"{TODAY}T07:00:00+00:00",
                      "detail": {"error": "Bank payment failed after checkout"}},
                     {"event": "dispute_opened",
                      "at": f"{TODAY}T07:30:00+00:00",
                      "detail": {"dispute_id": "dp_123",
                                 "reason": "fraudulent",
                                 "amount_cents": 40000}}])
    # #2 bank fee savings: $1,000 by bank, recorded platform fee 1500 (1.5%)
    inv_bank = make_invoice(aenv, amount=1000.0, customer_id=cust["id"])
    _set_invoice(aenv, inv_bank["id"], status="paid", paid_cents=100000,
                 payments=[{"date": TODAY, "amount_cents": 100000,
                            "method": "ach", "platform_fee_cents": 1500}])

    r = run(IA.compute_rundown(TID, f"{TODAY}T12:00:00+00:00"))
    assert [f["invoice_id"] for f in r["follow_up"]] == [inv_hot["id"]]
    assert r["follow_up"][0]["views"] == 3
    assert r["autopay_alerts"][0]["error"] == "Your card was declined."
    inst = r["installments"]
    assert len(inst) == 1 and inst[0]["label"] == "Deposit"
    assert inst[0]["overdue"] is True and r["installments_overdue"] == 1
    assert r["money_flags"][0]["kind"] == "payment_failed"
    assert "Bank payment failed" in r["money_flags"][0]["detail"]
    assert len(r["disputes"]) == 1
    assert r["disputes"][0]["packet_ready"] is True
    assert r["disputes"][0]["packet_url"].endswith("/dispute-packet")
    assert r["bank_paid_cents_30d"] == 100000
    # card estimate: round(100000*0.029)+30 = 2930; minus recorded 1500 fee
    assert r["est_fee_savings_cents_30d"] == 2930 - 1500
    assert "2.9%" in r["fee_savings_basis"]


def test_rundown_helps_empty_when_no_data(aenv):
    make_invoice(aenv, amount=25.0)
    r = run(IA.compute_rundown(TID, f"{TODAY}T12:00:00+00:00"))
    assert r["follow_up"] == [] and r["autopay_alerts"] == []
    assert r["installments"] == [] and r["money_flags"] == []
    assert r["disputes"] == [] and r["est_fee_savings_cents_30d"] == 0


def test_rundown_duplicate_count_matches_cleanup_rule(aenv):
    txns = Scoped(aenv.db.bank_transactions, TID)
    base = {"txn_date": TODAY, "status": "unmatched"}
    run(txns.insert_one({"id": "t1", "amount_cents": 4200,
                         "description": "ACME SUPPLY STORE #12", **base}))
    run(txns.insert_one({"id": "t2", "amount_cents": -4200,  # abs amount
                         "description": "acme supply store #12", **base}))
    run(txns.insert_one({"id": "t3", "amount_cents": 4200,  # diff desc
                         "description": "COMPLETELY DIFFERENT SHOP", **base}))
    run(txns.insert_one({"id": "t4", "amount_cents": 9900,  # diff amount
                         "description": "ACME SUPPLY STORE #12", **base}))
    r = run(IA.compute_rundown(TID, f"{TODAY}T12:00:00+00:00"))
    assert r["duplicates_flagged"] == 1  # only t2, exactly like cleanup.py
    assert r["duplicates_link"] == "/cleanup"


def test_walkthrough_offer_open_by_default(aenv):
    s = run(IA.get_settings(TID, USER["id"]))
    assert s["walkthrough_completed_at"] is None
    assert s["offer_full_walkthrough"] is True
    assert s["walkthrough_suppressed_until"] is None


def test_walkthrough_completion_suppresses_proactive_offer(aenv):
    out = run(IA.mark_walkthrough_completed(USER))
    assert out["ok"] is True
    assert out["walkthrough_completed_at"]
    assert out["offer_full_walkthrough"] is False  # inside the window
    assert out["walkthrough_suppressed_until"] > TODAY
    # persisted in settings reads too, and survives a settings save
    s = run(IA.get_settings(TID, USER["id"]))
    assert s["offer_full_walkthrough"] is False
    assert s["walkthrough_completed_at"] == out["walkthrough_completed_at"]


def test_walkthrough_offer_returns_after_window(aenv):
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc)
           - timedelta(days=IA.WALKTHROUGH_RESURFACE_DAYS + 1)).isoformat()
    run(IA._cfg_coll(TID).update_one(
        {"user_id": USER["id"]},
        {"$set": {"walkthrough_completed_at": old}}, upsert=True))
    s = run(IA.get_settings(TID, USER["id"]))
    assert s["offer_full_walkthrough"] is True  # window passed — may offer again
    assert s["walkthrough_completed_at"] == old
