"""横展開の候補: 同じ配信の中で AI が直した誤り（wrong と fix の違う部分）が、ほかの発言に残っているもの。
日常語（多くの配信に出る語）や短い切れ端は除く。結果は cands.jsonl（配信ごと）。"""
import collections, json, sqlite3, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
from azkims.llmfix import load_fixes
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "prop"
DF = int(sys.argv[1]) if len(sys.argv) > 1 else 20
fixes, _ = load_fixes(ROOT / "data/llm")
def core(w, f):
    a = 0
    while a < min(len(w), len(f)) and w[a] == f[a]: a += 1
    b = 0
    while b < min(len(w), len(f)) - a and w[-1 - b] == f[-1 - b]: b += 1
    return w[a:len(w) - b], f[a:len(f) - b]
con = sqlite3.connect(ROOT / "data/azkims.sqlite")
rows = collections.defaultdict(list)
for v, ms, t, ai in con.execute("SELECT video_id, start_ms, text, text_ai FROM utterances ORDER BY video_id, start_ms"):
    rows[v].append((ms, ai or t))
pairs = collections.defaultdict(lambda: collections.defaultdict(list))  # v -> core pair -> examples
for (v, ms), lst in fixes.items():
    for w, f in lst:
        cw, cf = core(w, f)
        if len(cw.strip()) >= 2 and not cw.isdigit(): pairs[v][(cw, cf)].append((ms, w, f))
# 語の出る配信の数（日常語の判定）。候補の語だけ数える
words = {cw for ps in pairs.values() for cw, _ in ps}
df = collections.Counter()
for v, rs in rows.items():
    blob = "\n".join(t for _, t in rs)
    for w in words:
        if w in blob: df[w] += 1
out = open(P / "cands.jsonl", "w", encoding="utf-8"); st = collections.Counter()
for v, ps in pairs.items():
    items = []
    for (cw, cf), ex in ps.items():
        if df[cw] > DF: st["日常語"] += 1; continue
        if len(cw) < 3 and len(ex) < 2: st["短く 1 回だけ"] += 1; continue
        fixed = {ms for ms, _, _ in ex}
        hits = [(i, ms) for i, (ms, t) in enumerate(rows[v]) if cw in t and ms not in fixed]
        if not hits: continue
        if len(hits) > 30: st["残りが多すぎる"] += 1; continue
        items.append({"wrong": cw, "fix": cf, "n": len(ex), "examples": [{"wrong": w, "fix": f} for _, w, f in ex[:3]], "hits": [ms for _, ms in hits]})
        st["pairs"] += 1; st["hits"] += len(hits)
    if items: out.write(json.dumps({"video_id": v, "items": items}, ensure_ascii=False) + "\n"); st["streams"] += 1
print(dict(st))
