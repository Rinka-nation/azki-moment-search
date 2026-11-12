"""検証パスの試験: 修正案 300 件（無作為）+ 既知の誤り 2 件を、前後の発言付きで items.jsonl に書く。"""
import json, random, re, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
L = Path(__file__).resolve().parents[3] / "data" / "llm"
from azkims.llmfix import _LINE, _locate
TITLE = re.compile(r"^配信タイトル: (.*)$", re.M)
items = []
for line in open(L / "manifest.jsonl", encoding="utf-8"):
    m = json.loads(line)
    out = L / "out" / f"{m['name']}.jsonl"
    if not out.exists(): continue
    text = (L / "in" / f"{m['name']}.txt").read_text(encoding="utf-8")
    utts = {int(x[1]): x[2] for x in _LINE.finditer(text)}
    title = TITLE.search(text)[1]
    for k, row in enumerate(out.read_text(encoding="utf-8").splitlines()):
        try: r = json.loads(row)
        except Exception: continue
        n, ne = r.get("n"), r.get("n_end") or r.get("n")
        w, f = (r.get("wrong") or ""), (r.get("fix") or "")
        if not isinstance(n, int) or not isinstance(ne, int) or not w or not f or w == f: continue
        i, _ = _locate(utts, n, ne, w)
        if i is None: continue
        ctx = [(j, utts[j]) for j in range(i - 3, i + 4) if j in utts]
        items.append({"id": f"{m['name']}#{k}", "title": title, "i": i, "ctx": ctx, "wrong": w, "fix": f,
                      "reason": r.get("reason", ""), "confidence": r.get("confidence", "")})
print("total", len(items))
known = [x for x in items if ("冷房" in x["wrong"] and "暖房" in x["fix"]) or ("小豆生産" in x["wrong"] and "AZKi生誕" in x["fix"])]
print("known", [(x["id"], x["wrong"], x["fix"]) for x in known])
random.seed(20261106)
sample = random.sample([x for x in items if x not in known], 300) + known
random.shuffle(sample)
with open(L / "verify_pilot" / "items.jsonl", "w", encoding="utf-8") as fo:
    for x in sample: fo.write(json.dumps(x, ensure_ascii=False) + "\n")
print("wrote", len(sample))
