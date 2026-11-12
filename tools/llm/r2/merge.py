"""1 周目（data/llm/out）と 2 周目（r2/out）の修正案を合わせ、r2/merged に書く（2026-11-07 霧島リンカさん承認の方針）。
- 2 周目の修正はすべて残す。
- 1 周目の修正は、2 周目の入力（今の本文）で同じ発言の同じ所（wrong が重なる）を 2 周目が直していれば捨てる（2 周目を採る）。
- 1 周目の修正のうち、タイムスタンプの言葉・辞書の語を含む wrong を、その語の無い fix に変えたもの（アズバード→シルバード の型）は捨てる。
- 今の本文に wrong が無い 1 周目の修正は捨てる（build でも当たらない）。
番号 n は 2 周目の入力の番号に直して書く（塊の切り方は同じ）。"""
import collections, json, re, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
from azkims.dictionary import load_dictionary
from azkims.llmfix import _LINE, _locate
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "r2"; L = P.parent
M = P / "merged"; M.mkdir(exist_ok=True)
canon = {t.canonical for t in load_dictionary(ROOT / "config/dictionary").terms if len(t.canonical) >= 2}
st = collections.Counter(); samples = collections.defaultdict(list)
def rows(p):
    out = []
    if p.exists():
        for l in p.read_text(encoding="utf-8").splitlines():
            try: out.append(json.loads(l))
            except Exception: pass
    return out
def _kind(c):
    return "k" if "ァ" <= c <= "ヺ" or c == "ー" else "l" if c.isascii() and c.isalnum() else "o"
def whole(x, w):
    """x が w の中で、同じ文字種の語の一部でなく 1 語として出ているか（モン in くまモン は除く）"""
    for m in re.finditer(re.escape(x), w):
        a, b = m.start(), m.end()
        if (a == 0 or _kind(w[a - 1]) != _kind(x[0]) or _kind(x[0]) == "o") and (b == len(w) or _kind(w[b]) != _kind(x[-1]) or _kind(x[-1]) == "o"):
            return True
    return False
def spans(text, w):
    return [(m.start(), m.start() + len(w)) for m in re.finditer(re.escape(w), text)]
for m in (json.loads(l) for l in open(P / "manifest.jsonl", encoding="utf-8")):
    name = m["name"]
    if not (P / "out" / f"{name}.jsonl").exists(): st["r2 未完"] += 1; continue
    text = (P / "in" / f"{name}.txt").read_text(encoding="utf-8")
    utts = {int(x[1]): x[2] for x in _LINE.finditer(text)}
    vs = text.split("に出てくる言葉（", 1)
    vocab = set(vs[1].split("\n\n", 1)[0].splitlines()[2:]) if len(vs) > 1 else set()
    words = {w for w in vocab | canon if len(w) >= 2}
    r2 = rows(P / "out" / f"{name}.jsonl"); r1 = rows(L / "out" / f"{name}.jsonl")
    taken = collections.defaultdict(list)  # 発言番号 → 2 周目が直した範囲
    out = []
    for r in r2:
        w = (r.get("wrong") or "").replace("\n", "")
        if not isinstance(r.get("n"), int) or not w: continue
        i, _ = _locate(utts, r["n"], r.get("n_end") or r["n"], w)
        if i is not None: taken[i] += spans(utts[i], w)
        out.append(r); st["r2"] += 1
    seen = {(r.get("wrong"), r.get("fix")) for r in r2}
    for r in r1:
        w = (r.get("wrong") or "").replace("\n", ""); f = (r.get("fix") or "").replace("\n", "")
        if not isinstance(r.get("n"), int) or not w or not f or w == f: st["r1 形が不正"] += 1; continue
        if (w, f) in seen: st["r1 2周目と同じ"] += 1; continue
        broke = [x for x in words if x not in f and whole(x, w)]
        if broke: st["r1 言葉を壊す"] += 1; samples["break"].append((w, f, broke[0])); continue
        i, _ = _locate(utts, r["n"], r.get("n_end") or r["n"], w)
        if i is None: st["r1 今の本文に無い"] += 1; continue
        sp = spans(utts[i], w)
        if any(a < d and c < b for a, b in sp for c, d in taken[i]):
            st["r1 2周目と食い違い"] += 1; samples["conflict"].append((w, f)); continue
        out.append({**r, "n": i, "n_end": i, "from": "r1"}); taken[i] += sp; st["r1 採用"] += 1
    (M / f"{name}.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out), encoding="utf-8")
print(dict(st))
for k, xs in samples.items():
    print(k, len(xs))
    for x in xs[:15]: print("  ", x)
