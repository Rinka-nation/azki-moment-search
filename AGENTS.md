# AZKi Moment Search - AGENTS.md

## Project overview

AZKi（ホロライブ 0期生）の配信アーカイブから「あの発言」を検索し、その秒数から YouTube で再生できるようにする**非公式ファンツール**。

- 2026-11-10 から β版として、URL を知っている人だけに公開している（検索エンジンには載せない。noindex）。
  非公式明記・削除依頼窓口（作者の X の DM @rinca_rain）・SQLite 互換スキーマを保つ。
- 文字起こしは YouTube 自動字幕（json3）を使う。音声・動画はダウンロードしない。
- 自動字幕の固有名詞誤認識を、辞書 + 読み（カナ）ベースで補正するのが中核。

詳細設計は `DESIGN.md`。

---

## Layout

```text
src/azkims/
  config.py      設定読み込み（config/config.toml → 無ければ config.example.toml）
  youtube.py     yt-dlp アダプタ（一覧・メタデータ・自動字幕のみ）
  filters.py     除外判定（メン限 / 歌枠 / 未終了配信 / 手動）
  captions.py    json3 → 発言（Utterance）分割
  textnorm.py    正規化（表示用 normalize_text / 読み比較用 fold_reading）
  dictionary.py  固有名詞辞書（TOML）とエイリアス置換
  reading.py     読み変換（辞書優先、残りは Sudachi）
  review.py      補正候補（読みあいまい一致）・未知語の抽出。読み取り専用
  timestamps.py  タイムスタンプコメントの解析
  align.py       タイムスタンプ × 字幕の照合（裏付けのある補正候補）。読み取り専用
  triage.py      補正候補のルール仕分け（accept / hold / reject）
  readout.py     スパチャ読みの名前の連続の判定（検索でだけ別のタブに分ける。表示と原文は変えない）
  oshi.py        推しマーク（config/oshi_marks.toml）を手がかりにしたホロメン名の補正候補
  llmfix.py      LLM の修正案（data/llm/out）を発言に対応させ、AI修正 の層（text_ai）として当てる
  reports.py     レビュー用 Markdown 出力
  db.py          SQLite スキーマ（FTS5 trigram）
  search.py      検索（表記 OR 読み）
  pipeline.py    sync → fetch → build
  cli.py         azkims コマンド
  server.py      Web UI 用の検索 API（azkims serve は 127.0.0.1 のみ。公開側は同じハンドラを Vercel の関数で使う）
  deploy.py      Vercel 用の一式を deploy/ に組み立てる（公開用の軽量 DB を含む）
  turso.py       Turso を HTTP で読む接続（検索用）
  publish.py     公開用 DB を Turso に入れ替え、プレビューと本番（β版）を切り替える
tools/llm/                 AI修正 を作るスクリプトと指示文（INSTRUCTIONS.md）。入力・出力は data/llm に読み書きする
scripts/daily.sh           週 1 回（月曜）の取得 → build → publish。配信日から 3 日たった配信だけ取り、（編集中）のタイムスタンプは取り直す。スケジュールタスクはこの 1 行だけを実行する（承認で止めないため形を固定。名前は daily のまま）
vercel/                    Vercel 用の雛形（api/index.py, vercel.json, requirements.txt）
web/                       画面（Vite + TypeScript + Vue 3）。npm run build → web/dist を serve が配信
config/dictionary/*.toml   辞書。accepted.toml は accept/reject の追記先
config/dictionary/games/   ゲーム別の辞書（そのゲームの配信でだけ使う）
data/                      字幕・コメントの原本キャッシュと SQLite（git 管理外）
output/                    レビュー用レポート（git 管理外）
```

---

## Rules

### データ

- `data/captions/*.json3`（字幕原本）と `data/comments/*.json`（コメント原本）は**書き換えない**。
  補正は常に build 時に原本から再計算する。
- タイムスタンプコメントは設定した投稿者のものだけを保存する。他の視聴者のコメントはサイトに載せない。
  ライブチャットは分析用のデータとして使ってよい。残すのは時刻ごとの件数だけにし、本文と投稿者は残さず、公開用 DB にも入れない。
  霧島リンカはこのツールの作者本人で、モンハンワイルズ（2025-03-17）以降の分だけが本人のもの。検索に出してよい。
  それより前の配信の、他の人のタイムスタンプは参考にとどめ、検索に出さない（作者に連絡が取れず正確性も担保できない）。
- DB の `text_raw` は字幕原文（正規化のみ）、`text` は辞書補正後。両方保持し、原文を失わない。
- `build` は全件作り直し。結果は「字幕キャッシュ + 辞書 + 設定」だけで決まる（決定的）。

### 補正

- `text` を書き換えてよいのは辞書の `aliases`（人が確定させたもの）だけ。
  例外として、LLM による発言ごとの修正は別の層に持ち、印（AI修正）を付けて表示・検索に使ってよい（2026-11-04 霧島リンカさんの判断）。
  `text_raw` は残し、LLM の出力は data/ にキャッシュして build で読む（build のたびに LLM を呼ばない）。
  検索では `utterances_fts` の `text_ai` 列で一致させ、tier 2（AI修正）とする（DESIGN.md 5.11）。
- あいまい一致は `text_fuzzy` / `fuzzy_matches` に**候補として別に持つ**。`text` には混ぜない。
  検索では「もしかして」（tier 4-5）として、一致（tier 0-3）とは別枠に出す。
- 自動で辞書に入れない。採否は人が `accept` / `reject` / `hold` で決めるか、人が合意したルール
  （`triage.py`）で `triage --apply` する。ルールを変えるときは DESIGN.md 5.7 も直す。
- 日常語（マイク、いくら）は補正しない。迷う句（赤いハート）は hold。
- 誤補正は未補正より悪い。迷う別表記は alias にせず、候補・読み一致に任せる。
  これは誤補正で文がさらに意味の通らないものになるのを避けるため。意味の通らない文を、文意がほぼ同じで正しい文に
  直す LLM の修正は、この原則より優先する（2026-11-04）。
- 実在する別の固有名詞（例: ジモゲッサー、ソロライブ）は reject して候補からも外す。
- alias は全辞書で一意（重複はロード時エラー）。2 文字未満の表記は登録不可。

### YouTube

- 取得は自動字幕、メタデータ、ライブチャットのみ。`skip_download` を外さない。
- ライブチャットは 1 配信で数百リクエストになる。速度を制限し（`--limit-rate`）、429 が出たら止める。Cookie は使わない。
- 1 配信ごとに `fetch.sleep_seconds` 待つ。並列化しない。
- 1 配信の失敗で全体を止めない（status = error で記録して続行）。
- メン限・歌枠は対象外。歌枠判定はタイトルのパターン + 手動リスト。

### 公開を見据えて

- 本番（β版、https://azki-moment-search.vercel.app）は `azkims publish` が更新する（2026-11-10 霧島リンカさんの決定）。
  プレビュー（保護あり）の確認に通ったら、`vercel deploy --prod --skip-domain` で作って確かめ、`vercel promote` で切り替える。
  それ以外の本番への変更（保護・Firewall・ドメインの設定、手での `--prod`）はユーザーの確認を取ってから行う。
- 本番は Deployment Protection の Standard（プレビューだけ保護）。`/api/` に Vercel Firewall の回数制限
  （IP ごと 60 秒 30 回、超えたら 429）。検索エンジンに載せない（meta robots・X-Robots-Tag・robots.txt）。
- 公開物に字幕・コメントの原本や取得エラーの内容を含めない（`deploy.export_slim_db`）。
- リポジトリ（GitHub）も公開する前提で書く（2026-11-12 霧島リンカさんの決定。信頼性のためにコードを見せる）。
  トークン・個人のメールアドレス・PC のユーザーのフォルダのパス・`data/` の中身をコミットしない。
  `.githooks/pre-commit` が `scripts/check_public.py --staged` で止め、UT-117 が追跡中の全ファイルを確かめる。
  hook は `git config core.hooksPath .githooks` で有効にする。`--no-verify` で飛ばさない。
  スクリプトのパスは `Path(__file__)` から作り、PC ごとのパスを書かない。

- スキーマは SQLite + FTS5 でそのまま使える範囲に留める。移行先は無料枠を実測して決める（D1 の無料枠は書き込みと容量の上限で不可）。
- 公開時は「非公式」「権利は各権利者に帰属」「誤りを含む」「削除依頼窓口」を必ず表示する。
- 広告・収益化はしない。

---

## Dev

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m pytest
.venv/Scripts/ruff check src tests
.venv/Scripts/ruff format src tests
```

テストはネットワークに出ない。YouTube を叩くのは `sync` / `fetch` / `fetch-comments` だけ。

```bash
cd web
npm install --legacy-peer-deps   # npm 10.8 の peer 解決の不具合回避
npx vitest --run
npm run build                    # 型チェック + ビルド
```

API の形式（server.py の /api/*）を変えるときは
web/src/api.ts と DESIGN.md 10.2 も合わせて直す。
