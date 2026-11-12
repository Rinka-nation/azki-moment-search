"""2 周目（2026-11-07 霧島リンカさん承認）: 全配信の入力を、タイムスタンプから抜いた言葉を足して作り直す。
塊の切り方は 1 周目（data/llm/manifest.jsonl）と同じ。発言の本文・用語集は今の DB と辞書から作る。
言葉: ゲームの配信は同じゲームの配信のタイムスタンプ（2 回以上出る語）＋その配信の語。複数のゲームに当たる配信と
ゲーム以外の配信は、その配信のタイムスタンプの語だけ。タイムスタンプが無ければ節を付けない。
他の人の 2025-03-17 より前のタイムスタンプも手がかりとしてだけ使う（表示はしない）。"""
import collections, json, re, sqlite3, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
from azkims.dictionary import load_dictionary, game_key
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "r2"; L = P.parent
(P / "in").mkdir(exist_ok=True); (P / "out").mkdir(exist_ok=True)
d = load_dictionary(ROOT / "config/dictionary")
con = sqlite3.connect(ROOT / "data/azkims.sqlite"); con.row_factory = sqlite3.Row
base = [t for t in d.terms if not t.game]
known = {t.canonical for t in d.terms}
gkeys = {game_key(g) for g in d.games}
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*[ァ-ヺー一-龯]+|[ァ-ヺー・]{2,}")
main = [json.loads(l) for l in open(L / "manifest.jsonl", encoding="utf-8")]
vids = list(dict.fromkeys(m["video_id"] for m in main))
title = {r["video_id"]: r["title"] for r in con.execute("SELECT video_id, title FROM streams")}
ctx = {v: d.title_context(title[v]) for v in vids}
# ホロ鯖RUST / ARK はマイクラの title_patterns（ホロ鯖）にも当たるので、シリーズにまとめない
notser = {v for v in vids if re.search(r"RUST|ARK", title[v])}
words = {}
for v in vids:
    c = collections.Counter()
    for (t,) in con.execute("SELECT text FROM ts_entries WHERE video_id = ?", (v,)):
        for w in WORD.findall(t):
            w = w.strip("・")
            if len(w) >= 2 and w not in known: c[w] += 1
    words[v] = c
series = collections.defaultdict(collections.Counter)
for v in vids:
    g = ctx[v] & gkeys
    if len(g) == 1 and v not in notser: series[next(iter(g))].update(words[v])
def vocab(v):
    own = [w for w, _ in words[v].most_common()]
    g = ctx[v] & gkeys if v not in notser else set()
    ser = [w for w, c in series[next(iter(g))].most_common() if c >= 2] if len(g) == 1 else []
    return list(dict.fromkeys(own + ser))[:200], len(g) == 1
rows_by = {}
mismatch = 0; man = []; nv = collections.Counter()
for m in main:
    v = m["video_id"]
    if v not in rows_by:
        rows_by[v] = con.execute("SELECT start_ms, text FROM utterances WHERE video_id=? AND text_kept IS NULL ORDER BY start_ms", (v,)).fetchall()
    rows = rows_by[v]
    i = int(m["name"].rsplit("_", 1)[1])
    k = -(-len(rows) // 250); size = -(-len(rows) // k)
    part = rows[i * size:(i + 1) * size]
    if [r["start_ms"] for r in part] != m["start_ms"]:
        mismatch += 1; continue
    old = (L / "in" / f"{m['name']}.txt").read_text(encoding="utf-8")
    extra = old[old.index("## 配信でよく出る言葉"):old.index("## 発言")]
    gl = "\n".join(f"{t.canonical}（{t.reading}）" for t in base + [t for t in d.terms if t.game and game_key(t.game) in ctx[v]])
    vw, isg = vocab(v)
    sec = ""
    if vw:
        head = "この配信シリーズのタイムスタンプに出てくる言葉" if isg else "この配信のタイムスタンプに出てくる言葉"
        sec = (f"## {head}（視聴者が書いた正しい表記。配信者が付けた名前・あだ名を含む）\n"
               f"字幕にこれらの言葉があれば、それは正しいので別の語に直さない。音の近い誤認識は、文脈に合えばこれらに直してよい。\n" + "\n".join(vw) + "\n\n")
    nv["series" if isg and vw else "own" if vw else "none"] += 1
    lines = "\n".join(f"{n+1}\t{r['start_ms']//60000}:{r['start_ms']//1000%60:02d}\t{r['text']}" for n, r in enumerate(part))
    body = (f"配信タイトル: {title[v]}\n\n## 用語集（この配信者・周辺の固有名詞。表記（読み））\n{gl}\n\n{sec}{extra}"
            f"## 発言（番号\t時刻\t本文）\n{lines}\n")
    (P / "in" / f"{m['name']}.txt").write_text(body, encoding="utf-8")
    man.append({"name": m["name"], "video_id": v, "upload_date": m["upload_date"], "start_ms": m["start_ms"], "chars": len(body), "vocab": len(vw), "has_ts": bool(words[v])})
# タイムスタンプのある配信から先に回す
man.sort(key=lambda x: (not x["has_ts"], x["name"]))
(P / "manifest.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in man) + "\n", encoding="utf-8")
print("chunks", len(man), "mismatch", mismatch, dict(nv), "avg chars", sum(x["chars"] for x in man) // len(man),
      "old avg", sum(len((L / "in" / f"{x['name']}.txt").read_text(encoding="utf-8")) for x in man) // len(man))
multi = [title[v] for v in vids if len(ctx[v] & gkeys) > 1]
print("multi-game streams", len(multi), multi[:5])
