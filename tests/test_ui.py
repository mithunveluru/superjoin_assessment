"""Phase 12 — the static, framework-free UI served by the API.

The rendering itself is browser code; these tests pin the servable contract:
the page and its two assets are served with the right content types, the page
wires to real endpoints, and there is no build step.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

STATIC = Path("app/static")


@pytest.fixture
def api(db_path):
    from app.config import get_settings

    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client


def test_root_serves_index_html(api):
    r = api.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Fact Knowledge Layer</title>" in r.text
    assert "/static/app.js" in r.text and "/static/style.css" in r.text


def test_static_assets_served(api):
    js = api.get("/static/app.js")
    assert js.status_code == 200
    assert js.headers["content-type"].split(";")[0] in {
        "text/javascript", "application/javascript"}
    css = api.get("/static/style.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert api.get("/static/index.html").status_code == 200


def test_app_js_calls_only_real_endpoints(api):
    src = (STATIC / "app.js").read_text(encoding="utf-8")
    paths = set(re.findall(r'(?:api|fetch)\(\s*[`"](/[^`"?]+)', src))
    paths |= set(re.findall(r'[`"](/[a-z][\w/]*)/\$\{', src))  # template-literal prefixes
    assert paths, "expected the UI to call the API"

    routes = list(app.openapi()["paths"])
    route_res = [re.compile("^" + re.sub(r"\{[^}]+\}", r"[^/]+", p) + "$") for p in routes]
    for p in sorted(paths):
        base = re.sub(r"/\$\{[^}]+\}", "/1", p).rstrip("/") or "/"
        assert any(rx.match(base) for rx in route_res), f"UI calls unknown endpoint: {p}"


def test_app_js_parses_as_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    r = subprocess.run([node, "--check", str(STATIC / "app.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_build_step():
    names = sorted(p.name for p in STATIC.iterdir())
    assert names == ["app.js", "index.html", "style.css"]
