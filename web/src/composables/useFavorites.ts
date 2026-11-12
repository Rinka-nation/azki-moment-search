import { computed, ref } from 'vue'

import type { Hit } from '@/api'

const KEY = 'azkims:favorites'

export interface Favorite extends Hit {
  saved_at: string
}

/**
 * お気に入りを見分けるキー。Hit.id は DB を作り直すと変わるので使わない。
 * 同じ時刻に複数の発言があるため、時刻に原文も加える。原文は辞書を変えても変わらない。
 */
export function favoriteKey(hit: Hit): string {
  return `${hit.kind}:${hit.video_id}:${hit.ms}:${hit.text_raw}`
}

/** 保存済みの id をキーに揃える。古い番号の id が残っていても、ここで置き換わる。 */
export function normalizeFavorites(items: Favorite[]): Favorite[] {
  const byKey = new Map<string, Favorite>()
  for (const item of items) {
    const key = favoriteKey(item)
    if (!byKey.has(key)) byKey.set(key, { ...item, id: key })
  }
  return [...byKey.values()]
}

function load(): Favorite[] {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? normalizeFavorites(JSON.parse(raw) as Favorite[]) : []
  } catch {
    return [] // プライベートウィンドウ等で localStorage が使えない
  }
}

function save(items: Favorite[]) {
  try {
    localStorage.setItem(KEY, JSON.stringify(items))
  } catch {
    // 保存できなくても画面上は動かす
  }
}

const favorites = ref<Favorite[]>(load())

/** お気に入り（このブラウザ内にだけ保存）。全コンポーネントで同じ状態を共有する。 */
export function useFavorites() {
  const keys = computed(() => new Set(favorites.value.map(favoriteKey)))
  function isFavorite(hit: Hit) {
    return keys.value.has(favoriteKey(hit))
  }
  function toggle(hit: Hit) {
    const key = favoriteKey(hit)
    if (isFavorite(hit)) {
      favorites.value = favorites.value.filter((f) => favoriteKey(f) !== key)
    } else {
      favorites.value = [{ ...hit, id: key, saved_at: new Date().toISOString() }, ...favorites.value]
    }
    save(favorites.value)
  }
  return { favorites, isFavorite, toggle }
}
