import { describe, expect, it } from 'vitest'

import type { Hit } from '@/api'

import { favoriteKey, normalizeFavorites, type Favorite, useFavorites } from './useFavorites'

function hit(over: Partial<Hit> = {}): Hit {
  return {
    kind: 'caption',
    id: 'u1',
    video_id: 'vid',
    title: '配信',
    upload_date: '2026-01-01',
    sec: 10,
    ms: 10000,
    timestamp: '0:10',
    url: 'https://youtu.be/vid?t=10',
    text: '補正後',
    text_raw: '原文',
    label: '一致',
    tier: 0,
    candidates: [],
    related_words: [],
    quotes: [],
    segments: [],
    ...over,
  }
}

function saved(over: Partial<Hit> = {}): Favorite {
  return { ...hit(over), saved_at: '2026-01-01T00:00:00Z' }
}

describe('favoriteKey', () => {
  it('DB を作り直して id や補正後の本文が変わっても同じ', () => {
    expect(favoriteKey(hit({ id: 'u999', text: '別の補正' }))).toBe(favoriteKey(hit()))
  })

  it('同じ時刻の別の発言を区別する', () => {
    expect(favoriteKey(hit({ text_raw: '別の発言' }))).not.toBe(favoriteKey(hit()))
  })
})

describe('normalizeFavorites', () => {
  it('古い番号の id をキーに置き換える', () => {
    const [item] = normalizeFavorites([saved()])
    expect(item?.id).toBe(favoriteKey(hit()))
  })

  it('同じ番号でも別の発言なら両方残し、同じ発言は 1 件にする', () => {
    const items = normalizeFavorites([saved(), saved({ text_raw: '別の発言' }), saved({ id: 'u2' })])
    expect(items).toHaveLength(2)
    expect(new Set(items.map((i) => i.id)).size).toBe(2)
  })

  it('2 回通しても変わらない', () => {
    const once = normalizeFavorites([saved()])
    expect(normalizeFavorites(once)).toEqual(once)
  })
})

describe('useFavorites', () => {
  it('番号が振り直された同じ発言に ★ が付き、別の発言には付かない', () => {
    const { isFavorite, toggle } = useFavorites()
    toggle(hit({ id: 'u1' }))
    expect(isFavorite(hit({ id: 'u500' }))).toBe(true)
    expect(isFavorite(hit({ id: 'u1', text_raw: '別の発言' }))).toBe(false)
    toggle(hit({ id: 'u500' }))
    expect(isFavorite(hit({ id: 'u1' }))).toBe(false)
  })
})
