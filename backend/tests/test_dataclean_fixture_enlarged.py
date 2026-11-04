"""Enlarged-fixture gate (power-up P8): precision == 1.00 is the
permanent ship gate; recall target >= 0.90 on the dogfood-derived
fixture (219 records, 61 labeled true pairs, hard negatives included).
Measured at build time (2026-11-03): precision 1.000, recall 1.000 on
both the sync and batched paths — see the pilot record in
~/workspace/buildout/dataclean-powerup/dogfood/.
"""
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.dataclean import linkage  # noqa: E402
from tests.dataclean_fixture_enlarged import RECORDS, TRUE_PAIRS  # noqa: E402

TRUE_SET = {(a, b) for a, b in TRUE_PAIRS} | {(b, a) for a, b in TRUE_PAIRS}


def _measure(result):
    got = {(p["a_id"], p["b_id"]) for p in result["pairs"]}
    tp = len(got & TRUE_SET)
    precision = tp / len(got) if got else 1.0
    recall = tp / len(TRUE_PAIRS) if TRUE_PAIRS else 1.0
    return got, precision, recall


def test_enlarged_fixture_precision_and_recall_sync():
    result = linkage.suggest_duplicates(RECORDS)
    got, precision, recall = _measure(result)
    assert precision == 1.0, (
        f"precision {precision:.3f}; false pairs: "
        f"{sorted(got - TRUE_SET)[:5]}")
    assert recall >= 0.90, f"recall {recall:.3f} below the 0.90 target"


def test_enlarged_fixture_precision_and_recall_batched():
    result = linkage.suggest_duplicates_batched(RECORDS)
    got, precision, recall = _measure(result)
    assert precision == 1.0, (
        f"batched precision {precision:.3f}; false pairs: "
        f"{sorted(got - TRUE_SET)[:5]}")
    assert recall >= 0.90, f"batched recall {recall:.3f} below 0.90"
