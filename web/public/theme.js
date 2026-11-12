// 保存された配色を描画前に当てる（ちらつき防止）。標準は明るい配色。
// CSP で inline の script を許さないので、index.html に直接書かずに別ファイルにしている
try {
  if (localStorage.getItem('azkims:theme') === 'dark') document.documentElement.dataset.theme = 'dark'
} catch {}
