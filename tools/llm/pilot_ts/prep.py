"""試験: クロノ・トリガーの配信を、タイムスタンプから抜いた言葉（配信者が付けた名前を含む）を足して Haiku に直させる入力を作る。
入力の形は data/llm/in と同じ（用語集 + よく出る言葉 + 発言）に「この配信シリーズのタイムスタンプに出てくる言葉」を足したもの。
タイムスタンプは霧島リンカさんのもの（2025-03-17 以降）と、それより前の他の人のもの（手がかりとしてだけ使ってよい。2026-11-07）。"""
import collections, json, math, re, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
from azkims.dictionary import load_dictionary, game_key
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "pilot_ts"
(P / "in").mkdir(exist_ok=True); (P / "out").mkdir(exist_ok=True)
pilot = Path(sys.argv[1]).read_text(encoding="utf-8")
extra = pilot[pilot.index("## 配信でよく出る言葉"):pilot.index("## 発言")]
d = load_dictionary(ROOT / "config/dictionary")
con = sqlite3.connect(ROOT / "data/azkims.sqlite"); con.row_factory = sqlite3.Row
base = [t for t in d.terms if not t.game]
streams = con.execute("SELECT video_id, title, upload_date FROM streams WHERE title LIKE '%クロノ・トリガー%' ORDER BY upload_date").fetchall()
# 言葉: カタカナ語、英字で始まる語（AZ魔王）、「」の外の漢字かな混じりは拾わない（多すぎて手がかりにならない）
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*[ァ-ヺー一-龯]+|[ァ-ヺー・]{2,}[ぁ-ゖ一-龯]{0,0}")
known = {t.canonical for t in d.terms}
cnt = collections.Counter()
for s in streams:
    for (t,) in con.execute("SELECT text FROM ts_entries WHERE video_id = ?", (s["video_id"],)):
        for w in WORD.findall(t):
            w = w.strip("・")
            if len(w) >= 2: cnt[w] += 1
vocab = [w for w, c in cnt.most_common() if c >= 2 and w not in known][:200]
print(len(vocab), "words:", " ".join(vocab[:80]))
vtext = "\n".join(vocab)
man = []
for s in streams:
    rows = con.execute("SELECT start_ms, text FROM utterances WHERE video_id=? AND text_kept IS NULL ORDER BY start_ms", (s["video_id"],)).fetchall()
    ctx = d.title_context(s["title"])
    gl = "\n".join(f"{t.canonical}（{t.reading}）" for t in base + [t for t in d.terms if t.game and game_key(t.game) in ctx])
    k = math.ceil(len(rows) / 250); size = math.ceil(len(rows) / k)
    for i in range(k):
        part = rows[i * size:(i + 1) * size]
        name = f"{s['upload_date']}_{s['video_id']}_{i:02d}"
        lines = "\n".join(f"{n+1}\t{r['start_ms']//60000}:{r['start_ms']//1000%60:02d}\t{r['text']}" for n, r in enumerate(part))
        body = (f"配信タイトル: {s['title']}\n\n## 用語集（この配信者・周辺の固有名詞。表記（読み））\n{gl}\n\n"
                f"## この配信シリーズのタイムスタンプに出てくる言葉（視聴者が書いた正しい表記。配信者が付けた名前・あだ名を含む）\n"
                f"字幕にこれらの言葉があれば、それは正しいので別の語に直さない。音の近い誤認識は、文脈に合えばこれらに直してよい。\n{vtext}\n\n{extra}"
                f"## 発言（番号\t時刻\t本文）\n{lines}\n")
        (P / "in" / f"{name}.txt").write_text(body, encoding="utf-8")
        man.append({"name": name, "video_id": s["video_id"], "upload_date": s["upload_date"], "start_ms": [r["start_ms"] for r in part], "chars": len(body)})
(P / "manifest.jsonl").write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in man) + "\n", encoding="utf-8")
print(len(streams), "streams", len(man), "chunks", "avg chars", sum(m["chars"] for m in man) // len(man))
