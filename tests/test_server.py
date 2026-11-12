import pytest

from azkims import db, related
from azkims.config import PROJECT_ROOT, Config
from azkims.dictionary import load_dictionary
from azkims.pipeline import analyze
from azkims.reading import ReadingConverter
from azkims.review import FuzzyMatcher
from azkims.server import ApiError, SearchService, _page
from azkims.textnorm import normalize_text


@pytest.fixture()
def service(tmp_path):
    cfg = Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=PROJECT_ROOT / "config" / "dictionary",
    )
    dictionary = load_dictionary(cfg.dictionary_dir, include_accepted=False)
    converter = ReadingConverter(dictionary)
    matcher = FuzzyMatcher(dictionary)
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '【GeoGuessr】鉄道', '2026-10-14', 'ok')"
    )
    for i, raw in enumerate(["ジオゲッサーやりたい", "開拓者のみんなありがとう", "釣り堀に行く"]):
        raw = normalize_text(raw)
        a = analyze(raw, "【GeoGuessr】鉄道", dictionary, converter, matcher)
        db.insert_utterance(
            conn,
            "v1",
            (i + 1) * 10_000,
            (i + 1) * 10_000 + 2000,
            raw,
            a.text,
            a.reading,
            a.corrections,
            a.text_fuzzy,
            a.fuzzy,
            a.words,
        )
    db.insert_ts_entry(
        conn, "v1", 12, "開拓者に感謝", [], False, "カイタクシヤニカンシヤ", ["カイタクシヤ"]
    )
    db.set_meta(conn, "built_at", "2026-10-25T00:00:00+00:00")
    conn.commit()
    conn.close()
    svc = SearchService(cfg)
    svc.converter = converter  # accepted.toml に左右されない同梱辞書で試す
    return svc


def test_search_response_shape(service):
    res = service.search({"q": "かいたくしゃ"})
    assert res["counts"] == {"match": 1, "related": 0, "maybe": 0, "readout": 0, "ts": 1}
    hit = res["hits"][0]
    assert hit["text"] == "開拓者のみんなありがとう"
    assert hit["label"] == "読み"
    assert hit["url"] == "https://www.youtube.com/watch?v=v1&t=18s"
    assert res["has_more"] is False


def test_search_tabs(service):
    ts = service.search({"q": "開拓者", "tab": "ts"})
    assert [h["kind"] for h in ts["hits"]] == ["timestamp"]
    maybe = service.search({"q": "つり", "tab": "maybe"})
    assert [h["text"] for h in maybe["hits"]] == ["釣り堀に行く"]


def test_search_never_scans_whole_tables(service):
    """外部 DB は読んだ行数で枠を消費する。全件走査が入ったらここで止める。"""
    import re
    import sqlite3

    statements: list[str] = []
    original = service._connect

    def traced():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    service._connect = traced
    for q in ("開拓者", "配信", "かな", "げっさー", "開拓者 配信"):
        for tab in ("match", "maybe", "ts"):
            service.search({"q": q, "tab": tab})

    plain = re.compile(r"SCAN (u|e|utterances|ts_entries)\b(?! USING)")
    virtual = re.compile(r"SCAN \w+ VIRTUAL TABLE INDEX \d+:$")
    conn = sqlite3.connect(service.cfg.db_path)
    conn.row_factory = sqlite3.Row
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert selects
    for sql in selects:
        if "_config" in sql:  # FTS5 が内部で読む設定の表
            continue
        for row in conn.execute("EXPLAIN QUERY PLAN " + sql):
            assert not plain.search(row["detail"]), (row["detail"], sql[:200])
            assert not virtual.search(row["detail"]), (row["detail"], sql[:200])
    conn.close()


def test_turso_connection_decodes_like_sqlite(monkeypatch):
    from azkims.turso import TursoConnection, TursoError, encode_arg

    cells = [
        {"type": "integer", "value": "1"},
        {"type": "float", "value": 2.5},
        {"type": "null"},
    ]
    result = {"cols": [{"name": "tier"}, {"name": "n"}, {"name": "t"}], "rows": [cells]}
    reply = {"results": [{"type": "ok", "response": {"result": result}}]}
    conn = TursoConnection("libsql://x.turso.io", "t")
    sent = []
    monkeypatch.setattr(conn, "_post", lambda body: sent.append(body) or reply)
    row = conn.execute("SELECT ?", ["a"]).fetchone()
    assert row["tier"] == 1 and row[1] == 2.5 and row["t"] is None
    assert b'"value": "a"' in sent[0]
    assert encode_arg(3) == {"type": "integer", "value": "3"}
    assert encode_arg(None) == {"type": "null"}

    monkeypatch.setattr(conn, "_post", lambda body: {"results": [{"type": "error", "error": "x"}]})
    with pytest.raises(TursoError):
        conn.execute("SELECT 1")


def test_search_errors(service):
    with pytest.raises(ApiError):
        service.search({"q": "a"})
    with pytest.raises(ApiError):
        service.search({"q": "開拓者", "tab": "nope"})


def test_context_and_stats(service):
    ctx = service.context({"video_id": "v1", "ms": "20000"})
    assert [u["text"] for u in ctx["utterances"]][:2] == [
        "GeoGuessrやりたい",
        "開拓者のみんなありがとう",
    ]
    stats = service.stats()
    assert stats["streams"] == 1 and stats["utterances"] == 3 and stats["timestamp_entries"] == 1


def test_export_slim_db(service, tmp_path):
    import sqlite3

    from azkims.deploy import export_slim_db

    dst = tmp_path / "slim.sqlite"
    export_slim_db(service.cfg.db_path, dst)
    conn = sqlite3.connect(dst)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "corrections" not in tables and "comment_fetch" not in tables
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["utterance_count"] == "3" and meta["timestamp_count"] == "1"
    assert "caption_error" not in [r[1] for r in conn.execute("PRAGMA table_info(streams)")]
    conn.close()
    slim = SearchService(service.cfg, immutable=True)
    slim.cfg = Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=service.cfg.dictionary_dir,
    )
    dst.replace(tmp_path / "azkims.sqlite")
    slim.converter = service.converter
    found = slim.search({"q": "かいたくしゃ"})
    assert found["counts"]["match"] == 1
    assert found["hits"][0]["text_raw"] == found["hits"][0]["text"]  # 空にした原文は text で補う
    ctx = slim.context({"video_id": "v1", "ms": "20000"})
    assert all(u["text_raw"] for u in ctx["utterances"])


def test_pack_and_unpack_db_roundtrip(tmp_path):
    from azkims.deploy import pack_db, unpack_db

    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(256)) * 4000)  # 1 MB
    parts = pack_db(src, tmp_path / "parts", part_size=1000)
    assert len(parts) > 1
    out = unpack_db(tmp_path / "parts", tmp_path / "out" / "db.sqlite")
    assert out.read_bytes() == src.read_bytes()


def test_exact_match(service):
    # 読みでは一致するが、表記が違うので完全一致では出ない
    assert service.search({"q": "かいたくしゃ", "exact": "1"})["counts"]["match"] == 0
    res = service.search({"q": "開拓者に感謝", "tab": "ts", "exact": "1"})
    assert [h["text"] for h in res["hits"]] == ["開拓者に感謝"]
    assert res["terms"] == [{"surface": "開拓者に感謝", "reading": ""}]


def test_related_tab(tmp_path):
    cfg = Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=PROJECT_ROOT / "config" / "dictionary",
    )
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', 'かなたんと雑談', '2026-10-14', 'ok'), "
        "('v2', '雑談', '2026-10-13', 'ok')"
    )
    rows = [
        ("v1", "天音かなたの話"),
        ("v1", "かなたん来た"),
        ("v1", "天使だった"),
        ("v2", "マジ天使"),
        ("v2", "中空の庭いいよね"),
        ("v2", "別の話"),
    ]
    for i, (vid, text) in enumerate(rows):
        db.insert_utterance(conn, vid, i * 1000, i * 1000 + 500, text, text, "", [], None, [], [])
    conn.execute("INSERT INTO utterances_fts(utterances_fts) VALUES ('rebuild')")
    db.insert_ts_entry(conn, "v1", 3, "ゴリラの話", [], False, "", [])
    service = SearchService(cfg)
    related.build_index(conn, service.related)
    conn.commit()
    conn.close()

    res = service.search({"q": "天音かなた", "tab": "related"})
    assert res["related"]["tag"] == "天音かなた"
    assert res["counts"]["match"] == 1
    # 関連語タブは検索した名前そのものの発言も含める
    assert res["counts"]["related"] == 4
    assert {h["text"] for h in res["hits"]} == {
        "天音かなたの話",
        "かなたん来た",
        "天使だった",
        "中空の庭いいよね",
    }
    assert {w["word"] for w in res["related"]["words"]} == {
        "天音かなた",
        "かなたん",
        "天使",
        "中空の庭",
    }

    only = service.search({"q": "天音かなた", "tab": "related", "word": "天使"})
    assert [h["text"] for h in only["hits"]] == ["天使だった"]
    assert only["hits"][0]["related_words"] == ["天使"]
    assert only["counts"]["related"] == 4 and not only["has_more"]

    maybe = service.search({"q": "天音かなた", "tab": "maybe"})
    assert maybe["counts"]["maybe"] == 1
    assert [h["text"] for h in maybe["hits"]] == ["マジ天使"]
    assert maybe["hits"][0]["label"] == "関連語"

    ts = service.search({"q": "天音かなた", "tab": "ts"})
    assert [h["text"] for h in ts["hits"]] == ["ゴリラの話"]

    # さらに読み込む: 件数を数えず、cursor で続きを取る
    seen, cursor = [], ""
    while True:
        page = service.search(
            {"q": "天音かなた", "tab": "related", "limit": "1", "counts": "0", "cursor": cursor}
        )
        assert page["counts"] is None and page["related"] is None
        seen += [h["text"] for h in page["hits"]]
        if not page["has_more"]:
            break
        cursor = page["cursor"]
    assert sorted(seen) == sorted(h["text"] for h in res["hits"])

    narrowed = service.search({"q": "天音かなた 中空の", "tab": "related"})
    assert [h["text"] for h in narrowed["hits"]] == ["中空の庭いいよね"]

    assert service.search({"q": "別の話"})["related"] is None
    assert service.search({"q": "天音かなた", "exact": "1"})["related"] is None


def test_readout_tab_separates_names_from_talk(tmp_path):
    cfg = Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=PROJECT_ROOT / "config" / "dictionary",
    )
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '雑談', '2026-10-14', 'ok')"
    )
    rows = [
        ("ハイドラさん、マコールさん、宮本さん、ありがとう。", ""),  # 全部が読み上げ
        ("ハイドラさん、マコールさん、宮本さん、ラーメンおいしかった。", "ラーメンおいしかった"),
        ("ハイドラの話をしよう", None),  # 読み上げではない
    ]
    for i, (text, kept) in enumerate(rows):
        db.insert_utterance(
            conn, "v1", i * 1000, i * 1000 + 500, text, text, "", [], None, [], [], kept, kept
        )
    conn.execute("INSERT INTO utterances_fts(utterances_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()
    service = SearchService(cfg)

    res = service.search({"q": "マコール"})
    assert res["counts"]["match"] == 0
    assert res["counts"]["readout"] == 2

    res = service.search({"q": "ハイドラ"})
    assert res["counts"]["match"] == 1
    assert [h["text"] for h in res["hits"]] == ["ハイドラの話をしよう"]
    assert res["counts"]["readout"] == 2

    res = service.search({"q": "ハイドラ", "tab": "readout"})
    assert len(res["hits"]) == 2
    assert res["has_more"] is False

    # 読み上げと同じ発言の中のメッセージは 一致 に出る
    assert service.search({"q": "ラーメン"})["counts"]["match"] == 1


def test_page_joins_primary_and_extra():
    primary = list(range(5))
    extra = ["a", "b", "c"]

    def get(rows):
        return lambda n, o: rows[o : o + n]

    pages, cursor = [], ""
    while cursor is not None:
        hits, cursor = _page(get(primary), get(extra), cursor, 3)
        pages.append(hits)
    assert pages == [[0, 1, 2], [3, 4, "a"], ["b", "c"]]
    # 本体がちょうど limit 件で終わるとき、次は続きの先頭から
    assert _page(get(primary[:3]), get(extra), "", 3) == ([0, 1, 2], "x0")
    assert _page(get(primary[:3]), None, "", 3) == ([0, 1, 2], None)
    assert _page(get(primary), get(extra), "x2", 3) == (["c"], None)


def test_newest_first_by_index_order(tmp_path):
    """番号順が新しい順の DB（build が作るもの）では、索引の番号で取っても並べ替えと同じ結果になる。"""
    cfg = Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=PROJECT_ROOT / "config" / "dictionary",
    )
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('new', '雑談', '2026-10-14', 'ok'), ('old', '雑談', '2026-10-01', 'ok')"
    )
    i = 0
    for vid in ("new", "old"):  # build と同じく新しい配信から番号を振る
        for n in range(4):
            i += 1
            text = f"開拓者のみんな{n}" if n % 2 == 0 else "別の話"
            db.insert_utterance(
                conn, vid, i * 1000, i * 1000 + 500, text, text, "", [], None, [], []
            )
    conn.execute("INSERT INTO utterances_fts(utterances_fts) VALUES ('rebuild')")
    conn.commit()
    service = SearchService(cfg)

    def pages():
        out, cursor = [], ""
        while cursor is not None:
            page = service.search({"q": "開拓者", "limit": "1", "cursor": cursor, "counts": "0"})
            out += [(h["video_id"], h["ms"]) for h in page["hits"]]
            cursor = page["cursor"]
        return out

    slow = pages()
    db.set_meta(conn, "utterance_order", "upload_date DESC, video_id, start_ms")
    conn.commit()
    conn.close()
    assert pages() == slow == [("new", 1000), ("new", 3000), ("old", 5000), ("old", 7000)]


def test_counts_without_streams_join(service):
    """外した配信の発言が無い DB（build が作るもの）では、配信と結合せずに数えても件数は同じ。"""
    before = service.search({"q": "かいたくしゃ"})["counts"]
    conn = db.connect(service.cfg.db_path)
    db.set_meta(conn, "included_only", "1")
    conn.commit()
    assert service.search({"q": "かいたくしゃ"})["counts"] == before
    assert service.search({"q": "開拓者", "title": "鉄道"})["counts"]["match"] == 1

    # 外す配信を決め直したら、次の build までは結合して外す
    from azkims.pipeline import reclassify

    reclassify(conn, service.cfg)
    conn.commit()
    assert conn.execute("SELECT value FROM meta WHERE key = 'included_only'").fetchone() is None
    conn.close()


def test_member_list_picks_the_most_used_name(tmp_path):
    cfg = Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=PROJECT_ROOT / "config" / "dictionary",
    )
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '雑談', '2026-10-14', 'ok')"
    )
    for i, text in enumerate(["かなたん来た", "かなたんと話した", "天音かなたの話", "ぺこちゃん"]):
        db.insert_utterance(conn, "v1", i * 1000, i * 1000 + 500, text, text, "", [], None, [], [])
    service = SearchService(cfg)
    related.build_index(conn, service.related)
    marks = {"👯": "兎田ぺこら", "💫": "天音かなた"}
    members = related.member_list(conn, service.related, marks)
    db.set_meta(conn, "members", __import__("json").dumps(members, ensure_ascii=False))
    conn.commit()
    conn.close()

    got = service.members()["members"]
    assert [m["name"] for m in got] == ["兎田ぺこら", "天音かなた"]  # 推しマークの表の順
    assert got[1] == {"name": "天音かなた", "mark": "💫", "nickname": "かなたん", "count": 3}
    assert got[0]["nickname"] == "ぺこちゃん"
    # その呼び名で検索すると関連語タブが出る
    assert service.search({"q": "かなたん", "tab": "related"})["related"]["tag"] == "天音かなた"


def _get(handler_cls, path: str, headers: dict[str, str] | None = None):
    """ハンドラを実際の HTTP サーバーで動かして 1 回 GET する。(状態, ヘッダー, 本文)"""
    import json as _json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers or {}), timeout=10
            ) as r:
                return r.status, r.headers, _json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.headers, _json.loads(e.read())
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unexpected_error_does_not_leak_details(service, monkeypatch, capsys):
    # NT-001（ISS-07）: 想定外の例外の型・内容は応答に入れず、ログにだけ残す
    from azkims.server import make_handler

    def boom(_params):
        raise RuntimeError("SELECT secret FROM C:/path/azkims.sqlite")

    monkeypatch.setattr(service, "search", boom)
    status, headers, body = _get(make_handler(service), "/api/search?q=abc")
    assert status == 500
    assert "RuntimeError" not in body["error"] and "secret" not in body["error"]
    assert headers["Cache-Control"] == "no-store"
    assert "RuntimeError: SELECT secret" in capsys.readouterr().out


def test_public_handler_lets_cdn_cache_only_successes(service):
    # NT-002（ISS-10）: 公開側は成功した応答だけ CDN にキャッシュさせる。ローカルは no-store のまま
    from azkims.server import PUBLIC_CACHE, make_handler

    status, headers, _ = _get(
        make_handler(service, cache=PUBLIC_CACHE), "/api/search?q=%E9%96%8B%E6%8B%93%E8%80%85"
    )
    assert status == 200 and "s-maxage=" in headers["Cache-Control"]
    assert "no-store" not in headers["Cache-Control"]
    status, headers, _ = _get(
        make_handler(service, cache=PUBLIC_CACHE), "/api/search?q=abc&tab=bogus"
    )
    assert status == 400 and headers["Cache-Control"] == "no-store"
    _, headers, _ = _get(make_handler(service), "/api/stats")
    assert headers["Cache-Control"] == "no-store"


def test_queries_without_an_index_are_refused(service):
    # NT-009（ISS-05）: 索引で引けない語は全件を読むので 400 で断る。2 文字でも読みがあれば探せる
    for params in ({"q": "開拓", "exact": "1"}, {"q": "!?"}, {"q": "??", "tab": "readout"}):
        with pytest.raises(ApiError):
            service.search(params)
    assert service.search({"q": "開拓"})["counts"]["match"] >= 1
    assert service.search({"q": "開拓者", "exact": "1"})["counts"]["match"] >= 1


def test_turso_connection_stops_within_the_function_limit(monkeypatch):
    # ISS-08: SQL を何本送っても、合計の待ち時間が関数の上限（15 秒）より前に TursoError にする
    from azkims.turso import TursoConnection, TursoError

    now = [0.0]
    conn = TursoConnection("libsql://x.turso.io", "t", budget=12.0, clock=lambda: now[0])
    timeouts = []

    class FakeHTTP:
        sock = None

        def __init__(self, host, timeout):
            timeouts.append(timeout)

        def request(self, *a, **k):
            now[0] += 5.0  # 1 本 5 秒かかる
            raise TimeoutError

        def close(self):
            pass

    monkeypatch.setattr("azkims.turso.http.client.HTTPSConnection", FakeHTTP)
    with pytest.raises(TursoError):
        conn.execute("SELECT 1")
    assert timeouts == [12.0, 7.0]  # やり直しは残りの時間だけ待つ
    with pytest.raises(TursoError):
        conn.execute("SELECT 1")  # 3 本目は残りの 2 秒だけ待ち、やり直しの時間は無いので止める
    assert len(timeouts) == 3 and timeouts[2] == 2.0


def test_cli_headers_follow_the_tier_numbers():
    # NT-012（ISS-12）: AI修正 を tier 2 に入れた後の番号で内訳を出す
    from azkims.cli import tab_header
    from azkims.search import TIER_AI, TIER_RAW, TIER_READING_PART

    counts = {TIER_RAW: 16, 1: 122, TIER_AI: 34, 3: 0, 4: 5, TIER_READING_PART: 7, 8: 1}
    assert tab_header("match", counts) == "■ 一致 172 件（原文 16 / 補正 122 / AI修正 34 / 読み 0）"
    assert tab_header("maybe", counts).startswith("■ もしかして 12 件（候補 5 / 読み(部分) 7）")


def test_timestamp_tab_follows_the_sort(service):
    # NT-010（ISS-09）: タイムスタンプのタブも 古い順 を守る。不正な並び順は 400
    conn = db.connect(service.cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v0', '古い配信', '2025-05-01', 'ok')"
    )
    db.insert_ts_entry(
        conn, "v0", 30, "開拓者と旅", [], False, "カイタクシヤトタビ", ["カイタクシヤ"]
    )
    conn.commit()
    conn.close()
    dates = lambda sort: [
        h["upload_date"] for h in service.search({"q": "開拓者", "tab": "ts", "sort": sort})["hits"]
    ]
    assert dates("new") == ["2026-10-14", "2025-05-01"]
    assert dates("old") == ["2025-05-01", "2026-10-14"]
    with pytest.raises(ApiError):
        service.search({"q": "開拓者", "tab": "ts", "sort": "bogus"})


def test_dates_must_be_iso(service):
    # NT-011（ISS-11）: from・to は YYYY-MM-DD だけ
    for bad in ("abc", "2026/10/01", "2026-13-01", "2026-1-1"):
        with pytest.raises(ApiError):
            service.search({"q": "開拓者", "from": bad})
    assert service.search({"q": "開拓者", "from": "2026-10-01", "to": "2026-10-31"})["hits"]
    assert service.search({"q": "開拓者", "from": "2026-10-15"})["hits"] == []


def test_context_hides_excluded_streams(service):
    # NT-004（ISS-01）: build し直す前の DB に残った、外した配信の発言は前後の発言に出さない
    assert service.context({"video_id": "v1", "ms": "20000"})["utterances"]
    conn = db.connect(service.cfg.db_path)
    conn.execute("UPDATE streams SET excluded_reason = 'manual' WHERE video_id = 'v1'")
    conn.commit()
    conn.close()
    assert service.context({"video_id": "v1", "ms": "20000"})["utterances"] == []


def test_local_serve_refuses_unknown_host(service):
    # UT-112: ローカルの serve は Host ヘッダーが手元の名前でなければ断る（DNS rebinding 対策）
    from azkims.server import make_handler

    handler = make_handler(service, hosts=frozenset({"127.0.0.1:8765", "localhost:8765"}))
    status, headers, body = _get(handler, "/api/stats", {"Host": "evil.example:8765"})
    assert status == 403 and body == {"error": "unknown host"}
    assert headers["Cache-Control"] == "no-store"
    status, _, body = _get(handler, "/api/stats", {"Host": "localhost:8765"})
    assert status == 200 and body["streams"] >= 0
    # 公開側（hosts を渡さない）は Host を見ない
    status, _, _ = _get(
        make_handler(service), "/api/stats", {"Host": "azki-moment-search.vercel.app"}
    )
    assert status == 200
