import json
import sqlite3
from datetime import UTC, datetime

import pytest

from azkims import db, publish
from azkims.config import PROJECT_ROOT, Config
from azkims.publish import PublishError, content_fingerprint, load_secrets
from azkims.publish import stale_inputs as real_stale_inputs


class FakePlatform:
    def __init__(self, events):
        self.events = events

    def create_for_upload(self, name):
        self.events.append(("create", name))
        return f"{name}-org.turso.io"

    def mint_token(self, name, read_only, expiration):
        return "read" if read_only else "write"

    def upload(self, hostname, token, path):
        assert token == "write"
        self.events.append(("upload", hostname))

    def delete(self, name):
        self.events.append(("delete", name))


class FakeVercel:
    def __init__(self, events, streams=1, broken=False, broken_production=False):
        self.events = events
        self.streams = streams
        self.broken = broken
        self.broken_production = broken_production

    def set_env(self, name, value, environment="preview"):
        self.events.append(("env", name, environment))

    def deploy_preview(self):
        self.events.append(("deploy",))
        return "https://new.vercel.app"

    def deploy_production(self):
        self.events.append(("deploy-prod",))
        if self.broken_production:
            return "https://broken-prod.vercel.app"
        return "https://prod.vercel.app"

    def promote(self, deployment):
        self.events.append(("promote", deployment))

    def get_json(self, deployment, path, params=None):
        if deployment == "https://broken-prod.vercel.app":
            return {"streams": 0}
        if path == "/api/stats":
            return {"streams": self.streams}
        return {"counts": {"match": 0 if self.broken else 1}, "hits": [] if self.broken else [1]}

    def set_alias(self, deployment, alias):
        self.events.append(("alias", deployment))


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    dictionary = tmp_path / "dictionary"
    dictionary.mkdir()
    (dictionary / "a.toml").write_text("# a", encoding="utf-8")
    cfg = Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=dictionary,
    )
    conn = db.connect(cfg.db_path)
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', 't', '2026-01-01', 'ok')"
    )
    conn.commit()
    conn.close()

    def export(cfg, log):
        path = cfg.data_dir / "turso_upload.sqlite"
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS streams (video_id TEXT)")
            c.execute("DELETE FROM streams")
            c.execute("INSERT INTO streams VALUES ('v1')")
        return path

    monkeypatch.setattr(publish, "export_for_turso", export)
    monkeypatch.setattr(publish, "bundle_vercel", lambda *a, **k: None)
    monkeypatch.setattr(publish, "verify_database", lambda *a, **k: None)
    # build し忘れの確認は test_stale_inputs_* で見る（ここではリポジトリの web/ の状態に左右されないように外す）
    monkeypatch.setattr(publish, "stale_inputs", lambda cfg: [])
    return cfg


def _publish(cfg, events, vercel=None, force=False):
    return publish.publish(
        cfg,
        lambda m: None,
        FakePlatform(events),
        vercel or FakeVercel(events),
        force=force,
        now=lambda: datetime(2026, 11, 1, 8, 40, tzinfo=UTC),
        sleep=lambda s: None,
    )


def _write_state(cfg, **state):
    (cfg.data_dir / "turso_active.json").write_text(json.dumps(state), encoding="utf-8")


def test_old_database_is_deleted_only_after_the_alias_moves(cfg):
    _write_state(cfg, name="azkims-main", streams=1, fingerprint="old")
    events = []
    assert _publish(cfg, events) is True
    names = [e[0] for e in events]
    assert names.index("upload") < names.index("deploy") < names.index("alias")
    assert names.index("alias") < events.index(("delete", "azkims-main"))
    state = publish.load_state(cfg)
    assert state["name"] == "azkims-20261101-0840"
    assert state["fingerprint"] == content_fingerprint(cfg)
    assert "read" not in state.values()  # トークンを保存しない


def test_failed_check_keeps_the_alias_and_old_database(cfg):
    _write_state(cfg, name="azkims-main", streams=1, fingerprint="old")
    events = []
    with pytest.raises(PublishError):
        _publish(cfg, events, FakeVercel(events, broken=True))
    assert ("alias", "https://new.vercel.app") not in events
    assert ("delete", "azkims-main") not in events
    assert ("delete", "azkims-20261101-0840") in events
    assert publish.load_state(cfg)["name"] == "azkims-main"


def test_wrong_stream_count_on_the_deployment_stops_the_switch(cfg):
    events = []
    with pytest.raises(PublishError):
        _publish(cfg, events, FakeVercel(events, streams=2))
    assert not any(e[0] == "alias" for e in events)


def test_nothing_happens_when_the_content_is_unchanged(cfg):
    _write_state(cfg, name="azkims-main", streams=1, fingerprint=content_fingerprint(cfg))
    events = []
    assert _publish(cfg, events) is False
    assert events == []


def test_force_publishes_unchanged_content(cfg):
    _write_state(cfg, name="azkims-main", streams=1, fingerprint=content_fingerprint(cfg))
    events = []
    assert _publish(cfg, events, force=True) is True


def test_shrinking_stream_count_is_refused_before_touching_turso(cfg):
    _write_state(cfg, name="azkims-main", streams=5, fingerprint="old")
    events = []
    with pytest.raises(PublishError):
        _publish(cfg, events)
    assert events == []


def test_fingerprint_follows_the_dictionary(cfg):
    before = content_fingerprint(cfg)
    (cfg.dictionary_dir / "a.toml").write_text("# b", encoding="utf-8")
    assert content_fingerprint(cfg) != before


def test_load_secrets_requires_the_api_token(tmp_path):
    path = tmp_path / "turso.env"
    path.write_text("TURSO_ORG=x\n", encoding="utf-8")
    with pytest.raises(PublishError, match="TURSO_API_TOKEN"):
        load_secrets(path)


def test_verify_retries_until_the_database_is_warm(monkeypatch):
    from azkims import publish as module
    from azkims.turso import TursoError

    calls = {"n": 0}

    class Local:
        def __init__(self, cfg, immutable=False, remote=None):
            self.remote = remote

        def search(self, params):
            if self.remote:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TursoError("cold")
            return {"counts": {"match": 1}}

    monkeypatch.setattr(module, "SearchService", Local)
    cfg = Config(
        channel_url="",
        data_dir=PROJECT_ROOT,
        output_dir=PROJECT_ROOT,
        dictionary_dir=PROJECT_ROOT,
    )
    module.verify_database(cfg, "u", "t", lambda m: None, sleep=lambda s: None)
    assert calls["n"] == len(module.VERIFY_QUERIES) + 1


def test_fingerprint_follows_timestamp_comments(cfg):
    cfg.comments_dir.mkdir()
    (cfg.comments_dir / "v1.json").write_text("[]", encoding="utf-8")
    before = content_fingerprint(cfg)
    (cfg.comments_dir / "v1.json").write_text('[{"text": "1:00 a"}]', encoding="utf-8")
    assert content_fingerprint(cfg) != before


def test_production_follows_after_the_checks(cfg):
    _write_state(cfg, name="azkims-main", production="azkims-prod", streams=1, fingerprint="old")
    events = []
    assert _publish(cfg, events) is True
    names = [e[0] for e in events]
    assert names.index("alias") < names.index("deploy-prod") < names.index("promote")
    assert ("promote", "https://prod.vercel.app") in events
    assert names.index("promote") < events.index(("delete", "azkims-main"))
    assert ("delete", "azkims-prod") in events
    state = publish.load_state(cfg)
    assert state["production"] == state["name"] == "azkims-20261101-0840"


def test_failed_production_keeps_its_database(cfg):
    _write_state(cfg, name="azkims-main", production="azkims-main", streams=1, fingerprint="old")
    events = []
    with pytest.raises(PublishError):
        _publish(cfg, events, FakeVercel(events, broken_production=True))
    assert not any(e[0] == "promote" for e in events)
    assert not any(e[0] == "delete" for e in events)  # 本番が使っている DB は消さない
    state = publish.load_state(cfg)
    assert state["name"] == "azkims-20261101-0840" and state["production"] == "azkims-main"


def test_smoke_check_retries_until_the_deployment_answers():
    from azkims.publish import SMOKE_CHECKS, smoke_deployment

    class Flaky:
        calls = 0

        def get_json(self, deployment, path, params=None):
            if path == "/api/stats":
                return {"streams": 1}
            self.calls += 1
            if self.calls == 1:
                return {"error": "not ready"}  # 切り替え直後の 1 回目（2026-11-10）
            return {"counts": {"match": 1}, "hits": [1]}

    v = Flaky()
    smoke_deployment(v, "https://x", 1, lambda m: None, sleep=lambda s: None)
    assert v.calls == len(SMOKE_CHECKS) + 1


def _upload_streams(monkeypatch, ids):
    def export(cfg, log):
        path = cfg.data_dir / "turso_upload.sqlite"
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS streams (video_id TEXT)")
            c.execute("DELETE FROM streams")
            c.executemany("INSERT INTO streams VALUES (?)", [(i,) for i in ids])
        return path

    monkeypatch.setattr(publish, "export_for_turso", export)


def test_streams_removed_on_purpose_do_not_stop_the_publish(cfg, monkeypatch):
    # NT-005（ISS-04）: 手元で外した配信が抜けただけなら通し、理由の無い減少は止める
    _write_state(cfg, name="azkims-main", streams=2, stream_ids=["v1", "v2"], fingerprint="old")
    with sqlite3.connect(cfg.db_path) as c:
        c.execute(
            "INSERT INTO streams (video_id, title, upload_date, caption_status, excluded_reason) "
            "VALUES ('v2', 't', '2026-01-02', 'ok', 'manual')"
        )
    _upload_streams(monkeypatch, ["v1"])
    assert _publish(cfg, []) is True
    assert publish.load_state(cfg)["stream_ids"] == ["v1"]

    _write_state(cfg, name="azkims-main", streams=2, stream_ids=["v1", "v3"], fingerprint="old")
    events = []
    with pytest.raises(PublishError, match="v3"):
        _publish(cfg, events)
    assert events == []


def test_fingerprint_follows_the_oshi_marks(cfg, tmp_path):
    # NT-006（ISS-04）: 推しマークと設定も中身の値に入る
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    marks = root / "config" / "oshi_marks.toml"
    marks.write_text("a", encoding="utf-8")
    before = [p.read_bytes() for p in publish._input_files(cfg, root)]
    assert marks in publish._input_files(cfg, root)
    marks.write_text("b", encoding="utf-8")
    assert [p.read_bytes() for p in publish._input_files(cfg, root)] != before


def test_stale_inputs_finds_changes_after_the_build(cfg, tmp_path):
    # ISS-04: publish は build しないので、build の後に直した辞書や画面があれば知らせる
    import os
    import time

    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "web" / "src").mkdir(parents=True)
    (root / "web" / "dist").mkdir(parents=True)
    old = time.time() - 100
    for p in [root / "web" / "src" / "App.vue", root / "web" / "dist" / "index.html"]:
        p.write_text("x", encoding="utf-8")
    os.utime(root / "web" / "src" / "App.vue", (old, old))
    os.utime(cfg.dictionary_dir / "a.toml", (old, old))
    conn = db.connect(cfg.db_path)
    db.set_meta(conn, "built_at", datetime.fromtimestamp(old + 50, UTC).isoformat())
    conn.commit()
    assert real_stale_inputs(cfg, root)[0].startswith("配信の除外")  # sync の後に build していない
    db.set_meta(conn, "included_only", "1")
    conn.commit()
    conn.close()
    assert real_stale_inputs(cfg, root) == []

    (cfg.dictionary_dir / "a.toml").write_text("# new", encoding="utf-8")
    (root / "web" / "src" / "App.vue").write_text("y", encoding="utf-8")
    stale = real_stale_inputs(cfg, root)
    assert stale[0] == "a.toml" and stale[1].startswith("web/src")
