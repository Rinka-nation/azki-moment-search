"""data/llm/out の出力を検査する: JSON として読めるか、wrong が該当発言に実際にあるか、wrong と fix が同じでないか。"""
import json, re, sys
from pathlib import Path
L = Path(__file__).resolve().parents[2] / "data" / "llm"  # 入力と出力は data/llm（git 管理外）
sys.stdout.reconfigure(encoding="utf-8")
ok = bad = same = files = 0
for f in sorted((L / "out").glob("*.jsonl")):
    files += 1
    U = {int(m[1]): m[2] for m in re.finditer(r"^(\d+)\t[\d:]+\t(.*)$", (L / "in" / (f.stem + ".txt")).read_text(encoding="utf-8"), re.M)}
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        try: r = json.loads(line)
        except Exception: bad += 1; continue
        n, e = r.get("n"), r.get("n_end") or r.get("n")
        src = "".join(U.get(i, "") for i in range(n, e + 1)) if isinstance(n, int) else ""
        w = (r.get("wrong") or "").replace("\n", "")
        if not w or w not in src: bad += 1
        elif w == (r.get("fix") or "").replace("\n", ""): same += 1
        else: ok += 1
print(f"files {files} ok {ok} invalid {bad} unchanged {same}")
