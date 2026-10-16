"""Regression tests for the customer dashboard bug-fix branch.

Bug 1 (dashboard crash) is a frontend fix — verified via build, not here.
Bug 2: statement must not mix up same-name customers' invoices.
Bug 3: POST /customers must reject duplicates with 409.

These are pure-function unit tests (no live Mongo required); run with:
    MONGO_URL=mongodb://localhost:27017 DB_NAME=test python -m pytest tests/test_customer_fixes.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routers.customers import (
    _norm_name,
    _norm_email,
    _norm_phone,
    _invoice_matches_statement,
    _find_duplicate,
)


class TestNormalization:
    def test_norm_name(self):
        assert _norm_name("  Acme   Co ") == "acme co"
        assert _norm_name(None) == ""
        assert _norm_name("O'Brien") == "o'brien"

    def test_norm_email(self):
        assert _norm_email("  FOO@Bar.COM ") == "foo@bar.com"
        assert _norm_email(None) == ""

    def test_norm_phone(self):
        assert _norm_phone("(555) 123-4567") == "5551234567"
        assert _norm_phone("+1-555-123-4567") == "15551234567"
        assert _norm_phone(None) == ""


class TestStatementMatching:
    """Bug 2: two customers named 'John Smith' must not see each other's invoices."""

    def test_linked_invoice_matches_by_id(self):
        inv = {"customer_id": "cust-A", "client_name": "John Smith"}
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", True) is True

    def test_linked_invoice_matches_even_if_name_differs(self):
        # customer_id is authoritative
        inv = {"customer_id": "cust-A", "client_name": "Jon Smith"}
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", True) is True

    def test_other_customers_linked_invoice_excluded_despite_name_match(self):
        # THE BUG: old code included this via the client_name fallback
        inv = {"customer_id": "cust-B", "client_name": "John Smith"}
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", True) is False
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", False) is False

    def test_unrelated_invoice_excluded(self):
        inv = {"customer_id": "cust-B", "client_name": "Jane Doe"}
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", True) is False

    def test_legacy_unlinked_invoice_included_when_name_unique(self):
        inv = {"client_name": "John Smith"}  # no customer_id (legacy)
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", True) is True

    def test_legacy_unlinked_invoice_excluded_when_name_ambiguous(self):
        # Two "John Smith" customers in the tenant: cannot safely attribute
        inv = {"client_name": "John Smith"}
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", False) is False
        assert _invoice_matches_statement(inv, "cust-B", "John Smith", False) is False

    def test_legacy_invoice_with_different_name_excluded(self):
        inv = {"client_name": "Jane Doe"}
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", True) is False

    def test_legacy_invoice_explicit_none_customer_id(self):
        inv = {"customer_id": None, "client_name": "John Smith"}
        assert _invoice_matches_statement(inv, "cust-A", "John Smith", True) is True


class TestDuplicateDetection:
    """Bug 3: same normalized name + same email (or phone) => duplicate."""

    def test_duplicate_by_email(self):
        existing = [{"id": "1", "name": "Acme Co", "email": "a@x.com", "phone": "555-0001"}]
        dup = _find_duplicate(existing, "acme co", "A@X.COM", "555-9999")
        assert dup is not None and dup["id"] == "1"

    def test_duplicate_by_phone(self):
        existing = [{"id": "1", "name": "Acme Co", "email": "a@x.com", "phone": "(555) 000-0001"}]
        dup = _find_duplicate(existing, "ACME CO", "other@y.com", "5550000001")
        assert dup is not None and dup["id"] == "1"

    def test_same_name_different_contact_is_not_duplicate(self):
        # Two genuinely different customers may share a name
        existing = [{"id": "1", "name": "John Smith", "email": "john1@x.com", "phone": "555-0001"}]
        assert _find_duplicate(existing, "John Smith", "john2@x.com", "555-0002") is None

    def test_name_only_cannot_disambiguate(self):
        existing = [{"id": "1", "name": "John Smith", "email": "john1@x.com", "phone": "555-0001"}]
        assert _find_duplicate(existing, "John Smith", None, None) is None

    def test_different_name_not_duplicate(self):
        existing = [{"id": "1", "name": "Acme Co", "email": "a@x.com", "phone": "555-0001"}]
        assert _find_duplicate(existing, "Beta LLC", "a@x.com", None) is None

    def test_whitespace_variants_match(self):
        existing = [{"id": "7", "name": "  Smith &   Sons ", "email": None, "phone": "5551234"}]
        dup = _find_duplicate(existing, "smith & sons", None, "555-1234")
        assert dup is not None and dup["id"] == "7"

    def test_empty_list(self):
        assert _find_duplicate([], "Acme", "a@x.com", None) is None

    def test_blank_name_never_matches(self):
        existing = [{"id": "1", "name": "Acme", "email": "a@x.com", "phone": None}]
        assert _find_duplicate(existing, "   ", "a@x.com", None) is None
