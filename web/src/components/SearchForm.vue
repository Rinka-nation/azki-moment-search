<script setup lang="ts">
import { computed, ref } from 'vue'

import type { Member, SearchParams, Sort, Tab } from '@/api'

import MemberPicker from './MemberPicker.vue'

const params = defineModel<SearchParams>({ required: true })
defineProps<{ counts: Record<Tab, number> | null }>()
const emit = defineEmits<{ search: []; member: [member: Member]; tab: [tab: Tab] }>()

// 検索欄の下は「ホロメン」「条件」の 2 つのボタンだけにし、中身は押したときだけ開く
const open = ref<'members' | 'options' | null>(null)
const filtered = computed(
  () => Boolean(params.value.title || params.value.from || params.value.to) || params.value.exact,
)
const changed = computed(() => filtered.value || params.value.sort !== 'new' || !SUBTITLE_TABS.includes(params.value.tab))

// 字幕の一致・関連語以外の結果は、タブに並べるとスマホで右にはみ出して気づけないので、条件で選ぶ（2026-11-10 霧島リンカさん）
const SUBTITLE_TABS: Tab[] = ['match', 'related']
const kinds: { id: Tab; label: string; note: string }[] = [
  { id: 'match', label: '字幕', note: '一致・関連語のタブ' },
  { id: 'ts', label: 'タイムスタンプ', note: '作者のタイムスタンプ' },
  { id: 'maybe', label: 'もしかして', note: '読みが近いだけのもの' },
  { id: 'readout', label: 'スパチャ読み', note: '名前の読み上げにだけ一致' },
]

function isKind(id: Tab) {
  return id === 'match' ? SUBTITLE_TABS.includes(params.value.tab) : params.value.tab === id
}

function pickKind(id: Tab) {
  open.value = null
  emit('tab', id)
}

const sorts: { value: Sort; label: string }[] = [
  { value: 'new', label: '新しい順' },
  { value: 'old', label: '古い順' },
]

function toggle(panel: 'members' | 'options') {
  open.value = open.value === panel ? null : panel
}

function submit() {
  if (open.value === 'members') open.value = null
  emit('search')
}

function pick(m: Member) {
  open.value = null
  emit('member', m)
}

// 親の params は reactive の const なので、丸ごと差し替えず中身を書き換える（差し替えは親に届かない）
function clearQuery() {
  params.value.q = ''
}

function reset() {
  Object.assign(params.value, { tab: 'match', title: '', from: '', to: '', sort: 'new', exact: false })
  emit('search')
}
</script>

<template>
  <form class="search" role="search" @submit.prevent="submit">
    <div class="box">
      <svg class="icon" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3.5-3.5" />
      </svg>
      <label class="sr-only" for="q">検索語</label>
      <input
        id="q"
        v-model="params.q"
        class="q"
        type="search"
        enterkeyhint="search"
        placeholder="覚えている言葉で検索"
        autocomplete="off"
      />
      <button v-if="params.q" type="button" class="clear" aria-label="検索語を消す" @click="clearQuery">×</button>
      <button class="primary" type="submit">検索</button>
    </div>

    <div class="tools">
      <button
        type="button"
        class="tool"
        :class="{ on: open === 'members' }"
        :aria-expanded="open === 'members'"
        @click="toggle('members')"
      >
        <span aria-hidden="true">☺</span> ホロメンから探す
      </button>
      <button
        type="button"
        class="tool"
        :class="{ on: open === 'options' }"
        :aria-expanded="open === 'options'"
        @click="toggle('options')"
      >
        <span aria-hidden="true">⚙</span> 条件
        <span v-if="changed" class="dot" aria-label="変更あり" />
      </button>
      <span class="tip">スペースで区切ると AND 検索</span>
    </div>

    <MemberPicker v-if="open === 'members'" @pick="pick" />

    <div v-if="open === 'options'" class="options">
      <fieldset class="kinds">
        <legend>探す結果</legend>
        <label v-for="k in kinds" :key="k.id" class="kind" :class="{ on: isKind(k.id) }">
          <input type="radio" name="kind" :checked="isKind(k.id)" @change="pickKind(k.id)" />
          <span class="kind-label">
            {{ k.label }}
            <span v-if="counts && k.id !== 'match'" class="num">{{ counts[k.id].toLocaleString() }}</span>
            <small>{{ k.note }}</small>
          </span>
        </label>
      </fieldset>
      <label class="field">
        <span>並び順</span>
        <select v-model="params.sort" @change="emit('search')">
          <option v-for="s in sorts" :key="s.value" :value="s.value">{{ s.label }}</option>
        </select>
      </label>
      <label class="field">
        <span>配信タイトル</span>
        <input v-model="params.title" type="text" placeholder="例: GeoGuessr" />
      </label>
      <div class="dates">
        <label class="field">
          <span>開始日</span>
          <input v-model="params.from" type="date" />
        </label>
        <label class="field">
          <span>終了日</span>
          <input v-model="params.to" type="date" />
        </label>
      </div>
      <label class="check">
        <input v-model="params.exact" type="checkbox" @change="emit('search')" />
        <span>完全一致<small>（書いたとおりの表記だけ。読みやあいまい一致を使わない）</small></span>
      </label>
      <div class="option-actions">
        <button type="button" class="link" @click="reset">条件を元に戻す</button>
        <button type="submit" class="apply">この条件で検索</button>
      </div>
    </div>
  </form>
</template>

<style scoped>
.search {
  display: grid;
  /* 列を内容の幅（入力欄の既定の幅）より狭くできるようにする。無いと 320px 幅で右にはみ出す（ISS-13） */
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
}

.box {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 4px 4px 14px;
  border: 1.5px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  box-shadow: var(--shadow);
}

.box:focus-within {
  border-color: var(--accent);
}

.icon {
  flex: none;
  width: 18px;
  height: 18px;
  fill: none;
  stroke: var(--text-muted);
  stroke-width: 2;
  stroke-linecap: round;
}

.q {
  flex: 1;
  min-width: 0;
  padding: 8px 4px;
  border: none;
  background: none;
  font-size: 16px; /* 16px 未満だと iPhone が拡大する */
  outline: none;
}

.q::-webkit-search-cancel-button {
  display: none;
}

.clear {
  flex: none;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 50%;
  background: none;
  color: var(--text-muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}

.clear:hover {
  background: var(--surface-2);
}

.primary {
  flex: none;
  padding: 9px 20px;
  border: none;
  border-radius: 999px;
  background: var(--accent);
  color: var(--on-accent);
  font-weight: 700;
  cursor: pointer;
}

.primary:hover {
  background: var(--accent-strong);
}

.tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.tool {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 12px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  cursor: pointer;
}

.tool:hover,
.tool.on {
  border-color: var(--accent);
  color: var(--accent-strong);
}

.tool.on {
  background: var(--accent-soft);
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
}

.tip {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-muted);
}

.options {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
  font-size: 13px;
}

.kinds {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin: 0;
  padding: 0;
  border: none;
}

.kinds legend {
  margin-bottom: 4px;
  padding: 0;
  color: var(--text-muted);
}

.kind {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.kind.on {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.kind input {
  margin-top: 3px;
  accent-color: var(--accent);
}

.kind-label {
  font-weight: 700;
}

.kind-label .num {
  margin-left: 2px;
  color: var(--accent-strong);
  font-variant-numeric: tabular-nums;
}

.kind-label small {
  display: block;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 400;
}

.field {
  display: grid;
  gap: 4px;
  color: var(--text-muted);
}

.field input,
.field select {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--text);
}

.dates {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.check {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  cursor: pointer;
}

.check input {
  margin-top: 4px;
  accent-color: var(--accent);
}

.check small {
  display: block;
  color: var(--text-muted);
  font-size: 12px;
}

.option-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.link {
  padding: 0;
  border: none;
  background: none;
  color: var(--accent-strong);
  cursor: pointer;
}

.apply {
  padding: 7px 16px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  background: var(--surface);
  color: var(--accent-strong);
  font-weight: 700;
  cursor: pointer;
}

@media (width < 520px) {
  .tip {
    display: none;
  }
}
</style>
