/* IHM locale Leboncoin Alertes : appels API, onglets, collecte, annonces, IA. */
'use strict';

/* ------------------------------------------------------------------ helpers */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

const eurFmt = new Intl.NumberFormat('fr-FR', { style: 'currency', currency: 'EUR' });
const dtFmt = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

/** Le prix arrive en centimes d'euro ; 0 / null = non précisé. */
function formatPrice(cents) {
  const n = Number(cents);
  if (!Number.isFinite(n) || n <= 0) return 'Prix non précisé';
  return eurFmt.format(n / 100);
}

function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : dtFmt.format(d);
}

/** Durée relative « maison » (aucune librairie). */
function relTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const delta = Math.round((Date.now() - d.getTime()) / 1000);
  const abs = Math.abs(delta);
  let unit;
  if (abs < 60) unit = 'quelques secondes';
  else if (abs < 3600) unit = Math.floor(abs / 60) + ' min';
  else if (abs < 86400) unit = Math.floor(abs / 3600) + ' h';
  else if (abs < 2592000) unit = Math.floor(abs / 86400) + ' j';
  else unit = Math.floor(abs / 2592000) + ' mois';
  return delta >= 0 ? 'il y a ' + unit : 'dans ' + unit;
}

/** Complète HH:MM:SS. */
function pad(n) {
  return String(n).padStart(2, '0');
}

function countdownText(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const ms = d.getTime() - Date.now();
  if (ms <= 0) return 'déclenchement imminent';
  const s = Math.floor(ms / 1000);
  return (
    'prochaine exécution dans ' +
    pad(Math.floor(s / 3600)) +
    ':' +
    pad(Math.floor((s % 3600) / 60)) +
    ':' +
    pad(s % 60)
  );
}

function truncate(text, max) {
  const t = String(text || '');
  return t.length > max ? t.slice(0, max).trimEnd() + '…' : t;
}

/* -------------------------------------------------------------------- toasts */

const toastsHost = $('#toasts');

function toast(message, kind) {
  const node = el('div', 'toast ' + (kind === 'error' ? 'toast-error' : 'toast-ok'));
  node.textContent = String(message);
  toastsHost.appendChild(node);
  setTimeout(() => {
    node.classList.add('toast-out');
    setTimeout(() => node.remove(), 250);
  }, 4000);
}

/* ---------------------------------------------------------------- API client */

const apiErrorBanner = $('#api-error');
let apiFailing = false;

function setApiError(on) {
  if (on === apiFailing) return;
  apiFailing = on;
  apiErrorBanner.hidden = !on;
}

async function request(path, options) {
  const opts = options || {};
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = null;
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || 'Erreur HTTP ' + res.status;
    const e = new Error(msg);
    e.data = data;
    throw e;
  }
  return data || {};
}

function getJson(path) {
  return request(path, { method: 'GET', headers: { Accept: 'application/json' } });
}

function postJson(path, body) {
  return request(path, {
    method: 'POST',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/** POST générique : toast de succès/erreur + rechargement de l'état. */
async function callPost(path, body, okMessage) {
  try {
    const data = await postJson(path, body);
    if (data.ok === false) {
      toast(data.error || 'Opération refusée', 'error');
      return null;
    }
    if (okMessage) toast(typeof okMessage === 'function' ? okMessage(data) : okMessage);
    return data;
  } catch (err) {
    toast(err.message || 'Appel API impossible', 'error');
    return null;
  } finally {
    refreshState();
  }
}

/* --------------------------------------------------------------------- état */

const S = {
  state: null,
  active: null,
  panels: new Map(), // nom de recherche -> panneau
  directivesLoaded: false,
};

const panelsHost = $('#panels');
const tabsHost = $('#tabs');
const tplSearch = $('#tpl-search-panel');
const tplAd = $('#tpl-ad-card');

const FIXED_TABS = [
  { key: 'collector', label: 'Collecte' },
  { key: 'directives', label: 'Directives IA' },
  { key: 'settings', label: 'Réglages' },
];

function panelIdFor(key) {
  if (key.indexOf('search:') === 0) {
    return 'panel-search-' + encodeURIComponent(key.slice(7));
  }
  return 'panel-' + key;
}

function keyForPanelNode(node) {
  return node.dataset.search ? 'search:' + node.dataset.search : node.id.slice(6);
}

/* -------------------------------------------------------------------- onglets */

function showPanel(key) {
  S.active = key;
  $$('.panel', panelsHost).forEach((p) => {
    p.hidden = keyForPanelNode(p) !== key;
  });
  $$('.tab', tabsHost).forEach((t) => {
    const on = t.dataset.tab === key;
    t.classList.toggle('is-active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (key === 'collector') loadLogs();
  if (key === 'directives') loadDirectives();
  if (key.indexOf('search:') === 0) loadAds(key.slice(7));
  try {
    localStorage.setItem('lbc.activeTab', key);
  } catch (err) {
    /* stockage indisponible : sans conséquence */
  }
}

tabsHost.addEventListener('click', (ev) => {
  const tab = ev.target.closest('.tab');
  if (tab && tab.dataset.tab) showPanel(tab.dataset.tab);
});

/** Panneau d'une recherche, créé à la demande et inséré avant « Collecte ». */
function ensureSearchPanel(name) {
  const existing = S.panels.get(name);
  if (existing) return existing;

  const node = tplSearch.content.firstElementChild.cloneNode(true);
  node.id = panelIdFor('search:' + name);
  node.dataset.search = name;

  const p = {
    name,
    node,
    filter: 'all',
    q: '',
    ads: [],
    expanded: new Set(),
    loaded: false,
  };

  const counts = node.querySelector('.counts');
  counts.textContent = '';

  node.querySelector('.segmented').addEventListener('click', (ev) => {
    const btn = ev.target.closest('.seg');
    if (!btn) return;
    p.filter = btn.dataset.filter;
    $$('.seg', node).forEach((b) => b.classList.toggle('is-active', b === btn));
    loadAds(name);
  });

  const q = node.querySelector('.q');
  let qTimer = null;
  q.addEventListener('input', () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      p.q = q.value.trim();
      loadAds(name);
    }, 300);
  });

  node.addEventListener('click', (ev) => onAdClick(ev, p));

  panelsHost.insertBefore(node, $('#panel-collector'));
  S.panels.set(name, p);
  return p;
}

function renderTabs() {
  const searches = (S.state && S.state.searches) || [];
  tabsHost.textContent = '';

  for (const s of searches) {
    const key = 'search:' + s.name;
    const tab = el('button', 'tab');
    tab.type = 'button';
    tab.dataset.tab = key;
    tab.setAttribute('role', 'tab');
    tab.appendChild(el('span', 'tab-name', s.name));
    const badge = el('span', 'tab-badge', s.retenu == null ? 0 : s.retenu);
    badge.title = 'Annonces retenues';
    tab.appendChild(badge);
    tabsHost.appendChild(tab);
    ensureSearchPanel(s.name);
  }

  for (const f of FIXED_TABS) {
    const tab = el('button', 'tab tab-fixed');
    tab.type = 'button';
    tab.dataset.tab = f.key;
    tab.setAttribute('role', 'tab');
    tab.appendChild(el('span', 'tab-name', f.label));
    tabsHost.appendChild(tab);
  }

  if (!S.active) {
    let initial = 'collector';
    try {
      const saved = localStorage.getItem('lbc.activeTab');
      if (saved && (saved === 'collector' || saved === 'directives' || saved === 'settings')) {
        initial = saved;
      } else if (saved && saved.indexOf('search:') === 0 && S.panels.has(saved.slice(7))) {
        initial = saved;
      }
    } catch (err) {
      /* stockage indisponible */
    }
    showPanel(initial);
  } else {
    syncActiveTab();
  }
}

function syncActiveTab() {
  $$('.tab', tabsHost).forEach((t) => {
    const on = t.dataset.tab === S.active;
    t.classList.toggle('is-active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

/* --------------------------------------------------------- bandeau d'état haut */

function renderBanner() {
  const st = S.state;
  if (!st) return;
  const c = st.collector || {};
  const ai = st.ai || {};

  const dot = $('#collector-dot');
  dot.className = 'dot ' + (c.running ? 'dot-on' : 'dot-off');
  $('#collector-status').textContent = c.running ? 'Collecteur en marche' : 'Collecteur en pause';

  $('#next-run').textContent = c.next_run_at ? formatDate(c.next_run_at) : '—';
  $('#countdown').textContent = c.running && c.next_run_at ? ' (' + countdownText(c.next_run_at) + ')' : '';

  $('#ai-model').textContent = ai.model || (ai.enabled ? 'activée' : 'désactivée');
  $('#ai-model').title = ai.base_url || '';
  $('#ai-dot').className = 'dot dot-sm ' + (ai.reachable ? 'dot-on' : 'dot-off');
  $('#chip-ai').title = ai.reachable ? 'Serveur IA joignable' : 'Serveur IA injoignable';

  const tg = st.telegram || {};
  $('#chip-telegram').hidden = Boolean(tg.configured);
}

$('#chip-telegram').addEventListener('click', () => showPanel('settings'));

/* -------------------------------------------------------------- onglet Collecte */

function renderCollector() {
  const st = S.state;
  if (!st) return;
  const c = st.collector || {};

  $('#c-status').textContent = c.running ? 'En marche' : 'En pause';
  $('#c-last').textContent = c.last_run_at
    ? formatDate(c.last_run_at) + ' (' + relTime(c.last_run_at) + ')'
    : 'jamais';
  $('#c-next').textContent = c.next_run_at
    ? formatDate(c.next_run_at) + ' (' + countdownText(c.next_run_at) + ')'
    : '—';
  $('#c-interval').textContent = c.poll_interval_min ? c.poll_interval_min + ' minutes' : '—';
  $('#c-started').textContent = c.started_at ? formatDate(c.started_at) : '—';

  const r = c.last_result;
  $('#c-result').textContent = r
    ? r.fetched + ' annonces vues · ' + r.kept + ' retenues · ' + r.new + ' nouvelles · ' +
      r.searches + ' recherche' + (r.searches > 1 ? 's' : '') +
      (r.blocked ? ' · collecte bloquée' : '')
    : '—';

  const runBtn = $('#btn-run-now');
  const toggleBtn = $('#btn-toggle');
  if (c.busy) {
    runBtn.disabled = true;
    runBtn.textContent = 'Collecte en cours…';
    toggleBtn.disabled = true;
  } else {
    runBtn.disabled = false;
    runBtn.textContent = 'Lancer maintenant';
    toggleBtn.disabled = false;
  }
  toggleBtn.textContent = c.running ? 'Arrêter' : 'Démarrer';

  const interval = c.poll_interval_min;
  if (interval) {
    const a = $('#interval-input');
    const b = $('#set-interval');
    if (document.activeElement !== a) a.value = interval;
    if (document.activeElement !== b) b.value = interval;
  }
}

$('#btn-run-now').addEventListener('click', () => {
  callPost('/api/collector/run-now', {}, (d) => d.message || 'Collecte déclenchée');
});

$('#btn-toggle').addEventListener('click', () => {
  const running = S.state && S.state.collector && S.state.collector.running;
  callPost(running ? '/api/collector/stop' : '/api/collector/start', {}, () =>
    running ? 'Collecteur arrêté' : 'Collecteur démarré'
  );
});

function saveInterval(input) {
  const minutes = Number(input.value);
  if (!Number.isFinite(minutes) || minutes < 30) {
    toast('Intervalle minimum : 30 minutes', 'error');
    return;
  }
  callPost('/api/collector/interval', { minutes: Math.round(minutes) }, (d) =>
    'Fréquence enregistrée : ' + d.minutes + ' minutes'
  );
}

$('#btn-interval').addEventListener('click', () => saveInterval($('#interval-input')));
$('#btn-set-interval').addEventListener('click', () => saveInterval($('#set-interval')));

/* ------------------------------------------------------------------- journal */

const logHost = $('#log');

function logLevel(line) {
  if (/\bERROR\b|\bCRITICAL\b/i.test(line)) return 'log-error';
  if (/\bWARNING\b|\bWARN\b/i.test(line)) return 'log-warn';
  if (/\bDEBUG\b/i.test(line)) return 'log-debug';
  return 'log-info';
}

async function loadLogs() {
  try {
    const data = await getJson('/api/logs?limit=300');
    setApiError(false);
    const lines = data.lines || [];
    const nearBottom =
      logHost.scrollTop + logHost.clientHeight >= logHost.scrollHeight - 40;
    logHost.textContent = '';
    const frag = document.createDocumentFragment();
    for (const line of lines) {
      frag.appendChild(el('div', 'log-line ' + logLevel(String(line)), line));
    }
    logHost.appendChild(frag);
    if (nearBottom) logHost.scrollTop = logHost.scrollHeight;
    $('#log-info').textContent = lines.length + ' ligne' + (lines.length > 1 ? 's' : '');
  } catch (err) {
    setApiError(true);
  }
}

$('#btn-log-refresh').addEventListener('click', loadLogs);

/* -------------------------------------------------------------- liste d'annonces */

const VERDICT_LABEL = { retenu: 'Retenue', rejete: 'Écartée' };

function searchInfo(name) {
  const list = (S.state && S.state.searches) || [];
  for (const s of list) if (s.name === name) return s;
  return null;
}

async function loadAds(name) {
  const p = ensureSearchPanel(name);
  const params = new URLSearchParams({
    search: name,
    verdict: p.filter,
    q: p.q,
    limit: '200',
  });
  try {
    const data = await getJson('/api/ads?' + params.toString());
    setApiError(false);
    p.ads = data.ads || [];
    p.loaded = true;
    renderAds(p);
  } catch (err) {
    setApiError(true);
    toast('Annonces indisponibles : ' + err.message, 'error');
  }
}

function renderAds(p) {
  const host = p.node.querySelector('.ads');
  const empty = p.node.querySelector('.empty');
  host.textContent = '';

  const info = searchInfo(p.name);
  if (info) {
    p.node.querySelector('.counts').textContent =
      info.total + ' annonce' + (info.total > 1 ? 's' : '') +
      ' · ' + info.retenu + ' retenue' + (info.retenu > 1 ? 's' : '') +
      ' · ' + info.pending + ' non analysée' + (info.pending > 1 ? 's' : '') +
      (info.last_run ? ' · dernière collecte ' + relTime(info.last_run) : '');
  }

  if (!p.ads.length) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  const frag = document.createDocumentFragment();
  for (const ad of p.ads) frag.appendChild(buildAdCard(p, ad));
  host.appendChild(frag);
}

function buildAdCard(p, ad) {
  const node = tplAd.content.firstElementChild.cloneNode(true);
  node.dataset.listId = String(ad.list_id);

  node.querySelector('.ad-title').textContent = ad.subject || '(sans titre)';
  node.querySelector('.ad-price').textContent = formatPrice(ad.price);

  const badge = node.querySelector('.badge');
  const verdict = ad.verdict === 'retenu' || ad.verdict === 'rejete' ? ad.verdict : null;
  badge.classList.add(verdict ? 'badge-' + verdict : 'badge-unknown');
  badge.textContent = verdict ? VERDICT_LABEL[verdict] : 'Non analysée';

  if (verdict && ad.verdict_source) {
    const src = node.querySelector('.tag-src');
    src.hidden = false;
    src.textContent = ad.verdict_source === 'manuel' ? 'Verdict manuel' : 'Verdict IA';
  }

  const reason = node.querySelector('.ad-reason');
  if (ad.verdict_reason) {
    reason.hidden = false;
    reason.textContent = 'Motif : ' + ad.verdict_reason;
  }

  const body = node.querySelector('.ad-body');
  const more = node.querySelector('[data-action="more"]');
  const full = String(ad.body || '');
  if (!full) {
    body.hidden = true;
    more.hidden = true;
  } else if (full.length > 400 && !p.expanded.has(ad.list_id)) {
    body.textContent = full.slice(0, 400).trimEnd() + '…';
    more.hidden = false;
    more.textContent = 'voir plus';
  } else {
    body.textContent = full;
    if (full.length > 400) {
      more.hidden = false;
      more.textContent = 'voir moins';
    } else {
      more.hidden = true;
    }
  }

  const seller = node.querySelector('.ad-seller');
  const sellerParts = [];
  if (ad.owner) sellerParts.push(ad.owner);
  if (ad.rating) {
    sellerParts.push(
      'note ' + ad.rating.toFixed(1).replace('.', ',') +
        (ad.rating_count ? ' (' + ad.rating_count + ')' : '')
    );
  }
  seller.textContent = sellerParts.join(' · ');
  if (!sellerParts.length) seller.hidden = true;

  const loc = node.querySelector('.ad-loc');
  if (ad.location) {
    loc.textContent = ad.location;
  } else {
    loc.hidden = true;
  }

  node.querySelector('.ad-date').textContent = ad.published
    ? 'Publiée le ' + formatDate(ad.published) + ' (' + relTime(ad.published) + ')'
    : 'Vue le ' + formatDate(ad.first_seen) + ' (' + relTime(ad.first_seen) + ')';

  const link = node.querySelector('.ad-link');
  if (ad.url) {
    link.href = ad.url;
    link.textContent = 'Annonce ↗';
    link.title = ad.url;
  } else {
    link.hidden = true;
  }

  if (ad.notified === false && ad.verdict === 'retenu') {
    node.querySelector('.tag-new').hidden = false;
  }

  return node;
}

/** Délégation : un seul gestionnaire par panneau de recherche. */
async function onAdClick(ev, p) {
  const btn = ev.target.closest('[data-action], [data-verdict]');
  if (!btn) return;

  if (btn.dataset.verdict) {
    const card = btn.closest('.ad');
    const listId = Number(card.dataset.listId);
    closeMenus(p);
    await callPost(
      '/api/ads/verdict',
      { search: p.name, list_id: listId, verdict: btn.dataset.verdict, reason: 'choix manuel' },
      () => (btn.dataset.verdict === 'none' ? 'Verdict effacé' : 'Verdict enregistré : ' + VERDICT_LABEL[btn.dataset.verdict])
    );
    loadAds(p.name);
    return;
  }

  const action = btn.dataset.action;

  if (action === 'more') {
    const card = btn.closest('.ad');
    const body = card.querySelector('.ad-body');
    const id = Number(card.dataset.listId);
    const ad = p.ads.find((a) => a.list_id === id);
    const full = ad ? String(ad.body || '') : body.textContent;
    if (p.expanded.has(id)) {
      p.expanded.delete(id);
      body.textContent = full.slice(0, 400).trimEnd() + '…';
      btn.textContent = 'voir plus';
    } else {
      p.expanded.add(id);
      body.textContent = full;
      btn.textContent = 'voir moins';
    }
    return;
  }

  if (action === 'menu') {
    const items = btn.parentElement.querySelector('.menu-items');
    const wasHidden = items.hidden;
    closeMenus(p);
    items.hidden = !wasHidden;
    return;
  }

  if (action === 'analyze') {
    const card = btn.closest('.ad');
    const listId = Number(card.dataset.listId);
    btn.disabled = true;
    btn.textContent = 'Analyse…';
    const data = await callPost('/api/ads/analyze', { search: p.name, list_id: listId }, (d) => {
      const v = d.verdict === 'retenu' ? 'retenue' : d.verdict === 'rejete' ? 'écartée' : 'sans verdict';
      return 'IA : annonce ' + v + (d.reason ? ' — ' + truncate(d.reason, 120) : '');
    });
    if (data) loadAds(p.name);
    return;
  }

  if (action === 'analyze-pending') {
    btn.disabled = true;
    btn.textContent = 'Analyse en cours…';
    const data = await callPost('/api/ads/analyze_pending', { search: p.name }, (d) => {
      return (
        d.analyzed + ' annonce' + (d.analyzed > 1 ? 's' : '') + ' analysée' +
        (d.analyzed > 1 ? 's' : '') + ' · ' + d.retained + ' retenue' + (d.retained > 1 ? 's' : '')
      );
    });
    btn.disabled = false;
    btn.textContent = 'Analyser les non analysées';
    if (data) loadAds(p.name);
  }
}

function closeMenus(p) {
  $$('.menu-items', p.node).forEach((m) => {
    m.hidden = true;
  });
}

document.addEventListener('click', (ev) => {
  if (ev.target.closest('.menu')) return;
  S.panels.forEach((p) => closeMenus(p));
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') S.panels.forEach((p) => closeMenus(p));
});

/* ------------------------------------------------------------- onglet Directives */

const dirHost = $('#dir-searches');

async function loadDirectives() {
  try {
    const data = await getJson('/api/directives');
    setApiError(false);
    renderDirectives(data || {});
  } catch (err) {
    setApiError(true);
    toast('Directives indisponibles : ' + err.message, 'error');
  }
}

function renderDirectives(data) {
  dirHost.textContent = '';
  $('#dir-global').value = data.global || '';

  const names = [];
  const seen = new Set();
  for (const s of (S.state && S.state.searches) || []) {
    if (!seen.has(s.name)) {
      seen.add(s.name);
      names.push(s.name);
    }
  }
  const per = data.per_search || {};
  for (const name of Object.keys(per)) {
    if (!seen.has(name)) names.push(name);
  }

  if (!names.length) {
    dirHost.appendChild(el('p', 'muted', 'Aucune recherche configurée.'));
    return;
  }

  for (const name of names) {
    const field = el('label', 'field field-text');
    field.appendChild(el('span', null, 'Directives pour « ' + name + ' »'));
    const ta = el('textarea');
    ta.rows = 5;
    ta.dataset.search = name;
    ta.value = per[name] || '';
    ta.placeholder = 'Règles spécifiques à cette recherche…';
    field.appendChild(ta);
    dirHost.appendChild(field);
  }
}

$('#btn-dir-save').addEventListener('click', async () => {
  const per = {};
  $$('textarea[data-search]', dirHost).forEach((ta) => {
    if (ta.value.trim()) per[ta.dataset.search] = ta.value;
  });
  await callPost('/api/directives', { global: $('#dir-global').value, per_search: per }, 'Directives enregistrées');
});

$('#btn-dir-reload').addEventListener('click', loadDirectives);

/* -------------------------------------------------------------- onglet Réglages */

function renderSettings() {
  const st = S.state;
  if (!st) return;
  const c = st.collector || {};
  const ai = st.ai || {};

  $('#set-c-status').textContent = c.running ? 'En marche' : 'En pause';
  $('#set-c-next').textContent = c.next_run_at
    ? formatDate(c.next_run_at) + ' (' + countdownText(c.next_run_at) + ')'
    : '—';

  $('#set-ai-enabled').textContent = ai.enabled ? 'Activée' : 'Désactivée';
  $('#set-ai-model').textContent = ai.model || '—';
  $('#set-ai-url').textContent = ai.base_url || '—';
  $('#set-ai-reach').textContent = ai.reachable ? 'Joignable' : 'Injoignable';
  $('#set-ai-auto').textContent = ai.auto_analyze ? 'Automatique' : 'Manuelle';

  const tg = st.telegram || {};
  $('#set-tg').textContent = tg.configured ? 'Configuré' : 'Non configuré';
}

$('#btn-test-ai').addEventListener('click', async () => {
  const data = await callPost('/api/test/ai', {}, (d) => 'IA répond : ' + (d.reply || 'ok'));
  if (data) renderSettings();
});

$('#btn-test-telegram').addEventListener('click', () => {
  callPost('/api/test/telegram', {}, 'Telegram fonctionne');
});

/* ------------------------------------------------------------------ rafraîchir */

async function refreshState() {
  try {
    const data = await getJson('/api/state');
    setApiError(false);
    S.state = data;
    renderBanner();
    renderTabs();
    renderCollector();
    renderSettings();
  } catch (err) {
    setApiError(true);
  }
}

/* --------------------------------------------------------------------- horloge */

setInterval(() => {
  const st = S.state;
  if (!st || !st.collector || !st.collector.next_run_at) return;
  const txt = countdownText(st.collector.next_run_at);
  if (st.collector.running) {
    $('#countdown').textContent = ' (' + txt + ')';
  }
  if (!$('#panel-collector').hidden) {
    $('#c-next').textContent = formatDate(st.collector.next_run_at) + ' (' + txt + ')';
  }
  if (!$('#panel-settings').hidden) {
    $('#set-c-next').textContent = formatDate(st.collector.next_run_at) + ' (' + txt + ')';
  }
}, 1000);

/* --------------------------------------------------------------------- amorçage */

renderTabs();
refreshState();
setInterval(refreshState, 5000);
setInterval(() => {
  if (S.active === 'collector') loadLogs();
}, 5000);
