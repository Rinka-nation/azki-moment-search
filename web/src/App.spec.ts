import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Hit, SearchResponse, Tab } from './api'

// 検索の結果で自動でタブを変えたとき、履歴を積まない（NT-007、NT-008。ISS-03）
function response(counts: Partial<Record<Tab, number>>, related = false): SearchResponse {
  return {
    terms: [],
    related: related ? { tag: 'x', words: [] } : null,
    counts: { match: 0, related: 0, ts: 0, maybe: 0, readout: 0, ...counts },
    tiers: {},
    tab: 'match',
    limit: 20,
    cursor: null,
    has_more: false,
    hits: [],
  }
}

let next: SearchResponse
vi.mock('./api', async (orig) => ({
  ...(await orig<typeof import('./api')>()),
  searchApi: vi.fn(async () => next),
  statsApi: vi.fn(async () => ({
    streams: 1,
    utterances: 1,
    timestamp_entries: 1,
    date_from: null,
    date_to: null,
    built_at: null,
  })),
  membersApi: vi.fn(async () => ({ members: [] })),
}))

async function open(search: string) {
  history.replaceState(null, '', '/')
  history.pushState(null, '', search)
  const before = history.length
  const { default: App } = await import('./App.vue')
  const w = mount(App)
  await flushPromises()
  return { w, before }
}

afterEach(() => vi.resetModules())

describe('automatic tab switch', () => {
  it('replaces the entry when only related words match', async () => {
    next = response({ related: 133 }, true)
    const { w, before } = await open('/?q=x')
    expect(location.search).toBe('?q=x&tab=related')
    expect(history.length).toBe(before)
    w.unmount()
  })

  it('replaces the entry when only readout names match', async () => {
    next = response({ readout: 3 })
    const { w, before } = await open('/?q=y')
    expect(location.search).toBe('?q=y&tab=readout')
    expect(history.length).toBe(before)
    w.unmount()
  })
})

// 0 件のとき、お気に入りの見出しを出さず、次に試せることを出す（2026-11-11）
describe('empty result', () => {
  it('shows hints instead of the favorites heading', async () => {
    next = response({})
    const { w } = await open('/?q=xyz123&exact=1&from=2024-01-01')
    expect(w.text()).not.toContain('★ お気に入り')
    expect(w.text()).toContain('見つかりませんでした。')
    expect(w.text()).toContain('完全一致を外すと')
    expect(w.text()).toContain('絞り込みを外して')
    w.unmount()
  })
})

// 戻る・進むでは、覚えている結果を API を呼ばずにすぐ出す。覚えていない検索は前の結果を消してから取り直す（2026-11-11。案 A）
describe('back and forward', () => {
  const hit = (text: string): Hit => ({
    kind: 'caption',
    id: text,
    video_id: 'v',
    title: 't',
    upload_date: null,
    sec: 0,
    ms: 0,
    timestamp: '0:00',
    url: '',
    text,
    text_raw: text,
    label: '一致',
    tier: 0,
    candidates: [],
    related_words: [],
    quotes: [],
    segments: [{ text }],
  })
  const withHit = (text: string) => ({ ...response({ match: 1 }), hits: [hit(text)] })

  it('restores a remembered search without calling the API', async () => {
    next = withHit('最初の発言')
    const { w } = await open('/?q=a')
    const { searchApi } = await import('./api')
    expect(w.text()).toContain('最初の発言')

    next = withHit('次の発言')
    history.pushState(null, '', '/?q=b')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await w.vm.$nextTick()
    expect(w.text()).not.toContain('最初の発言') // 取り直す間、前の結果を残さない
    await flushPromises()
    expect(w.text()).toContain('次の発言')

    const calls = vi.mocked(searchApi).mock.calls.length
    history.replaceState(null, '', '/?q=a')
    window.dispatchEvent(new PopStateEvent('popstate'))
    await w.vm.$nextTick()
    expect(w.text()).toContain('最初の発言')
    expect(w.text()).not.toContain('次の発言')
    expect(vi.mocked(searchApi).mock.calls.length).toBe(calls)
    w.unmount()
  })
})

// 最初の画面: 見出しと収録データの数字を出し、タブは出さない。PC 用の使い方は常に DOM にある（表示は CSS で PC だけ）（2026-11-12）
describe('first screen', () => {
  it('shows the hero with the scale and no tabs before searching', async () => {
    const { w } = await open('/')
    expect(w.find('h1.catch').exists()).toBe(true)
    expect(w.find('.scale').text()).toContain('本の配信')
    expect(w.find('.tabs').exists()).toBe(false)
    expect(w.find('aside.side').text()).toContain('このサイトでの探し方')
    w.unmount()
  })
})

// ホロメンの一覧は、サイトを開いた時点で 1 回だけ読む。最初の画面に関連語の多い順で並べる（2026-11-12）
describe('members', () => {
  it('loads members when the page opens and shows 8 of them at random', async () => {
    const api = await import('./api')
    const m = (nickname: string, count: number) => ({ name: nickname, mark: '', nickname, count })
    const all = Array.from({ length: 12 }, (_, i) => m(`m${i}`, i))
    vi.mocked(api.membersApi).mockResolvedValueOnce({ members: all })
    const before = vi.mocked(api.membersApi).mock.calls.length
    const { w } = await open('/')
    expect(vi.mocked(api.membersApi).mock.calls.length).toBe(before + 1)
    const chips = w.findAll('.chips li button').map((b) => b.text())
    const shown = chips.slice(0, -1)
    expect(shown).toHaveLength(8)
    expect(new Set(shown).size).toBe(8)
    expect(shown.every((s) => all.some((x) => x.nickname === s))).toBe(true)
    expect(chips.at(-1)).toContain('すべてのホロメン（12 人）')
    w.unmount()
  })
})
