"""公開リポジトリに入れてはいけないものが無いかを確かめる（2026-11-12。リポジトリを Public にするため）。

  python scripts/check_public.py --staged   コミットする内容（.githooks/pre-commit が呼ぶ）
  python scripts/check_public.py --all      追跡しているすべてのファイル（tests/test_public.py が呼ぶ）

見つけたら 1 を返してコミットを止める。誤検知の行は、その行に `public-ok` と書けば通る（理由も添える）。
標準ライブラリだけで動かす（.venv が無くても hook が動くように）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import PurePosixPath

# 公開しないもの（.gitignore と同じ考え。ignore を外したり -f で足したりしても止める）
FORBIDDEN_PATHS = [
    (re.compile(r"^(data|output|deploy)/"), "字幕・コメントの原本、DB、公開用の一式（git 管理外）"),
    (re.compile(r"^config/config\.toml$"), "手元の設定"),
    (re.compile(r"(^|/)\.vercel/"), "Vercel のプロジェクトの紐付け"),
    (re.compile(r"(^|/)(\.env[^/]*|[^/]*\.env)$"), "環境変数のファイル（トークンを含む）"),
    (
        re.compile(r"\.(db|sqlite3?|db-wal|db-shm|json3|pem|key|p12|pfx)$", re.IGNORECASE),
        "DB・字幕・鍵のファイル",
    ),
]
MAX_BYTES = 1_000_000  # これより大きいファイルは、データを誤って入れた可能性が高い

SECRETS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"), "JWT（Turso のトークンなど）"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "秘密鍵"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "GitHub のトークン"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"), "GitHub のトークン"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic の API キー"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "API キー"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS のキー"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google の API キー"),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), "Slack のトークン"),
    (
        # 文字列をそのまま書いたもの（token = "..."）。関数の呼び出し（token = mint_token(...)）は止めない
        re.compile(
            r"(?i)\b[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)\w*\s*[=:]\s*['\"][A-Za-z0-9_\-./+=]{16,}['\"]"
        ),
        "トークン・パスワードの代入",
    ),
    (
        # .env の形（TURSO_AUTH_TOKEN=...）
        re.compile(
            r"(?i)^\s*(export\s+)?[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|API_KEY)\w*=[A-Za-z0-9_\-./+=]{16,}\s*$"
        ),
        "トークン・パスワードの代入",
    ),
]

# 個人を特定できるもの。メールアドレスは、GitHub の noreply と例示用だけを許す
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_EMAIL = re.compile(
    r"(@users\.noreply\.github\.com|^noreply@anthropic\.com|@example\.(com|org|net))$",
    re.IGNORECASE,
)
LOCAL_PATH = re.compile(
    r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!<)[^\\/\s\"'`]+|/(home|Users)/(?!<)[a-z0-9._-]+/"
)

SKIP_CONTENT = {"web/package-lock.json"}  # 依存の一覧（npm のメタデータ）。中身は npm が作る
OK_MARK = "public-ok"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def staged_files() -> list[str]:
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.decode("utf-8").split("\0") if p]


def tracked_files() -> list[str]:
    return [p for p in git("ls-files", "-z").decode("utf-8").split("\0") if p]


def read(path: str, staged: bool) -> bytes:
    if staged:
        return git("show", f":{path}")
    with open(path, "rb") as f:
        return f.read()


def check_file(path: str, data: bytes) -> list[str]:
    problems: list[str] = []
    posix = str(PurePosixPath(path))
    for pat, why in FORBIDDEN_PATHS:
        if pat.search(posix):
            problems.append(f"{path}: 公開しないファイル（{why}）")
    if len(data) > MAX_BYTES:
        problems.append(
            f"{path}: {len(data):,} バイト。データを入れていないか確かめる（上限 {MAX_BYTES:,}）"
        )
    if posix in SKIP_CONTENT or b"\0" in data[:8000]:
        return problems
    text = data.decode("utf-8", errors="replace")
    for n, line in enumerate(text.splitlines(), 1):
        if OK_MARK in line:
            continue
        for pat, why in SECRETS:
            if pat.search(line):
                problems.append(f"{path}:{n}: {why}らしい文字列")
        for m in EMAIL.finditer(line):
            if not ALLOWED_EMAIL.search(m.group(0)):
                problems.append(f"{path}:{n}: メールアドレス（{m.group(0)}）")
        if LOCAL_PATH.search(line):
            problems.append(f"{path}:{n}: PC のユーザーのフォルダのパス（ユーザー名が出る）")
    return problems


def check_author() -> list[str]:
    """作者のアドレスは、このリポジトリの設定（git config --local）にあるものだけを使う。
    リポジトリの設定が無いと、PC 全体の設定の別のアドレスで記録されてしまう（2026-11-11 に起きた）。"""
    local = subprocess.run(
        ["git", "config", "--local", "user.email"], check=False, capture_output=True, text=True
    ).stdout.strip()
    used = subprocess.run(
        ["git", "var", "GIT_AUTHOR_IDENT"], check=False, capture_output=True, text=True
    ).stdout
    if not local:
        return [
            "作者のアドレスがリポジトリに設定されていない（git config --local user.email で設定する）"
        ]
    if f"<{local}>" not in used:
        return [
            "作者のアドレスが、リポジトリの設定と違う（環境変数 GIT_AUTHOR_EMAIL などを確かめる）"
        ]
    return []


def main(argv: list[str]) -> int:
    sys.stderr.reconfigure(encoding="utf-8")  # Git Bash などに送ったときに化けないように
    staged = "--staged" in argv
    files = staged_files() if staged else tracked_files()
    problems = check_author() if staged else []
    for path in files:
        try:
            data = read(path, staged)
        except (OSError, subprocess.CalledProcessError):
            continue  # 消したファイル（--all で作業ツリーに無いもの）
        problems += check_file(path, data)
    if problems:
        print(
            "公開リポジトリに入れてはいけないものがあります（scripts/check_public.py）:",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(f"誤検知なら、その行に `{OK_MARK}` と理由を書いてください。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
