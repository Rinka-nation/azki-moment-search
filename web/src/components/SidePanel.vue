<script setup lang="ts">
// PC だけで検索結果の右に出す「このサイトでの探し方」（2026-11-12 霧島リンカさん）。
// 手順を並べるのではなく、このサイトにしかない探し方を、結果のラベルと同じ色で見せる。
// スマホでは出さない。ページの下に並べず「このサイトについて」にまとめる決まり（2026-11-10）を、スマホでは守る
const emit = defineEmits<{ about: [] }>()
</script>

<template>
  <aside class="side" aria-labelledby="side-title">
    <section class="panel">
      <h2 id="side-title">このサイトでの探し方</h2>
      <ul class="ways">
        <li>
          <span class="tag match">読み</span>
          <div>
            <b>ひらがなで書いても見つかる</b>
            <p>「かいたくしゃ」で「開拓者」の発言も出ます。字幕の表記が違っても、読みで探します。</p>
          </div>
        </li>
        <li>
          <span class="tag match">関連語</span>
          <div>
            <b>ホロメンの名前から、話題まで</b>
            <p>名前で探すと、AZKi さんがよく使う呼び名や持ちネタの発言も、関連語のタブに出ます。</p>
          </div>
        </li>
        <li>
          <span class="tag ts">タイムスタンプ</span>
          <div>
            <b>作者のタイムスタンプも探せる</b>
            <p>「条件」の「探す結果」で選べます。件数があるときは、結果の上にも出ます。</p>
          </div>
        </li>
        <li>
          <span class="tag ai">下線</span>
          <div>
            <b>直した字幕には下線</b>
            <p>
              <span class="u solid">辞書</span>・<span class="u dotted">AI</span>・<span class="u dashed">候補</span>
              の 3 種類。元の字幕は「字幕原文を見る」で見られます。
            </p>
          </div>
        </li>
      </ul>
      <p class="foot">
        結果のラベルを押すと、意味が出ます。
        <button type="button" class="link" @click="emit('about')">このサイトについて</button>
      </p>
    </section>
  </aside>
</template>

<style scoped>
.side {
  display: grid;
  align-content: start;
}

.panel {
  padding: 18px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
}

h2 {
  margin: 0 0 14px;
  font-size: 16px;
}

.ways {
  display: grid;
  gap: 14px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.ways li {
  display: grid;
  gap: 4px;
}

.tag {
  justify-self: start;
  padding: 0 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  line-height: 20px;
}

.tag.match {
  background: var(--accent);
  color: var(--on-accent);
}

.tag.ts {
  background: var(--ts-soft);
  color: var(--ts);
}

.tag.ai {
  border: 1px solid var(--ai);
  background: var(--ai-soft);
  color: var(--ai);
}

b {
  font-size: 13px;
}

.ways p,
.foot {
  margin: 2px 0 0;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.65;
}

.foot {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.u {
  color: var(--text);
  text-decoration-line: underline;
  text-decoration-thickness: 2px;
  text-underline-offset: 3px;
}

.u.solid {
  text-decoration-color: var(--accent);
}

.u.dotted {
  text-decoration-style: dotted;
  text-decoration-color: var(--ai);
}

.u.dashed {
  text-decoration-style: dashed;
  text-decoration-color: var(--maybe);
}

.link {
  padding: 0;
  border: none;
  background: none;
  color: var(--accent-strong);
  font-size: 12px;
  text-decoration: underline;
  text-underline-offset: 3px;
  cursor: pointer;
}
</style>
