<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import {
  type Hit,
  type Member,
  searchApi,
  type SearchParams,
  type SearchResponse,
  type Stats,
  statsApi,
  type Tab,
  withCounts,
} from '@/api'
import AboutDialog from '@/components/AboutDialog.vue'
import AppFooter from '@/components/AppFooter.vue'
import ResultCard from '@/components/ResultCard.vue'
import SearchForm from '@/components/SearchForm.vue'
import SidePanel from '@/components/SidePanel.vue'
import MemberChips from '@/components/MemberChips.vue'
import { useFavorites } from '@/composables/useFavorites'
import { loadMembers } from '@/composables/useMembers'
import { useSearchState } from '@/composables/useSearchState'
import { useTheme } from '@/composables/useTheme'

const PAGE = 20

const { params, pushUrl, readUrl } = useSearchState()
const { favorites } = useFavorites()
const { theme, toggle: toggleTheme } = useTheme()

const view = ref<Tab | 'fav'>(params.tab)
const response = ref<SearchResponse | null>(null)
const hits = ref<Hit[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const playingId = ref<string | null>(null)
const stats = ref<Stats | null>(null)
const about = ref<InstanceType<typeof AboutDialog> | null>(null)
const form = ref<InstanceType<typeof SearchForm> | null>(null)
const examples = ['かいたくしゃ', 'じおげっさー', 'ござるさん', 'おにぎり']

// 説明は 1 行に収まる長さにする。詳しい見方は「このサイトについて」
const tabs: { id: Tab; label: string; note: string }[] = [
  { id: 'match', label: '一致', note: '下線は直した箇所。元の字幕は「字幕原文を見る」で見られます' },
  { id: 'related', label: '関連語', note: '呼び名・用語・曲名など、その人に関係する言葉です' },
  { id: 'ts', label: 'タイムスタンプ', note: '作者が投稿したタイムスタンプコメントです' },
  { id: 'maybe', label: 'もしかして', note: '読みが近いだけのものも含みます' },
  { id: 'readout', label: 'スパチャ読み', note: '名前の読み上げの部分にだけ一致した発言です' },
]

// タブに並べるのは字幕の 一致・関連語 だけ。ほかの結果は 条件 で選び、タブの下に件数を出す（スマホで右にはみ出して
// 気づけなかったため。2026-11-10 霧島リンカさん）。関連語タブは検索語がホロメン・ユニットの名前のときだけ出す
const SUBTITLE_TABS: Tab[] = ['match', 'related']
const isSubtitle = computed(() => SUBTITLE_TABS.includes(view.value as Tab))
const shownTabs = computed(() =>
  isSubtitle.value
    ? tabs.filter((t) => SUBTITLE_TABS.includes(t.id) && (t.id !== 'related' || response.value?.related))
    : tabs.filter((t) => t.id === view.value),
)
const otherTabs = computed(() =>
  tabs.filter((t) => !SUBTITLE_TABS.includes(t.id) && t.id !== view.value && (response.value?.counts[t.id] ?? 0) > 0),
)
const words = computed(() => response.value?.terms.map((t) => t.surface) ?? [])
const filtered = computed(() => Boolean(params.title || params.from || params.to))
const compact = computed(() => Boolean(response.value) || loading.value || view.value === 'fav')
const shown = computed(() => (view.value === 'fav' ? favorites.value : hits.value))
const hasMore = computed(() => view.value !== 'fav' && Boolean(response.value?.has_more))
const readings = computed(() =>
  (response.value?.terms ?? [])
    .filter((t) => t.reading)
    .map((t) => `${t.surface}＝${t.reading}`)
    .join('、'),
)

let requestSeq = 0

// 表示中の結果を取ったときの条件。検索欄と条件の入力は params をそのまま書き換えるので、検索を押す前の書きかけと分ける。
// 書きかけのままタブを切り替えると、結果は新しい語なのに件数が前の検索のままになっていた（2026-11-14）
let shownFor: SearchParams | null = null
// 件数を変える条件。タブ・関連語・並び順は件数を変えない
const COUNTED = ['q', 'title', 'from', 'to', 'exact'] as const
const sameCounts = (a: SearchParams, b: SearchParams) => COUNTED.every((k) => a[k] === b[k])

// 戻る・進むで前の結果を API を呼ばずにすぐ出すため、最近の結果を条件ごとに覚えておく（画面の中だけ。再読み込みで消える）。
// 以前は戻った直後に、直前の検索の結果が残って見えていた（2026-11-11 霧島リンカさん。案 A）
const CACHE_SIZE = 20
const cache = new Map<string, { response: SearchResponse; hits: Hit[] }>()
const cacheKey = (p: SearchParams) => JSON.stringify({ ...p })
function remember() {
  if (!response.value || !shownFor) return
  const key = cacheKey(shownFor)
  cache.delete(key)
  cache.set(key, { response: response.value, hits: hits.value })
  while (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value!)
}

// fresh: 検索語や条件が変わった検索。タブ・関連語の切り替えは件数が変わらないので数え直さない。
// ただし表示中の結果と件数の条件が違えば（書きかけの語のままタブを押したとき）、数え直す
async function runSearch(fresh = true) {
  error.value = ''
  playingId.value = null
  if (!params.q.trim()) {
    response.value = null
    hits.value = []
    shownFor = null
    return
  }
  const seq = ++requestSeq
  const asked = { ...params }
  const prev = fresh || !shownFor || !sameCounts(asked, shownFor) ? null : response.value
  loading.value = true
  try {
    const page = await searchApi(asked, { limit: PAGE, counts: !prev })
    if (seq !== requestSeq) return // 古い検索の結果は捨てる
    const res = prev ? withCounts(page, prev) : (page as SearchResponse)
    response.value = res
    hits.value = res.hits
    shownFor = asked
    remember()
    if (prev) return
    // タグにならない語に検索し直したときは、関連語タブから 一致 に戻す
    if (params.tab === 'related' && !res.related) {
      selectTab('match', true)
      return
    }
    // 名前そのものの発言が無く呼び名だけで話されている人（星街すいせい など）は関連語タブを開く
    if (params.tab === 'match' && res.counts.match === 0 && res.counts.related > 0) {
      selectTab('related', true)
      return
    }
    // スパチャ読みの名前にしか出てこない語（リスナーの名前など）はそのタブを開く
    if (params.tab === 'match' && res.counts.match + res.counts.maybe === 0 && res.counts.readout > 0) {
      selectTab('readout', true)
      return
    }
  } catch (e) {
    if (seq !== requestSeq) return
    response.value = null
    hits.value = []
    shownFor = null
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

// 続きは表示中の結果と同じ条件で取る（検索欄の書きかけの語で次のページを取ると、別の検索の結果が混ざる）
async function loadMore() {
  const prev = response.value
  if (!prev?.cursor || !shownFor) return
  loadingMore.value = true
  const seq = requestSeq
  try {
    const page = await searchApi({ ...shownFor }, { cursor: prev.cursor, limit: PAGE, counts: false })
    if (seq !== requestSeq || response.value !== prev) return // 待つ間に別の検索・戻るがあった
    response.value = withCounts(page, prev)
    hits.value = [...hits.value, ...page.hits]
    remember()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loadingMore.value = false
  }
}

function submit() {
  view.value = params.tab
  params.word = ''
  pushUrl()
  runSearch()
}

// auto: 検索の結果で自動で切り替えたとき。今の履歴の項目を書き換える（戻るで前の検索に戻れるように）
function selectTab(tab: Tab | 'fav', auto = false) {
  view.value = tab
  playingId.value = null
  if (tab === 'fav') return
  params.tab = tab
  params.word = ''
  pushUrl(auto)
  runSearch(false)
}

function selectWord(word: string) {
  params.word = params.word === word ? '' : word
  pushUrl()
  runSearch(false)
}

// ホロメンの一覧から: いちばんよく呼ぶ呼び名で検索し、関連語タブを開く
function onMember(m: Member) {
  view.value = 'related'
  params.q = m.nickname
  params.tab = 'related'
  params.word = ''
  pushUrl()
  runSearch()
}

function searchExample(q: string) {
  params.q = q
  submit()
}

function onPlay(hit: Hit, sec: number | null) {
  playingId.value = sec === null ? null : hit.id
}

function onPopState() {
  readUrl()
  view.value = params.tab
  playingId.value = null
  error.value = ''
  const saved = cache.get(cacheKey(params))
  if (saved) {
    requestSeq++ // 走っている検索の結果で上書きしない
    loading.value = false
    response.value = saved.response
    hits.value = saved.hits
    shownFor = { ...params }
    return
  }
  // 覚えていない検索は、前の結果を消してから取り直す（古い結果が一瞬見えないように）
  response.value = null
  hits.value = []
  runSearch()
}

onMounted(async () => {
  window.addEventListener('popstate', onPopState)
  void loadMembers() // 「ホロメンから探す」を押してから待たないよう、先に読んでおく（2026-11-12）
  if (params.q) runSearch()
  try {
    stats.value = await statsApi()
  } catch {
    stats.value = null
  }
})
onBeforeUnmount(() => window.removeEventListener('popstate', onPopState))
</script>

<template>
  <header class="bar">
    <div class="bar-inner">
      <a class="brand" href="/" aria-label="AZKi Moment Search（トップへ）">
        <span class="mark">AZKi</span> Moment Search
        <span class="beta">β</span>
      </a>
      <nav class="bar-actions" aria-label="サイト">
        <button
          type="button"
          class="icon-btn"
          :class="{ active: view === 'fav' }"
          :aria-pressed="view === 'fav'"
          title="お気に入り"
          @click="selectTab(view === 'fav' ? params.tab : 'fav')"
        >
          ★<span class="num">{{ favorites.length }}</span>
        </button>
        <button
          type="button"
          class="icon-btn"
          :aria-label="theme === 'dark' ? '明るい配色にする' : '暗い配色にする'"
          :title="theme === 'dark' ? '明るい配色にする' : '暗い配色にする'"
          @click="toggleTheme"
        >
          {{ theme === 'dark' ? '☀' : '☾' }}
        </button>
        <button
          type="button"
          class="icon-btn"
          title="このサイトについて"
          aria-label="このサイトについて"
          @click="about?.open()"
        >
          ?
        </button>
      </nav>
    </div>
  </header>

  <div class="page">
    <!-- 最初の画面は見出し・説明・収録データの数字を大きく出し、検索した後は検索欄だけにする（2026-11-12 霧島リンカさん。殺風景さの改善） -->
    <section class="hero" :class="{ compact }">
      <div v-if="!compact" class="hero-text">
        <p class="lead">AZKi さんの配信の字幕から発言を探して、<wbr />その場面から再生できる非公式のファンツールです。</p>
        <p v-if="stats" class="scale">
          {{ stats.streams.toLocaleString() }} 本の配信、{{ stats.utterances.toLocaleString() }} 件の発言から探せます
          <span class="built">（{{ stats.built_at?.slice(0, 10).replaceAll('-', '/') }} 時点）</span>
        </p>
      </div>
      <div class="search-card">
        <SearchForm
          ref="form"
          v-model="params"
          :counts="response?.counts ?? null"
          @search="submit"
          @member="onMember"
          @tab="selectTab"
        />
      </div>
      <MemberChips v-if="!compact" @pick="onMember" @more="form?.openMembers()" />
    </section>

    <!-- PC では結果の右に使い方を並べる。スマホでは 1 列（使い方は「このサイトについて」） -->
    <div class="layout">
    <div class="main-col">
    <!-- 検索する前は出さない（タブが 1 つだけ浮いて見えるため。2026-11-12） -->
    <nav v-if="view !== 'fav' && (response || loading)" class="tabs" aria-label="検索結果の種類">
      <button
        v-for="t in shownTabs"
        :key="t.id"
        type="button"
        :class="{ active: view === t.id }"
        :aria-current="view === t.id"
        @click="selectTab(t.id)"
      >
        {{ t.label }}
        <span v-if="response" class="count">{{ response.counts[t.id].toLocaleString() }}</span>
      </button>
      <button v-if="!isSubtitle" type="button" class="back" @click="selectTab('match')">字幕の結果に戻る</button>
    </nav>
    <p v-if="view !== 'fav' && response && otherTabs.length" class="others">
      <span>ほかの結果:</span>
      <button v-for="t in otherTabs" :key="t.id" type="button" @click="selectTab(t.id)">
        {{ t.label }} {{ response.counts[t.id].toLocaleString() }}
      </button>
    </p>
    <!-- v-else にすると、ほかの結果が無いときや断られたとき（0 件・2 文字の完全一致）にお気に入りの見出しが出てしまう -->
    <div v-if="view === 'fav'" class="fav-head">
      <h2>★ お気に入り</h2>
      <button type="button" class="link" @click="selectTab(params.tab)">検索結果に戻る</button>
    </div>

    <section class="results" aria-live="polite">
      <p v-if="view !== 'fav' && response" class="tab-note">
        {{ tabs.find((t) => t.id === view)?.note }}
        <span v-if="readings" class="readings">読み: {{ readings }}</span>
      </p>
      <p v-else-if="view === 'fav'" class="tab-note">このブラウザに保存したお気に入りです。</p>

      <div v-if="view === 'related' && response?.related" class="word-chips">
        <button
          v-for="w in response.related.words"
          :key="w.word"
          type="button"
          :class="{ active: params.word === w.word }"
          :aria-pressed="params.word === w.word"
          @click="selectWord(w.word)"
        >
          {{ w.word }} <span class="count">{{ w.count.toLocaleString() }}</span>
        </button>
      </div>

      <p v-if="error" class="message error">{{ error }}</p>
      <p v-else-if="loading" class="message">検索中…</p>
      <div v-else-if="view !== 'fav' && !params.q.trim()" class="welcome">
        <p>かな・カタカナでも、漢字や英語の言葉を探せます。</p>
        <div class="examples">
          <span>例:</span>
          <button v-for="q in examples" :key="q" type="button" @click="searchExample(q)">{{ q }}</button>
        </div>
      </div>
      <p v-else-if="shown.length === 0 && view === 'fav'" class="message">お気に入りはまだありません。結果の ☆ で追加できます。</p>
      <div v-else-if="shown.length === 0" class="message">
        <p>見つかりませんでした。</p>
        <!-- 次に試せることを出す（2026-11-11。0 件の文だけだと次に何をすればいいか分からないという指摘） -->
        <ul class="hints">
          <li v-if="params.exact">完全一致を外すと、表記が少し違うものも探せます</li>
          <li v-if="filtered">条件（タイトル・日付）の絞り込みを外してみてください</li>
          <li>ひらがな・カタカナで書いたり、短い言葉にしたりすると見つかることがあります</li>
        </ul>
      </div>

      <ol v-else class="list">
        <li v-for="h in shown" :key="h.id">
          <ResultCard :hit="h" :words="words" :playing="playingId === h.id" @play="(sec) => onPlay(h, sec)" />
        </li>
      </ol>

      <div v-if="hasMore && !loading" class="more">
        <button type="button" :disabled="loadingMore" @click="loadMore">
          {{ loadingMore ? '読み込み中…' : `さらに ${PAGE} 件読み込む` }}
        </button>
      </div>
    </section>
    </div>
    <SidePanel class="side-col" @about="about?.open()" />
    </div>

    <AppFooter @about="about?.open()" />
  </div>

  <AboutDialog ref="about" :stats="stats" />
</template>

<style scoped>
.bar {
  position: sticky;
  top: 0;
  z-index: 10;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(90deg, var(--bar-from), var(--bar-to));
  backdrop-filter: blur(8px);
}

.bar-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  max-width: var(--page-width);
  height: 52px;
  margin: 0 auto;
  padding: 0 16px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 0.02em;
  text-decoration: none;
  white-space: nowrap;
}

.brand .mark {
  color: var(--accent);
}

.beta {
  padding: 0 7px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
  line-height: 18px;
}

.bar-actions {
  display: flex;
  gap: 4px;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 3px;
  min-width: 36px;
  height: 36px;
  padding: 0 8px;
  border: none;
  border-radius: 999px;
  background: none;
  color: var(--text-muted);
  font-size: 16px;
  cursor: pointer;
}

.icon-btn:hover,
.icon-btn.active {
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.icon-btn .num {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

/* 幅 360px 未満（320px の端末）ではヘッダーが 1 行に収まらず右にはみ出していた（ISS-13、2026-11-11） */
@media (width < 360px) {
  .bar-inner {
    padding: 0 12px;
  }

  .brand {
    gap: 4px;
    font-size: 14px;
    letter-spacing: 0;
  }

  .bar-actions {
    gap: 0;
  }

  .icon-btn {
    min-width: 30px;
    padding: 0 5px;
  }
}

/* PC は幅を広げて結果と使い方を 2 列にする。スマホ・タブレットは 1 列（2026-11-12） */
.page {
  max-width: var(--page-width);
  margin: 0 auto;
  padding: 0 16px;
}

.hero {
  position: relative;
  margin: 20px 0 8px;
  padding: 36px clamp(16px, 4vw, 40px) 28px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: calc(var(--radius) + 8px);
  background:
    radial-gradient(circle at 92% 8%, var(--hero-glow) 0, transparent 42%),
    radial-gradient(circle at 4% 100%, var(--hero-glow-2) 0, transparent 38%),
    linear-gradient(135deg, var(--hero-from), var(--hero-to));
}

/* 飾りの輪（画像は使わない） */
.hero::after {
  position: absolute;
  top: -70px;
  right: -50px;
  width: 260px;
  height: 260px;
  border: 26px solid var(--hero-ring);
  border-radius: 50%;
  content: '';
  pointer-events: none;
}

.hero.compact {
  margin-top: 16px;
  padding: 16px clamp(12px, 3vw, 24px);
}

.hero.compact::after {
  display: none;
}

.hero-text {
  position: relative;
  z-index: 1;
  max-width: 780px;
  margin-bottom: 22px;
}

.lead {
  margin: 0;
  font-size: clamp(14px, 1.8vw, 17px);
}

.search-card {
  position: relative;
  z-index: 1;
}

.hero:not(.compact) .search-card {
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
}

.scale {
  margin: 12px 0 0;
  color: var(--accent-strong);
  font-size: 14px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.built {
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 400;
}

.layout {
  display: block;
}

.side-col {
  display: none;
}

@media (width >= 1024px) {
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 300px;
    gap: 28px;
    align-items: start;
  }

  .side-col {
    /* 使い方は画面より高いので、追従（sticky）させない。下まで読めなくなるため */
    display: grid;
    margin-top: 18px;
  }
}

.tabs {
  display: flex;
  gap: 6px;
  margin: 18px -16px 10px;
  padding: 2px 16px 6px;
  overflow-x: auto;
  scrollbar-width: none;
}

.tabs::-webkit-scrollbar {
  display: none;
}

.tabs button {
  flex: none;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 700;
  white-space: nowrap;
  cursor: pointer;
}

.tabs button.active {
  border-color: var(--accent);
  background: var(--accent);
  color: var(--on-accent);
}

.count {
  margin-left: 4px;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.tabs button.back {
  border: none;
  background: none;
  color: var(--accent-strong);
  font-weight: 400;
}

.others {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  margin: -4px 0 10px;
  font-size: 13px;
  color: var(--text-muted);
}

.others button {
  padding: 0;
  border: none;
  background: none;
  color: var(--accent-strong);
  text-decoration: underline;
  text-underline-offset: 3px;
  cursor: pointer;
}

.tabs button.active .count {
  background: rgb(255 255 255 / 25%);
  color: var(--on-accent);
}

.fav-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 18px 0 10px;
}

.fav-head h2 {
  margin: 0;
  font-size: 16px;
}

.link {
  padding: 0;
  border: none;
  background: none;
  color: var(--accent-strong);
  font-size: 13px;
  cursor: pointer;
}

.word-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0 0 12px;
}

.word-chips button {
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}

.word-chips button.active {
  border-color: var(--accent);
  color: var(--accent);
}

.tab-note {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--text-muted);
}

.readings {
  display: inline-block;
  margin-left: 8px;
}

.message {
  padding: 28px 0;
  text-align: center;
  color: var(--text-muted);
}

.message.error {
  color: var(--accent-strong);
}

.message p {
  margin: 0;
}

.hints {
  display: inline-block;
  margin: 12px 0 0;
  padding-left: 1.2em;
  font-size: 13px;
  text-align: left;
}

.welcome {
  margin-top: 18px;
  padding: 22px 16px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--text-muted);
  font-size: 13px;
  text-align: center;
}

.welcome p {
  margin: 0 0 10px;
}

.examples {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.examples button {
  padding: 4px 12px;
  border: none;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent-strong);
  cursor: pointer;
}

.examples button:hover {
  background: var(--mark-bg);
}

.list {
  display: grid;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.more {
  margin: 20px 0;
  text-align: center;
}

.more button {
  padding: 10px 28px;
  border: 2px solid var(--accent);
  border-radius: 999px;
  background: var(--surface);
  color: var(--accent-strong);
  font-weight: 700;
  cursor: pointer;
}

.more button:disabled {
  opacity: 0.6;
}
</style>
