"""試験の結果から、人が正誤を付ける一覧（output/verify_pilot_review.md）を作る。"""
import json, random, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "verify_pilot"
items = {json.loads(l)["id"]: json.loads(l) for l in open(P / "items.jsonl", encoding="utf-8")}
h = {json.loads(l)["id"]: json.loads(l) for l in open(P / "haiku.jsonl", encoding="utf-8")}
s = {json.loads(l)["id"]: json.loads(l) for l in open(P / "sonnet.jsonl", encoding="utf-8")}
J = {"keep": "残す", "revert": "戻す", "unsure": "迷う"}
flag = [k for k in items if "revert" in (h[k]["verdict"], s[k]["verdict"])]
random.seed(1)
ctrl = random.sample([k for k in items if h[k]["verdict"] == s[k]["verdict"] == "keep"], 6)
rows = flag + ctrl
out = ["# AI修正の検証パス 試験：正誤の確認", "",
       "各件の「あなたの判定」に ○（修正が正しい）／×（元の字幕のままが正しい・修正が誤り）／？ を付けてください。",
       "番号と記号だけ返してもらえれば十分です（例: 1○ 2× 3？）。", "",
       f"- 1〜{len(flag)}: Haiku か Sonnet のどちらかが「戻す」と判定したもの",
       f"- {len(flag)+1}〜{len(rows)}: 両方「残す」としたものからの無作為 6 件（比較用）", "",
       "「>>」の行が対象の発言です（字幕の原文。発言の区切りは字幕の都合で文の途中にあります）。", ""]
for no, k in enumerate(rows, 1):
    x = items[k]
    vid = k.split("_")[1] if k.count("_") >= 2 else ""
    vid = k[11:22]
    sec = None
    out += [f"## {no}. {x['wrong']} → {x['fix']}", "",
            f"配信: {x['title']}（{vid}）", "", "```"]
    out += [(">> " if j == x["i"] else "   ") + t for j, t in x["ctx"] if abs(j - x["i"]) <= 1]
    out += ["```", "",
            f"- 修正者（Haiku）の理由: {x['reason']}",
            f"- Haiku の検証: **{J[h[k]['verdict']]}** — {h[k]['reason']}",
            f"- Sonnet の検証: **{J[s[k]['verdict']]}** — {s[k]['reason']}",
            "- あなたの判定: ", ""]
(Path(__file__).resolve().parents[3] / "output" / "verify_pilot_review.md").write_text("\n".join(out), encoding="utf-8")
json.dump(rows, open(P / "review_ids.json", "w", encoding="utf-8"))
print(len(flag), len(rows))
