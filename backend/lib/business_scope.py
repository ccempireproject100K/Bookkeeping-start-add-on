"""Business-scoped Mongo queries for Empire's internal multi-business bookkeeping.

Tenant access is still enforced by authentication; this module additionally
requires an explicit business identifier so reports never silently combine
records from multiple businesses. It does not grant authorization to a business.
"""
import re
from typing import Any

_BUSINESS_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_business_id(business_id: str) -> str:
    """Reject absent or unsafe identifiers instead of falling back to all books."""
    if not isinstance(business_id, str) or not _BUSINESS_ID.fullmatch(business_id):
        raise ValueError("A valid business_id is required")
    return business_id


def business_filter(tenant_id: str, business_id: str, **criteria: Any) -> dict[str, Any]:
    """Build an exact tenant-and-business filter; callers must authorize both."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("A valid tenant_id is required")
    bid = validate_business_id(business_id)
    if any(key in criteria for key in ("tenant_id", "business_id", "$or", "$and", "$where")):
        raise ValueError("Scope fields and query operators cannot be overridden")
    return {"tenant_id": tenant_id, "business_id": bid, **criteria}
