<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { contextApi, type ContextUtterance } from '@/api'

const props = defineProps<{ videoId: string; ms: number }>()
const emit = defineEmits<{ seek: [sec: number] }>()

const items = ref<ContextUtterance[]>([])
const error = ref('')
const loading = ref(true)

function fmt(ms: number) {
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

onMounted(async () => {
  try {
    items.value = (await contextApi(props.videoId, props.ms)).utterances
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="context">
    <p v-if="loading" class="muted">読み込み中…</p>
    <p v-else-if="error" class="muted">前後の発言を取得できませんでした: {{ error }}</p>
    <p v-else-if="items.length === 0" class="muted">前後 30 秒に字幕がありません</p>
    <ol v-else>
      <li v-for="u in items" :key="u.id" :class="{ current: Math.abs(u.start_ms - ms) < 1000 }">
        <button type="button" :title="`${fmt(u.start_ms)} から再生`" @click="emit('seek', Math.max(0, u.start_ms / 1000 - 2))">
          {{ fmt(u.start_ms) }}
        </button>
        <span>{{ u.text }}<span v-if="u.ai" class="ai" :title="`AI修正。字幕では「${u.text_raw}」`">AI修正</span></span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.context {
  max-height: 260px;
  overflow-y: auto;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  font-size: 13px;
}

ol {
  margin: 0;
  padding: 0;
  list-style: none;
}

li {
  display: flex;
  gap: 8px;
  padding: 2px 4px;
  border-radius: 6px;
}

.ai {
  margin-left: 6px;
  padding: 0 5px;
  border: 1px solid var(--ai);
  border-radius: 999px;
  color: var(--ai);
  font-size: 11px;
  font-weight: 700;
}

li.current {
  background: var(--accent-soft);
  font-weight: 700;
}

li button {
  flex: none;
  padding: 0;
  border: none;
  background: none;
  color: var(--accent-strong);
  font-variant-numeric: tabular-nums;
  cursor: pointer;
}

.muted {
  margin: 0;
  color: var(--text-muted);
}
</style>
