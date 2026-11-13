"""字幕を手で直す機能（manualfix。2026-11-12）。"""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from azkims import db, manualfix, youtube
from azkims.config import PROJECT_ROOT, Config
from azkims.manualfix import ManualFix
from azkims.pipeline import build
from azkims.server import ManualEditor, SearchService, make_handler


def _cfg(tmp_path):
    return Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=PROJECT_ROOT / "config" / "dictionary",
    )


def test_latest_line_wins_and_null_removes(tmp_path):
    """UT-120: 同じ発言は後の行が勝つ。text が null は取り消し。壊れた行は飛ばす"""
    path = tmp_path / manualfix.FILE
    manualfix.append(path, ManualFix("v1", 1000, "文教", "分岐"))
    manualfix.append(path, ManualFix("v1", 1000, "文教", "分岐点"))
    manualfix.append(path, ManualFix("v2", 2000, "あ", "い"))
    manualfix.append(path, ManualFix("v2", 2000, "あ", None))
    with path.open("a", encoding="utf-8") as f:
        f.write("{壊れた行\n")
    fixes = manualfix.load(path)
    assert {k: v.text for k, v in fixes.items()} == {("v1", 1000): "分岐点"}
    assert fixes[("v1", 1000)].at  # 時刻が入る
    assert fixes[("v1", 1000)].by == "human"


def test_lookup_skips_when_raw_changed():
    """UT-121: 原文が保存したときと違えば当てずに数える。同じ時刻の別の発言に当たれば知らせない"""
    fixes = {
        ("v1", 1000): ManualFix("v1", 1000, "文教", "分岐"),
        ("v1", 2000): ManualFix("v1", 2000, "古い字幕", "直した文"),
    }
    stats = manualfix.ApplyStats()
    assert manualfix.lookup(fixes, "v1", 1000, "文教", stats) == "分岐"
    # 同じ開始時刻の別の発言（長さ 0 の発言）は当てないが、1000 は当たっているので知らせない（2026-11-13）
    assert manualfix.lookup(fixes, "v1", 1000, "別の字幕", stats) is None
    assert manualfix.lookup(fixes, "v1", 5000, "文教", stats) is None
    assert manualfix.lookup(fixes, "v1", 2000, "新しい字幕", stats) is None
    assert stats.used == 1 and stats.raw_changed == [("v1", 2000)]


def _caption(cfg, video_id, events):
    path = youtube.caption_path(cfg.captions_dir, video_id, cfg.caption_lang)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"events": [{"tStartMs": 0, "dDurationMs": 999999, "id": 1}, *events]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_build_uses_hand_fix_before_ai_and_dictionary(tmp_path):
    """UT-122: build は手で直した文を（全角・半角を揃えて）本文にし、AI修正 は当てない。読みでも引ける"""
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '雑談', '2025-11-20', 'ok')"
    )
    conn.commit()
    _caption(
        cfg,
        "v1",
        [
            {"tStartMs": 1000, "segs": [{"utf8": "どんな文教を迎えても"}]},
            {"tStartMs": 9000, "segs": [{"utf8": "開拓者のみんなありがとう"}]},
        ],
    )
    raw = "どんな文教を迎えても"
    manualfix.append(
        manualfix.path_for(cfg.data_dir), ManualFix("v1", 1000, raw, "どんな分岐を迎えても！")
    )
    logs: list[str] = []
    build(conn, cfg, logs.append)
    rows = dict(conn.execute("SELECT start_ms, text FROM utterances").fetchall())
    assert rows[1000] == "どんな分岐を迎えても!"  # 検索の語と同じく全角を半角に揃える（2026-11-13）
    assert rows[9000] == "開拓者のみんなありがとう"
    ai = conn.execute("SELECT text_ai, text_raw FROM utterances WHERE start_ms = 1000").fetchone()
    assert ai[0] is None and ai[1] == raw
    assert any("手で直した字幕: 1 件のうち 1 件を適用" in m for m in logs)
    conn.close()


def _service(tmp_path):
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '雑談', '2025-11-20', 'ok')"
    )
    uid = db.insert_utterance(
        conn,
        "v1",
        1000,
        3000,
        "どんな文教を迎えても",
        "どんな文教を迎えても",
        "ドンナブンキヨウヲムカエテモ",
        [],
    )
    db.set_meta(conn, "built_at", "2025-11-21T00:00:00+00:00")
    conn.commit()
    conn.close()
    return cfg, SearchService(cfg), uid


def _request(handler, method, path, body=None, headers=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=20)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        return r.status, json.loads(r.read())
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_public_handler_has_no_admin(tmp_path):
    """UT-123: 公開側の handler（editor なし）には編集の API が無い"""
    _, service, uid = _service(tmp_path)
    handler = make_handler(service)
    assert _request(handler, "GET", "/api/admin")[0] == 404
    status, _ = _request(
        handler,
        "POST",
        "/api/admin/fix",
        {"id": f"u{uid}", "text": "x"},
        {"Content-Type": "application/json"},
    )
    assert status == 404


def test_local_editor_saves_and_refuses_other_sites(tmp_path):
    """UT-124: 手元の serve は直した文を保存してすぐ反映する。ほかのサイトからの送信（Origin 違い）は断る"""
    cfg, service, uid = _service(tmp_path)
    host = "127.0.0.1:8765"
    handler = make_handler(service, hosts=frozenset({host}), editor=ManualEditor(cfg, service))
    ok_headers = {"Host": host, "Origin": f"http://{host}", "Content-Type": "application/json"}
    body = {"id": f"u{uid}", "text": "どんな分岐を迎えても", "note": "AI の門出を直す"}

    assert _request(handler, "GET", "/api/admin", headers={"Host": host}) == (200, {"admin": True})
    bad = {**ok_headers, "Origin": "https://evil.example"}
    assert _request(handler, "POST", "/api/admin/fix", body, bad)[0] == 403
    form = {**ok_headers, "Content-Type": "text/plain"}
    assert _request(handler, "POST", "/api/admin/fix", body, form)[0] == 403
    assert _request(handler, "POST", "/api/admin/fix", {**body, "id": "t1"}, ok_headers)[0] == 400

    status, res = _request(handler, "POST", "/api/admin/fix", body, ok_headers)
    assert status == 200 and res["text"] == "どんな分岐を迎えても"
    saved = manualfix.load(manualfix.path_for(cfg.data_dir))[("v1", 1000)]
    assert (saved.raw, saved.text, saved.note) == (
        "どんな文教を迎えても",
        "どんな分岐を迎えても",
        "AI の門出を直す",
    )
    # 手元の DB にもすぐ反映され、新しい言葉で検索できる
    hits = service.search({"q": "分岐を迎え"})["hits"]
    assert [h["text"] for h in hits] == ["どんな分岐を迎えても"]
