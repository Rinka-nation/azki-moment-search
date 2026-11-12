// 検索 API の型と呼び出し。
// ローカルは Python（azkims serve）、公開側は Vercel の Python 関数（同じハンドラ、DB は Turso）が同じ形式で返す。

export type Tab = 'match' | 'related' | 'ts' | 'maybe' | 'readout'
export type Sort = 'new' | 'old' | 'relevance'

export interface SearchTerm {
  surface: string
  reading: string
}

export interface Candidate {
  surface: string
  canonical: string
  distance: number
}

/** 表示テキストの断片。raw がある断片は補正箇所（raw = 字幕原文）。
 *  kind: 辞書で補正 / 補正候補 / AI修正（LLM の修正） */
export interface Segment {
  text: string
  raw?: string
  kind?: 'confirmed' | 'candidate' | 'ai'
}

export interface Hit {
  kind: 'caption' | 'timestamp'
  id: string
  video_id: string
  title: string
  upload_date: string | null
  sec: number
  ms: number
  timestamp: string
  url: string
  text: string
  text_raw: string
  label: string
  tier: number
  candidates: Candidate[]
  related_words: string[]
  quotes: string[]
  segments: Segment[]
}

/** 検索語がホロメン・ユニットの名前のとき。words は関連語タブの語ごとの件数（多い順）。 */
export interface RelatedInfo {
  tag: string
  words: { word: string; count: number }[]
}

export interface SearchResponse {
  terms: SearchTerm[]
  related: RelatedInfo | null
  counts: Record<Tab, number>
  tiers: Record<string, number>
  tab: Tab
  limit: number
  /** 続きを取るときに渡す位置。続きが無ければ null */
  cursor: string | null
  has_more: boolean
  hits: Hit[]
}

/** counts: false で取ったページ。件数・関連語・tier は数えないので null（最初の検索の値を使い続ける） */
export type SearchPage = Omit<SearchResponse, 'counts' | 'tiers'> & {
  counts: Record<Tab, number> | null
  tiers: Record<string, number> | null
}

export interface ContextUtterance {
  id: number
  start_ms: number
  text: string
  text_raw: string
  ai?: boolean // text が AI修正 を当てたもの
}

export interface Stats {
  streams: number
  utterances: number
  timestamp_entries: number
  date_from: string | null
  date_to: string | null
  built_at: string | null
}

export interface SearchParams {
  q: string
  tab: Tab
  word: string
  title: string
  from: string
  to: string
  sort: Sort
  exact: boolean
}

export class ApiError extends Error {}

async function getJson<T>(path: string, params: Record<string, string | number>): Promise<T> {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== '' && v !== undefined) qs.set(k, String(v))
  }
  const res = await fetch(`${path}?${qs}`)
  const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }))
  if (!res.ok) throw new ApiError(body.error ?? `HTTP ${res.status}`)
  return body as T
}

/** 検索。counts: false なら件数を数えない（読み取りがほぼ半分になる。さらに読み込む・タブの切り替え用） */
export function searchApi(
  params: SearchParams,
  { cursor = '', limit = 20, counts = true }: { cursor?: string; limit?: number; counts?: boolean } = {},
): Promise<SearchPage> {
  const { exact, ...rest } = params
  return getJson('/api/search', { ...rest, exact: exact ? 1 : '', cursor, limit, counts: counts ? '' : 0 })
}

/** 件数を数えずに取ったページを、最初の検索の件数・関連語と合わせる */
export function withCounts(page: SearchPage, prev: SearchResponse): SearchResponse {
  return {
    ...page,
    counts: page.counts ?? prev.counts,
    related: page.counts ? page.related : prev.related,
    tiers: page.tiers ?? prev.tiers,
  }
}

export function contextApi(videoId: string, ms: number): Promise<{ utterances: ContextUtterance[] }> {
  return getJson('/api/context', { video_id: videoId, ms })
}

export function statsApi(): Promise<Stats> {
  return getJson('/api/stats', {})
}

/** 「ホロメンから探す」の 1 人分。nickname は AZKi の字幕にいちばん多く出る呼び名（これで検索する） */
export interface Member {
  name: string
  mark: string
  nickname: string
  count: number
}

export function membersApi(): Promise<{ members: Member[] }> {
  return getJson('/api/members', {})
}
