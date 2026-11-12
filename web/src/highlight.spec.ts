import { describe, expect, it } from 'vitest'

import { paramsFromUrl, paramsToUrl } from './composables/useSearchState'
import { hasRawToShow, highlight, isKanaOnly, type Piece } from './highlight'

describe('ruby display', () => {
  it('skips ruby for kana-only fixes', () => {
    expect(isKanaOnly({ text: 'ぺこら', mark: false, ruby: 'ペコラ' })).toBe(true)
    expect(isKanaOnly({ text: 'ホロ鯖', mark: false, ruby: 'ホロサバ' })).toBe(false)
  })

  it('offers the raw caption whenever a fix is more than kana', () => {
    const fix = (text: string, ruby: string): Piece => ({ text, mark: false, ruby, kind: 'ai' })
    const plain = (text: string): Piece => ({ text, mark: false })
    expect(hasRawToShow([plain('ホロ鯖でやってもいい?'), fix('連打', 'レンダー')])).toBe(true)
    expect(hasRawToShow([plain('え、'), fix('ぺこら', 'ペコラ')])).toBe(false)
    expect(hasRawToShow([plain('ふつうの発言')])).toBe(false)
  })
})

describe('highlight', () => {
  it('marks every occurrence, case-insensitively', () => {
    expect(highlight('GeoGuessrとgeoguessr', ['geoguessr'])).toEqual([
      { text: 'GeoGuessr', mark: true },
      { text: 'と', mark: false },
      { text: 'geoguessr', mark: true },
    ])
  })

  it('merges overlapping terms and leaves text alone without terms', () => {
    expect(highlight('開拓者のみんな', ['開拓', '拓者'])).toEqual([
      { text: '開拓者', mark: true },
      { text: 'のみんな', mark: false },
    ])
    expect(highlight('そのまま', [])).toEqual([{ text: 'そのまま', mark: false }])
  })
})

describe('search state URL', () => {
  it('round-trips and ignores unknown values', () => {
    const p = paramsFromUrl('?q=%E9%96%8B%E6%8B%93%E8%80%85&tab=ts&sort=old&from=2026-01-01')
    expect(p).toEqual({
      q: '開拓者',
      tab: 'ts',
      word: '',
      title: '',
      from: '2026-01-01',
      to: '',
      sort: 'old',
      exact: false,
    })
    expect(paramsToUrl(p)).toBe('?q=%E9%96%8B%E6%8B%93%E8%80%85&tab=ts&from=2026-01-01&sort=old')
    expect(paramsFromUrl('?tab=bogus&sort=x').tab).toBe('match')
    expect(paramsFromUrl('?q=a&sort=relevance').sort).toBe('new') // 画面から外した並び順
    expect(paramsToUrl({ ...p, exact: true })).toContain('exact=1')
  })
})

describe('pieces', () => {
  it('keeps corrected segments whole with ruby and highlights across segments', async () => {
    const { pieces } = await import('./highlight')
    const segs = [
      { text: 'ホロ' },
      { text: 'メン', raw: '面', kind: 'confirmed' as const },
      { text: 'の中に' },
    ]
    expect(pieces(segs, ['ホロメン'])).toEqual([
      { text: 'ホロ', mark: true },
      { text: 'メン', mark: true, ruby: '面', kind: 'confirmed' },
      { text: 'の中に', mark: false },
    ])
  })
})

describe('pieces with original spelling', () => {
  it('marks a corrected segment when the query matches its original (ruby) text', async () => {
    const { pieces } = await import('./highlight')
    const segs = [{ text: 'あずきち', raw: '水きち', kind: 'confirmed' as const }, { text: 'かわいい' }]
    expect(pieces(segs, ['水きち'])[0]).toEqual({
      text: 'あずきち',
      mark: true,
      ruby: '水きち',
      kind: 'confirmed',
    })
  })
})
