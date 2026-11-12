import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { Hit } from '@/api'

import ResultCard from './ResultCard.vue'

// ラベルを押すと意味が出る（スマホにはホバーが無いので。2026-11-12、外部の試用での指摘）
function hit(label: string, tier: number): Hit {
  return {
    kind: 'caption',
    id: '1',
    video_id: 'v',
    title: 't',
    upload_date: null,
    sec: 0,
    ms: 0,
    timestamp: '0:00',
    url: '',
    text: 'かいたくしゃ',
    text_raw: 'かいたくしゃ',
    label,
    tier,
    candidates: [],
    related_words: [],
    quotes: [],
    segments: [{ text: 'かいたくしゃ' }],
  }
}

describe('badge help', () => {
  it('explains the label on tap and hover', async () => {
    const w = mount(ResultCard, { props: { hit: hit('読み', 3), words: [], playing: false } })
    const badge = w.get('button.badge')
    expect(badge.attributes('title')).toContain('読みが検索語と同じ')
    expect(w.find('.help').exists()).toBe(false)
    await badge.trigger('click')
    expect(w.get('.help').text()).toContain('読みが検索語と同じ')
    await badge.trigger('click')
    expect(w.find('.help').exists()).toBe(false)
  })
})
