"""霧島リンカさんの正誤（2026-11-07）で、検証の判定を採点する。o=修正が正しい x=元が正しい b=両方誤り"""
import json, sys, collections
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "verify_pilot"
U = "o o o o b b o o o x o o b o b b o o o o o o b o o o b b x o o o b b o o o b o o o o o".split()
ids = json.load(open(P / "review_ids.json", encoding="utf-8"))
assert len(ids) == len(U) == 43
h = {json.loads(l)["id"]: json.loads(l)["verdict"] for l in open(P / "haiku.jsonl", encoding="utf-8")}
s = {json.loads(l)["id"]: json.loads(l)["verdict"] for l in open(P / "sonnet.jsonl", encoding="utf-8")}
json.dump(dict(zip(ids, U)), open(P / "user_labels.json", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
for name, d in (("haiku", h), ("sonnet", s)):
    c = collections.Counter((u, d[k]) for k, u in zip(ids, U))
    print(name, sorted(c.items()))
print("flag only (1-37):", collections.Counter(U[:37]), " control:", collections.Counter(U[37:]))
