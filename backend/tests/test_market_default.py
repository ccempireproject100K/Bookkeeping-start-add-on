"""Market-face default ruling (owner, 2026-11-01): no page ever
silently wears a non-US market face.

Precedence: (1) the visitor's own stored choice; (2) the detected
country (CF-IPCountry at the edge) when it has a market pack;
(3) the US market. A detected country with NO pack (e.g. ID) falls
through to the US default — never to some other market. The
/detect endpoint echoes the request's edge country so the public
pages can apply step (2); books' API host is direct-to-Render
today (no Cloudflare in the request path), so in practice it
answers "default" until the host is edge-proxied.

Run from the backend directory with:
    python -m pytest tests/test_market_default.py
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017/test")
os.environ.setdefault("DB_NAME", "test")

from lib.markets import market_for_country, resolve_market_code
from routers.market_packs import detect_market


def _request(country=None):
    headers = {} if country is None else {"cf-ipcountry": country}
    return SimpleNamespace(headers=headers)


# ---------- market_for_country: the detection signal ----------

def test_country_with_a_pack_maps_to_itself():
    assert market_for_country("GB") == "GB"
    assert market_for_country("gb") == "GB"  # header case normalized
    assert market_for_country(" DE ") == "DE"


def test_no_signal_or_sentinels_map_to_nothing():
    assert market_for_country(None) is None
    assert market_for_country("") is None
    assert market_for_country("XX") is None  # Cloudflare "unknown"
    assert market_for_country("T1") is None  # Tor exit


def test_country_without_a_pack_maps_to_nothing_never_a_substitute():
    # Indonesia is what the owner saw offered this morning; there is
    # no ID pack, and detection must NOT dress the visitor in any
    # other market in its place.
    assert market_for_country("ID") is None


# ---------- resolve_market_code: the precedence ----------

def test_stored_choice_wins_over_detection():
    assert resolve_market_code("GB", "DE") == ("GB", "choice")
    assert resolve_market_code("gb", None) == ("GB", "choice")


def test_detection_beats_default():
    assert resolve_market_code(None, "GB") == ("GB", "detected")
    assert resolve_market_code("", "FR") == ("FR", "detected")


def test_default_is_always_the_us_market():
    assert resolve_market_code(None, None) == ("US", "default")
    assert resolve_market_code(None, "XX") == ("US", "default")
    assert resolve_market_code(None, "T1") == ("US", "default")


def test_unpacked_detected_country_falls_to_us_not_elsewhere():
    assert resolve_market_code(None, "ID") == ("US", "default")


def test_bogus_stored_choice_does_not_stick():
    # A stale/garbage stored value is ignored, not worn.
    assert resolve_market_code("XX", "GB") == ("GB", "detected")
    assert resolve_market_code("ZZ", None) == ("US", "default")


# ---------- GET /market-packs/detect ----------

def test_detect_without_header_is_us_default():
    body = asyncio.run(detect_market(_request()))
    assert body["market_code"] == "US"
    assert body["source"] == "default"
    assert body["country"] is None
    assert body["market"]["code"] == "US"


def test_detect_with_packed_country_reports_detected():
    body = asyncio.run(detect_market(_request("GB")))
    assert body["market_code"] == "GB"
    assert body["source"] == "detected"
    assert body["country"] == "GB"
    assert body["market"]["name"] == "United Kingdom"


def test_detect_with_unpacked_country_is_us_default():
    body = asyncio.run(detect_market(_request("ID")))
    assert body["market_code"] == "US"
    assert body["source"] == "default"


def test_detect_with_sentinel_is_us_default():
    body = asyncio.run(detect_market(_request("XX")))
    assert body["market_code"] == "US"
    assert body["source"] == "default"
