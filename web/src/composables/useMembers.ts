import { ref } from 'vue'

import { type Member, membersApi } from '@/api'

// ホロメンの一覧。サイトを開いた時点で 1 回だけ取り、最初の画面のボタンと「ホロメンから探す」で使い回す。
// 以前は「ホロメンから探す」を押してから取っていたので、出るまで待たされた（2026-11-12 霧島リンカさん）。
// /api/members は build が保存した 1 行を返すだけで、CDN にも 1 日残る（読む量はほぼ増えない）
const members = ref<Member[] | null>(null)
const error = ref('')
let loading: Promise<void> | null = null

export function loadMembers(): Promise<void> {
  loading ??= membersApi()
    .then((r) => {
      members.value = r.members
    })
    .catch((e: unknown) => {
      error.value = e instanceof Error ? e.message : String(e)
      loading = null // 次に開いたときに取り直す
    })
  return loading
}

export function useMembers() {
  void loadMembers()
  return { members, error }
}
