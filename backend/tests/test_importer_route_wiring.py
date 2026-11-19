"""Regression: POST /import/run must be wired to run_import, not to the
_persist_import_run helper (a misplaced decorator once left the real
handler unrouted)."""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_importer_route_wiring")

from routers import importer as IMP  # noqa: E402


def test_import_run_route_is_the_real_handler():
    routes = [r for r in IMP.router.routes
              if r.path == "/import/run" and "POST" in r.methods]
    assert len(routes) == 1
    assert routes[0].endpoint is IMP.run_import
