import { ref } from 'vue'

export type Theme = 'light' | 'dark'

const KEY = 'azkims:theme'

function current(): Theme {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

const theme = ref<Theme>(current())

/** 配色の切り替え。初期値は index.html のスクリプトが localStorage から当てる（標準は light）。 */
export function useTheme() {
  function setTheme(t: Theme) {
    theme.value = t
    if (t === 'dark') document.documentElement.dataset.theme = 'dark'
    else delete document.documentElement.dataset.theme
    try {
      localStorage.setItem(KEY, t)
    } catch {
      // 保存できなくても切り替え自体は効く
    }
  }
  function toggle() {
    setTheme(theme.value === 'dark' ? 'light' : 'dark')
  }
  return { theme, setTheme, toggle }
}
