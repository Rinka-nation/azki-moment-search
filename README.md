# AZKi Moment Search

AZKi の配信アーカイブの発言を検索し、その瞬間から再生するための**非公式ファンツール**です。
YouTube の自動字幕を使っているため、誤りを含みます。内容は必ず元の配信で確認してください。

広告や収益化をしない非公式のファンツールなので、運営に費用がかからない構成にしています。
悪用で使い切られても課金されず、止まるだけの構成です（設計書の数字はそのためのものです）。

## データの扱い

- 文字起こしは YouTube の自動字幕だけを使います。音声・動画はダウンロードしません。
- 検索に出すのは、AZKi さんの配信の発言と、作者（霧島リンカ）が投稿したタイムスタンプコメントだけです。
  他の視聴者のコメントやライブチャットの本文は、サイトに載せません。
- メンバー限定配信・歌枠・ライブ本番、タイトルで切り抜きを禁止している配信は対象外です。
- 字幕の誤認識は、人が確かめた辞書で直します。AI（大規模言語モデル）で直した文には「AI修正」の印を付け、
  元の字幕（`text_raw`）も残して「字幕原文を見る」で見られるようにしています。
- 字幕・コメントの原本と DB は、このリポジトリに含めません。
- 削除のご依頼・ご意見は、作者の X の DM（@rinca_rain）へお願いします。

## セットアップ

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
```

公開リポジトリなので、コミットの前に `scripts/check_public.py` で、トークン・個人のアドレス・PC のパス・データが入っていないかを確かめます。clone したら、最初に次を 1 回実行してください。

```bash
git config core.hooksPath .githooks
git config user.email <GitHub に登録したアドレス>
```

設定を変える場合は `config/config.example.toml` を `config/config.toml` にコピーして編集します。

## 使い方

```bash
# 1. 配信一覧を取得（新しい順に 40 件）
.venv/Scripts/azkims sync --limit 40

# 2. 自動字幕を取得（既定 20 件 / 回。1 件ごとに数秒待つ）
.venv/Scripts/azkims fetch

# 2b. タイムスタンプコメントを取得（config の [timestamps] の投稿者のもの）
.venv/Scripts/azkims fetch-comments --limit 300

# 3. 検索 DB を作る（辞書を直したら毎回これ）
.venv/Scripts/azkims build

# 4. 検索
.venv/Scripts/azkims search すいせい
.venv/Scripts/azkims search 開拓者 ありがとう --sort old
.venv/Scripts/azkims search 地図 --title GeoGuessr --from 2026-01-01

# 状況確認
.venv/Scripts/azkims stats
```

検索結果は 5 つに分かれます（画面の 一致・関連語 のタブと、条件の「探す結果」の タイムスタンプ・もしかして・スパチャ読み）。
CLI は 一致・もしかして・タイムスタンプ を出します。

| 区分 | ラベル | 意味 |
|---|---|---|
| 一致 | 原文 | 字幕の原文にそのまま一致 |
| 一致 | 補正 | 確定済みの辞書補正（例: ホロ面→ホロメン）のおかげで一致 |
| 一致 | AI修正 | LLM が直した文で一致（DESIGN.md 5.11） |
| 一致 | 読み | 読みが語単位で一致（例: かいたくしゃ→開拓者、じおげっさー→GeoGuessr） |
| 関連語 | 関連語 | ホロメン・ユニットの名前で検索したとき、その人の呼び名・用語・曲名などの一致 |
| タイムスタンプ | タイムスタンプ | 作者（霧島リンカ）が投稿したタイムスタンプコメント |
| スパチャ読み | スパチャ読み | スーパーチャットの名前を読み上げている部分にだけ一致 |
| もしかして | 候補 | 読みが近い未確定の補正候補（例: ホロ取り→ホロドリ）で一致。誤検出の可能性あり |
| もしかして | 読み(部分) | 読みが語の途中で一致（例: つり→夏祭り）。誤検出の可能性あり |

「もしかして」は確からしい順（候補 → 読み(部分)、候補は読みの距離が小さい順）に並びます。
`--tab match` / `--tab maybe` / `--tab all` で片方だけ表示できます。

## Web UI（この PC のブラウザで使う）

手元の serve で開くと、字幕の結果に「✎ 直す」が出て、字幕を手で直せます（`data/manual_fixes.jsonl` に保存。公開しているサイトには出ません。DESIGN.md 5.12）。

```bash
# 初回 / 画面を変えたとき: 画面をビルド
cd web && npm install --legacy-peer-deps && npm run build && cd ..

# 検索 API + 画面を起動 → http://127.0.0.1:8765/
.venv/Scripts/azkims serve
```

画面を開発するときは `azkims serve` を起動したまま、`web` で `npm run dev`（http://127.0.0.1:5173/、
`/api` は 8765 に転送）。`npm install` は npm 10.8 の不具合回避のため `--legacy-peer-deps` を付ける。

## Vercel への公開（β版）

```bash
# 一式を deploy/ に、Turso 用の DB を data/turso_upload.sqlite に作る（web は先に npm run build）
.venv/Scripts/azkims bundle-vercel --turso --turso-export
# 2 回目以降は、DB の入れ替え・プレビューの切り替え・本番（β版）の切り替えを 1 コマンドで行う（DESIGN.md 10.5）。
# publish は build しない。辞書や画面を直したら先に build（と npm run build）をする（しないと publish が止まる）
.venv/Scripts/azkims publish

# 初回だけ: ログイン（ブラウザで認証）
cd deploy && npx vercel login

# 手でプレビューを出す場合（Deployment Protection により本人だけが見られる）。本番は publish だけが切り替える
npx vercel deploy
```

## 固有名詞の補正

```bash
# 読みが辞書の語に近い「未登録の表記」を一覧 → output/review_candidates.md
.venv/Scripts/azkims review

# タイムスタンプと字幕を照合し、裏付けのある補正候補を一覧 → output/timestamp_alignment.md
.venv/Scripts/azkims align

# タイムスタンプの推しマークを手がかりにホロメン名の崩れを探す → output/oshi_candidates.md
.venv/Scripts/azkims oshi            # 案だけ
.venv/Scripts/azkims oshi --apply    # accepted.toml に書き込む（話題の配信だけで効く）

# 辞書に無い語（ゲーム名・企画名など）を一覧 → output/unknown_terms.md
.venv/Scripts/azkims discover

# 候補を採用 / 却下（config/dictionary/accepted.toml に追記）
.venv/Scripts/azkims accept "ホロ面" "ホロメン"
.venv/Scripts/azkims accept "ほろり" "ホロドリ" --scope title   # タイトルに語がある配信だけ置換
.venv/Scripts/azkims reject "マイク" "マイクラ"

# 保留（補正はせず候補として残す）
.venv/Scripts/azkims hold "赤いハート" "赤井はあと"

# 候補をルールでまとめて仕分け（案 → output/triage.md、--apply で書き込み）
.venv/Scripts/azkims triage
.venv/Scripts/azkims triage --apply

# 強め補正（保留分も含めて採用、source = aggressive）と、その取り消し
.venv/Scripts/azkims triage --aggressive --apply
.venv/Scripts/azkims drop-source aggressive

# 反映
.venv/Scripts/azkims build
```

新しい固有名詞は `config/dictionary/*.toml` に `[[term]]` で追加します（書式はファイル冒頭のコメント参照）。

## 注意

- 非公式・非営利。画像・映像・音声の権利は各権利者に帰属します。
- メンバー限定配信・歌枠は対象外です。
