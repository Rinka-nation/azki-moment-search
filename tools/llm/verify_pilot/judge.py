"""検証パスの試験: items.jsonl の修正案を、契約分のモデル（-p、ツールなし）に 20 件ずつ判定させる。
使い方: judge.py haiku|sonnet [v2]  → {model}{_v2}.jsonl（id, verdict, reason）と {model}{_v2}_log.jsonl
v2: 2026-11-07 の正誤を受けて「戻す」を、元の文が通じていて修正で意味が変わるものに絞った指示"""
import json, subprocess, sys, time, os, shutil, tempfile
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "verify_pilot"
CLAUDE = os.environ.get("CLAUDE_EXE") or shutil.which("claude") or "claude"  # Claude Code の実行ファイル
SYSTEM = """あなたは YouTube 自動字幕の修正案を検証する係です。配信者は AZKi（ホロライブ 0期生のVTuber、ファンは「開拓者」）。
自動字幕は音の似た別の語に誤認識しやすい（例: AZKi→小豆、開拓者→開脚者）。別の係が出した修正案を 1 件ずつ判定してください。

判定（verdict）:
- keep: 元の文は誤認識で意味が通らない（または明らかに不自然）、かつ修正後は音が近く、文脈に合う正しい文になる。
- revert: 次のどれかに当たる。元の文がすでに日本語として意味が通る（修正で意味が変わる・逆になるものを含む）／修正後の語が元と音が大きく違い推測にすぎない／修正後も文脈に合わない／句読点や表記ゆれだけの変更。
- unsure: どちらとも決められない。
迷う修正は通さない。誤った修正は、直さないより悪い。ただし意味の通らない文を、音が近く文意の通る文に直すものは keep。

入力: 1 件ごとに id、配信タイトル、前後の発言（>> が対象の発言）、wrong（元の部分）、fix（修正後）、修正者の理由。
出力: 1 件につき 1 行の JSON だけ（JSON Lines）。前置き・説明・コードブロックは書かない。
{"id": "...", "verdict": "keep|revert|unsure", "reason": "短い理由"}"""
model = sys.argv[1]
ver = sys.argv[2] if len(sys.argv) > 2 else ""
if ver == "v2":
    SYSTEM = """あなたは YouTube 自動字幕の修正案を検証する係です。配信者は AZKi（ホロライブ 0期生のVTuber、ファンは「開拓者」）。
自動字幕は音の似た別の語に誤認識しやすい（例: AZKi→小豆、開拓者→開脚者）。別の係が出した修正案を 1 件ずつ判定してください。
修正案の大半は正しい。あなたの役目は、少数の「直してはいけない所を直した」修正を見つけることです。

revert にするのは次の場合だけ:
- 元の部分（wrong）が、その文脈でそのまま日本語として自然に意味が通り、修正でその意味が変わる・逆になる（例: 季節の話で「冷房使う」→「暖房使う」。元のままでも通じる）。
- 修正後の文が、前後の発言と矛盾するか、明らかに意味を成さない。
それ以外は keep:
- 元の部分が意味の通らない・不自然な文で、修正後のほうが通じる（修正後の語が正しいか確かめられなくても、元より文意が通るなら keep）。
- 誤った漢字を正しい漢字に直すもの（例: 栄花→栄華）、語の言い換えでなく誤認識を直すもの。
- 知らない固有名詞・あだ名・ゲーム用語は、知らないことを理由に revert しない。
unsure は、元の部分が自然に意味の通る文で、修正後も通じ、どちらが正しいか決められないときだけ。

入力: 1 件ごとに id、配信タイトル、前後の発言（>> が対象の発言）、wrong（元の部分）、fix（修正後）、修正者の理由。
前後の発言は字幕の原文で、別の箇所にも同じ誤りが残っていることがある。前後の文言を正しさの根拠にしない。
出力: 1 件につき 1 行の JSON だけ（JSON Lines）。前置き・説明・コードブロックは書かない。
{"id": "...", "verdict": "keep|revert|unsure", "reason": "短い理由"}"""
tag = model + (f"_{ver}" if ver else "")
items = [json.loads(l) for l in open(P / "items.jsonl", encoding="utf-8")]
outp = P / f"{tag}.jsonl"
done = {json.loads(l)["id"] for l in open(outp, encoding="utf-8")} if outp.exists() else set()
todo = [x for x in items if x["id"] not in done]
log = open(P / f"{tag}_log.jsonl", "a", encoding="utf-8")
fo = open(outp, "a", encoding="utf-8")
def render(x):
    ctx = "\n".join((">> " if j == x["i"] else "   ") + t for j, t in x["ctx"])
    return f"### id: {x['id']}\n配信タイトル: {x['title']}\n{ctx}\nwrong: {x['wrong']}\nfix: {x['fix']}\n修正者の理由: {x['reason']}\n"
for b in range(0, len(todo), 20):
    batch = todo[b:b + 20]
    text = "\n".join(render(x) for x in batch)
    t0 = time.time()
    p = subprocess.run([CLAUDE, "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                        "--no-session-persistence", "--disable-slash-commands", "--output-format", "json", "--system-prompt", SYSTEM],
                       input=text, capture_output=True, text=True, encoding="utf-8", timeout=900, cwd=tempfile.gettempdir())
    try: r = json.loads(p.stdout)
    except Exception: print("bad stdout", p.stdout[:300], p.stderr[:300]); sys.exit(1)
    if r.get("is_error"): print("error", r.get("result", "")[:300]); sys.exit(1)
    ids = {x["id"] for x in batch}; got = 0
    for line in r["result"].splitlines():
        line = line.strip().strip("`")
        if not line.startswith("{"): continue
        try: row = json.loads(line)
        except Exception: continue
        if row.get("id") in ids:
            fo.write(json.dumps(row, ensure_ascii=False) + "\n"); got += 1; ids.discard(row["id"])
    fo.flush()
    u = r.get("usage", {})
    rec = {"batch": b, "n": len(batch), "got": got, "sec": round(time.time() - t0), "cost_usd": r.get("total_cost_usd"),
           "usage": {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}, "models": list(r.get("modelUsage", {}))}
    log.write(json.dumps(rec, ensure_ascii=False) + "\n"); log.flush()
    print(rec, flush=True)
