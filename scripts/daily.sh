#!/usr/bin/env bash
# 週 1 回（月曜）の取得 → DB 作成 → 公開（プレビューと本番の β版。publish が両方を切り替える）。スケジュールタスクはこの 1 本だけを実行する。
# 名前は daily.sh のまま（スケジュールタスクの許可がこのコマンドの形に一致しているため）。
# 配信日から fetch.min_age_days（3 日）たった配信だけを取る。（編集中）のタイムスタンプは毎回取り直す。
# コマンドの形を固定し、1 つの許可で最後まで止まらずに動くようにする。
# 辞書は変更しない（triage は案を作るだけ）。--force / --prod は付けない。
set -u
export PYTHONUTF8=1

cd "$(dirname "$0")/.."
AZKIMS="${AZKIMS:-.venv/Scripts/azkims}"
BLOCKED='HTTP Error 429|ブロックされた可能性'

exec 3>&1
exec > output/daily_log.txt 2>&1

step() { echo "=== $1 $(date '+%H:%M:%S')"; }
blocked() { grep -qE "$BLOCKED" "$1"; }

step sync
"$AZKIMS" sync || echo "sync 失敗: exit=$?"

step fetch-trial
"$AZKIMS" fetch --limit 1 --retry-errors > output/fetch_trial.txt 2>&1
cat output/fetch_trial.txt
if blocked output/fetch_trial.txt; then
  echo "result fetch=blocked-at-trial"
else
  step fetch
  "$AZKIMS" fetch --limit 50 > output/fetch_log.txt 2>&1
  echo "fetch ok=$(grep -c ' ok ' output/fetch_log.txt) lines=$(wc -l < output/fetch_log.txt)"
  grep -v ' ok ' output/fetch_log.txt
  if blocked output/fetch_log.txt; then
    echo "result fetch=blocked"
  else
    echo "result fetch=done"
    step comments
    "$AZKIMS" fetch-comments --limit 60 > output/comments_log.txt 2>&1
    tail -3 output/comments_log.txt
    blocked output/comments_log.txt && echo "result comments=blocked"
  fi
fi

step build
if ! "$AZKIMS" build; then
  echo "result build=failed"
  echo "daily finished: build failed" >&3
  exit 1
fi

step stats
"$AZKIMS" stats
step triage
"$AZKIMS" triage
step align
"$AZKIMS" align

step publish
"$AZKIMS" publish > output/publish_log.txt 2>&1
status=$?
echo "result publish exit=$status"
step end
echo "daily finished: publish exit=$status" >&3
exit "$status"
