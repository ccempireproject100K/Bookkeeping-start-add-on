"""Provision the DataClean family's Stripe products + monthly prices.

Idempotent by construction: lib/dataclean_billing.ensure_products()
uses deterministic Stripe ids (mae_dataclean, mae_security,
mae_bundle + their monthly price ids) and retrieves before creating,
so re-running converges on the same three objects in whichever
account STRIPE_SECRET_KEY points at — run it once with the test key
(sk_test_…) and again with the live key when the owner goes live.
The resulting mapping is recorded in the `dataclean_stripe_products`
collection (the checkout flow reads it from there) and printed here
for the ops record / optional STRIPE_PRICE_* env pinning.

Usage (from the backend directory, with the deployment's env):
    STRIPE_SECRET_KEY=sk_test_… python scripts/ensure_stripe_products.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import dataclean_billing  # noqa: E402


async def _main() -> int:
    if not dataclean_billing.configured():
        print("STRIPE_SECRET_KEY is not set — nothing to provision.")
        return 2
    mapping = await dataclean_billing.ensure_products()
    print(json.dumps(mapping, indent=2, sort_keys=True))
    mode = "live" if dataclean_billing.secret().startswith("sk_live_") \
        else "test"
    print(f"\nProvisioned in Stripe {mode} mode. Optional env pinning:")
    for pid, row in sorted(mapping.items()):
        env = dataclean_billing.product_catalog()[pid]["price_env"]
        print(f"  {env}={row['stripe_price_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
