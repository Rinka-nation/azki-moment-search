"""Assemble a Vercel deployment directory (deploy/).

    deploy/
      index.html, assets/...      web/dist（画面）
      api/index.py                vercel/api/index.py（検索 API の入口）
      azkims/                     src/azkims（検索コード）
      dictionary/                 config/dictionary（辞書）
      data/azkims.sqlite.gz.000…  公開用の DB（検索に使わない表を除き VACUUM → gzip → 90MB ごとに分割）
      vercel.json, requirements.txt, .vercelignore

字幕・コメントの原本（data/captions, data/comments）は含めない。

Vercel は 1 ファイル 100MB までしかアップロードできないので、DB は圧縮・分割して載せ、
関数の起動時に unpack_db で /tmp に戻す。
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from azkims.config import PROJECT_ROOT, Config

DEPLOY_DIR = PROJECT_ROOT / "deploy"
TEMPLATE_DIR = PROJECT_ROOT / "vercel"
WEB_DIST = PROJECT_ROOT / "web" / "dist"

DB_PART_PREFIX = "azkims.sqlite.gz."
PART_SIZE = 90 * 1024 * 1024  # Vercel の 1 ファイル上限 100MB に余裕を持たせる
# 関数の /tmp（512MB）には実行時に入る依存（Sudachi 辞書など）と展開した DB が同居する。
# 347MB の DB は No space left on device で起動できなかった（2026-10-31）
MAX_DB_MB = 335

# 検索・表示に使わない表（レビュー用・取得管理用）
_DROP_TABLES = ("corrections", "comment_fetch")
# streams のうち公開側で使う列だけ残す（取得エラーの内容などは出さない）
_STREAM_COLUMNS = (
    "video_id, title, list_order, upload_date, duration_sec, excluded_reason, caption_status"
)


def export_slim_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    # sqlite3 の with は接続を閉じないので closing で閉じる（Windows で一時ファイルを消せなくなる）
    with (
        closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as s,
        closing(sqlite3.connect(dst)) as d,
    ):
        s.backup(d)
    conn = sqlite3.connect(dst)
    try:
        for t in _DROP_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        # 検索対象外の配信と、その列の不要な情報を落とす
        conn.executescript(
            f"""
            CREATE TABLE streams_slim AS
                SELECT {_STREAM_COLUMNS} FROM streams
                WHERE excluded_reason IS NULL AND caption_status = 'ok';
            DROP TABLE streams;
            ALTER TABLE streams_slim RENAME TO streams;
            CREATE UNIQUE INDEX idx_streams_video ON streams(video_id);
            """
        )
        # 大半の発言は原文と補正後が同じ。読むときは search.RAW_SQL で text に読み替える
        conn.execute("UPDATE utterances SET text_raw = '' WHERE text_raw = text")
        # 画面を開くたびに全行を数えないよう、件数を保存しておく
        for key, table in (("utterance_count", "utterances"), ("timestamp_count", "ts_entries")):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(n)))
        conn.execute("INSERT INTO utterances_fts(utterances_fts) VALUES ('optimize')")
        conn.execute("INSERT INTO ts_fts(ts_fts) VALUES ('optimize')")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def pack_db(src: Path, out_dir: Path, part_size: int = PART_SIZE) -> list[Path]:
    """DB を gzip して part_size ごとに分割する。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(DB_PART_PREFIX + "*"):
        old.unlink()
    with tempfile.TemporaryDirectory() as tmp:
        gz = Path(tmp) / "db.gz"
        with open(src, "rb") as f, gzip.open(gz, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1024 * 1024)
        parts = []
        with open(gz, "rb") as f:
            while chunk := f.read(part_size):
                part = out_dir / f"{DB_PART_PREFIX}{len(parts):03d}"
                part.write_bytes(chunk)
                parts.append(part)
    return parts


class _Concat:
    """分割ファイルを 1 つのストリームとして読む（gzip に渡す用）。"""

    def __init__(self, paths: list[Path]):
        self._paths = list(paths)
        self._f = None

    def read(self, n: int = -1) -> bytes:
        while True:
            if self._f is None:
                if not self._paths:
                    return b""
                self._f = open(self._paths.pop(0), "rb")  # noqa: SIM115  read() の呼び出しをまたいで開いておく
            data = self._f.read(n)
            if data:
                return data
            self._f.close()
            self._f = None


def unpack_db(parts_dir: Path, dst: Path) -> Path:
    """pack_db の逆。dst が既にあれば何もしない（関数のウォーム起動時）。"""
    if dst.exists():
        return dst
    parts = sorted(parts_dir.glob(DB_PART_PREFIX + "*"))
    if not parts:
        raise FileNotFoundError(f"DB parts not found in {parts_dir}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".partial")
    with gzip.GzipFile(fileobj=_Concat(parts)) as g, open(tmp, "wb") as out:
        shutil.copyfileobj(g, out, 1024 * 1024)
    tmp.replace(dst)
    return dst


def _copy_tree(src: Path, dst: Path, ignore=None) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)


def export_for_turso(cfg: Config, log) -> Path:
    """Turso の画面からアップロードする公開用 DB を作る（WAL が必要）。"""
    dst = cfg.data_dir / "turso_upload.sqlite"
    export_slim_db(cfg.db_path, dst)
    with closing(sqlite3.connect(dst)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log(f"Turso 用の DB を作成しました: {dst}（{dst.stat().st_size / 1e6:.0f} MB）")
    return dst


def bundle_vercel(cfg: Config, log, out: Path = DEPLOY_DIR, remote_db: bool = False) -> Path:
    if not (WEB_DIST / "index.html").exists():
        raise SystemExit("web/dist がありません。web で `npm run build` を実行してください")
    if not cfg.db_path.exists():
        raise SystemExit("DB がありません。`azkims build` を実行してください")

    out.mkdir(parents=True, exist_ok=True)
    # 前回の画面ファイルを消してから入れ直す（data/ 等は個別に作り直す）
    for p in out.iterdir():
        if p.name not in (".vercel",):  # vercel link の設定は残す
            shutil.rmtree(p) if p.is_dir() else p.unlink()

    for p in WEB_DIST.iterdir():
        (shutil.copytree if p.is_dir() else shutil.copy2)(p, out / p.name)
    _copy_tree(TEMPLATE_DIR / "api", out / "api", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("vercel.json", "requirements.txt", ".vercelignore"):
        shutil.copy2(TEMPLATE_DIR / name, out / name)
    _copy_tree(
        PROJECT_ROOT / "src" / "azkims",
        out / "azkims",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )
    _copy_tree(cfg.dictionary_dir, out / "dictionary")

    if remote_db:
        log("DB は Turso に置くので deploy/ に含めません（`--turso-export` で作成）")
        return out

    log("公開用 DB を作成中（検索に使わない表を除いて VACUUM → 圧縮・分割）…")
    with tempfile.TemporaryDirectory() as tmp:
        slim = Path(tmp) / "azkims.sqlite"
        export_slim_db(cfg.db_path, slim)
        raw_size = slim.stat().st_size / 1e6
        if raw_size > MAX_DB_MB:
            raise SystemExit(
                f"公開用 DB が {raw_size:.0f} MB で上限 {MAX_DB_MB} MB を超えています。"
                "デプロイしても関数が起動しません（/tmp の容量不足）"
            )
        parts = pack_db(slim, out / "data")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    packed = sum(p.stat().st_size for p in parts) / 1e6
    log(
        f"deploy/ を作成しました（合計 {size:.0f} MB。DB {raw_size:.0f} MB → 圧縮 {packed:.0f} MB、"
        f"{len(parts)} 分割）"
    )
    log(
        "Python の依存（Sudachi 辞書 約 200 MB）は Vercel 側でインストールされる。関数の上限は 500 MB"
    )
    return out
