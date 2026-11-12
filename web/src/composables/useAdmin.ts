import { ref } from 'vue'

import { adminApi } from '@/api'

// 手元の serve で開いたときだけ「✎ 直す」を出す（2026-11-12 霧島リンカさん）。
// 公開側（Vercel）には /api/admin が無いので、手元のアドレスのときだけ問い合わせる（本番で 404 を出さない）
const LOCAL_HOSTS = ['127.0.0.1', 'localhost']
const admin = ref(false)
let asked = false

export function useAdmin() {
  if (!asked && LOCAL_HOSTS.includes(location.hostname)) {
    asked = true
    adminApi()
      .then((r) => (admin.value = r.admin === true))
      .catch(() => (admin.value = false))
  }
  return { admin }
}
