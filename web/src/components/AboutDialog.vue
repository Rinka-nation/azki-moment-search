<script setup lang="ts">
import { ref } from 'vue'

import type { Stats } from '@/api'

// 注意書き・使い方・データの概要。ページの下に並べず、ここにまとめる（2026-11-10 霧島リンカさん）
defineProps<{ stats: Stats | null }>()

const dialog = ref<HTMLDialogElement | null>(null)

function day(iso: string | null) {
  return iso ? iso.slice(0, 10) : '不明'
}

defineExpose({ open: () => dialog.value?.showModal() })
</script>

<template>
  <dialog ref="dialog" class="about" aria-labelledby="about-title" @click.self="dialog?.close()">
    <div class="body">
      <header>
        <h2 id="about-title">このサイトについて</h2>
        <button type="button" class="close" aria-label="閉じる" @click="dialog?.close()">×</button>
      </header>

      <p>
        AZKi さんの配信で話した言葉を検索して、その場面から再生できる非公式のファンツールです。
        いまは β版として、試しに使ってもらっています。
      </p>

      <h3>ご注意</h3>
      <ul>
        <li>ホロライブプロダクション・カバー株式会社とは関係ありません。</li>
        <li>映像・音声・画像などの権利は、各権利者に帰属します。</li>
        <li>発言は YouTube の自動字幕をもとにしているため、誤りを含みます。内容は元の配信で確かめてください。</li>
        <li>一部の発言は、AI（大規模言語モデル）で誤認識を直しています。AI の修正にも誤りがあります。</li>
        <li>メンバー限定配信・歌枠・3D ライブなどは対象外です。すべての発言を網羅してはいません。</li>
        <li>タイムスタンプは、作者（霧島リンカ）が投稿したタイムスタンプコメントです。</li>
      </ul>

      <h3>結果の見方</h3>
      <dl class="legend">
        <dt>ラベル（原文・補正・読み など）</dt>
        <dd>どう一致したかを示します。結果のラベルを押すと、意味が出ます。</dd>
        <dt><span class="u solid">下線（実線）</span></dt>
        <dd>辞書で直した箇所。元の字幕は「字幕原文を見る」で全体を見られます（ひらがな・カタカナの違いだけの補正では出しません）。</dd>
        <dt><span class="u dotted">下線（点線）</span></dt>
        <dd>AI が直した箇所（「AI修正」の印）。</dd>
        <dt><span class="u dashed">下線（破線）</span></dt>
        <dd>読みが近いだけの補正候補。</dd>
        <dt>関連語</dt>
        <dd>ホロメンの呼び名・持ちネタ・曲名など、その人に関係する言葉を含む発言。</dd>
        <dt>もしかして</dt>
        <dd>読みが近い語や、語の途中での読みの一致。誤検出を含みます。</dd>
        <dt>スパチャ読み</dt>
        <dd>スーパーチャットの名前を読み上げている部分にだけ一致した発言。</dd>
        <dt>条件の「探す結果」</dt>
        <dd>タイムスタンプ・もしかして・スパチャ読みはここで選べます。件数があるときは、タブの下にも出ます。</dd>
      </dl>

      <h3>削除のご依頼・ご意見</h3>
      <p>
        作者の X の DM へお送りください:
        <a href="https://x.com/rinca_rain" target="_blank" rel="noopener noreferrer">@rinca_rain</a>
      </p>

      <p v-if="stats" class="stats">
        対象 {{ stats.streams.toLocaleString() }} 配信（{{ stats.date_from ?? '?' }} 〜 {{ stats.date_to ?? '?' }}）<br />
        発言 {{ stats.utterances.toLocaleString() }} 件・タイムスタンプ {{ stats.timestamp_entries.toLocaleString() }} 件<br />
        データ作成 {{ day(stats.built_at) }}
      </p>
    </div>
  </dialog>
</template>

<style scoped>
.about {
  width: min(560px, calc(100vw - 24px));
  max-height: calc(100dvh - 48px);
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text);
}

.about::backdrop {
  background: rgb(40 10 25 / 45%);
}

.body {
  padding: 4px 20px 20px;
  font-size: 14px;
}

header {
  position: sticky;
  top: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 0 8px;
  background: var(--surface);
}

h2 {
  margin: 0;
  font-size: 18px;
}

h3 {
  margin: 18px 0 6px;
  font-size: 14px;
  color: var(--accent-strong);
}

p {
  margin: 6px 0;
}

ul {
  margin: 0;
  padding-left: 1.2em;
}

li + li {
  margin-top: 2px;
}

.close {
  width: 32px;
  height: 32px;
  border: none;
  border-radius: 50%;
  background: none;
  color: var(--text-muted);
  font-size: 20px;
  cursor: pointer;
}

.close:hover {
  background: var(--surface-2);
}

.legend {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  margin: 0;
}

.legend dt {
  font-weight: 700;
}

.legend dd {
  margin: 0;
  color: var(--text-muted);
}

.u {
  text-decoration-line: underline;
  text-decoration-thickness: 2px;
  text-underline-offset: 3px;
}

.u.solid {
  text-decoration-color: var(--accent);
}

.u.dotted {
  text-decoration-style: dotted;
  text-decoration-color: var(--ai);
}

.u.dashed {
  text-decoration-style: dashed;
  text-decoration-color: var(--maybe);
}

.stats {
  margin-top: 18px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

@media (width < 480px) {
  .legend {
    grid-template-columns: 1fr;
  }

  .legend dd {
    margin-bottom: 6px;
  }
}
</style>
