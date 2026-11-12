"""公開リポジトリに入れてはいけないものの検査（scripts/check_public.py。2026-11-12）。
見本の文字列は、このファイル自体が検査に引っかからないよう、つなげて作る。"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_public", ROOT / "scripts" / "check_public.py")
check_public = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_public)


def problems(path: str, text: str) -> list[str]:
    return check_public.check_file(path, text.encode("utf-8"))


@pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(), reason="git のリポジトリでない"
)
def test_tracked_files_are_safe_to_publish():
    """UT-117: 追跡しているファイルに、トークン・個人のアドレス・PC のパス・データが無い"""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public.py"), "--all"],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("a.py", 'x = "' + "eyJ" + "hbGciOiJFZERTQSJ9" + ".eyJ" + 'pYXQiOjE3MDAwMDAwMDB9.sig"'),
        ("a.env", "TURSO_ORG=x"),
        ("data/azkims.db", ""),
        ("output/report.md", "x"),
        ("a.md", "連絡先 someone" + "@" + "gmail.com"),
        ("a.py", 'ROOT = r"C:' + '\\Users\\someone\\project"'),
        ("a.sh", "TURSO_AUTH_" + "TOKEN=abcdefghijklmnop1234"),
        ("a.py", 'api_key = "' + "abcdefghijklmnopqrstu" + '"'),
        ("a.txt", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
    ],
)
def test_finds_what_must_not_be_published(path, text):
    """UT-118: 止めるべきものを見つける"""
    assert problems(path, text)


@pytest.mark.parametrize(
    "text",
    [
        "write_token = platform.mint_token(name, read_only=False)",
        "Co-Authored-By: Claude <noreply" + "@anthropic.com>",
        "1+x" + "@users.noreply.github.com",
        "`%TEMP%\\pytest-of-<ユーザー名>`",
        'x = "' + "eyJ" + 'abcdefghijklmnop.eyJabcdefghijklmnop" # public-ok: テストの見本',
    ],
)
def test_allows_ordinary_lines(text):
    """UT-119: 普通のコード・許したアドレス・public-ok の行は止めない"""
    assert problems("a.py", text) == []
