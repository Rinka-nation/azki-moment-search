import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Hit } from '@/api'

// 「✎ 直す」は手元の serve（/api/admin がある）で開いたときだけ出す（2026-11-12）
const hit: Hit = {
  kind: 'caption',
  id: 'u1',
  video_id: 'v',
  title: 't',
  upload_date: null,
  sec: 0,
  ms: 0,
  timestamp: '0:00',
  url: '',
  text: 'どんな門出を迎えても',
  text_raw: 'どんな文教を迎えても',
  label: '原文',
  tier: 0,
  candidates: [],
  related_words: [],
  quotes: [],
  segments: [{ text: 'どんな門出を迎えても' }],
}

const admin = vi.fn()
const save = vi.fn()
vi.mock('@/api', async (orig) => ({
  ...(await orig<typeof import('@/api')>()),
  adminApi: () => admin(),
  saveFixApi: (...a: unknown[]) => save(...a),
}))

afterEach(() => {
  vi.resetModules()
  admin.mockReset()
  save.mockReset()
})

async function card() {
  const { default: ResultCard } = await import('./ResultCard.vue')
  const w = mount(ResultCard, { props: { hit, words: [], playing: false } })
  await flushPromises()
  return w
}

describe('manual fix', () => {
  it('hides the edit button on the public site', async () => {
    admin.mockRejectedValue(new Error('not found'))
    const w = await card()
    expect(w.text()).not.toContain('✎ 直す')
  })

  it('saves a fix and shows it right away on the local serve', async () => {
    admin.mockResolvedValue({ admin: true })
    save.mockResolvedValue({ text: 'どんな分岐を迎えても', at: 'x' })
    const w = await card()
    const btn = w.findAll('button').find((b) => b.text() === '✎ 直す')!
    await btn.trigger('click')
    expect(w.get('.edit').text()).toContain('字幕の原文: どんな文教を迎えても')
    await w.get('textarea').setValue('どんな分岐を迎えても')
    await w.get('form.edit').trigger('submit')
    await flushPromises()
    expect(save).toHaveBeenCalledWith('u1', 'どんな分岐を迎えても', '')
    expect(w.get('.text').text()).toBe('どんな分岐を迎えても')
    expect(w.find('.edit').exists()).toBe(false)
  })
})
