# tools/llm

AI修正（DESIGN.md 5.11）を作るスクリプトと、モデルに渡す指示文（`INSTRUCTIONS.md`）です。
2026-11-11 に `data/llm/` から移しました（ISS-14。手順を git に残すため）。

- 入力・出力は今までどおり `data/llm/` にあります（git 管理外。字幕の本文を含み、容量も大きいため）。
  各スクリプトは自分の場所から `data/llm/`（とその下の `r2/`、`prop/` など）を探します。
- Claude Code は環境変数 `CLAUDE_EXE`（実行ファイルのパス）か、PATH の `claude` を使います。
  リポジトリの公開に合わせて、PC ごとのパスをコードから外しました（2026-11-12）。`pilot_ts/prep.py` は試しの出力のパスを引数で受け取ります
- `run.py`: `data/llm/in` の入力を Claude Code の `-p`（Haiku、ツールなし）に 1 つずつ渡し、`data/llm/out` に書く
- `r2/`: 2 周目（タイムスタンプの言葉つき）の入力作成・実行・1 周目との統合
- `prop/`: 横展開（同じ配信の同じ誤りを探して判定させる。出力は `data/llm/prop/out`）
- `check.py`: 出力の検査。`pilot_ts/`、`verify_pilot/`、`run_pilot_ts.py` は試験の記録
- 定期実行ではモデルを呼ばない。build は `data/llm` の出力を読むだけ
- `manual_examples.py`: 人が手で直した字幕（`data/manual_fixes.jsonl`、DESIGN.md 5.12）を、次に AI修正 を作るときの例として出す。人が AI修正 を直し直した数も出す（モデルは呼ばない。出力は `output/manual_examples.md`）
