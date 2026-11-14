"""Wave-2 (owner additions 2026-11-14): WhatsApp seam, all-channel nudges,
channel study, bank steering payload, fee suggestion box.

Reuses the FakeDB fixtures/helpers from test_invoice_delivery.py.
"""
import pytest

from test_invoice_delivery import (env, run, make_customer, make_invoice,
                                   stored_invoice, USER)  # noqa: F401

from routers import invoice_delivery as D
from lib import invoice_pay as IP


def test_whatsapp_seam_default_off(env):
    cust = make_customer(env, phone="+12135550142")
    inv = make_invoice(env, customer_id=cust["id"])
    res = run(IP.send_invoice_whatsapp(USER["tenant_id"], inv))
    assert res["channel"] == "whatsapp"
    assert res["status"] == "skipped"
    assert "not yet enabled" in res["error"]
    stored = stored_invoice(env, inv["id"])
    assert stored["sends"][-1]["channel"] == "whatsapp"


def test_send_all_channels(env):
    cust = make_customer(env, phone="+12135550142")
    inv = make_invoice(env, customer_id=cust["id"])
    out = run(D.send_invoice(inv["id"], D.SendBody(channel="all"), USER))
    channels = [s["channel"] for s in out["sends"]]
    assert channels == ["email", "sms", "whatsapp"]


def test_client_page_pay_options_and_fee_note(env):
    cust = make_customer(env)
    inv = make_invoice(env, amount=200.0, customer_id=cust["id"])
    token = run(IP.ensure_client_token(USER["tenant_id"], cust["id"], "Acme Co"))
    page = run(D.client_page(token))
    assert page["fee_note"] and "keep fees low" in page["fee_note"]
    opts = {o["method"]: o for o in page["open"][0]["pay_options"]}
    assert opts["card"]["charge_cents"] == 20000
    assert opts["ach"]["charge_cents"] == 20000
    # owner-directed ACH price shape: 1.5% in config -> platform fee 300 on ach, 0 on card
    run(IP.set_pay_config(USER["tenant_id"],
                          {"fee_percent": 1.5, "fee_methods": ["ach"]}))
    page2 = run(D.client_page(token))
    opts2 = {o["method"]: o for o in page2["open"][0]["pay_options"]}
    assert opts2["ach"]["platform_fee_cents"] == 300
    assert opts2["card"]["platform_fee_cents"] == 0


def test_fee_suggestion_roundtrip(env):
    cust = make_customer(env)
    inv = make_invoice(env, customer_id=cust["id"])
    token = run(IP.ensure_client_token(USER["tenant_id"], cust["id"], "Acme Co"))
    out = run(D.fee_suggestion(token, D.FeeSuggestionBody(text="Let us pay by bank monthly")))
    assert out["ok"] is True
    rows = run(D.list_fee_suggestions(USER))
    assert rows and rows[0]["text"] == "Let us pay by bank monthly"
    assert rows[0]["client_name"] == "Acme Co"


def test_channel_study_attributes_view_and_payment(env):
    cust = make_customer(env)
    inv = make_invoice(env, customer_id=cust["id"])
    run(IP.send_invoice_email(USER["tenant_id"], inv, "invoice", USER))
    fresh = stored_invoice(env, inv["id"])
    run(IP.add_timeline(USER["tenant_id"], inv["id"], "viewed",
                        {"after_channel": IP.last_send_channel(fresh)}))
    run(D.record_online_payment(USER["tenant_id"], inv["id"], 10000, "ach",
                                "sess_x", "stripe_invoice"))
    study = run(D.channel_study(USER))
    agg = {a["channel"]: a for a in study["aggregate"]}
    assert agg["email"]["sends"] == 1
    assert agg["email"]["views"] == 1
    assert agg["email"]["payments"] == 1
    assert agg["email"]["response_rate"] == 1.0
    clients = {c["client_name"]: c for c in study["clients"]}
    assert clients["Acme Co"]["best_channel"] == "email"


def test_collections_step_fires_all_channels(env):
    from routers import collections as C
    cust = make_customer(env, phone="+12135550142")
    inv = make_invoice(env, amount=80.0, customer_id=cust["id"],
                       due="2026-10-20")
    cfg = {"enabled": True,
           "steps": [{"days": 3, "action": "nudge",
                      "template": "Hi {client_name} {balance}",
                      "channels": ["email", "sms", "whatsapp"]}],
           "late_fee_days": 21, "late_fee_percent": 1.5,
           "late_fee_min_cents": 2500, "late_fee_max_cents": 25000,
           "pause_service_days": 35}
    run(C.write_config(C.ConfigBody(**cfg), USER))
    out = run(C.autopilot_run(USER))
    nudge = [a for a in out["actions"] if a["action"] == "nudge"]
    assert nudge, out
    fired = {d["channel"] for d in nudge[0]["deliveries"]}
    assert fired == {"email", "sms", "whatsapp"}



# ---------------- wave 3 (owner addendum 2026-11-14 ~07:35 CST) ----------------
def test_fee_note_final_wording(env):
    cust = make_customer(env)
    inv = make_invoice(env, customer_id=cust["id"])
    token = run(IP.ensure_client_token(USER["tenant_id"], cust["id"], "Acme Co"))
    page = run(D.client_page(token))
    assert page["fee_note"] == ("We're always trying to keep fees low for our "
                                "customers. Please let us know if you have any "
                                "ideas of how we can.")
    assert page["help"]["label"] == "Payment problem or question?"
    assert page["help"]["desk_configured"] is False


def test_payment_help_records_ticket_with_context(env):
    cust = make_customer(env)
    inv = make_invoice(env, amount=50.0, customer_id=cust["id"])
    token = run(IP.ensure_client_token(USER["tenant_id"], cust["id"], "Acme Co"))
    out = run(D.payment_help(token, D.PaymentHelpBody(
        invoice_id=inv["id"], message="My bank says the payment is on hold")))
    assert out["ok"] is True and out["ticket_id"]
    assert out["desk_configured"] is False  # honest fallback: desk URL unset
    tickets = run(D.list_support_tickets(USER))
    assert tickets and tickets[0]["id"] == out["ticket_id"]
    ctx = tickets[0]["invoice_context"]
    assert ctx["invoice_id"] == inv["id"] and ctx["total_cents"] == 5000
    assert tickets[0]["queue"] == "ai_first_support"
    stored = stored_invoice(env, inv["id"])
    assert any(e["event"] == "help_requested" for e in stored["timeline"])


def test_payment_help_rejects_foreign_invoice(env):
    cust = make_customer(env)
    other = make_customer(env, cid="cust-2", name="Other Co")
    inv = make_invoice(env, customer_id=cust["id"])
    inv2 = make_invoice(env, client="Other Co", customer_id=other["id"])
    token = run(IP.ensure_client_token(USER["tenant_id"], cust["id"], "Acme Co"))
    out = run(D.payment_help(token, D.PaymentHelpBody(invoice_id=inv2["id"],
                                                      message="wrong invoice")))
    tickets = run(D.list_support_tickets(USER))
    mine = [t for t in tickets if t["id"] == out["ticket_id"]][0]
    assert mine["invoice_context"] is None  # foreign invoice context never attached



def test_split_parent_not_payable_or_double_counted(env):
    from fastapi import HTTPException
    cust = make_customer(env)
    inv = make_invoice(env, amount=200.0, customer_id=cust["id"])
    out = run(D.split_invoice(inv["id"], D.SplitBody(parts=[
        D.SplitPart(label="Deposit", amount=50.0),
        D.SplitPart(label="Balance", amount=150.0)]), USER))
    token = run(IP.ensure_client_token(USER["tenant_id"], cust["id"], "Acme Co"))
    page = run(D.client_page(token))
    refs = [i["id"] for i in page["open"]]
    assert inv["id"] not in refs                      # parent not listed
    assert page["total_open_cents"] == 20000           # parts only, no double count
    parent = stored_invoice(env, inv["id"])
    with pytest.raises(HTTPException) as ei:
        run(D.create_checkout_for_invoice(USER["tenant_id"], parent, "card",
                                          "http://x", token, None))
    assert ei.value.status_code == 409
