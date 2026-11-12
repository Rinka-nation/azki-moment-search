"""Turso（libSQL）を HTTP で読む、sqlite3 の接続と同じ形の薄いラッパー。

検索は Vercel の関数で動くので、DB は /tmp に展開せず Turso に置く（/tmp は 512MB で全配信が載らない）。
search.py / server.py が使う execute / fetchone / fetchall / 列名アクセスだけを持つ。標準ライブラリのみ。
"""

from __future__ import annotations

import http.client
import json
import time
from urllib.parse import urlsplit


class TursoError(Exception):
    """Turso が応答しない、または SQL を実行できない（無料枠の上限で止められた場合を含む）。"""


class Row(tuple):
    """sqlite3.Row と同じく、位置でも列名でも読める行。"""

    def __new__(cls, columns: dict[str, int], values: list):
        row = super().__new__(cls, values)
        row._columns = columns
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return super().__getitem__(self._columns[key])
        return super().__getitem__(key)


class Cursor:
    def __init__(self, rows: list[Row]):
        self._rows = rows

    def fetchone(self) -> Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Row]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


def encode_arg(value) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool | int):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def decode_cell(cell: dict):
    kind = cell["type"]
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    return cell["value"]


def decode_result(result: dict) -> list[Row]:
    columns = {c["name"]: i for i, c in enumerate(result["cols"])}
    return [Row(columns, [decode_cell(c) for c in r]) for r in result["rows"]]


# 1 回の API 呼び出し（接続 1 つ）で Turso を待てる合計の秒数。関数の上限（vercel.json の maxDuration 15 秒）より
# 短くし、超えそうなら Vercel に打ち切られる前に TursoError（画面には 503 の定型文）にする。
# 検索 1 回は SQL を 4〜10 本送るので、1 本ごとの待ち時間ではなく合計で区切る（ISS-08、2026-11-11）
REQUEST_BUDGET_SECONDS = 12.0


class TursoConnection:
    def __init__(
        self, url: str, token: str, budget: float = REQUEST_BUDGET_SECONDS, clock=time.monotonic
    ):
        self._host = urlsplit(url.replace("libsql://", "https://")).netloc
        self._token = token
        self._clock = clock
        self._deadline = clock() + budget
        self._http: http.client.HTTPSConnection | None = None

    def _remaining(self) -> float:
        left = self._deadline - self._clock()
        if left <= 0.5:
            raise TursoError("Turso の応答が遅く、時間内に検索を終えられません")
        return left

    def _post(self, body: bytes) -> dict:
        # 接続が途中で切れることがあるので、通信の失敗は 1 回だけやり直す（残り時間があるときだけ）
        for attempt in (0, 1):
            left = self._remaining()
            if self._http is None:
                self._http = http.client.HTTPSConnection(self._host, timeout=left)
            elif self._http.sock is not None:
                self._http.sock.settimeout(left)
            try:
                self._http.request(
                    "POST",
                    "/v2/pipeline",
                    body,
                    {
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                )
                response = self._http.getresponse()
                data = response.read()
            except (OSError, http.client.HTTPException) as e:
                self.close()
                if attempt:
                    raise TursoError(f"Turso に接続できません: {type(e).__name__}") from e
                continue
            if response.status != 200:
                raise TursoError(f"Turso の応答が HTTP {response.status} でした: {data[:200]!r}")
            return json.loads(data)
        raise AssertionError("unreachable")

    def execute(self, sql: str, params=()) -> Cursor:
        stmt = {"sql": sql, "args": [encode_arg(p) for p in params]}
        requests = [{"type": "execute", "stmt": stmt}, {"type": "close"}]
        reply = self._post(json.dumps({"requests": requests}).encode())
        first = reply["results"][0]
        if first["type"] != "ok":
            raise TursoError(f"SQL を実行できません: {str(first.get('error'))[:200]}")
        return Cursor(decode_result(first["response"]["result"]))

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
