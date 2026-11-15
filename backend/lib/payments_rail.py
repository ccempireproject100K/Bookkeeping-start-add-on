"""PARTNER SEAM — where a future payments rail plugs in (Top-20 gap #14).

Today Books PREPARES bill payments (queue, approval, scheduling, packet)
and RECORDS them after the shop pays outside. It does NOT move money:
the owner's standing posture (2026-11-14) is that money movement waits
for a payments partner standing behind it (the same road as payroll).

To connect a partner later, this is wiring, not surgery:
  1. Implement an adapter with the same interface as NullRail below
     (status / execute_payment) around the partner's API.
  2. Register it in _ADAPTERS under the provider name.
  3. Set BILLPAY_RAIL_PROVIDER=<name> (and the partner's own keys) in env.
The bill-pay UI and endpoints read status() from here, so the day a rail
connects, the honest "not connected" copy flips from one place.
"""
import os


class RailNotConnected(Exception):
    """Raised by any execute attempt while no partner rail is connected."""


class NullRail:
    """The honest default: no partner, no money movement."""
    name = None

    def status(self):
        return {
            "connected": False,
            "provider": None,
            "message": ("No payments partner is connected. Books prepares "
                        "and records bill payments — it does not send money."),
        }

    async def execute_payment(self, **kwargs):
        raise RailNotConnected(self.status()["message"])


_ADAPTERS = {}  # provider name -> adapter instance; partner adapters land here


def get_rail():
    provider = os.environ.get("BILLPAY_RAIL_PROVIDER") or None
    if provider and provider in _ADAPTERS:
        return _ADAPTERS[provider]
    return NullRail()


def rail_status():
    return get_rail().status()
