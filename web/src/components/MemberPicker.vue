<script lang="ts">
import type { Member } from '@/api'

// 開くたびに読み直さないよう、ページの中で 1 回だけ取る（コンポーネントの外に置く）
let cache: Member[] | null = null
</script>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { membersApi } from '@/api'

// 推しマークの一覧。押すと、その人を AZKi がいちばんよく呼ぶ呼び名で検索し、関連語タブを開く
const emit = defineEmits<{ pick: [member: Member] }>()

const members = ref<Member[] | null>(cache)
const error = ref('')

onMounted(async () => {
  if (cache) return
  try {
    cache = (await membersApi()).members
    members.value = cache
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
})

// 推しマークが無い人は名前の頭の 2 文字
function face(m: Member) {
  return m.mark || m.name.slice(0, 2)
}
</script>

<template>
  <div class="picker">
    <p class="hint">押すと、AZKi がよく使う呼び名で検索して、関連する話題も出します</p>
    <p v-if="error" class="hint">読み込めませんでした: {{ error }}</p>
    <p v-else-if="!members" class="hint">読み込み中…</p>
    <ul v-else class="grid">
      <li v-for="m in members" :key="m.name">
        <button type="button" :title="`${m.name}（${m.nickname} で検索）`" @click="emit('pick', m)">
          <span class="mark" :class="{ text: !m.mark }" aria-hidden="true">{{ face(m) }}</span>
          <span class="nick">{{ m.nickname }}</span>
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.picker {
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
}

.hint {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--text-muted);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(76px, 1fr));
  gap: 4px;
  max-height: 248px;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
  overscroll-behavior: contain;
}

.grid button {
  display: grid;
  justify-items: center;
  gap: 2px;
  width: 100%;
  padding: 6px 2px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: none;
  cursor: pointer;
}

.grid button:hover {
  border-color: var(--border);
  background: var(--surface-2);
}

.mark {
  font-family: 'Apple Color Emoji', 'Segoe UI Emoji', 'Noto Color Emoji', var(--font);
  font-size: 24px;
  line-height: 1.2;
  font-variant-emoji: emoji;
}

.mark.text {
  font-size: 14px;
  font-weight: 700;
  line-height: 29px;
  color: var(--accent-strong);
}

.nick {
  max-width: 100%;
  overflow: hidden;
  font-size: 11px;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
