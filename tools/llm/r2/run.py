"""2 周目（r2）。data/llm/r2/in の入力を、契約分の Haiku（Claude Code の -p、ツールなし）に 1 つずつ渡し、data/llm/r2/out に書く。
既に出力があるものは飛ばす。週間の使用率は見られないので、件数（--limit）で止める。並列にしない。"""
import json, subprocess, sys, time, os, shutil, tempfile
from pathlib import Path
L = Path(__file__).resolve().parents[3] / "data" / "llm" / "r2"
CLAUDE = os.environ.get("CLAUDE_EXE") or shutil.which("claude") or "claude"  # Claude Code の実行ファイル
instr = (Path(__file__).parent.parent / "INSTRUCTIONS.md").read_text(encoding="utf-8")
SYSTEM = instr.split("やること:", 1)[0].split("\n")[0] + "\n\nやること:" + instr.split("やること:", 1)[1].split("複数の名前を渡されたときは")[0]
SYSTEM = SYSTEM.replace("出力形式: 1 行に 1 件の JSON（UTF-8）。", "出力形式: 1 行に 1 件の JSON だけを出力する（JSON Lines）。前置き・説明・コードブロックは書かない。")
SYSTEM = SYSTEM.replace("該当が無ければ空のファイルを作る。", "該当が無ければ NONE とだけ出力する。")
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1
log = open(L / "run_log.jsonl", "a", encoding="utf-8")
done = 0
for m in (json.loads(l) for l in open(L / "manifest.jsonl", encoding="utf-8")):
    out = L / "out" / f"{m['name']}.jsonl"
    if out.exists(): continue
    if done >= limit: break
    text = (L / "in" / f"{m['name']}.txt").read_text(encoding="utf-8")
    t0 = time.time()
    p = subprocess.run([CLAUDE, "-p", "--model", "haiku", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                        "--no-session-persistence", "--disable-slash-commands", "--output-format", "json", "--system-prompt", SYSTEM],
                       input=text, capture_output=True, text=True, encoding="utf-8", timeout=900, cwd=tempfile.gettempdir())
    try: r = json.loads(p.stdout)
    except Exception: print("bad stdout", m["name"], p.stdout[:300], p.stderr[:300]); sys.exit(1)
    if r.get("is_error"):
        print("error", m["name"], r.get("result", "")[:300]); sys.exit(1)
    rows = []
    for line in r["result"].splitlines():
        line = line.strip().strip("`")
        if line.startswith("{"):
            try: rows.append(json.loads(line))
            except Exception: pass
    out.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    u = r.get("usage", {})
    log.write(json.dumps({"name": m["name"], "sec": round(time.time() - t0), "rows": len(rows), "usage": {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}, "model": list(r.get("modelUsage", {}))}, ensure_ascii=False) + "\n"); log.flush()
    print(m["name"], f"{time.time()-t0:.0f}s rows {len(rows)}", {k: u.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")}, list(r.get("modelUsage", {})), flush=True)
    done += 1
