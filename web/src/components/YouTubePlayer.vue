<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ videoId: string; start: number }>()

// 埋め込みは Cookie を使わない youtube-nocookie.com を使う
const src = computed(
  () =>
    `https://www.youtube-nocookie.com/embed/${encodeURIComponent(props.videoId)}` +
    `?start=${Math.max(0, Math.floor(props.start))}&autoplay=1&rel=0`,
)
</script>

<template>
  <div class="player">
    <iframe
      :key="src"
      :src="src"
      title="YouTube プレイヤー"
      allow="autoplay; encrypted-media; picture-in-picture"
      allowfullscreen
    ></iframe>
  </div>
</template>

<style scoped>
.player {
  position: relative;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border-radius: var(--radius-sm);
  background: #000;
}

iframe {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
}
</style>
