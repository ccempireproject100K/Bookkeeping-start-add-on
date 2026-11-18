"""Invoice Design Shop (2026-11-14): branding extension on whitelabel,
client page carries the business's face, invoice email carries it too.
Reuses the FakeDB fixtures from test_invoice_delivery.py.
"""
import pytest

from test_invoice_delivery import (env, run, make_customer, make_invoice,  # noqa: F401
                                   USER)
from lib import invoice_pay as IP

from routers import whitelabel as WL
from routers import invoice_delivery as D

TID = USER["tenant_id"]

FULL = WL.BrandingBody(
    firm_name="Proof Plumbing LLC", logo_url="https://example.com/logo.png",
    accent_color="#0f766e", tagline="Done right, first time",
    layout="bold", banner_url="https://example.com/crew.jpg",
    default_message="Thanks for your business!",
    footer_note="PO required on all jobs over $500.")


@pytest.fixture()
def wenv(env, monkeypatch):
    monkeypatch.setattr(WL, "db", env.db)
    return env


def test_branding_extension_roundtrip(wenv):
    out = run(WL.set_branding(FULL, USER))
    assert out["layout"] == "bold" and out["banner_url"].endswith("crew.jpg")
    got = run(WL.get_branding(USER))
    assert got["firm_name"] == "Proof Plumbing LLC"
    assert got["default_message"] == "Thanks for your business!"
    assert got["footer_note"].startswith("PO required")


def test_branding_validation(wenv):
    with pytest.raises(Exception) as e1:
        run(WL.set_branding(WL.BrandingBody(layout="fancy"), USER))
    assert getattr(e1.value, "status_code", None) == 422
    with pytest.raises(Exception) as e2:
        run(WL.set_branding(WL.BrandingBody(banner_url="javascript:alert(1)"),
                            USER))
    assert getattr(e2.value, "status_code", None) == 422
    with pytest.raises(Exception) as e3:
        run(WL.set_branding(WL.BrandingBody(logo_url="ftp://x/y.png"), USER))
    assert getattr(e3.value, "status_code", None) == 422
    with pytest.raises(Exception) as e4:
        run(WL.set_branding(WL.BrandingBody(accent_color="teal"), USER))
    assert getattr(e4.value, "status_code", None) == 422
    # clearing a field with an empty string is allowed
    out = run(WL.set_branding(WL.BrandingBody(banner_url=""), USER))
    assert out["banner_url"] == ""


def test_client_page_returns_branding(wenv):
    cust = make_customer(wenv)
    make_invoice(wenv, customer_id=cust["id"])
    token = run(IP.ensure_client_token(TID, cust["id"], "Acme Co"))
    page = run(D.client_page(token))
    # no branding set yet -> graceful defaults
    assert page["branding"]["layout"] == "classic"
    assert page["branding"]["logo_url"] == ""
    run(WL.set_branding(FULL, USER))
    page2 = run(D.client_page(token))
    b = page2["branding"]
    assert b["layout"] == "bold"
    assert b["logo_url"] == "https://example.com/logo.png"
    assert b["banner_url"] == "https://example.com/crew.jpg"
    assert b["accent_color"] == "#0f766e"
    assert b["tagline"] == "Done right, first time"
    assert b["footer_note"].startswith("PO required")


def test_invoice_email_carries_branding(wenv):
    inv = {"id": "inv-12345678", "total_cents": 15000, "paid_cents": 0,
           "lines": [{"name": "Service", "amount": 150.0}],
           "issue_entry": "JE-1"}
    subject, h, t = IP.invoice_email_html(
        "Proof Plumbing", inv, "http://x/c/tok", "invoice",
        branding={"logo_url": "https://example.com/logo.png",
                  "accent_color": "#0f766e",
                  "default_message": "Thanks for your business!"})
    assert "https://example.com/logo.png" in h
    assert "background:#0f766e" in h
    assert "Thanks for your business!" in h
    # no branding -> no logo, default button color, still valid
    _, h2, _ = IP.invoice_email_html("Proof Plumbing", inv,
                                     "http://x/c/tok", "invoice")
    assert "<img src=\"https" not in h2
    assert "background:#0f766e" in h2  # house default happens to be teal
