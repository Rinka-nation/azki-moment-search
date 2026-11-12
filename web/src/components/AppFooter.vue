<script setup lang="ts">
// 必ず出す 4 点（非公式・誤りを含む・権利・削除依頼窓口）だけを短く。詳しくは「このサイトについて」
const emit = defineEmits<{ about: [] }>()
</script>

<template>
  <footer class="footer">
    <p>
      <span>非公式のファンツールです。</span><span>字幕と AI の修正には誤りがあります。</span><span>権利は各権利者に帰属します。</span>
    </p>
    <p class="links">
      <button type="button" class="link" @click="emit('about')">このサイトについて</button>
      <span aria-hidden="true">・</span>
      <span>削除依頼・ご意見: <a href="https://x.com/rinca_rain" target="_blank" rel="noopener noreferrer">X @rinca_rain</a></span>
    </p>
  </footer>
</template>

<style scoped>
.footer {
  margin-top: 40px;
  padding: 16px 0 28px;
  border-top: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
}

p {
  margin: 2px 0;
}

/* 文の途中で折り返さず、文の切れ目で折り返す */
p > span {
  display: inline-block;
}

.link {
  padding: 0;
  border: none;
  background: none;
  color: var(--accent-strong);
  text-decoration: underline;
  cursor: pointer;
}
</style>
