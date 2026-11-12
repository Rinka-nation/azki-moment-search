<script setup lang="ts">
import { computed } from 'vue'

import type { Member } from '@/api'
import { useMembers } from '@/composables/useMembers'

// 最初の画面に、話題に出ることの多いホロメンを並べる。このサイトにしかない探し方を前に出すため
// （2026-11-12 霧島リンカさん。数字のタイルは参考サイトと同じ項目だったのでやめた）
const SHOWN = 8
const emit = defineEmits<{ pick: [member: Member]; more: [] }>()

const { members } = useMembers()
const top = computed(() => [...(members.value ?? [])].sort((a, b) => b.count - a.count).slice(0, SHOWN))
</script>

<template>
  <div v-if="top.length" class="chips">
    <p class="label">ホロメンの名前から、呼び名や持ちネタまで探す</p>
    <ul>
      <li v-for="m in top" :key="m.name">
        <button type="button" :title="`${m.name}（${m.nickname} で検索）`" @click="emit('pick', m)">
          <span v-if="m.mark" class="mark" aria-hidden="true">{{ m.mark }}</span>{{ m.nickname }}
        </button>
      </li>
      <li>
        <button type="button" class="more" @click="emit('more')">すべてのホロメン（{{ members?.length }} 人）</button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.chips {
  position: relative;
  z-index: 1;
  margin-top: 18px;
}

.label {
  margin: 0 0 8px;
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 700;
}

ul {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  font-size: 14px;
  cursor: pointer;
}

button:hover {
  border-color: var(--accent);
  color: var(--accent-strong);
}

.mark {
  font-size: 15px;
}

.more {
  border-style: dashed;
  background: none;
  color: var(--accent-strong);
}
</style>
