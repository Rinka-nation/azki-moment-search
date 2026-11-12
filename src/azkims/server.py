"""Local search API for the Web UI (standard library only).

azkims serve は 127.0.0.1 でだけ待ち受ける。公開側は同じハンドラを Vercel の Python 関数
（vercel/api/index.py）が使い、DB は Turso を読む（DESIGN.md 10 章）。

    GET /api/search?q=...&tab=match|related|maybe|readout|ts&word=&title=&from=&to=&sort=new|old|relevance&cursor=&limit=&counts=0
    GET /api/context?video_id=...&ms=...      前後の発言
    GET /api/stats                             データの概要
    GET /api/members                           ホロメンの推しマークと検索に使う呼び名
    それ以外                                   web/dist の静的ファイル（ビルド済みの画面）
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
import traceback
from contextlib import closing
from dataclasses import asdict
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from azkims import db
from azkims.config import PROJECT_ROOT, Config
from azkims.dictionary import load_dictionary
from azkims.reading import ReadingConverter
from azkims.related import load_related
from azkims.search import (
    RAW_SQL,
    SORTS,
    TIER_LABELS,
    TIER_READOUT,
    QueryError,
    count_related,
    count_tiers,
    parse_query,
    related_query,
    related_word_counts,
    ruby_segments,
    search,
    search_related,
    search_related_ts,
    search_timestamps,
    tab_count,
)
from azkims.turso import TursoConnection, TursoError

DIST_DIR = PROJECT_ROOT / "web" / "dist"
MAX_LIMIT = 50
CONTEXT_BEFORE_MS = 30_000
CONTEXT_AFTER_MS = 30_000
TABS = ("match", "related", "maybe", "readout", "ts")


class ApiError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def _page(primary, extra, cursor: str, limit: int) -> tuple[list, str | None]:
    """本体（primary）の後ろに続き（extra）をつなげて limit 件取る。(hits, 次の cursor) を返す。

    どちらも (件数, offset) → 結果のリスト。1 件多く取って、続きがあるかを件数を数えずに知る。
    """
    in_extra = cursor.startswith("x")
    pos = _int(cursor.removeprefix("x"), 0, 0, 100_000)
    hits: list = []
    if not in_extra:
        got = primary(limit + 1, pos)
        if len(got) > limit:
            return got[:limit], str(pos + limit)
        hits, pos = got, 0
    if extra is None:
        return hits, None
    need = limit - len(hits)
    got = extra(need + 1, pos)
    if len(got) > need:
        return hits + got[:need], f"x{pos + need}"
    return hits + got, None


class SearchService:
    """検索まわりの状態（辞書・Sudachi）を持つ。リクエストごとに読み取り専用で DB を開く。"""

    def __init__(self, cfg: Config, immutable: bool = False, remote: tuple[str, str] | None = None):
        """immutable: DB ファイルが書き換わらない環境（Vercel 等の読み取り専用 FS）。
        remote: Turso の (URL, トークン)。あれば手元の DB ファイルを使わない。"""
        self.cfg = cfg
        self.immutable = immutable
        self.remote = remote
        dictionary = load_dictionary(cfg.dictionary_dir)
        self.converter = ReadingConverter(dictionary)
        self.related = load_related(cfg.dictionary_dir, dictionary)
        # Sudachi の tokenizer はスレッド間で共有しない
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection | TursoConnection:
        if self.remote:
            return TursoConnection(*self.remote)
        if not self.cfg.db_path.exists():
            raise ApiError(
                "DB がありません。`azkims build` を実行してください", HTTPStatus.SERVICE_UNAVAILABLE
            )
        mode = "mode=ro&immutable=1" if self.immutable else "mode=ro"
        conn = sqlite3.connect(f"file:{self.cfg.db_path}?{mode}", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def search(self, params: dict[str, str]) -> dict:
        """counts=0 なら件数を数えない（さらに読み込む・タブの切り替え。画面は最初の検索の件数を使う）。

        件数も 20 件の並べ替えも一致をすべて読むので、件数を省くと読み取りがほぼ半分になる（DESIGN.md 10.5）。
        続きは cursor で取る。タイムスタンプ・もしかして のタブは本体の後ろに関連語の一致を続けるので、
        cursor は本体の位置（"20"）か、続きの位置（"x5"）。
        """
        query = params.get("q", "").strip()
        tab = params.get("tab", "match")
        if tab not in TABS:
            raise ApiError(f"unknown tab: {tab}")
        sort = params.get("sort", "new")
        # タイムスタンプのタブは search() を通らないので、ここで確かめる（ISS-09）
        if sort not in SORTS:
            raise ApiError(f"unknown sort: {sort}")
        cursor = params.get("cursor") or ""
        limit = _int(params.get("limit"), 20, 1, MAX_LIMIT)
        with_counts = params.get("counts") != "0"
        filters = {
            "title": params.get("title") or None,
            "date_from": _date(params.get("from")),
            "date_to": _date(params.get("to")),
        }
        word = params.get("word") or None
        exact = params.get("exact") == "1"
        dictionary = self.converter.dictionary
        try:
            with self._lock:
                terms = parse_query(query, self.converter, exact=exact)
            rq = None if exact else related_query(terms, self.related)
            with closing(self._connect()) as conn:
                counts = tiers = related = None
                if with_counts:
                    tiers = count_tiers(conn, terms, **filters)
                    ts_total = search_timestamps(conn, terms, limit=0, **filters)[1]
                    related_count = related_extra = ts_extra = 0
                    word_counts: list[tuple[str, int]] = []
                    if rq:
                        related_count = count_related(conn, rq, 1, **filters)
                        related_extra = count_related(conn, rq, 0, **filters)
                        word_counts = related_word_counts(conn, rq, **filters)
                        ts_extra = search_related_ts(conn, rq, limit=0, **filters)[1]
                        related = {
                            "tag": rq.tag,
                            "words": [{"word": w, "count": n} for w, n in word_counts],
                        }
                    counts = {
                        "match": tab_count(tiers, "match"),
                        "related": related_count,
                        "maybe": tab_count(tiers, "maybe") + related_extra,
                        "readout": tiers[TIER_READOUT],
                        "ts": ts_total + ts_extra,
                    }

                def captions(rows):
                    return [_caption_hit(h, dictionary) for h in rows]

                def stamps(rows):
                    return [_ts_hit(h) for h in rows]

                primary = extra = None
                if tab == "related":
                    if rq:
                        primary = lambda n, o: captions(
                            search_related(
                                conn, rq, 1, word=word, sort=sort, limit=n, offset=o, **filters
                            )
                        )
                elif tab == "ts":
                    primary = lambda n, o: stamps(
                        search_timestamps(
                            conn, terms, limit=n, offset=o, count=False, sort=sort, **filters
                        )[0]
                    )
                    # タイムスタンプ本体の後ろに関連語の一致を続ける
                    if rq:
                        extra = lambda n, o: stamps(
                            search_related_ts(
                                conn, rq, limit=n, offset=o, count=False, sort=sort, **filters
                            )[0]
                        )
                else:
                    primary = lambda n, o: captions(
                        search(conn, terms, tab=tab, sort=sort, limit=n, offset=o, **filters)
                    )
                    # もしかして の後ろに、話題でない配信の weak の語を続ける
                    if tab == "maybe" and rq:
                        extra = lambda n, o: captions(
                            search_related(conn, rq, 0, sort=sort, limit=n, offset=o, **filters)
                        )
                hits, next_cursor = _page(primary, extra, cursor, limit) if primary else ([], None)
        except QueryError as e:
            raise ApiError(str(e)) from e

        return {
            "terms": [asdict(t) for t in terms],
            "related": related,
            "counts": counts,
            "tiers": tiers and {TIER_LABELS[k]: v for k, v in tiers.items()},
            "tab": tab,
            "limit": limit,
            "cursor": next_cursor,
            "has_more": next_cursor is not None,
            "hits": hits,
        }

    def context(self, params: dict[str, str]) -> dict:
        video_id = params.get("video_id", "")
        ms = _int(params.get("ms"), 0, 0, 100_000_000)
        if not video_id:
            raise ApiError("video_id が必要です")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT u.id, u.start_ms, {RAW_SQL} AS text_raw, u.text, u.text_ai "
                "FROM utterances u "
                "WHERE u.video_id = ? AND u.start_ms BETWEEN ? AND ? "
                # 外した配信の発言は出さない（build し直す前の DB でも。ISS-01）。配信の行を 1 回引くだけ
                "AND EXISTS (SELECT 1 FROM streams s WHERE s.video_id = ? AND s.excluded_reason IS NULL) "
                "ORDER BY u.start_ms",
                (video_id, ms - CONTEXT_BEFORE_MS, ms + CONTEXT_AFTER_MS, video_id),
            ).fetchall()
        return {
            "video_id": video_id,
            "utterances": [
                {
                    "id": r["id"],
                    "start_ms": r["start_ms"],
                    "text": r["text_ai"] or r["text"],
                    "text_raw": r["text_raw"],
                    "ai": r["text_ai"] is not None,
                }
                for r in rows
            ],
        }

    def members(self) -> dict:
        """「ホロメンから探す」の一覧（build が meta に保存したもの。1 行読むだけ）。"""
        with closing(self._connect()) as conn:
            return {"members": json.loads(db.get_meta(conn, "members") or "[]")}

    def stats(self) -> dict:
        with closing(self._connect()) as conn:
            one = lambda sql: conn.execute(sql).fetchone()[0]
            # 全行を数えないよう、公開用 DB では export_slim_db が保存した件数を読む
            saved = lambda key, sql: int(db.get_meta(conn, key) or one(sql))
            lo, hi = conn.execute(
                "SELECT MIN(upload_date), MAX(upload_date) FROM streams "
                "WHERE caption_status = 'ok' AND excluded_reason IS NULL"
            ).fetchone()
            return {
                "streams": one(
                    "SELECT COUNT(*) FROM streams WHERE caption_status = 'ok' "
                    "AND excluded_reason IS NULL"
                ),
                "utterances": saved("utterance_count", "SELECT COUNT(*) FROM utterances"),
                "timestamp_entries": saved("timestamp_count", "SELECT COUNT(*) FROM ts_entries"),
                "date_from": lo,
                "date_to": hi,
                "built_at": db.get_meta(conn, "built_at"),
            }


def _date(value: str | None) -> str | None:
    """from・to は YYYY-MM-DD だけ受け付ける。日付は文字列で比べるので、形が違うと黙って結果がずれる（ISS-11）。"""
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat() if len(value) == 10 else _bad_date(value)
    except ValueError:
        return _bad_date(value)


def _bad_date(value: str):
    raise ApiError(f"日付は YYYY-MM-DD の形で指定してください: {value[:20]!r}")


def _int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value))) if value is not None else default
    except ValueError:
        return default


def _caption_hit(h, dictionary=None) -> dict:
    spans = [(m.start, m.end) for m in dictionary.find(h.display_text)] if dictionary else []
    return {
        "kind": "caption",
        "id": f"u{h.utterance_id}",
        "video_id": h.video_id,
        "title": h.title,
        "upload_date": h.upload_date,
        "sec": h.start_sec,
        "ms": h.start_ms,
        "timestamp": h.timestamp,
        "url": h.url,
        "text": h.display_text,
        "text_raw": h.text_raw,
        "label": h.label,
        "tier": h.tier,
        "candidates": [asdict(c) for c in h.candidates],
        "related_words": list(h.related_words),
        "quotes": [],
        "segments": ruby_segments(h.text_raw, h.text, h.text_fuzzy, spans, ai=h.text_ai),
    }


def _ts_hit(h) -> dict:
    return {
        "kind": "timestamp",
        "id": f"t{h.entry_id}",
        "video_id": h.video_id,
        "title": h.title,
        "upload_date": h.upload_date,
        "sec": h.sec,
        "ms": h.sec * 1000,
        "timestamp": h.timestamp,
        "url": h.url,
        "text": h.text,
        "text_raw": h.text,
        "label": h.label,
        "tier": h.tier,
        "candidates": [],
        "quotes": list(h.quotes),
        "related_words": list(h.related_words),
        "segments": [{"text": h.text}],
    }


# 公開側（Vercel）で成功した応答に付ける。Vercel の CDN のキャッシュはデプロイの URL ごとに分かれ、
# publish は毎回新しいデプロイを作るので、DB を入れ替えた後に古い結果は返らない（公式の Cache keys の説明。
# 2026-11-11 に確認）。データはデプロイ中は変わらないので 1 日にした。s-maxage と stale-while-revalidate は
# CDN がブラウザへ渡す前に外す。ローカルの serve は辞書を直して試すので、既定の no-store のまま
PUBLIC_CACHE = "public, s-maxage=86400, stale-while-revalidate=86400"


def make_handler(
    service: SearchService,
    dist_dir: Path = DIST_DIR,
    cache: str = "no-store",
    hosts: frozenset[str] | None = None,
):
    """hosts: 受け付ける Host ヘッダー。ローカルの serve だけが渡す（公開側は Vercel が振り分ける）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "azkims"

        def log_message(self, format, *args):
            pass

        def _json(self, status: HTTPStatus, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            # 失敗（400・503 など）はキャッシュさせない。DB の一時的な失敗が残らないように
            self.send_header("Cache-Control", cache if status == HTTPStatus.OK else "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str) -> None:
            if not dist_dir.exists():
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "画面がビルドされていません（web で npm run build）"},
                )
                return
            target = (dist_dir / path.lstrip("/")).resolve()
            if not target.is_relative_to(dist_dir.resolve()) or not target.is_file():
                target = dist_dir / "index.html"  # SPA: 不明なパスは画面に任せる
            data = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # 他のサイトが自分のドメインを 127.0.0.1 に向け直して（DNS rebinding）手元の API を読むのを防ぐ
            if hosts is not None and self.headers.get("Host", "").lower() not in hosts:
                self._json(HTTPStatus.FORBIDDEN, {"error": "unknown host"})
                return
            url = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            # Vercel: /api/<route> は vercel.json の rewrite で /api/index?route=<route> に来る
            if path.startswith("/api/index") and "route" in params:
                path = "/api/" + params.pop("route")
            routes = {
                "/api/search": service.search,
                "/api/context": service.context,
                "/api/stats": lambda _p: service.stats(),
                "/api/members": lambda _p: service.members(),
            }
            if path in routes:
                try:
                    self._json(HTTPStatus.OK, routes[path](params))
                except ApiError as e:
                    self._json(e.status, {"error": str(e)})
                except TursoError as e:
                    print(f"azkims: {e}", flush=True)
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "データベースに接続できません。時間をおいて試してください"},
                    )
                except Exception:  # noqa: BLE001  画面にエラーを返す
                    # 本番は誰でも呼べるので、例外の内容（SQL の断片やパス）は応答に入れずログにだけ残す
                    print(f"azkims: {path} failed\n{traceback.format_exc()}", flush=True)
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "検索中にエラーが起きました。時間をおいて試してください"},
                    )
                return
            if path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._static(path)

    return Handler


def serve(cfg: Config, port: int, log) -> None:
    service = SearchService(cfg)
    hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(service, hosts=hosts))
    log(f"http://127.0.0.1:{port}/ で待ち受けています（Ctrl+C で終了）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
