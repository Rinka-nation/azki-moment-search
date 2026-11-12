"""Vercel Python Function: /api/* をすべて受ける検索 API。

azkims bundle-vercel が deploy/ に組み立てたときの配置:
    deploy/api/index.py              ← このファイル
    deploy/azkims/                   ← 検索コード（src/azkims のコピー）
    deploy/dictionary/               ← 辞書（config/dictionary のコピー）
    deploy/data/azkims.sqlite.gz.*   ← 公開用 DB（圧縮・分割。起動時に /tmp に展開）
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from azkims.config import Config
from azkims.deploy import unpack_db
from azkims.server import PUBLIC_CACHE, SearchService, make_handler

# 関数のファイルシステムは読み取り専用なので、DB は書き込める /tmp に展開する。
# 同じインスタンスが使い回される間（ウォーム）は展開済みのものを使う。
# TURSO_URL / TURSO_TOKEN があれば DB は展開せず Turso を読む。
TMP_DATA = Path("/tmp/azkims")
_url, _token = os.environ.get("TURSO_URL"), os.environ.get("TURSO_TOKEN")
_remote = (_url, _token) if _url and _token else None
if _remote is None:
    _t = time.monotonic()
    unpack_db(ROOT / "data", TMP_DATA / "azkims.sqlite")
    print(f"azkims: DB ready in {time.monotonic() - _t:.2f}s", flush=True)

_cfg = Config(
    channel_url="",
    data_dir=TMP_DATA,
    output_dir=TMP_DATA,
    dictionary_dir=ROOT / "dictionary",
)

# Vercel の Python ランタイムは BaseHTTPRequestHandler の子クラス `handler` を呼ぶ。
# 画面（静的ファイル）は Vercel が配信するので、ここでは /api/* だけを扱う。
_Handler = make_handler(
    SearchService(_cfg, immutable=True, remote=_remote),
    dist_dir=ROOT / "__no_static__",
    cache=PUBLIC_CACHE,
)


# Vercel はファイル上の `class handler` を探すので、代入ではなく class で書く
class handler(_Handler):
    pass
