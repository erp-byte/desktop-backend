"""Verifies the Phase-2 routes are registered AND /export precedes /{company}. Run:
    PYTHONPATH=. python tests/services/test_cr_box_routes.py
"""
from app.main import app

PREFIX = "/api/v1/customer-returns"


def _routes():
    return [(m, r.path) for r in app.routes
            for m in (getattr(r, "methods", set()) or set())]


def main() -> None:
    routes = _routes()
    present = set(routes)
    for m, p in [
        ("GET", f"{PREFIX}/export"),
        ("POST", f"{PREFIX}/box-edit-log"),
        ("PUT", f"{PREFIX}/{{company}}/{{cr_id}}/box"),
        ("PUT", f"{PREFIX}/{{company}}/{{cr_id}}/boxes"),
    ]:
        assert (m, p) in present, f"missing route {m} {p}"

    # /export must be declared before GET /{company} (FastAPI matches in order)
    ordered = [p for (m, p) in routes if m == "GET"]
    assert ordered.index(f"{PREFIX}/export") < ordered.index(f"{PREFIX}/{{company}}"), \
        "GET /export must be declared before GET /{company}"

    # POST /box-edit-log before POST /{company}
    posts = [p for (m, p) in routes if m == "POST"]
    assert posts.index(f"{PREFIX}/box-edit-log") < posts.index(f"{PREFIX}/{{company}}"), \
        "POST /box-edit-log must be declared before POST /{company}"
    print("ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
