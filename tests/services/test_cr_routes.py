"""Verifies the customer-returns routes are registered on the app. No DB. Run:
    PYTHONPATH=. python tests/services/test_cr_routes.py
"""
from app.main import app

EXPECTED = {
    ("POST", "/api/v1/customer-returns/{company}"),
    ("GET", "/api/v1/customer-returns/{company}"),
    ("GET", "/api/v1/customer-returns/{company}/{cr_id}"),
    ("PUT", "/api/v1/customer-returns/{company}/{cr_id}"),
    ("PUT", "/api/v1/customer-returns/{company}/{cr_id}/lines"),
    ("DELETE", "/api/v1/customer-returns/{company}/{cr_id}"),
}


def main() -> None:
    present = {(m, r.path) for r in app.routes for m in getattr(r, "methods", set()) or set()}
    missing = {e for e in EXPECTED if e not in present}
    assert not missing, f"missing routes: {sorted(missing)}"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
