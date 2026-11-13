"""公開用 DB を Turso に入れ替え、プレビューと本番（β版）を切り替える（azkims publish）。

新しい DB を作る → 手元と同じ結果か確かめる → Vercel にプレビューをデプロイして API を確かめる →
固定 URL を切り替える → 本番用にデプロイ（--prod --skip-domain）して確かめる → promote → 古い DB を消す、の順に進む。

途中で失敗しても、固定 URL と本番は前のデプロイのまま動く（環境変数はデプロイに固定されるため）。
古い DB を消すのは切り替えの後だけ（2026-11-10 霧島リンカさんの決定で、本番も publish が更新する）。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from azkims.config import PROJECT_ROOT, Config
from azkims.deploy import DEPLOY_DIR, bundle_vercel, export_for_turso
from azkims.search import INCLUDED_ONLY
from azkims.server import SearchService
from azkims.turso import TursoError

PREVIEW_ALIAS = "azki-moment-search-preview.vercel.app"
# β版（URL を知っている人だけ。2026-11-10 霧島リンカさんの決定）。確認に通ったら毎回ここも切り替える
PRODUCTION_URL = "https://azki-moment-search.vercel.app"
VERCEL_PROJECT = "azki-moment-search"
# 消してよいのはこのプロジェクトの個々のデプロイの URL だけ。`vercel remove <プロジェクト名>` は全部を消すので、
# 形の違うものは渡さない
DEPLOYMENT_MB = 84  # 1 デプロイの大きさ（検索の関数。大半は Sudachi の辞書）
DEPLOYMENT_WARN = 30  # これを超えたら片付けを促す（約 2.5GB。上限は 10GB）
API_HOST = "api.turso.tech"
# 手元と Turso で件数を比べる語（3 文字以上、2 文字、読み、タイムスタンプ）
VERIFY_QUERIES = (
    {"q": "開拓者"},
    {"q": "配信"},
    {"q": "かな"},
    {"q": "かいたくしゃ"},
    {"q": "開拓者", "tab": "ts"},
)
RETRY_WAIT_SECONDS = 5
# 入れ替え直後の最初の検索は 25 秒かかって失敗した（2026-10-31）。温まるまでやり直す
VERIFY_ATTEMPTS = 4

SMOKE_CHECKS = (
    ("3 文字以上", {"q": "開拓者"}, lambda r: r["counts"]["match"] > 0),
    ("2 文字", {"q": "配信"}, lambda r: r["counts"]["match"] > 0),
    ("関連語", {"q": "天音かなた", "tab": "related"}, lambda r: r["hits"]),
    ("タイムスタンプ", {"q": "開拓者", "tab": "ts"}, lambda r: r["hits"]),
)


class PublishError(Exception):
    """入れ替えを続けられない。固定 URL は前のデプロイのまま。"""


def load_secrets(path: Path) -> dict[str, str]:
    """data/turso.env（git 管理外）を読む。値は表示しない。"""
    if not path.exists():
        raise PublishError(f"{path} がありません")
    secrets = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            secrets[key.replace("export ", "").strip()] = value.strip().strip('"')
    missing = [k for k in ("TURSO_API_TOKEN", "TURSO_ORG") if not secrets.get(k)]
    if missing:
        raise PublishError(f"{path} に {', '.join(missing)} がありません")
    return secrets


class TursoPlatform:
    """Turso の DB を作る・消す API。トークンは DB の削除もできるので手元だけで使う。"""

    def __init__(self, token: str, org: str, group: str = "default"):
        self._token = token
        self._org = org
        self._group = group

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = http.client.HTTPSConnection(API_HOST, timeout=60)
        try:
            conn.request(
                method,
                f"/v1/organizations/{self._org}{path}",
                json.dumps(body) if body is not None else None,
                {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            data = response.read()
        except (OSError, http.client.HTTPException) as e:
            raise PublishError(f"Turso API に接続できません: {type(e).__name__}") from e
        finally:
            conn.close()
        if response.status != 200:
            raise PublishError(f"Turso API が HTTP {response.status} でした: {data[:200]!r}")
        return json.loads(data)

    def create_for_upload(self, name: str) -> str:
        body = {"name": name, "group": self._group, "seed": {"type": "database_upload"}}
        return self._request("POST", "/databases", body)["database"]["Hostname"]

    def mint_token(self, name: str, read_only: bool, expiration: str) -> str:
        access = "read-only" if read_only else "full-access"
        query = f"expiration={expiration}&authorization={access}"
        return self._request("POST", f"/databases/{name}/auth/tokens?{query}", {})["jwt"]

    def upload(self, hostname: str, token: str, path: Path) -> None:
        conn = http.client.HTTPSConnection(hostname, timeout=1800)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Length": str(path.stat().st_size),
        }
        try:
            with open(path, "rb") as f:
                conn.request("POST", "/v1/upload", f, headers)
                response = conn.getresponse()
                data = response.read()
        except (OSError, http.client.HTTPException) as e:
            raise PublishError(f"アップロードに失敗しました: {type(e).__name__}") from e
        finally:
            conn.close()
        if response.status != 200:
            raise PublishError(f"アップロードが HTTP {response.status} でした: {data[:200]!r}")

    def delete(self, name: str) -> None:
        self._request("DELETE", f"/databases/{quote(name)}")


class VercelCli:
    """deploy/ から vercel コマンドを実行する。Vercel へのログインは手元のものを使う。"""

    def __init__(self, cwd: Path = DEPLOY_DIR):
        self._cwd = cwd
        self._npx = shutil.which("npx")

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        done = subprocess.run(
            [self._npx, "vercel", *args],
            check=False,
            cwd=self._cwd,
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if done.returncode != 0:
            raise PublishError(f"vercel {args[0]} に失敗しました: {done.stderr[-300:]}")
        return done

    def set_env(self, name: str, value: str, environment: str = "preview") -> None:
        # 値はコマンド引数に載せない（プロセス一覧やエラー表示に出さない）
        self._run(
            ["env", "add", name, "--environment", environment, "--force", "--sensitive", "--yes"],
            stdin=value,
        )

    def _deploy(self, args: list[str]) -> str:
        done = self._run(["deploy", *args, "--yes"])
        found = re.search(r"https://[a-z0-9-]+\.vercel\.app", done.stdout + done.stderr)
        if not found:
            raise PublishError("デプロイ先の URL を読み取れません")
        return found.group(0)

    def deploy_preview(self) -> str:
        return self._deploy([])

    def deploy_production(self) -> str:
        """本番の環境変数で作るが、本番のドメインには付けない（確かめてから promote する）。"""
        return self._deploy(["--prod", "--skip-domain"])

    def promote(self, deployment: str) -> None:
        self._run(["promote", deployment, "--yes"])

    def get_json(self, deployment: str, path: str, params: dict[str, str] | None = None) -> dict:
        args = ["curl", path, "--deployment", deployment, "--yes"]
        with tempfile.TemporaryDirectory() as tmp:
            if params:
                # Windows のコマンド経由だと引数の日本語が化けるので、値はファイルから curl に渡す
                args += ["--", "--get", "--silent"]
                for i, (key, value) in enumerate(params.items()):
                    file = Path(tmp) / str(i)
                    file.write_text(value, encoding="utf-8")
                    args += ["--data-urlencode", f"{key}@{file}"]
            return json.loads(self._run(args).stdout)

    def set_alias(self, deployment: str, alias: str) -> None:
        self._run(["alias", "set", deployment, alias])

    def list_deployments(self) -> list[str]:
        """このプロジェクトのデプロイの URL（https:// なし）。新しい順。"""
        hosts: list[str] = []
        page: int | None = None
        for _ in range(100):
            args = ["ls", VERCEL_PROJECT, "--format", "json"]
            if page:
                args += ["--next", str(page)]
            data = json.loads(self._run(args).stdout)
            hosts += [d["url"] for d in data.get("deployments", [])]
            page = (data.get("pagination") or {}).get("next")
            if not page:
                break
        return hosts


def _input_files(cfg: Config, root: Path = PROJECT_ROOT) -> list[Path]:
    """build の結果を決めるファイル。推しマークと設定は補正と関連語の索引に効く（ISS-04、2026-11-11）。"""
    files = sorted(cfg.dictionary_dir.rglob("*.toml"))
    files += [p for p in (root / "config" / n for n in CONFIG_FILES) if p.exists()]
    return files


CONFIG_FILES = ("oshi_marks.toml", "config.toml", "config.example.toml")


def stale_inputs(cfg: Config, root: Path = PROJECT_ROOT) -> list[str]:
    """DB を作った後に直された入力。publish は build をしないので、build し忘れた DB を公開しないために見る。

    週 1 回の実行（daily.sh）は build の直後に publish するので、ここに引っかかるのは手で直したときだけ。
    画面は web/dist を公開するので、web/src が web/dist より新しければ npm run build のし忘れ。
    """
    with closing(sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        included = conn.execute("SELECT value FROM meta WHERE key = ?", (INCLUDED_ONLY,)).fetchone()
    if not row:
        return ["DB（build されていません）"]
    # sync で外す配信が変わると消える印。build し直すまで、外した配信の発言が DB に残っている（ISS-01）
    if not included:
        return ["配信の除外（sync の後に build されていません）"]
    built = datetime.fromisoformat(row[0]).timestamp()
    inputs = _input_files(cfg, root) + sorted((cfg.data_dir / "llm").glob("**/out/*.jsonl"))
    inputs += sorted(cfg.comments_dir.glob("*.json")) + sorted(cfg.captions_dir.glob("*"))
    # built_at は秒単位で切り捨てているので 1 秒の余裕を見る
    stale = [p.name for p in inputs if p.stat().st_mtime > built + 1]
    dist = root / "web" / "dist" / "index.html"
    src = [
        p
        for p in (root / "web" / "src").rglob("*")
        if p.is_file() and not p.name.endswith(".spec.ts")
    ]
    if dist.exists() and src and max(p.stat().st_mtime for p in src) > dist.stat().st_mtime:
        stale.append("web/src（npm run build で web/dist を作り直してください）")
    return stale


def content_fingerprint(cfg: Config) -> str:
    """公開する中身が変わったかを見る値。結果は字幕 + タイムスタンプ + 辞書 + AI修正 で決まる（build 時刻は含めない）。

    検索のコードと画面（src/azkims, vercel, web/dist）も含める。API だけ直したときも公開し直すため（2026-11-09）。
    """
    digest = hashlib.sha256()
    with closing(sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)) as conn:
        videos = conn.execute(
            "SELECT video_id FROM streams "
            "WHERE excluded_reason IS NULL AND caption_status = 'ok' ORDER BY video_id"
        )
        for (video_id,) in videos:
            digest.update(video_id.encode())
        digest.update(str(conn.execute("SELECT COUNT(*) FROM ts_entries").fetchone()[0]).encode())
    # タイムスタンプは件数が同じまま書き直されることがある（（編集中）→ 完成版）
    for path in sorted(cfg.comments_dir.glob("*.json")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    for path in _input_files(cfg):
        digest.update(path.read_bytes())
    llm = cfg.data_dir / "llm"
    code = [PROJECT_ROOT / "src" / "azkims", PROJECT_ROOT / "vercel", PROJECT_ROOT / "web" / "dist"]
    for root, pattern in [(llm / "out", "*.jsonl"), (llm / "prop" / "out", "*.jsonl")] + [
        (c, "*") for c in code
    ]:
        for path in sorted(p for p in root.rglob(pattern) if p.is_file()):
            if "__pycache__" in path.parts:
                continue
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _state_path(cfg: Config) -> Path:
    return cfg.data_dir / "turso_active.json"


def load_state(cfg: Config) -> dict:
    path = _state_path(cfg)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _stream_ids(path: Path) -> set[str]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        return {r[0] for r in conn.execute("SELECT video_id FROM streams")}


def _check_removed_streams(cfg: Config, state: dict, ids: set[str]) -> None:
    """公開中より配信が減るなら止める。誤って小さい DB を公開しないための確認。

    ただし手元の DB で外した（excluded_reason を付けた）配信が抜けただけなら通す。削除依頼で配信を外したとき
    公開が止まらないように（ISS-04。2026-11-09 は turso_active.json を手で書き換えて通した）。
    公開中の配信の一覧を持っていない古い記録では、本数だけを比べる。
    """
    if "stream_ids" not in state:
        if len(ids) < state.get("streams", 0):
            raise PublishError(f"配信数が減っています: {state['streams']} → {len(ids)}")
        return
    removed = set(state["stream_ids"]) - ids
    if not removed:
        return
    with closing(sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)) as conn:
        marks = ",".join("?" * len(removed))
        excluded = {
            r[0]
            for r in conn.execute(
                f"SELECT video_id FROM streams WHERE video_id IN ({marks}) "
                "AND excluded_reason IS NOT NULL",
                sorted(removed),
            )
        }
    unexplained = sorted(removed - excluded)
    if unexplained:
        shown = "、".join(unexplained[:5]) + ("…" if len(unexplained) > 5 else "")
        raise PublishError(
            f"公開中の配信が {len(unexplained)} 本消えています（外す設定もありません）: {shown}"
        )


def verify_database(cfg: Config, url: str, token: str, log, sleep=time.sleep) -> None:
    """Turso の検索結果が手元の DB と同じことを確かめる。同時に DB を温める。"""
    local = SearchService(cfg)
    remote = SearchService(cfg, immutable=True, remote=(url, token))
    for params in VERIFY_QUERIES:
        expected = local.search(params)["counts"]
        for attempt in range(1, VERIFY_ATTEMPTS + 1):
            try:
                actual = remote.search(params)["counts"]
                break
            except TursoError:
                if attempt == VERIFY_ATTEMPTS:
                    raise
                log(f"  Turso の応答待ち（{attempt} 回目）…")
                sleep(RETRY_WAIT_SECONDS)
        if actual != expected:
            raise PublishError(f"件数が手元と違います: {params} {actual} != {expected}")
    log(f"Turso の検索結果が手元と一致しました（{len(VERIFY_QUERIES)} 件）")


def smoke_deployment(
    vercel: VercelCli, deployment: str, streams: int, log, sleep=time.sleep
) -> None:
    stats = vercel.get_json(deployment, "/api/stats")
    if stats.get("streams") != streams:
        raise PublishError(f"配信数が違います: {stats.get('streams')} != {streams}")
    for label, params, ok in SMOKE_CHECKS:
        # 本番の切り替え直後の 1 回目は、検索の形でない応答が返って止まった（2026-11-10。直後に試すと正常）。
        # 温まるまでやり直す
        for attempt in range(1, VERIFY_ATTEMPTS + 1):
            try:
                passed = bool(ok(vercel.get_json(deployment, "/api/search", params)))
            except (KeyError, TypeError, ValueError, PublishError):
                passed = False
            if passed:
                break
            if attempt == VERIFY_ATTEMPTS:
                raise PublishError(f"デプロイ先の確認に失敗しました: {label}")
            log(f"  デプロイ先の応答待ち（{label}、{attempt} 回目）…")
            sleep(RETRY_WAIT_SECONDS)
    log(f"デプロイ先の API を確認しました（配信 {streams} 本）")


def publish(
    cfg: Config,
    log,
    platform: TursoPlatform,
    vercel: VercelCli,
    force: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """入れ替えたら True、中身が変わっていなければ何もせず False。"""
    state = load_state(cfg)
    fingerprint = content_fingerprint(cfg)
    if not force and state.get("fingerprint") == fingerprint:
        log("公開中の DB と中身が同じなので、入れ替えません")
        return False

    stale = stale_inputs(cfg)
    if stale:
        shown = "、".join(stale[:5]) + ("…" if len(stale) > 5 else "")
        raise PublishError(
            f"DB を作った後に直したものがあります。build してから公開してください: {shown}"
        )

    upload = export_for_turso(cfg, log)
    ids = _stream_ids(upload)
    streams = len(ids)
    _check_removed_streams(cfg, state, ids)

    name = f"azkims-{now():%Y%m%d-%H%M}"
    log(f"Turso に {name} を作成してアップロード中…")
    hostname = platform.create_for_upload(name)
    try:
        write_token = platform.mint_token(name, read_only=False, expiration="1h")
        platform.upload(hostname, write_token, upload)
        read_token = platform.mint_token(name, read_only=True, expiration="never")
        url = f"libsql://{hostname}"
        verify_database(cfg, url, read_token, log)

        bundle_vercel(cfg, log, remote_db=True)
        vercel.set_env("TURSO_URL", url)
        vercel.set_env("TURSO_TOKEN", read_token)
        deployment = vercel.deploy_preview()
        log(f"デプロイしました: {deployment}")
        smoke_deployment(vercel, deployment, streams, log, sleep)
        vercel.set_alias(deployment, PREVIEW_ALIAS)
    except Exception:
        log(f"失敗したので {name} を消します。固定 URL は前のデプロイのままです")
        _delete_quietly(platform, name, log)
        raise

    old = {state.get("name"), state.get("production")} - {None, name}
    new_state = {
        "name": name,
        "streams": streams,
        "stream_ids": sorted(ids),
        "fingerprint": fingerprint,
        "deployment": deployment,
        "production": state.get("production"),
    }
    _save_state(cfg, new_state)
    log(f"切り替えました: https://{PREVIEW_ALIAS}（配信 {streams} 本）")

    # 本番（β版）も同じ DB に切り替える。失敗したら本番は前の DB のまま動くので、その DB は消さない
    promoted = False
    try:
        vercel.set_env("TURSO_URL", url, "production")
        vercel.set_env("TURSO_TOKEN", read_token, "production")
        production = vercel.deploy_production()
        log(f"本番用にデプロイしました: {production}")
        smoke_deployment(vercel, production, streams, log, sleep)
        vercel.promote(production)
        promoted = True
        smoke_deployment(vercel, PRODUCTION_URL, streams, log, sleep)
    except Exception:
        if promoted:
            # 切り替えは済んでいる。前の DB は消さずに残す（次の publish が消す）
            log(
                f"本番は {name} に切り替わりましたが、切り替え後の確認に失敗しました。"
                f"{PRODUCTION_URL} を確かめてください（前の DB {state.get('production')} は残しています）"
            )
        else:
            log(
                f"本番の切り替えに失敗しました。本番は前の DB（{state.get('production')}）のままです"
            )
        raise
    _save_state(cfg, {**new_state, "production": name, "production_deployment": production})
    log(f"本番を切り替えました: {PRODUCTION_URL}（配信 {streams} 本）")
    for db_name in sorted(old):
        _delete_quietly(platform, db_name, log)
    report_deployments(vercel, log)
    return True


def report_deployments(vercel: VercelCli, log) -> int | None:
    """デプロイの数を表示するだけで、消さない（2026-11-13 霧島リンカさん）。

    Vercel の Hobby はデプロイの保存が 10GB まで。publish のたびに 約 84MB が 2 個増える。
    Vercel CLI はエージェントからの削除に人の承認を求めるので、片付けは霧島リンカさんの承認を得て手で行う
    （`vercel remove <URL> --safe`。今の本番・プレビュー・1 つ前の本番は残す）。一覧を取れなくても公開は止めない。
    """
    try:
        count = len(vercel.list_deployments())
    except PublishError as e:
        log(f"デプロイの一覧を取れませんでした（公開は済んでいます）: {e}")
        return None
    note = f"デプロイは {count} 個（約 {count * DEPLOYMENT_MB / 1000:.1f} GB、上限 10 GB）"
    if count > DEPLOYMENT_WARN:
        note += "。古いものを片付けてください（DESIGN.md 10.4）"
    log(note)
    return count


def _save_state(cfg: Config, state: dict) -> None:
    _state_path(cfg).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _delete_quietly(platform: TursoPlatform, name: str, log) -> None:
    try:
        platform.delete(name)
        log(f"{name} を消しました")
    except PublishError as e:
        log(f"{name} を消せませんでした。Turso の画面で確認してください: {e}")


def run(cfg: Config, log, force: bool = False) -> bool:
    secrets = load_secrets(cfg.data_dir / "turso.env")
    platform = TursoPlatform(
        secrets["TURSO_API_TOKEN"], secrets["TURSO_ORG"], secrets.get("TURSO_GROUP", "default")
    )
    return publish(cfg, log, platform, VercelCli(), force=force)
