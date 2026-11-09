// Vérifie que chaque classe utilisée dans index.html/app.js est présente dans style.css.
const fs = require('fs');
const dir = 'C:\\Users\\cneuv\\projects\\leboncoin-alertes\\src\\lbc_alertes\\web\\static';
const css = fs.readFileSync(dir + '\\style.css', 'utf8');
const used = new Set();
for (const f of ['index.html', 'app.js']) {
  const t = fs.readFileSync(dir + '\\' + f, 'utf8');
  for (const m of t.matchAll(/class(?:Name)?\s*=\s*['"`]([^'"`]+)['"`]/g)) {
    m[1].split(/\s+/).forEach((c) => c && used.add(c));
  }
  for (const m of t.matchAll(/classList\.(?:add|toggle|remove)\(\s*'([^']+)'/g)) used.add(m[1]);
  for (const m of t.matchAll(/className\s*=\s*'([^']*)'/g)) m[1].split(/\s+/).forEach((c) => c && used.add(c));
}
const missing = [...used].filter((c) => !new RegExp('\\.' + c.replace(/[-]/g, '\\-') + '\\b').test(css));
console.log('used=' + used.size + ' missing=' + missing.join(','));
