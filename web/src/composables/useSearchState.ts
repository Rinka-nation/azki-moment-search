import { reactive } from 'vue'

import type { SearchParams, Sort, Tab } from '@/api'

const TABS: Tab[] = ['match', 'related', 'ts', 'maybe', 'readout']
// 関連度順は画面から外した（中身が短い発言順・関連語の数の順で、名前と合わないため。2026-11-10 霧島リンカさん）。
// 古い URL の sort=relevance は新しい順に読み替える。API と CLI は今も受け付ける
const SORTS: Sort[] = ['new', 'old']

export function emptyParams(): SearchParams {
  return { q: '', tab: 'match', word: '', title: '', from: '', to: '', sort: 'new', exact: false }
}

/** URL のクエリ文字列 ⇔ 検索条件。検索結果の URL をそのまま保存・共有できるようにする。 */
export function paramsFromUrl(search: string): SearchParams {
  const qs = new URLSearchParams(search)
  const p = emptyParams()
  p.q = qs.get('q') ?? ''
  p.word = qs.get('word') ?? ''
  p.title = qs.get('title') ?? ''
  p.from = qs.get('from') ?? ''
  p.to = qs.get('to') ?? ''
  const tab = qs.get('tab') as Tab | null
  if (tab && TABS.includes(tab)) p.tab = tab
  const sort = qs.get('sort') as Sort | null
  if (sort && SORTS.includes(sort)) p.sort = sort
  p.exact = qs.get('exact') === '1'
  return p
}

export function paramsToUrl(p: SearchParams): string {
  const qs = new URLSearchParams()
  if (p.q) qs.set('q', p.q)
  if (p.tab !== 'match') qs.set('tab', p.tab)
  if (p.word) qs.set('word', p.word)
  if (p.title) qs.set('title', p.title)
  if (p.from) qs.set('from', p.from)
  if (p.to) qs.set('to', p.to)
  if (p.sort !== 'new') qs.set('sort', p.sort)
  if (p.exact) qs.set('exact', '1')
  const s = qs.toString()
  return s ? `?${s}` : location.pathname
}

export function useSearchState() {
  const params = reactive<SearchParams>(paramsFromUrl(location.search))
  // replace: 検索の結果で自動でタブを変えたときは履歴を積まない。積むと、戻った先でまた切り替わって戻れない（ISS-03）
  function pushUrl(replace = false) {
    const url = paramsToUrl(params)
    if (url === location.search) return
    if (replace) history.replaceState(null, '', url)
    else history.pushState(null, '', url)
  }
  function readUrl() {
    Object.assign(params, paramsFromUrl(location.search))
  }
  return { params, pushUrl, readUrl }
}
