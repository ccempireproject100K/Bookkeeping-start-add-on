#!/usr/bin/env python3
"""Sentinel CLI: run the security watcher once. Intended for cron/systemd.

Every 15 minutes is a good cadence:
    */15 * * * *  cd /opt/bookkeeping/backend && python3 sentinel.py >> /var/log/sentinel.log 2>&1

See docs/sentinel.md for the full setup (systemd timer alternative included).
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

from lib.sentinel import run_sentinel  # noqa: E402


async def main():
    results = await run_sentinel()
    print(f"sentinel done: {results}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
