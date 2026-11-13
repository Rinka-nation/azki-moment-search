<script setup lang="ts">
import { computed, ref } from 'vue'

import { type Hit, saveFixApi } from '@/api'
import { useAdmin } from '@/composables/useAdmin'
import { useFavorites } from '@/composables/useFavorites'
import { hasRawToShow, pieces } from '@/highlight'

import ContextPanel from './ContextPanel.vue'
import YouTubePlayer from './YouTubePlayer.vue'

// tier の番号は search.py と同じ
const TIER_AI = 2
const TIER_CANDIDATE = 4 // これ以上は もしかして 系
const TIER_RELATED = 6 // 関連語タブの一致（確からしい）。もしかして に出る関連語は 7
const FIX_TITLES = { confirmed: '辞書で補正', candidate: '補正候補', ai: 'AI修正' } as const

const props = defineProps<{ hit: Hit; words: string[]; playing: boolean }>()
const emit = defineEmits<{ play: [sec: number | null] }>()

const { isFavorite, toggle } = useFavorites()
const showContext = ref(false)
const showRaw = ref(false)
const relatedWords = computed(() => props.hit.related_words ?? [])
const start = ref(props.hit.sec)

const parts = computed(() =>
  pieces(props.hit.segments ?? [{ text: props.hit.text }], [...props.words, ...relatedWords.value]),
)
// 字幕原文はルビにせず、下線とボタンで見せる（hasRawToShow）
const hasRaw = computed(() => hasRawToShow(parts.value))
// 一致したのが AI修正 の文でなくても、表示が AI修正 を含むなら印を出す
const hasAi = computed(() => props.hit.tier !== TIER_AI && parts.value.some((p) => p.kind === 'ai'))
// 確からしさでラベルの色を分ける: 一致系 / もしかして系 / 人が書いたタイムスタンプ
const badgeClass = computed(() => {
  if (props.hit.kind === 'timestamp') return 'ts'
  // AI修正 で一致したものも、AI修正 を含む表示の印（hasAi）と同じ見た目にそろえる（2026-11-10）
  if (props.hit.tier === TIER_AI && relatedWords.value.length === 0) return 'ai'
  return props.hit.tier >= TIER_CANDIDATE && props.hit.tier !== TIER_RELATED ? 'maybe' : 'match'
})
const badgeText = computed(() => {
  if (relatedWords.value.length > 0) return `関連語: ${relatedWords.value.join('、')}`
  return props.hit.kind === 'timestamp' ? 'タイムスタンプ' : props.hit.label
})
// ラベルの意味。モーダルを開かないと分からなかった（2026-11-12、外部の試用での指摘）。
// スマホにはホバーが無いので、title に加えて押すと下に出す
const BADGE_HELP: Record<string, string> = {
  原文: '検索語が元の字幕にそのまま出てくる発言です',
  補正: '辞書で直した字幕で一致した発言です（下線が直した箇所）',
  AI修正: 'AI が直した字幕で一致した発言です。AI の修正にも誤りがあります',
  読み: '表記は違うが、語の読みが検索語と同じ発言です',
  候補: '読みが近いだけの補正候補で一致した発言です。誤検出を含みます',
  '読み(部分)': '語の途中で読みが一致した発言です。誤検出を含みます',
  スパチャ読み: 'スーパーチャットの名前を読み上げている部分にだけ一致した発言です',
  タイムスタンプ: '作者（霧島リンカ）が投稿したタイムスタンプコメントです',
  関連語: 'ホロメンの呼び名・用語・曲名など、検索した人に関係する言葉を含む発言です',
}
const AI_HELP = 'この発言は、AI が字幕を直した文で表示しています。AI の修正にも誤りがあります'
const badgeHelp = computed(() =>
  relatedWords.value.length > 0 ? BADGE_HELP['関連語']! : (BADGE_HELP[badgeText.value] ?? ''),
)
const help = ref<'badge' | 'ai' | null>(null)
function toggleHelp(which: 'badge' | 'ai') {
  help.value = help.value === which ? null : which
}

// 字幕を手で直す（手元の serve で開いたときだけ。manualfix、2026-11-12）。保存すると手元の DB にすぐ反映し、
// 本番には次の build・publish で出る。印は付けない（霧島リンカさんの判断）
const { admin } = useAdmin()
const editing = ref(false)
const draft = ref('')
const note = ref('')
const saving = ref(false)
const editError = ref('')
const edited = ref<string | null>(null)

function startEdit() {
  draft.value = edited.value ?? parts.value.map((p) => p.text).join('')
  note.value = ''
  editError.value = ''
  editing.value = true
}

async function saveEdit() {
  saving.value = true
  editError.value = ''
  try {
    edited.value = (await saveFixApi(props.hit.id, draft.value, note.value)).text
    editing.value = false
  } catch (e) {
    editError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

function play(sec: number) {
  start.value = sec
  emit('play', sec)
}
</script>

<template>
  <article class="card" :class="{ playing }">
    <header class="meta">
      <button
        type="button"
        class="badge"
        :class="badgeClass"
        :title="badgeHelp"
        :aria-expanded="help === 'badge'"
        :disabled="!badgeHelp"
        @click="toggleHelp('badge')"
      >
        {{ badgeText }}
      </button>
      <button
        v-if="hasAi"
        type="button"
        class="badge ai"
        :title="AI_HELP"
        :aria-expanded="help === 'ai'"
        @click="toggleHelp('ai')"
      >
        AI修正
      </button>
      <time>{{ hit.upload_date ?? '日付不明' }}</time>
      <span class="ts-time">{{ hit.timestamp }}</span>
      <button
        type="button"
        class="fav"
        :aria-pressed="isFavorite(hit)"
        :title="isFavorite(hit) ? 'お気に入りから外す' : 'お気に入りに追加'"
        @click="toggle(hit)"
      >
        {{ isFavorite(hit) ? '★' : '☆' }}
      </button>
    </header>
    <p v-if="help" class="note help">{{ help === 'ai' ? AI_HELP : badgeHelp }}</p>

    <p v-if="edited !== null" class="text">{{ edited }}</p>
    <p v-else class="text">
      <template v-for="(p, i) in parts" :key="i">
        <span
          v-if="p.ruby !== undefined"
          class="fix"
          :class="p.kind"
          :title="`字幕では「${p.ruby || '（なし）'}」（${FIX_TITLES[p.kind ?? 'confirmed']}）`"
          ><mark v-if="p.mark">{{ p.text }}</mark><template v-else>{{ p.text }}</template></span
        >
        <mark v-else-if="p.mark">{{ p.text }}</mark>
        <template v-else>{{ p.text }}</template>
      </template>
    </p>

    <!-- 補正候補の「A → B（読み距離 n）」の行は、ごちゃごちゃするので出さない（2026-11-10 霧島リンカさん。破線の下線で分かる） -->
    <p v-if="hasRaw" class="note">
      <button type="button" class="link" @click="showRaw = !showRaw">
        {{ showRaw ? '字幕原文を隠す' : '字幕原文を見る' }}
      </button>
      <span v-if="showRaw" class="raw">{{ hit.text_raw }}</span>
    </p>

    <p class="title" :title="hit.title">{{ hit.title }}</p>

    <div class="actions">
      <button
        type="button"
        class="play"
        :title="playing ? 'プレーヤーを閉じる' : 'この場面から再生'"
        @click="playing ? emit('play', null) : play(hit.sec)"
      >
        {{ playing ? '■ 閉じる' : '▶ 再生' }}
      </button>
      <button type="button" class="ghost" :aria-expanded="showContext" @click="showContext = !showContext">
        前後の発言
      </button>
      <a class="ghost" :href="hit.url" target="_blank" rel="noopener" title="YouTube で開く">YouTube ↗</a>
      <button v-if="admin && hit.kind === 'caption'" type="button" class="ghost" @click="startEdit">✎ 直す</button>
    </div>

    <form v-if="editing" class="edit" @submit.prevent="saveEdit">
      <p class="note">字幕の原文: {{ hit.text_raw }}</p>
      <textarea v-model="draft" rows="3" maxlength="300" aria-label="直した文" />
      <input v-model="note" type="text" maxlength="200" placeholder="メモ（なぜ直したか。任意）" aria-label="メモ" />
      <p v-if="editError" class="note error">{{ editError }}</p>
      <div class="edit-actions">
        <button type="submit" class="play" :disabled="saving || !draft.trim()">{{ saving ? '保存中…' : '保存' }}</button>
        <button type="button" class="ghost" @click="editing = false">やめる</button>
        <span class="note">手元にすぐ反映。本番には次の publish で出ます</span>
      </div>
    </form>

    <YouTubePlayer v-if="playing" :video-id="hit.video_id" :start="start" />
    <ContextPanel v-if="showContext" :video-id="hit.video_id" :ms="hit.ms" @seek="play" />
  </article>
</template>

<style scoped>
.card {
  display: grid;
  gap: 8px;
  padding: 14px clamp(12px, 3.5vw, 16px);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
}

.card.playing {
  border-color: var(--accent);
}

.meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.badge {
  padding: 1px 8px;
  border: none;
  border-radius: 999px;
  font: inherit;
  font-size: 12px;
  font-weight: 700;
  line-height: inherit;
  cursor: help;
}

.badge:disabled {
  cursor: default;
}

.help {
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
}

.badge.match {
  background: var(--accent);
  color: var(--on-accent);
}

.badge.maybe {
  border: 1px solid var(--maybe);
  background: var(--maybe-soft);
  color: var(--maybe);
}

.badge.ts {
  background: var(--ts-soft);
  color: var(--ts);
}

.badge.ai {
  border: 1px solid var(--ai);
  background: var(--ai-soft);
  color: var(--ai);
}

.ts-time {
  color: var(--accent-strong);
  font-weight: 700;
}

.fav {
  margin-left: auto;
  padding: 0 4px;
  border: none;
  background: none;
  color: var(--accent);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}

.text {
  margin: 0;
  font-size: 16px;
  overflow-wrap: anywhere;
  word-break: normal;
  text-wrap: wrap;
}

/* 補正箇所の下線: 辞書の確定補正は実線、補正候補は破線、AI修正 は点線 */
.fix {
  text-decoration-line: underline;
  text-decoration-thickness: 2px;
  text-underline-offset: 3px;
  text-decoration-color: var(--accent);
}

.fix.candidate {
  text-decoration-style: dashed;
  text-decoration-color: var(--maybe);
}

.fix.ai {
  text-decoration-style: dotted;
  text-decoration-color: var(--ai);
}

.note {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
}

.raw {
  margin-left: 8px;
}

.title {
  margin: 0;
  overflow: hidden;
  font-size: 12px;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.play,
.ghost {
  padding: 6px 12px;
  white-space: nowrap;
  border-radius: 999px;
  font-size: 13px;
  text-decoration: none;
  cursor: pointer;
}

.play {
  border: none;
  background: var(--accent);
  color: var(--on-accent);
  font-weight: 700;
}

.play:hover {
  background: var(--accent-strong);
}

.ghost {
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
}

.ghost:hover {
  border-color: var(--accent);
}

.edit {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px dashed var(--accent);
  border-radius: var(--radius-sm);
}

.edit textarea,
.edit input {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  resize: vertical;
}

.edit-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.error {
  color: var(--accent-strong);
}

.link {
  padding: 0;
  border: none;
  background: none;
  color: var(--accent-strong);
  font-size: 12px;
  cursor: pointer;
}
</style>
