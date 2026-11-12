"""横展開（2026-11-09 霧島リンカさん「実行してみましょう」）: cands.jsonl の候補を、配信ごとに Haiku に判定させる。
同じ配信で AI が直した例（wrong → fix）を見せ、同じ誤りが残っている発言を前後の発言つきで渡す。
直すべきものだけ、発言の中の wrong と fix を返させる。出力は out/{video_id}.jsonl（start_ms, wrong, fix, reason）。
使い方: run.py [配信数の上限]。既に出力がある配信は飛ばす。並列にしない。"""
import json, sqlite3, subprocess, sys, time, os, shutil, tempfile
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
P = Path(__file__).resolve().parents[3] / "data" / "llm" / "prop"
(P / "out").mkdir(exist_ok=True)
CLAUDE = os.environ.get("CLAUDE_EXE") or shutil.which("claude") or "claude"  # Claude Code の実行ファイル
SYSTEM = """あなたは YouTube 自動字幕の誤認識を直す係です。配信者は AZKi（ホロライブ 0期生のVTuber、ファンは「開拓者」）。
同じ配信の中で、別の係が誤認識を直した例（例: 川嘘 → カワウ荘）を渡します。同じ誤認識が、ほかの発言にも残っていることがあります。
同じ誤りに直し方が複数あるときは全部を見せるので、候補ごとに文脈に合うほうを選んでください
（例: 川嘘 → カワウ荘（拠点・グループの名前）／カワウソ（動物））。
タイムスタンプの言葉（視聴者が書いた正しい表記）があれば、それも手がかりにしてください。
候補の発言（>> の行）を 1 つずつ見て、例と同じ誤認識なら直してください。

直すのは次の場合だけ:
- 候補の発言でも、その語が例と同じ意味の誤認識で、直すと文意が通る。
直さない:
- 候補の発言では、その語が普通の言葉として正しく使われている（例: 例は 勇気→有機 でも、「勇気を出す」は直さない）。
- 直すと文意が通らなくなる、またはどちらか決められない。
誤った修正は、直さないより悪い。迷ったら直さない。

入力: 配信タイトル、例ごとに「例: wrong → fix」と候補（id、前後の発言。>> が候補の発言）。
出力: 直すものだけ、1 件 1 行の JSON（JSON Lines）。前置き・説明・コードブロックは書かない。直すものが無ければ NONE とだけ出力する。
{"id": "...", "wrong": "候補の発言から正確に写した誤りの部分", "fix": "直した後", "reason": "短い理由"}
wrong は候補の発言（>> の行）の中にそのままある文字列にする。"""
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1
con = sqlite3.connect(f"file:{ROOT / 'data/azkims.sqlite'}?mode=ro", uri=True)
title = dict(con.execute("SELECT video_id, title FROM streams"))
log = open(P / "run_log.jsonl", "a", encoding="utf-8")
# 2 周目の入力にあるタイムスタンプの言葉（配信者が付けた名前など）。配信ごとに最初の塊から
vocab = {}
for m in (json.loads(l) for l in open(ROOT / "data/llm/manifest.jsonl", encoding="utf-8")):
    if m["video_id"] in vocab: continue
    t = (ROOT / "data/llm/in" / f"{m['name']}.txt").read_text(encoding="utf-8")
    k = t.find("に出てくる言葉（")
    vocab[m["video_id"]] = t[t.rindex("## ", 0, k):t.index("\n\n", k) + 2] if k >= 0 else ""
done = 0
for c in (json.loads(l) for l in open(P / "cands.jsonl", encoding="utf-8")):
    v = c["video_id"]
    out = P / "out" / f"{v}.jsonl"
    if out.exists(): continue
    if done >= limit: break
    rows = con.execute("SELECT start_ms, coalesce(text_ai, text) FROM utterances WHERE video_id = ? ORDER BY start_ms", (v,)).fetchall()
    idx = {}  # 同じ start_ms の発言が複数あることがある
    for i, (ms, _) in enumerate(rows): idx.setdefault(ms, []).append(i)
    # 同じ誤りに直し方が複数あるもの（川嘘 → カワウ荘 / カワウソ）は 1 つにまとめ、例を全部見せる
    groups = {}
    for it in c["items"]:
        g = groups.setdefault(it["wrong"], {"fixes": [], "examples": [], "hits": []})
        g["fixes"].append(f"{it['fix']}（{it['n']} 回）")
        g["examples"] += it["examples"]
        g["hits"] += [ms for ms in it["hits"] if ms not in g["hits"]]
    parts, ids = [], {}
    for k, (gw, it) in enumerate(groups.items()):
        ex = "\n".join(f"例: {e['wrong']} → {e['fix']}" for e in it["examples"])
        cs = []
        for ms in it["hits"]:
            for i in idx.get(ms, []):
                if gw not in rows[i][1]: continue
                cid = f"{k}-{len(cs)}"; ids[cid] = (ms, rows[i][1])
                ctx = "\n".join((">> " if n == i else "   ") + rows[n][1] for n in range(max(0, i - 2), min(len(rows), i + 3)))
                cs.append(f"id: {cid}\n{ctx}")
        if cs: parts.append(f"### 例 {k}（{gw} → {' / '.join(it['fixes'])}）\n{ex}\n\n" + "\n\n".join(cs))
    if not parts:
        out.write_text("", encoding="utf-8"); continue
    text = f"配信タイトル: {title.get(v, '')}\n\n{vocab.get(v, '')}" + "\n\n".join(parts) + "\n"
    t0 = time.time()
    p = subprocess.run([CLAUDE, "-p", "--model", "haiku", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                        "--no-session-persistence", "--disable-slash-commands", "--output-format", "json", "--system-prompt", SYSTEM],
                       input=text, capture_output=True, text=True, encoding="utf-8", timeout=900, cwd=tempfile.gettempdir())
    try: r = json.loads(p.stdout)
    except Exception: print("bad stdout", v, p.stdout[:300], p.stderr[:300]); sys.exit(1)
    if r.get("is_error"): print("error", v, r.get("result", "")[:300]); sys.exit(1)
    res, bad = [], 0
    for line in r["result"].splitlines():
        line = line.strip().strip("`")
        if not line.startswith("{"): continue
        try: x = json.loads(line)
        except Exception: bad += 1; continue
        if x.get("id") not in ids: bad += 1; continue
        ms, t = ids[x["id"]]
        w, f = x.get("wrong") or "", x.get("fix") or ""
        if not w or w == f or w not in t: bad += 1; continue
        res.append({"start_ms": ms, "wrong": w, "fix": f, "reason": x.get("reason", ""), "id": x["id"]})
    out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in res), encoding="utf-8")
    u = r.get("usage", {})
    rec = {"video_id": v, "cands": len(ids), "fixed": len(res), "bad": bad, "sec": round(time.time() - t0),
           "usage": {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}, "model": list(r.get("modelUsage", {}))}
    log.write(json.dumps(rec, ensure_ascii=False) + "\n"); log.flush(); print(rec, flush=True)
    done += 1
