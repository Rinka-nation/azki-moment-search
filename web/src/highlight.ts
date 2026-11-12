import type { Segment as TextSegment } from './api'

export interface Segment {
  text: string
  mark: boolean
}

/** 画面に並べる単位。ruby があれば補正箇所で、ruby は補正前の字幕原文（画面には並べず、下線の title と「字幕原文を見る」で見せる）。 */
export interface Piece {
  text: string
  mark: boolean
  ruby?: string
  kind?: 'confirmed' | 'candidate' | 'ai'
}

/** text の中の検索語（表記一致）に印を付けて分割する。大文字小文字は区別しない。
 *  読みだけで一致した場合は表記が違うので印は付かない（ラベルで示す）。 */
export function highlight(text: string, words: string[]): Segment[] {
  const needles = words.map((w) => w.toLowerCase()).filter((w) => w.length > 0)
  if (needles.length === 0) return [{ text, mark: false }]
  const lower = text.toLowerCase()
  const marked = new Array<boolean>(text.length).fill(false)
  for (const n of needles) {
    let i = lower.indexOf(n)
    while (i !== -1) {
      marked.fill(true, i, i + n.length)
      i = lower.indexOf(n, i + n.length)
    }
  }
  const out: Segment[] = []
  for (let i = 0; i < text.length; i++) {
    const last = out[out.length - 1]
    if (last && last.mark === marked[i]) last.text += text[i]
    else out.push({ text: text[i]!, mark: marked[i]! })
  }
  return out
}

const toHiragana = (s: string) => s.replace(/[ァ-ヶ]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0x60))

/** ひらがなとカタカナの違いだけの補正（ペコ → ぺこ）。原文を見せても読みが同じなので数えない（下線だけ）。 */
export function isKanaOnly(p: Piece): boolean {
  return p.ruby !== undefined && toHiragana(p.ruby) === toHiragana(p.text)
}

/** 「字幕原文を見る」を出すか。字幕原文はルビで本文の上に出すと、元の字幕なのに読み（ふりがな）の誤りに見えるので、
 *  補正のある発言はすべてボタンで全体を見せる（2026-11-11 霧島リンカさん。以前は補正の少ない発言だけルビ）。 */
export function hasRawToShow(ps: Piece[]): boolean {
  return ps.some((p) => p.ruby !== undefined && !isKanaOnly(p))
}

/** 補正の断片（ルビ付き）と検索語のハイライトを合わせる。
 *  ハイライトは表示テキスト全体で計算し、断片の境界で切り分ける。補正箇所は分割しない。 */
export function pieces(segments: TextSegment[], words: string[]): Piece[] {
  const full = segments.map((s) => s.text).join('')
  // 位置は UTF-16 の単位で数える（絵文字などでずれないよう、文字列の length に合わせる）
  const marks: boolean[] = []
  for (const h of highlight(full, words)) marks.push(...new Array<boolean>(h.text.length).fill(h.mark))
  const out: Piece[] = []
  let pos = 0
  for (const seg of segments) {
    const end = pos + seg.text.length
    if (seg.raw !== undefined) {
      // 補正前の原文（ルビ）に検索語がある場合も強調する（原文の表記で検索したとき）
      const rawLower = seg.raw.toLowerCase()
      const inRaw = words.some((w) => w && rawLower.includes(w.toLowerCase()))
      const mark = marks.slice(pos, end).some(Boolean) || inRaw
      out.push({ text: seg.text, mark, ruby: seg.raw, kind: seg.kind })
    } else {
      let i = pos
      while (i < end) {
        let j = i + 1
        while (j < end && marks[j] === marks[i]) j++
        out.push({ text: full.slice(i, j), mark: Boolean(marks[i]) })
        i = j
      }
    }
    pos = end
  }
  return out
}
