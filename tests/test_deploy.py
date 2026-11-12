import json
import re
from importlib import metadata

from azkims.config import PROJECT_ROOT

VERCEL = PROJECT_ROOT / "vercel"


def test_vercel_requirements_are_pinned_to_local_versions():
    # UT-113: 本番に入る Python の依存は版を固定し、手元（build と検索を試した環境）と同じ版にする
    lines = [
        line.strip()
        for line in (VERCEL / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    for line in lines:
        name, sep, version = line.partition("==")
        assert sep, f"版が固定されていません: {line}"
        assert metadata.version(name) == version, f"{name} の版が手元と違います"


def test_security_headers_cover_api_and_static():
    # UT-114: ヘッダーの規則は /api の振り分けより前に置き、API の応答にも付ける
    routes = json.loads((VERCEL / "vercel.json").read_text(encoding="utf-8"))["routes"]
    first = routes[0]
    assert first["src"] == "/(.*)" and first.get("continue") is True
    headers = first["headers"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Robots-Tag"] == "noindex, nofollow"
    assert headers["X-Frame-Options"] == "DENY"
    csp = dict(
        (d.split(" ", 1) + [""])[:2]
        for d in map(str.strip, headers["Content-Security-Policy"].split(";"))
    )
    assert csp["script-src"] == "'self'" and csp["frame-ancestors"] == "'none'"
    assert csp["frame-src"] == "https://www.youtube-nocookie.com"
    assert any(r.get("src") == "/api/(.*)" for r in routes[1:])


def test_page_has_no_inline_script_and_static_files_are_built():
    # UT-115: CSP は inline の script を許さないので、index.html に中身のある <script> を置かない。
    # public/ のファイルは vercel.json の builds に無いと配信されない
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    builds = {
        b["src"] for b in json.loads((VERCEL / "vercel.json").read_text(encoding="utf-8"))["builds"]
    }
    for p in (PROJECT_ROOT / "web" / "public").iterdir():
        assert p.name in builds, f"{p.name} が vercel.json の builds にありません"
