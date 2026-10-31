"""Python bridge to the merged TypeScript tax engine (tax-engine/).

The engine (PR #147, merged) is a standalone TS package; the backend runs
it through a tiny Node driver (lib/tax_engine_driver.mjs) rather than
reimplementing any tax math in Python — one engine, one source of truth.
Requires the engine to be built (`cd tax-engine && npm ci && npm run
build`); when it isn't, TaxEngineUnavailable carries the reason and the
API answers 503 with it, never a fabricated estimate.
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

DRIVER = Path(__file__).parent / "tax_engine_driver.mjs"


class TaxEngineUnavailable(Exception):
    pass


def _run_sync(payload: dict) -> dict:
    node = shutil.which("node")
    if not node:
        raise TaxEngineUnavailable("node runtime not available on this host")
    try:
        proc = subprocess.run(
            [node, str(DRIVER)], input=json.dumps(payload),
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise TaxEngineUnavailable("tax engine timed out")
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        raise TaxEngineUnavailable(
            f"tax engine produced no output (exit {proc.returncode}): "
            f"{(proc.stderr or '')[:200]}")
    try:
        result = json.loads(out[-1])
    except json.JSONDecodeError:
        raise TaxEngineUnavailable("tax engine returned unparseable output")
    if result.get("error"):
        raise TaxEngineUnavailable(
            f"{result['error']}: {result.get('detail', '')}".strip())
    return result


async def run_tax_estimate(net_profit_cents: int, jurisdiction: str,
                           tax_profile: dict) -> dict:
    """net profit in integer cents (books convention) → engine dollars in,
    TaxResult (cents inside) out."""
    payload = {"netProfitDollars": net_profit_cents / 100.0,
               "jurisdiction": jurisdiction,
               "taxProfile": tax_profile}
    return await asyncio.to_thread(_run_sync, payload)
