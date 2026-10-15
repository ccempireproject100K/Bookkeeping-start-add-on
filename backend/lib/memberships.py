"""Workspace memberships: one login, many workspaces.

A user row carries the ACTIVE workspace in `tenant_id`/`role` (everything
existing reads those), plus `memberships: [{tenant_id, role}]` listing every
workspace the login can access. Older rows without `memberships` are lazily
treated as single-workspace.
"""


def memberships(user: dict) -> list:
    ms = user.get("memberships")
    if not ms and user.get("tenant_id"):
        ms = [{"tenant_id": user["tenant_id"], "role": user.get("role", "owner")}]
    return ms or []
