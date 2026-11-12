"""試験の比較: クロノ 7 配信の、前回の修正案（data/llm/out）と今回（pilot_ts/out。タイムスタンプの言葉つき）。"""
import json, sys, collections, random
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "pilot_ts"; L = P.parent
names = [json.loads(l)["name"] for l in open(P / "manifest.jsonl", encoding="utf-8")]
def rows(d, n):
    p = d / f"{n}.jsonl"
    if not p.exists(): return []
    out = []
    for l in p.read_text(encoding="utf-8").splitlines():
        try: out.append(json.loads(l))
        except Exception: pass
    return out
old = {n: rows(L / "out", n) for n in names}; new = {n: rows(P / "out", n) for n in names if (P / "out" / f"{n}.jsonl").exists()}
print("chunks", len(names), "done", len(new))
o = [(n, r) for n in new for r in old[n]]; w = [(n, r) for n in new for r in new[n]]
print("fixes old", len(o), "new", len(w))
RENAMED = ("アズノ", "AZ魔王", "アズバード", "アズたち")
ORIG = ("クロノ", "魔王", "シルバード")
def bad(r): return any(k in r.get("wrong", "") for k in ("アズ", "あず魔", "アズマ", "アスマ")) and any(k in r.get("fix", "") for k in ORIG) and not any(k in r.get("fix", "") for k in RENAMED)
def good(r): return any(k in r.get("fix", "") for k in RENAMED) and not any(k in r.get("wrong", "") for k in RENAMED)
for tag, xs in (("old", o), ("new", w)):
    b = [r for _, r in xs if bad(r)]; g = [r for _, r in xs if good(r)]
    print(tag, "壊した", len(b), "直した", len(g))
    for r in b[:8]: print("   壊", r["wrong"][:25], "→", r["fix"][:25])
    for r in g[:12]: print("   直", r["wrong"][:25], "→", r["fix"][:25])
ok = {(n, r.get("wrong"), r.get("fix")) for n, r in o}; nk = {(n, r.get("wrong"), r.get("fix")) for n, r in w}
print("same", len(ok & nk), "old only", len(ok - nk), "new only", len(nk - ok))
