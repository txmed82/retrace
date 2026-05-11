"""Route detection across JS, Python (FastAPI/Flask/Django), and Ruby (Rails).

The `pr_review` flow uses `load_route_manifest` to figure out which
files own which `/api/...` route. Until now it only covered
Express/Next.js conventions, so Python and Rails repos got zero
prior-failure linkage. These tests pin the parsers for each framework
flavor.
"""

from __future__ import annotations

from pathlib import Path

from retrace.matching.routes import RouteDefinition, load_route_manifest, route_matches


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# JS (regression — make sure we didn't break the original behaviour)
# ---------------------------------------------------------------------------


def test_js_routes_still_picked_up(tmp_path: Path) -> None:
    _write(
        tmp_path / "server" / "routes.ts",
        "router.get('/api/login', loginHandler);\n"
        "app.post('/api/logout', logoutHandler);\n",
    )
    manifest = load_route_manifest(tmp_path)
    routes = {(r.method, r.route, r.file_path) for r in manifest}
    assert ("GET", "/api/login", "server/routes.ts") in routes
    assert ("POST", "/api/logout", "server/routes.ts") in routes


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------


def test_fastapi_router_decorator_detected(tmp_path: Path) -> None:
    _write(
        tmp_path / "app" / "routes.py",
        """
from fastapi import APIRouter
router = APIRouter()

@router.get("/api/users")
async def list_users(): ...

@router.post("/api/users")
async def create_user(): ...
""".lstrip(),
    )
    manifest = load_route_manifest(tmp_path)
    methods = sorted((r.method, r.route) for r in manifest)
    assert ("GET", "/api/users") in methods
    assert ("POST", "/api/users") in methods
    sources = {r.source for r in manifest if r.route == "/api/users"}
    assert "python_decorator" in sources


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------


def test_flask_route_with_methods_emits_one_per_verb(tmp_path: Path) -> None:
    _write(
        tmp_path / "app.py",
        """
from flask import Flask
app = Flask(__name__)

@app.route("/api/login", methods=["POST"])
def login(): ...

@app.route("/api/health")
def health(): ...

@app.get("/api/short")
def short_form(): ...
""".lstrip(),
    )
    manifest = load_route_manifest(tmp_path)
    pairs = {(r.method, r.route) for r in manifest}
    assert ("POST", "/api/login") in pairs
    # Default method when `methods=` is omitted is GET.
    assert ("GET", "/api/health") in pairs
    # The decorator form coexists with the classic route().
    assert ("GET", "/api/short") in pairs


def test_flask_route_with_multiple_methods(tmp_path: Path) -> None:
    _write(
        tmp_path / "app.py",
        """
@app.route("/api/items", methods=["GET", "POST"])
def items(): ...
""".lstrip(),
    )
    manifest = load_route_manifest(tmp_path)
    pairs = {(r.method, r.route) for r in manifest}
    assert ("GET", "/api/items") in pairs
    assert ("POST", "/api/items") in pairs


# ---------------------------------------------------------------------------
# Django
# ---------------------------------------------------------------------------


def test_django_urls_py_picked_up(tmp_path: Path) -> None:
    _write(
        tmp_path / "myapp" / "urls.py",
        """
from django.urls import path, re_path
from . import views

urlpatterns = [
    path("api/users", views.user_list),
    path("api/users/<int:pk>", views.user_detail),
    path("api/items/<slug:name>", views.item),
    re_path(r"^api/legacy/(?P<id>[0-9]+)$", views.legacy),
]
""".lstrip(),
    )
    manifest = load_route_manifest(tmp_path)
    routes = {r.route for r in manifest}
    # Django dynamic segments must normalize to the `:name` shape the
    # rest of the matcher understands — otherwise `route_matches`
    # never matches a real request like `/api/users/42`.
    assert "/api/users" in routes
    assert "/api/users/:pk" in routes
    assert "/api/items/:name" in routes
    assert "/api/legacy/:id" in routes

    # Verify a real request resolves to the dynamic entry.
    rd_users = next(r for r in manifest if r.route == "/api/users/:pk")
    assert route_matches(rd_users, "/api/users/42")
    rd_legacy = next(r for r in manifest if r.route == "/api/legacy/:id")
    assert route_matches(rd_legacy, "/api/legacy/123")


def test_django_admin_urls_py_is_not_treated_as_url_conf(tmp_path: Path) -> None:
    """`admin_urls.py` (or any other suffix-only match) must NOT be
    treated as a Django urls.py — the basename check should keep it
    out."""
    _write(
        tmp_path / "myapp" / "admin_urls.py",
        'urlpatterns = [path("api/admin-only", lambda r: None)]\n',
    )
    manifest = load_route_manifest(tmp_path)
    assert all(r.route != "/api/admin-only" for r in manifest)


def test_django_path_outside_urls_py_is_ignored(tmp_path: Path) -> None:
    """A `path()` call in some random helper module shouldn't masquerade
    as a real URL conf entry."""
    _write(
        tmp_path / "myapp" / "helpers.py",
        'def fake(): path("api/should-not-show", None)\n',
    )
    manifest = load_route_manifest(tmp_path)
    assert all(r.route != "/api/should-not-show" for r in manifest)


# ---------------------------------------------------------------------------
# Rails
# ---------------------------------------------------------------------------


def test_rails_routes_rb_picked_up(tmp_path: Path) -> None:
    _write(
        tmp_path / "config" / "routes.rb",
        """
Rails.application.routes.draw do
  get '/api/users'
  post '/api/users', to: 'users#create'
  patch '/api/users/:id'
  match '/api/legacy', via: [:get, :post]
end
""".lstrip(),
    )
    manifest = load_route_manifest(tmp_path)
    pairs = {(r.method, r.route) for r in manifest}
    assert ("GET", "/api/users") in pairs
    assert ("POST", "/api/users") in pairs
    assert ("PATCH", "/api/users/:id") in pairs
    # `match` carries multiple verbs; we leave method blank.
    assert ("", "/api/legacy") in pairs


def test_rails_get_in_a_model_is_ignored(tmp_path: Path) -> None:
    """A `get` call elsewhere isn't a real route — the routes.rb
    restriction filters it out."""
    _write(
        tmp_path / "app" / "models" / "user.rb",
        "class User\n  def self.fetch; get '/api/sneaky'; end\nend\n",
    )
    manifest = load_route_manifest(tmp_path)
    assert all(r.route != "/api/sneaky" for r in manifest)


def test_rails_my_routes_rb_is_not_treated_as_routes_file(tmp_path: Path) -> None:
    """A file called `myroutes.rb` shouldn't pass the basename guard
    just because it ends in `routes.rb`."""
    _write(
        tmp_path / "config" / "myroutes.rb",
        "get '/api/imposter'\n",
    )
    manifest = load_route_manifest(tmp_path)
    assert all(r.route != "/api/imposter" for r in manifest)


# ---------------------------------------------------------------------------
# Sanity: route_matches still works with the new entries
# ---------------------------------------------------------------------------


def test_route_matches_after_polyglot_detection(tmp_path: Path) -> None:
    _write(
        tmp_path / "app" / "urls.py",
        "",  # we'll construct manually
    )
    rd = RouteDefinition(route="/api/users/:id", file_path="app/urls.py", method="PATCH")
    assert route_matches(rd, "/api/users/42", method="PATCH")
    assert not route_matches(rd, "/api/users/42", method="GET")
