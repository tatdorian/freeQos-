/* freeQoS - interface d'administration.
 *
 * Sans framework ni CDN, volontairement : un controleur souverain doit
 * fonctionner sur une VM de management coupee d'internet. Les graphes sont du
 * SVG genere a la main, ce qui evite d'embarquer une bibliotheque entiere pour
 * deux courbes.
 */
'use strict';

const API = '/api/v1';

/* ------------------------------------------------------------------ outils */

async function api(path, options) {
  const res = await fetch(API + path, {
    headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : res.status + ' ' + res.statusText;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body;
}

/** Formate un debit en bits/s. Retourne la valeur et l'unite separement pour
 *  que les grands chiffres du tableau de bord puissent styler l'unite. */
function bps(v) {
  const n = Number(v) || 0;
  if (n >= 1e9) return { v: (n / 1e9).toFixed(2), u: 'Gbps' };
  if (n >= 1e6) return { v: (n / 1e6).toFixed(1), u: 'Mbps' };
  if (n >= 1e3) return { v: (n / 1e3).toFixed(0), u: 'Kbps' };
  return { v: n.toFixed(0), u: 'bps' };
}
function bpsText(v) { const b = bps(v); return b.v + ' ' + b.u; }
/** Debit ultra-compact pour les etiquettes d'arete : 640M, 1.2G, 92M. */
function bpsShort(v) {
  const n = Number(v) || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + 'G';
  if (n >= 1e6) return (n / 1e6).toFixed(0) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k';
  return n.toFixed(0);
}
/** Formate un debit exprime en Mbps, en choisissant l'unite lisible.
 *  Un lien de secours a 512 kbps ne doit pas s'afficher "0.5 Mbps". */
function mbps(v) {
  const n = Number(v) || 0;
  if (n && n < 1) return (n * 1000).toFixed(n < 0.1 ? 1 : 0) + ' kbps';
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 2) + ' Gbps';
  return n.toFixed(n >= 100 ? 0 : 1) + ' Mbps';
}

/** Meilleure unite pour PRE-REMPLIR un champ de saisie, avec sa valeur. */
function bestUnit(mbpsValue) {
  const n = Number(mbpsValue);
  if (!n) return { value: '', unit: 'mbps' };
  if (n < 1) return { value: +(n * 1000).toFixed(3), unit: 'kbps' };
  if (n >= 1000) return { value: +(n / 1000).toFixed(3), unit: 'gbps' };
  return { value: +n.toFixed(3), unit: 'mbps' };
}

const UNITES = [['kbps', 'kbps'], ['mbps', 'Mbps'], ['gbps', 'Gbps']];

/** Select d'unite associe a un champ de debit. */
function unitSelect(id, selected) {
  return '<select id="' + id + '" style="width:auto;flex:0 0 auto">' +
    UNITES.map(([v, label]) => '<option value="' + v + '"' +
      (v === selected ? ' selected' : '') + '>' + label + '</option>').join('') +
    '</select>';
}

/** Lit un couple (champ, unite) et renvoie la valeur en Mbps, ou null. */
function readRate(inputId, unitId) {
  const brut = document.getElementById(inputId).value;
  if (brut === '') return null;
  const n = Number(brut);
  if (!isFinite(n)) return null;
  const unite = document.getElementById(unitId).value;
  if (unite === 'kbps') return n / 1000;
  if (unite === 'gbps') return n * 1000;
  return n;
}

function pct(value, max) {
  if (!max || max <= 0) return null;
  return Math.max(0, Math.min(999, (value / max) * 100));
}

/** Seuils communs a toutes les jauges : vert / ambre / rouge. */
function severity(p) {
  if (p === null) return '';
  if (p >= 90) return 'crit';
  if (p >= 70) return 'warn';
  return 'ok';
}

/** Latence : les seuils sont ceux qui comptent pour un usage temps reel
 *  (visio, jeu). Au-dela de 100 ms l'experience se degrade nettement. */
function rtt(value) {
  if (value === null || value === undefined) {
    return '<span style="color:var(--faint)">-</span>';
  }
  const ms = Number(value);
  const color = ms < 30 ? 'var(--ok)' : ms < 100 ? 'var(--warn)' : 'var(--crit)';
  return '<span style="color:' + color + '">' + ms.toFixed(ms < 10 ? 1 : 0) + ' ms</span>';
}

/** Pastille de note de bufferbloat : couleur = severite, titre = le detail
 *  (latence a vide -> sous charge). Une note absente veut dire "pas mesurable",
 *  pas "bon" : on l'affiche en gris, jamais en vert. */
function bloatBadge(v) {
  if (!v || !v.grade) {
    return '<span class="badge" title="Pas assez de charge sur la periode pour ' +
      'mesurer le bufferbloat de cet abonne.">n/d</span>';
  }
  return '<span class="badge ' + esc(v.severity) + '" title="Latence a vide ' +
    esc(v.idle_ms) + ' ms, sous charge ' + esc(v.loaded_ms) + ' ms, sur ' +
    esc(v.samples) + ' echantillon(s)">' + esc(v.grade) + ' &middot; +' +
    esc(v.bloat_ms) + ' ms</span>';
}

function uptime(seconds) {
  const s = Number(seconds);
  if (!s && s !== 0) return '-';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + 'j ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function meter(value, max, kind) {
  const p = pct(value, max);
  if (p === null) return '<span class="pct" style="color:var(--faint)">-</span>';
  const cls = kind || severity(p);
  return '<div class="meter"><div class="track"><div class="fill ' + cls +
    '" style="width:' + Math.min(100, p).toFixed(1) + '%"></div></div>' +
    '<span class="pct">' + p.toFixed(0) + '%</span></div>';
}

function clock(ts) {
  if (!ts) return '-';
  return new Date(ts).toLocaleTimeString('fr-FR', { hour12: false });
}

/* ------------------------------------------------------- graphe en aires */

const SVG_NS = 'http://www.w3.org/2000/svg';
let tooltipEl = null;

function svgEl(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  return node;
}

/**
 * Graphe miroir : download au-dessus de l'axe, upload en dessous.
 * Cette forme rend immediatement lisible l'asymetrie d'un reseau d'acces,
 * bien plus qu'une superposition de deux courbes.
 */
function renderThroughput(container, points, options) {
  const opt = options || {};
  const legende = opt.labels || { down: 'Download', up: 'Upload', extra: 'Abonnes' };
  container.innerHTML = '';
  if (!points || points.length === 0) {
    container.innerHTML = '<div class="empty">Aucune mesure sur la periode.</div>';
    return;
  }

  const W = Math.max(320, container.clientWidth);
  const H = 260;
  const M = { top: 12, right: 12, bottom: 22, left: 62 };
  const iw = W - M.left - M.right;
  const ih = H - M.top - M.bottom;
  const zeroY = M.top + ih / 2;

  const down = points.map((p) => Number(p.tx_bps) || 0);
  const up = points.map((p) => Number(p.rx_bps) || 0);
  const peak = Math.max(1, ...down, ...up);

  const x = (i) => M.left + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw);
  const yDown = (v) => zeroY - (v / peak) * (ih / 2);
  const yUp = (v) => zeroY + (v / peak) * (ih / 2);

  const svg = svgEl('svg', {
    class: 'chart', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H,
  });

  // Grille horizontale : 0, 50 % et 100 % du pic, de part et d'autre.
  [0, 0.5, 1].forEach((f) => {
    [yDown(peak * f), yUp(peak * f)].forEach((yy) => {
      svg.appendChild(svgEl('line', {
        class: f === 0 ? 'zero' : 'grid-line', x1: M.left, x2: W - M.right, y1: yy, y2: yy,
      }));
    });
    if (f > 0) {
      const label = bpsText(peak * f);
      [[yDown(peak * f), label], [yUp(peak * f), label]].forEach(([yy, text]) => {
        const t = svgEl('text', { class: 'axis-label', x: M.left - 8, y: yy + 3, 'text-anchor': 'end' });
        t.textContent = text;
        svg.appendChild(t);
      });
    }
  });

  const area = (values, yFn) => {
    let d = 'M ' + x(0) + ' ' + zeroY;
    values.forEach((v, i) => { d += ' L ' + x(i).toFixed(1) + ' ' + yFn(v).toFixed(1); });
    d += ' L ' + x(values.length - 1) + ' ' + zeroY + ' Z';
    return d;
  };
  const line = (values, yFn) =>
    values.map((v, i) => (i ? 'L' : 'M') + ' ' + x(i).toFixed(1) + ' ' + yFn(v).toFixed(1)).join(' ');

  svg.appendChild(svgEl('path', { d: area(down, yDown), fill: 'var(--down-dim)' }));
  svg.appendChild(svgEl('path', { d: area(up, yUp), fill: 'var(--up-dim)' }));
  svg.appendChild(svgEl('path', { d: line(down, yDown), fill: 'none', stroke: 'var(--down)', 'stroke-width': 1.7 }));
  svg.appendChild(svgEl('path', { d: line(up, yUp), fill: 'none', stroke: 'var(--up)', 'stroke-width': 1.7 }));

  // Axe des temps : trois reperes suffisent, davantage encombre.
  [0, Math.floor(points.length / 2), points.length - 1].forEach((i) => {
    if (i < 0 || !points[i]) return;
    const t = svgEl('text', {
      class: 'axis-label', x: x(i), y: H - 6,
      'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle',
    });
    t.textContent = clock(points[i].bucket);
    svg.appendChild(t);
  });

  const hoverLine = svgEl('line', { class: 'hover-line', y1: M.top, y2: M.top + ih, opacity: 0 });
  const hoverDot1 = svgEl('circle', { r: 3.5, fill: 'var(--down)', opacity: 0 });
  const hoverDot2 = svgEl('circle', { r: 3.5, fill: 'var(--up)', opacity: 0 });
  svg.appendChild(hoverLine); svg.appendChild(hoverDot1); svg.appendChild(hoverDot2);

  const overlay = svgEl('rect', {
    x: M.left, y: M.top, width: iw, height: ih, fill: 'transparent', style: 'cursor:crosshair',
  });
  svg.appendChild(overlay);

  overlay.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const rel = event.clientX - box.left - M.left;
    const i = Math.max(0, Math.min(points.length - 1,
      Math.round((rel / iw) * (points.length - 1))));
    const px = x(i);
    hoverLine.setAttribute('x1', px); hoverLine.setAttribute('x2', px); hoverLine.setAttribute('opacity', 1);
    hoverDot1.setAttribute('cx', px); hoverDot1.setAttribute('cy', yDown(down[i])); hoverDot1.setAttribute('opacity', 1);
    hoverDot2.setAttribute('cx', px); hoverDot2.setAttribute('cy', yUp(up[i])); hoverDot2.setAttribute('opacity', 1);
    showTooltip(event, points[i], down[i], up[i], legende);
  });
  overlay.addEventListener('mouseleave', () => {
    [hoverLine, hoverDot1, hoverDot2].forEach((n) => n.setAttribute('opacity', 0));
    hideTooltip();
  });

  container.appendChild(svg);
}

function showTooltip(event, point, down, up, legende) {
  if (!tooltipEl) {
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'tooltip';
    document.body.appendChild(tooltipEl);
  }
  const lib = legende || { down: 'Download', up: 'Upload', extra: 'Abonnes' };
  tooltipEl.innerHTML =
    '<div class="t">' + esc(new Date(point.bucket).toLocaleString('fr-FR')) + '</div>' +
    '<div class="row"><span style="color:var(--down)">' + esc(lib.down) + '</span><span>' + esc(bpsText(down)) + '</span></div>' +
    '<div class="row"><span style="color:var(--up)">' + esc(lib.up) + '</span><span>' + esc(bpsText(up)) + '</span></div>' +
    (lib.extra === null ? ''
      : '<div class="row"><span style="color:var(--faint)">' + esc(lib.extra) + '</span><span>' +
        esc(point.subscribers || 0) + '</span></div>');
  tooltipEl.style.display = 'block';
  const pad = 14;
  const x = Math.min(event.clientX + pad, window.innerWidth - tooltipEl.offsetWidth - 8);
  const y = Math.min(event.clientY + pad, window.innerHeight - tooltipEl.offsetHeight - 8);
  tooltipEl.style.left = x + 'px';
  tooltipEl.style.top = y + 'px';
}
function hideTooltip() { if (tooltipEl) tooltipEl.style.display = 'none'; }

/* ------------------------------------------------------- tableau de bord */

const state = {
  view: 'dashboard', rangeMinutes: 60, execRange: 60, subSearch: '', subPop: '',
  routers: [], lastPoints: [], lastTree: [],
  // Lien suivi dans le tiroir, et derniere mesure instantanee affichee.
  link: null, linkLive: null,
};

function statCard(cls, label, value, unit, sub) {
  return '<div class="card stat ' + cls + '"><div class="label">' + esc(label) + '</div>' +
    '<div class="value">' + esc(value) + (unit ? '<span class="unit">' + esc(unit) + '</span>' : '') + '</div>' +
    (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>';
}

async function loadDashboard() {
  const [overview, tree] = await Promise.all([api('/overview'), api('/network/tree')]);

  const down = bps(overview.tx_bps), up = bps(overview.rx_bps);
  // Taux de sur-souscription : ce qui est vendu face a ce qui transite reellement.
  const soldDown = (overview.sold_down_mbps || 0) * 1e6;
  const ratio = soldDown > 0 ? (overview.tx_bps / soldDown) * 100 : null;

  document.getElementById('stat-row').innerHTML =
    statCard('down', 'Download', down.v, down.u, 'somme des sessions actives') +
    statCard('up', 'Upload', up.v, up.u, 'somme des sessions actives') +
    statCard('', 'Abonnes en ligne', overview.online || 0, '',
      esc((overview.subscribers || 0) + ' connus') ) +
    statCard('', 'Debit vendu', (overview.sold_down_mbps || 0).toFixed(0), 'Mbps',
      ratio === null ? 'aucun plan connu' : 'utilise a ' + ratio.toFixed(0) + '%') +
    statCard('', 'Capacite backhaul', (overview.backhaul_capacity_mbps || 0).toFixed(0), 'Mbps',
      esc((overview.backhauls || 0) + ' lien(s) mesure(s)'));

  await loadThroughput();
  await loadTopTalkers();
  renderBackhaulCards(tree);
}

async function loadThroughput() {
  const minutes = state.rangeMinutes;
  // Environ 180 points quelle que soit la fenetre : au-dela, le trace se brouille
  // et la requete grossit pour rien.
  const bucket = Math.max(10, Math.round((minutes * 60) / 180 / 10) * 10);
  const data = await api('/throughput?minutes=' + minutes + '&bucket_seconds=' + bucket);
  state.lastPoints = data.points;
  renderThroughput(document.getElementById('throughput-chart'), data.points);
}

async function loadTopTalkers() {
  const rows = await api('/subscribers/latest?limit=12');
  const host = document.getElementById('top-talkers');
  if (!rows.length) {
    host.innerHTML = '<div class="empty">Aucune session active.<br>Connectez un PoP dans l\'onglet PoPs.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Login</th><th>PoP</th><th class="num">Download</th>' +
    '<th style="width:150px">vs limite</th><th class="num">Upload</th>' +
    '<th class="num">Latence</th></tr></thead><tbody>' +
    rows.map((r) => {
      const limiteDown = (r.effective_down_mbps || 0) * 1e6;
      return '<tr class="clickable" data-sub="' + r.subscriber_id + '">' +
        '<td class="login">' + esc(r.pppoe_login) + '</td>' +
        '<td>' + esc(r.pop_name || '-') + '</td>' +
        '<td class="num" style="color:var(--down)">' + esc(bpsText(r.tx_bps)) + '</td>' +
        '<td>' + meter(r.tx_bps, limiteDown) + '</td>' +
        '<td class="num" style="color:var(--up)">' + esc(bpsText(r.rx_bps)) + '</td>' +
        '<td class="num">' + rtt(r.rtt_ms) + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
  host.querySelectorAll('tr[data-sub]').forEach((tr) => {
    tr.addEventListener('click', () => openSubscriber(tr.dataset.sub));
  });
}

function renderBackhaulCards(tree) {
  const links = [];
  tree.forEach((pop) => (pop.backhauls || []).forEach((b) => links.push({ pop, b })));
  const host = document.getElementById('backhaul-cards');
  if (!links.length) {
    host.innerHTML = '<div class="card"><div class="empty">Aucun backhaul declare.<br>' +
      'Section <code>backhauls</code> de l\'inventaire.</div></div>';
    return;
  }
  host.innerHTML = links.map(({ pop, b }) => {
    const capacity = Number(b.capacity_mbps) || 0;
    const nominal = Number(b.nominal_capacity_mbps) || 0;
    const load = ((Number(pop.tx_bps) || 0) + (Number(pop.rx_bps) || 0)) / 1e6;
    const fade = nominal > 0 ? pct(capacity, nominal) : null;
    return '<div class="card" style="margin-bottom:.7rem">' +
      '<div class="node-head" style="margin-bottom:.7rem">' +
        '<div class="node-title">' + esc(b.name) +
          (b.online === false ? ' <span class="badge crit">hors ligne</span>' : '') +
          '<span class="host">' + esc(pop.name || '') + '</span></div>' +
        '<div class="node-metrics"><span>' + esc(mbps(capacity)) + '</span></div>' +
      '</div>' +
      '<div class="child" style="border:0;padding:.25rem 0">' +
        '<span class="name" style="color:var(--muted)">Capacite vs nominal</span>' +
        (fade === null ? '<span class="pct">-</span>'
          : meter(capacity, nominal, fade < 50 ? 'crit' : fade < 80 ? 'warn' : 'ok')) +
      '</div>' +
      '<div class="child" style="border:0;padding:.25rem 0">' +
        '<span class="name" style="color:var(--muted)">Charge vs capacite</span>' +
        meter(load, capacity) +
      '</div>' +
      '<div class="child" style="border:0;padding:.25rem 0;font-size:.75rem;color:var(--faint)">' +
        '<span>Signal ' + esc(b.signal_dbm !== null && b.signal_dbm !== undefined ? b.signal_dbm + ' dBm' : '-') + '</span>' +
        '<span>Airtime ' + esc(b.airtime_pct !== null && b.airtime_pct !== undefined ? Math.round(b.airtime_pct) + ' %' : '-') + '</span>' +
        '<span>' + esc(clock(b.ts)) + '</span>' +
      '</div></div>';
  }).join('');
}

/* -------------------------------------------------------------- executif */

/** QoE 0..100 derivee du RTT, meme formule que le backend (proxy latence). */
function qoeScore(ms) {
  if (ms === null || ms === undefined) return null;
  return Math.max(0, Math.min(100, Math.round(100 - Math.max(0, ms - 10) * 0.6)));
}
function qoeSev(score) {
  if (score === null) return 'none';
  return score >= 80 ? 'ok' : score >= 50 ? 'warn' : 'crit';
}
/** Severite RTT (memes seuils que le backend), en chaine pour la coloration. */
function rttSevJs(ms) {
  if (ms === null || ms === undefined) return 'none';
  return ms < 30 ? 'ok' : ms < 100 ? 'warn' : 'crit';
}

/** Etat de la vue Files live : noeuds agreges, index abonnes, selection et
 *  branches depliees (conservees entre deux rafraichissements). */
const exec = { nodes: [], subsById: {}, bloatById: {}, selected: null, expanded: new Set() };

/** Petit carre colore devant une valeur, signature visuelle de LibreQoS. */
function sqCell(text, sev) {
  return '<span class="sq ' + (sev || 'none') + '"></span>' + esc(text);
}

/** Nombre d'equipements sous un noeud, deduit du graphe (degre - lien amont).
 *  Approximatif mais honnete : "-" quand le noeud n'est pas dans la topologie. */
function childCountsFromTopo(topoData) {
  const counts = {};
  if (!topoData || !topoData.links) return counts;
  const nameByKey = {};
  (topoData.nodes || []).forEach((n) => { nameByKey[n.key] = (n.name || '').toLowerCase(); });
  const deg = {};
  topoData.links.forEach((l) => {
    const s = nameByKey[l.source_key];
    const t = nameByKey[l.target_key];
    if (s) deg[s] = (deg[s] || 0) + 1;
    if (t) deg[t] = (deg[t] || 0) + 1;
  });
  Object.keys(deg).forEach((name) => { counts[name] = Math.max(0, deg[name] - 1); });
  return counts;
}

// Roles "feuille" (cote client) qu'on n'affiche PAS comme noeud d'infrastructure
// dans l'Executif : un noeud est un site/routeur, pas un abonne.
const LEAF_KINDS = new Set(['client', 'subscriber', 'cpe']);

/** Construit des lignes de noeud a partir de la TOPOLOGIE quand aucun abonne
 *  n'est encore mesure. Le tableau reste vide sinon : ici on montre le reseau
 *  reel (les routeurs / PoPs connectes) meme sans trafic, avec debit / RTT / QoO
 *  en n/d — jamais des zeros inventes. */
function nodesFromTopology(topoData, childCounts) {
  const counts = childCounts || {};
  const list = (topoData && Array.isArray(topoData.nodes)) ? topoData.nodes : [];
  const nodes = list
    .filter((n) => !LEAF_KINDS.has(n.kind))
    .map((n) => ({
      name: n.name || n.key, kind: n.kind || 'unknown', synthetic: true,
      circuits: 0, tx: 0, rx: 0, effDown: 0, effUp: 0, confDown: 0, confUp: 0,
      rttMax: null, subs: [],
      nodesCount: counts[(n.name || '').toLowerCase()] ?? null,
    }));
  nodes.sort((a, b) => {
    const ra = KIND_ORDER.indexOf(a.kind);
    const rb = KIND_ORDER.indexOf(b.kind);
    if (ra !== rb) return (ra < 0 ? 99 : ra) - (rb < 0 ? 99 : rb);
    return a.name.localeCompare(b.name);
  });
  return nodes;
}

/** Etat de la sonde RTT dans la barre d'outils : la case reflete le drapeau
 *  (base), et un encart rappelle que sans elle QoO/RTT/bufferbloat restent vides. */
function renderRttControl(state) {
  const box = document.getElementById('rtt-toggle');
  // La case reflete le drapeau ; le bandeau explicatif est pose par
  // renderExecNotice (un seul endroit qui compose tous les messages).
  if (box && state) box.checked = !!state.enabled;
}

async function toggleRtt(enabled) {
  try {
    await api('/rtt', { method: 'PUT', body: JSON.stringify({ enabled: enabled }) });
    await loadExec();
  } catch (err) { alert(err.message); }
}

function selectionExists(sel) {
  if (!sel) return false;
  if (sel.type === 'node') return exec.nodes.some((n) => n.name === sel.name);
  return !!exec.subsById[sel.id];
}

async function loadExec() {
  const minutes = state.execRange || 60;
  const buckets = minutes <= 15 ? 15 : minutes <= 60 ? 30 : 36;
  // Chaque source est isolee : une seule qui echoue ne doit pas laisser l'onglet
  // BLANC. On garde une trace de l'echec pour l'expliquer, plutot que rien.
  let firstError = null;
  const grab = (p) => p.catch((err) => { firstError = firstError || err; return undefined; });
  const [heat, subsRaw, bloat, topoData, tree, rttState] = await Promise.all([
    grab(api('/heatmap?minutes=' + minutes + '&buckets=' + buckets)),
    grab(api('/subscribers/latest?limit=500&order_by=login')),
    api('/bufferbloat?minutes=' + minutes).catch(() => null),
    api('/topology').catch(() => null),
    api('/network/tree').catch(() => []),
    api('/rtt').catch(() => null),
  ]);
  const subs = Array.isArray(subsRaw) ? subsRaw : [];
  renderRttControl(rttState);
  exec.bloatById = {};
  if (bloat) (bloat.subscribers || []).forEach((b) => { exec.bloatById[b.subscriber_id] = b; });
  // Enveloppe partagee par PoP : capacite du backhaul (le vrai goulot commun).
  // C'est sous cette limite que les circuits se disputent la bande passante.
  exec.envByPop = {};
  (tree || []).forEach((p) => {
    const cap = (p.backhauls || []).reduce((a, b) => a + (Number(b.capacity_mbps) || 0), 0);
    const nom = (p.backhauls || []).reduce((a, b) => a + (Number(b.nominal_capacity_mbps) || 0), 0);
    exec.envByPop[p.name] = { capacity: cap || null, nominal: nom || null };
  });
  exec.subsById = {};
  subs.forEach((s) => { exec.subsById[s.subscriber_id] = s; });
  const cc = childCountsFromTopo(topoData);
  exec.nodes = aggregateNodes(subs, cc);
  // Aucun abonne mesure mais des routeurs connectes : on montre quand meme le
  // reseau reel (topologie), sinon l'onglet reste desesperement vide.
  const fromTopo = exec.nodes.length === 0;
  if (fromTopo) exec.nodes = nodesFromTopology(topoData, cc);
  // Selection par defaut : le noeud le plus charge, tant que rien n'est choisi.
  if (!selectionExists(exec.selected)) {
    exec.selected = exec.nodes.length ? { type: 'node', name: exec.nodes[0].name } : null;
  }

  renderQueuePanels();
  renderNodeTable(document.getElementById('exec-nodes'));
  renderHeatmap(document.getElementById('exec-heatmap'), heat);
  renderExecSankey(document.getElementById('exec-sankey'), subs);
  document.getElementById('exec-count').textContent =
    exec.nodes.length + ' noeud(s), ' + subs.length + ' circuit(s)';
  renderExecNotice(rttState, firstError, {
    noNodes: exec.nodes.length === 0,
    topoOnly: fromTopo && exec.nodes.length > 0,
  });
}

/** Bandeau d'etat de l'onglet : erreur de chargement, sonde coupee, ou reseau
 *  vide. Il y a TOUJOURS quelque chose a l'ecran, jamais un blanc silencieux. */
function renderExecNotice(rttState, error, st) {
  const notice = document.getElementById('exec-notice');
  if (!notice) return;
  const state = st || {};
  let html = '';
  if (error) {
    html += '<div class="notice err"><b>Chargement partiel.</b> ' + esc(error.message) +
      '<span class="hint">Une source n\'a pas repondu (base indisponible, ou endpoint ' +
      'absent d\'un deploiement plus ancien). Le reste de l\'onglet reste affiche.</span></div>';
  }
  if (state.noNodes && !error) {
    html += '<div class="notice"><b>Aucun noeud.</b> Connectez un routeur dans l\'onglet ' +
      '<b>Equipements</b> : ses PoPs et leurs files apparaitront ici. ' +
      'Ajoutez une antenne pour la capacite partagee, et activez la <b>Sonde RTT</b> ' +
      'ci-dessus pour RTT / QoO / bufferbloat.</div>';
  }
  if (state.topoOnly && !error) {
    html += '<div class="notice"><b>Reseau affiche d\'apres la topologie.</b> Les routeurs ' +
      'connectes sont la, mais aucun abonne n\'est encore mesure : debit, RTT et QoO restent ' +
      'en <code>n/d</code> tant qu\'aucun circuit ne passe (et que la <b>Sonde RTT</b> ci-dessus ' +
      'n\'est pas activee). Ils se rempliront au prochain cycle de collecte.</div>';
  }
  if (rttState && !rttState.enabled) {
    html += '<div class="notice"><b>Sonde RTT coupee.</b> RTT, QoO et bufferbloat resteront ' +
      'vides tant qu\'elle n\'est pas activee (case <b>Sonde RTT</b> ci-dessus). Elle envoie ' +
      'des <code>/ping</code> depuis le PoP ; le compte de lecture doit avoir la policy ' +
      '<code>test</code>. Aucune variable d\'environnement necessaire.</div>';
  }
  notice.innerHTML = html;
}

function renderHeatmap(host, heat) {
  if (!heat || !Array.isArray(heat.rows)) {
    host.innerHTML = '<div class="empty">Heatmap indisponible pour le moment.</div>';
    return;
  }
  host.innerHTML = heat.rows.map((row) => {
    if (row.unavailable) {
      return '<div class="heat-row"><span class="heat-label">' + esc(row.label) + '</span>' +
        '<span class="heat-unavail" title="' + esc(row.reason || '') + '">n/d &mdash; ' +
        esc(row.reason || 'indisponible hors-bande') + '</span></div>';
    }
    const last = [...row.cells].reverse().find((c) => c.value !== null && c.value !== undefined);
    const cells = row.cells.map((c) => {
      const t = c.value !== null && c.value !== undefined
        ? new Date(c.ts).toLocaleTimeString('fr-FR', { hour12: false }) + ' : ' +
          c.value + (row.unit ? ' ' + row.unit : '')
        : 'pas de mesure';
      return '<span class="heat-cell ' + esc(c.severity) + '" title="' + esc(t) + '"></span>';
    }).join('');
    const now = last ? (last.value + (row.unit ? ' ' + row.unit : '')) : '-';
    return '<div class="heat-row">' +
      '<span class="heat-label">' + esc(row.label) +
        (row.unit ? ' <span class="u">(' + esc(row.unit) + ')</span>' : '') + '</span>' +
      '<span class="heat-cells">' + cells + '</span>' +
      '<span class="heat-now">' + esc(now) + '</span></div>';
  }).join('');
}

/** Agrege les abonnes par PoP en lignes de "files", facon LibreQoS. */
function aggregateNodes(subs, childCounts) {
  const counts = childCounts || {};
  const parPop = new Map();
  subs.forEach((s) => {
    const nom = s.pop_name || '(sans PoP)';
    if (!parPop.has(nom)) {
      parPop.set(nom, { name: nom, circuits: 0, tx: 0, rx: 0, effDown: 0, effUp: 0,
        confDown: 0, confUp: 0, rttMax: null, subs: [] });
    }
    const n = parPop.get(nom);
    n.circuits += 1;
    n.tx += Number(s.tx_bps) || 0;
    n.rx += Number(s.rx_bps) || 0;
    n.effDown += (Number(s.effective_down_mbps) || 0) * 1e6;
    n.effUp += (Number(s.effective_up_mbps) || 0) * 1e6;
    n.confDown += (Number(s.plan_down_mbps) || 0) * 1e6;
    n.confUp += (Number(s.plan_up_mbps) || 0) * 1e6;
    if (s.rtt_ms !== null && s.rtt_ms !== undefined) {
      n.rttMax = n.rttMax === null ? s.rtt_ms : Math.max(n.rttMax, s.rtt_ms);
    }
    n.subs.push(s);
  });
  const nodes = [...parPop.values()];
  nodes.forEach((n) => {
    const c = counts[n.name.toLowerCase()];
    n.nodesCount = c === undefined ? null : c;
  });
  return nodes.sort((a, b) => (b.tx + b.rx) - (a.tx + a.rx));
}

function renderNodeTable(host) {
  const nodes = exec.nodes;
  if (!nodes.length) {
    host.innerHTML = '<div class="empty">Aucun circuit actif.</div>';
    return;
  }
  const rttSq = (ms) => (ms === null || ms === undefined)
    ? sqCell('-', 'none') : sqCell(Math.round(ms) + 'ms', rttSevJs(ms));
  const naSq = sqCell('n/d', 'none');
  const head =
    '<table><thead><tr><th></th><th>Node</th><th class="num">Circuits</th>' +
    '<th class="num">Nodes</th><th class="num">Effective</th><th class="num">Configured</th>' +
    '<th class="num">&darr;</th><th class="num">&uarr;</th>' +
    '<th class="num">RTT</th><th class="num">QoO</th>' +
    '<th class="num" title="Retransmissions TCP — hors-bande">Retr</th>' +
    '<th class="num" title="Marks qdisc — hors-bande">Marks</th>' +
    '<th class="num" title="Drops qdisc — hors-bande">Drops</th></tr></thead><tbody>';

  const body = nodes.map((n) => {
    const qoe = qoeScore(n.rttMax);
    const open = exec.expanded.has(n.name);
    const sel = exec.selected && exec.selected.type === 'node' && exec.selected.name === n.name;
    // Noeud synthetique (issu de la topologie, sans abonne mesure) : debit /
    // effectif / RTT / QoO en n/d, jamais des zeros inventes.
    const effCell = n.synthetic ? '<td class="num na">-</td>'
      : '<td class="num">' + esc(mbps(n.effDown / 1e6) + ' / ' + mbps(n.effUp / 1e6)) + '</td>';
    const confCell = n.synthetic ? '<td class="num na">-</td>'
      : '<td class="num na">' + esc(mbps(n.confDown / 1e6) + ' / ' + mbps(n.confUp / 1e6)) + '</td>';
    const txCell = n.synthetic ? '<td class="num">' + naSq + '</td>'
      : '<td class="num">' + sqCell(bpsText(n.tx), severity(pct(n.tx, n.effDown))) + '</td>';
    const rxCell = n.synthetic ? '<td class="num">' + naSq + '</td>'
      : '<td class="num">' + sqCell(bpsText(n.rx), severity(pct(n.rx, n.effUp))) + '</td>';
    const nodeRow =
      '<tr class="node-row' + (sel ? ' selected' : '') + '" data-node="' + esc(n.name) + '">' +
      '<td>' + (n.synthetic ? ''
        : '<span class="expand" data-expand="' + esc(n.name) + '">' + (open ? '−' : '+') + '</span>') + '</td>' +
      '<td><strong>' + esc(n.name) + '</strong>' +
        (n.synthetic && n.kind ? ' <span class="badge">' + esc(KIND_LABEL[n.kind] || n.kind) +
          '</span>' : '') + '</td>' +
      '<td class="num">' + n.circuits + '</td>' +
      '<td class="num">' + (n.nodesCount == null ? '<span class="na">-</span>' : n.nodesCount) + '</td>' +
      effCell + confCell + txCell + rxCell +
      '<td class="num">' + rttSq(n.rttMax) + '</td>' +
      '<td class="num">' + (qoe == null ? sqCell('-', 'none') : sqCell(String(qoe), qoeSev(qoe))) + '</td>' +
      '<td class="num">' + naSq + '</td><td class="num">' + naSq + '</td><td class="num">' + naSq + '</td></tr>';

    const subRows = !open ? '' : n.subs.map((s) => {
      const b = exec.bloatById[s.subscriber_id];
      const eff = (Number(s.effective_down_mbps) || 0) * 1e6;
      const effU = (Number(s.effective_up_mbps) || 0) * 1e6;
      const cq = qoeScore(s.rtt_ms);
      const csel = exec.selected && exec.selected.type === 'client' && exec.selected.id === s.subscriber_id;
      return '<tr class="sub-row' + (csel ? ' selected' : '') + '" data-client="' + s.subscriber_id + '">' +
        '<td></td><td class="login">' + esc(s.pppoe_login) + '</td>' +
        '<td class="num"></td><td class="num"></td>' +
        '<td class="num">' + esc(mbps(s.effective_down_mbps || 0) + ' / ' + mbps(s.effective_up_mbps || 0)) + '</td>' +
        '<td class="num na">' + esc(mbps(s.plan_down_mbps || 0) + ' / ' + mbps(s.plan_up_mbps || 0)) + '</td>' +
        '<td class="num">' + sqCell(bpsText(s.tx_bps), severity(pct(s.tx_bps, eff))) + '</td>' +
        '<td class="num">' + sqCell(bpsText(s.rx_bps), severity(pct(s.rx_bps, effU))) + '</td>' +
        '<td class="num">' + rttSq(s.rtt_ms) + '</td>' +
        '<td class="num">' + (b ? sqCell(b.grade, b.severity)
          : (cq == null ? sqCell('-', 'none') : sqCell(String(cq), qoeSev(cq)))) + '</td>' +
        '<td class="num">' + naSq + '</td><td class="num">' + naSq + '</td><td class="num">' + naSq + '</td></tr>';
    }).join('');
    return nodeRow + subRows;
  }).join('');

  host.innerHTML = head + body + '</tbody></table>';

  host.querySelectorAll('[data-expand]').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const name = el.dataset.expand;
      if (exec.expanded.has(name)) exec.expanded.delete(name);
      else exec.expanded.add(name);
      renderNodeTable(host);
    });
  });
  host.querySelectorAll('tr.node-row').forEach((tr) => {
    tr.addEventListener('click', (e) => {
      if (e.target.closest('[data-expand]')) return;
      exec.selected = { type: 'node', name: tr.dataset.node };
      renderQueuePanels();
      renderNodeTable(host);
    });
  });
  host.querySelectorAll('tr.sub-row').forEach((tr) => {
    tr.addEventListener('click', () => {
      exec.selected = { type: 'client', id: Number(tr.dataset.client) };
      renderQueuePanels();
      renderNodeTable(host);
    });
  });
}

/* --------------------------------------------- jauge + live queue + details */

/** Arc SVG de startAngle a endAngle (degres, 0 = droite, sens trigo). */
function arcPath(cx, cy, r, startAngle, endAngle) {
  const rad = (a) => (a * Math.PI) / 180;
  const x1 = cx + r * Math.cos(rad(startAngle));
  const y1 = cy - r * Math.sin(rad(startAngle));
  const x2 = cx + r * Math.cos(rad(endAngle));
  const y2 = cy - r * Math.sin(rad(endAngle));
  const large = Math.abs(endAngle - startAngle) > 180 ? 1 : 0;
  const sweep = endAngle < startAngle ? 1 : 0;
  return 'M ' + x1.toFixed(1) + ' ' + y1.toFixed(1) + ' A ' + r + ' ' + r + ' 0 ' +
    large + ' ' + sweep + ' ' + x2.toFixed(1) + ' ' + y2.toFixed(1);
}

/** Compteur de vitesse : demi-cercle d'utilisation + une barre QoE. */
function gaugeSvg(downBps, upBps, maxBps, qoe) {
  const cx = 92;
  const cy = 96;
  const r = 72;
  const util = maxBps > 0 ? Math.min(1, Math.max(downBps, upBps) / maxBps) : 0;
  const sev = severity(util * 100);
  const col = sev === 'crit' ? 'var(--crit)' : sev === 'warn' ? 'var(--warn)' : 'var(--ok)';
  const end = 180 - util * 180;
  const qCol = qoe === null ? 'var(--faint)'
    : qoeSev(qoe) === 'crit' ? 'var(--crit)' : qoeSev(qoe) === 'warn' ? 'var(--warn)' : 'var(--ok)';
  const qh = qoe === null ? 0 : (qoe / 100) * 120;

  return '<div class="gauge-wrap"><svg class="gauge" width="200" height="120" viewBox="0 0 200 120">' +
    '<path class="track" d="' + arcPath(cx, cy, r, 180, 0) + '" fill="none" stroke-width="12"></path>' +
    '<path d="' + arcPath(cx, cy, r, 180, end) + '" fill="none" stroke="' + col +
      '" stroke-width="12" stroke-linecap="round"></path>' +
    '<text x="' + cx + '" y="80" text-anchor="middle" font-size="20" font-weight="700">' +
      (util * 100).toFixed(0) + '%</text>' +
    '<text class="lbl" x="' + cx + '" y="96" text-anchor="middle">utilisation</text>' +
    '<text x="34" y="114" text-anchor="middle" font-size="10" fill="var(--down)">&darr;' +
      esc(bpsShort(downBps)) + '</text>' +
    '<text x="150" y="114" text-anchor="middle" font-size="10" fill="var(--up)">&uarr;' +
      esc(bpsShort(upBps)) + '</text>' +
    '</svg>' +
    '<svg width="46" height="128" viewBox="0 0 46 128"><text class="lbl" x="23" y="10" ' +
      'text-anchor="middle">QoO</text>' +
    '<rect x="14" y="14" width="18" height="120" rx="3" fill="var(--surface-2)"></rect>' +
    '<rect x="14" y="' + (14 + 120 - qh) + '" width="18" height="' + qh + '" rx="3" fill="' +
      qCol + '"></rect>' +
    '<text x="23" y="' + (10 + 120) + '" text-anchor="middle" font-size="11" font-weight="700" ' +
      'fill="' + qCol + '">' + (qoe === null ? '-' : qoe) + '</text></svg></div>';
}

/** Les trois panneaux (Live Queue State | Node Snapshot | Node Details) pour le
 *  noeud ou le client selectionne dans le tableau. Reproduit l'ecran LibreQoS :
 *  un noeud est un agregat (lecture seule), un client peut recevoir un override. */
function renderQueuePanels() {
  const live = document.getElementById('lq-live');
  const snap = document.getElementById('lq-snapshot');
  const det = document.getElementById('lq-details');
  if (!live || !snap || !det) return;

  const sel = exec.selected;
  if (!selectionExists(sel)) {
    const vide = '<div class="empty">Selectionnez un noeud ou un client dans le tableau.</div>';
    live.innerHTML = snap.innerHTML = det.innerHTML = vide;
    return;
  }

  const isClient = sel.type === 'client';
  const client = isClient ? exec.subsById[sel.id] : null;
  const node = isClient ? null : exec.nodes.find((n) => n.name === sel.name);
  const title = isClient ? client.pppoe_login : node.name;
  const down = isClient ? (Number(client.tx_bps) || 0) : node.tx;
  const up = isClient ? (Number(client.rx_bps) || 0) : node.rx;
  const effDown = isClient ? (Number(client.effective_down_mbps) || 0) * 1e6 : node.effDown;
  const effUp = isClient ? (Number(client.effective_up_mbps) || 0) * 1e6 : node.effUp;
  const confDown = isClient ? (Number(client.plan_down_mbps) || 0) * 1e6 : node.confDown;
  const confUp = isClient ? (Number(client.plan_up_mbps) || 0) * 1e6 : node.confUp;
  const rttMs = isClient ? client.rtt_ms : node.rttMax;
  const qoe = qoeScore(rttMs);
  const b = isClient ? exec.bloatById[client.subscriber_id] : null;

  // Noeud issu de la seule topologie (aucun abonne mesure) : tout ce qui est
  // "live" reste en n/d — on ne fabrique pas de zeros.
  const synth = !isClient && !!node.synthetic;
  const rttSq = (ms) => (ms === null || ms === undefined)
    ? sqCell('-', 'none') : sqCell(Math.round(ms) + 'ms', rttSevJs(ms));
  const qooSq = b ? sqCell(b.grade + ' (+' + b.bloat_ms + 'ms)', b.severity)
    : (qoe == null ? sqCell('-', 'none') : sqCell(String(qoe), qoeSev(qoe)));
  const naSq = sqCell('n/d', 'none');
  const naCell = '<td class="num na">' + naSq + '</td>';

  // ---- Live Queue State
  const dwn = (t, s) => '<td class="num">' + sqCell(t, s) + '</td>';
  live.innerHTML =
    '<h3>&#9881; Live Queue State</h3>' +
    '<table class="lq-table"><thead><tr><th></th><th>Download</th><th>Upload</th></tr></thead><tbody>' +
    '<tr><td>Effective Limit</td>' + (synth ? naCell + naCell
      : dwn(mbps(effDown / 1e6), 'ok') + dwn(mbps(effUp / 1e6), 'ok')) + '</tr>' +
    '<tr><td>Configured Limit</td>' + (synth ? naCell + naCell
      : '<td class="num na">' + sqCell(mbps(confDown / 1e6), 'none') +
        '</td><td class="num na">' + sqCell(mbps(confUp / 1e6), 'none') + '</td>') + '</tr>' +
    '<tr><td>Throughput</td>' + (synth ? naCell + naCell
      : dwn(bpsText(down), severity(pct(down, effDown))) +
        dwn(bpsText(up), severity(pct(up, effUp)))) + '</tr>' +
    '<tr><td>RTT</td><td class="num">' + rttSq(rttMs) + '</td><td class="num">' + rttSq(rttMs) + '</td></tr>' +
    '<tr><td>QoO</td><td class="num">' + qooSq + '</td><td class="num">' + qooSq + '</td></tr>' +
    '<tr><td>TCP Retransmits</td><td class="num">' + naSq + '</td><td class="num">' + naSq + '</td></tr>' +
    '</tbody></table>';

  // ---- Node Snapshot (jauge, ou n/d si aucune mesure)
  snap.innerHTML = '<h3>&#128200; Node Snapshot</h3>' +
    (synth
      ? '<div class="empty">Aucune mesure pour ce noeud pour le moment ' +
        '(affiche d\'apres la topologie).</div>'
      : gaugeSvg(down, up, Math.max(effDown, down, 1), qoe));

  // ---- Node Details
  const limitedBy = isClient
    ? ({ plan: 'Plan', override: 'Override', boost: 'Boost' }[client.limit_source] || client.limit_source || '-')
    : 'Agregat';
  const override = isClient
    ? (client.limit_source === 'override' || client.limit_source === 'boost'
        ? mbps(client.effective_down_mbps || 0) + ' / ' + mbps(client.effective_up_mbps || 0)
        : 'None')
    : '—';
  const dPre = isClient ? bestUnitMbps(client.effective_down_mbps) : '';
  const uPre = isClient ? bestUnitMbps(client.effective_up_mbps) : '';
  det.innerHTML =
    '<h3>&#9432; Node Details</h3>' +
    '<div class="lq-kv">' +
      '<span class="k">Base Configured Rate</span><span class="v">' +
        (synth ? 'n/d' : esc(mbps(confDown / 1e6) + ' / ' + mbps(confUp / 1e6))) + '</span>' +
      '<span class="k">Effective Now</span><span class="v">' +
        (synth ? 'n/d' : esc(mbps(effDown / 1e6) + ' / ' + mbps(effUp / 1e6))) + '</span>' +
      '<span class="k">Rate Override</span><span class="v">' + esc(override) + '</span>' +
      '<span class="k">Limited By</span><span class="v">' + esc(limitedBy) + '</span>' +
      '<span class="k">Topology Override</span><span class="v">None</span>' +
      '<span class="k">Active Attachment</span><span class="v">' +
        esc(isClient ? (client.pop_name || '-') : title) + '</span>' +
    '</div>' +
    (isClient
      ? '<div class="lq-rate">D <input id="lq-d" type="number" min="0" step="any" value="' + esc(dPre) +
          '"> U <input id="lq-u" type="number" min="0" step="any" value="' + esc(uPre) + '">' +
          '<button class="sm primary" id="lq-save">Save</button>' +
          '<button class="sm" id="lq-clear">Clear</button></div>' +
        '<div class="lq-note">Debit en Mbps. Enregistrer pose un override sur cet abonne ' +
          '(vu ensuite dans le plan Shaping). Retr / marks / drops : hors-bande, indisponibles.</div>' +
        '<div class="actions" style="margin-top:.6rem">' +
          '<button class="sm" id="lq-open">Ouvrir dans l\'arbre</button></div>' +
        '<div id="lq-result"></div>'
      : sharedCapacityBlock(node) +
        '<div class="lq-note">' + (synth
          ? '<b>Noeud issu de la topologie.</b> Aucun abonne mesure ici pour le moment : ' +
            'ses files apparaitront au prochain cycle de collecte. Le debit du parent ' +
            '(l\'enveloppe partagee) se regle deja sur son lien, bouton <b>Bande passante</b> ' +
            'dans l\'arbre.'
          : '<b>' + node.circuits + ' circuit(s).</b> Un noeud est un ' +
            'agregat : depliez-le et selectionnez un client pour imposer un debit. Le debit ' +
            'du parent (l\'enveloppe partagee) se regle sur son lien, bouton <b>Bande ' +
            'passante</b> dans l\'arbre. Retr / marks / drops : hors-bande, indisponibles.') + '</div>' +
        '<div class="actions" style="margin-top:.6rem">' +
          '<button class="sm" id="lq-open">Regler l\'enveloppe dans l\'arbre</button></div>');

  const open = document.getElementById('lq-open');
  if (open) open.addEventListener('click', () => { location.hash = '#/network'; });
  const save = document.getElementById('lq-save');
  if (save) save.addEventListener('click', () => saveClientRate(client));
  const clear = document.getElementById('lq-clear');
  if (clear) clear.addEventListener('click', () => clearClientRate(client));
}

/** Bloc "capacite partagee" d'un noeud : l'enveloppe du parent (backhaul), le
 *  debit vendu (somme des plans) et la sur-souscription. C'est le coeur du
 *  topology-aware shaping : les circuits se disputent CETTE enveloppe, meme si
 *  la somme de leurs plans la depasse. */
function sharedCapacityBlock(node) {
  const env = (exec.envByPop || {})[node.name] || {};
  const envDown = env.capacity || env.nominal || null;   // Mbps
  const soldDown = node.confDown / 1e6;                   // somme des plans
  const measured = node.tx / 1e6;
  if (!envDown) {
    return '<div class="lq-note">Enveloppe partagee inconnue : aucun backhaul mesure ' +
      'pour ce PoP. Ajoutez son antenne (onglet Equipements) ou fixez la sur son lien.</div>';
  }
  const ratio = soldDown / envDown;
  const sev = ratio <= 1 ? 'ok' : ratio <= 2 ? 'warn' : 'crit';
  return '<div class="lq-kv" style="margin-top:.6rem">' +
    '<span class="k">Capacite partagee</span><span class="v">' + esc(mbps(envDown)) + '</span>' +
    '<span class="k">Vendu (&Sigma; plans)</span><span class="v">' + esc(mbps(soldDown)) + '</span>' +
    '<span class="k">Ecoule (mesure)</span><span class="v">' + esc(mbps(measured)) + '</span>' +
    '<span class="k">Sur-souscription</span><span class="v">' +
      '<span class="sq ' + sev + '"></span>' + ratio.toFixed(1) + '&times;</span>' +
    '</div>' +
    '<div class="lq-note">' + (ratio > 1
      ? 'Les plans vendus totalisent <b>' + ratio.toFixed(1) + '&times;</b> l\'enveloppe : ' +
        'les circuits se partagent le parent sous charge (c\'est voulu, CAKE arbitre).'
      : 'Sous l\'enveloppe : pas de sur-souscription sur ce parent.') + '</div>';
}

/** Valeur Mbps pre-remplie dans un champ (nombre propre, sans zeros inutiles). */
function bestUnitMbps(mbpsValue) {
  const n = Number(mbpsValue);
  return n ? +n.toFixed(3) : '';
}

async function saveClientRate(client) {
  const host = document.getElementById('lq-result');
  const down = document.getElementById('lq-d').value;
  const up = document.getElementById('lq-u').value;
  try {
    await api('/shaping/policies', {
      method: 'PUT',
      body: JSON.stringify({
        scope: 'subscriber', target_key: client.pppoe_login,
        max_down_mbps: down === '' ? null : Number(down),
        max_up_mbps: up === '' ? null : Number(up),
        enabled: true, note: 'impose depuis Files live',
      }),
    });
    if (host) host.innerHTML = '<div class="notice ok">Override enregistre. Visible dans le plan Shaping.</div>';
    await loadExec();
  } catch (err) {
    if (host) host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

async function clearClientRate(client) {
  try {
    await api('/shaping/policies/subscriber/' + encodeURIComponent(client.pppoe_login), { method: 'DELETE' });
    await loadExec();
  } catch (err) { alert(err.message); }
}

/* ------------------------------------------------------- flux (sankey) */

/** Sankey maison : une colonne "reseau" -> une colonne par PoP, largeur des
 *  bandes proportionnelle au debit descendant. Sans dependance, comme le reste. */
function renderExecSankey(host, subs) {
  const nodes = aggregateNodes(subs, {});
  const total = nodes.reduce((a, n) => a + n.tx, 0);
  if (!total) {
    host.innerHTML = '<div class="empty">Aucun trafic descendant a representer.</div>';
    return;
  }
  const W = Math.max(360, host.clientWidth - 4);
  const H = Math.max(160, Math.min(520, nodes.length * 46 + 20));
  const M = 12;
  const srcX = M;
  const srcW = 16;
  const dstX = W - 190;
  const dstW = 16;
  const scale = (H - 2 * M) / total;

  let y = M;
  const bands = [];
  const parts = ['<div class="sankey"><svg width="' + W + '" height="' + H +
    '" viewBox="0 0 ' + W + ' ' + H + '">'];
  // Noeud source (tout le reseau).
  parts.push('<rect class="node" x="' + srcX + '" y="' + M + '" width="' + srcW +
    '" height="' + (H - 2 * M) + '" fill="var(--accent)"></rect>');
  parts.push('<text class="nlabel" x="' + (srcX + srcW + 4) + '" y="' + (M + 12) +
    '" transform="rotate(90 ' + (srcX + srcW + 4) + ' ' + (M + 12) + ')">Reseau</text>');

  let sy = M;
  nodes.forEach((n) => {
    const h = Math.max(2, n.tx * scale);
    const col = severity(pct(n.tx, n.effDown || total));
    const colVar = col === 'crit' ? 'var(--crit)' : col === 'warn' ? 'var(--warn)' : 'var(--down)';
    const y0 = sy;
    const y1 = y;
    const d = 'M ' + (srcX + srcW) + ' ' + y0 + ' C ' + ((srcX + srcW + dstX) / 2) + ' ' + y0 +
      ', ' + ((srcX + srcW + dstX) / 2) + ' ' + y1 + ', ' + dstX + ' ' + y1 +
      ' L ' + dstX + ' ' + (y1 + h) + ' C ' + ((srcX + srcW + dstX) / 2) + ' ' + (y1 + h) +
      ', ' + ((srcX + srcW + dstX) / 2) + ' ' + (y0 + h) + ', ' + (srcX + srcW) + ' ' + (y0 + h) + ' Z';
    parts.push('<path class="flow" d="' + d + '" fill="' + colVar + '"><title>' + esc(n.name) +
      ' : ' + esc(bpsText(n.tx)) + '</title></path>');
    parts.push('<rect class="node" x="' + dstX + '" y="' + y1 + '" width="' + dstW +
      '" height="' + h + '" fill="' + colVar + '"></rect>');
    parts.push('<text class="nlabel" x="' + (dstX + dstW + 6) + '" y="' + (y1 + Math.min(h, 12)) +
      '">' + esc(topoTrim(n.name, 22)) + ' &middot; ' + esc(bpsText(n.tx)) + '</text>');
    sy += h;
    y += h;
    bands.push(n);
  });
  parts.push('</svg></div>');
  host.innerHTML = parts.join('');
}

/* ---------------------------------------------------------- arbre reseau */

const ICONE = {
  gateway: 'GW', core: 'CORE', pop: 'POP', radio: 'RF',
  sector: 'SECT', cpe: 'CPE', client: 'CLI', unknown: '?', subscriber: 'ABO',
};

/** Charge le graphe et les abonnes une seule fois, partage entre l'arbre
 *  editable (onglet Arbre reseau) et le tableau des liens (onglet Topologie). */
async function fetchTopo() {
  const [data, subs] = await Promise.all([
    api('/topology'),
    // Les abonnes, pour les rattacher a leur PoP dans l'arbre.
    api('/subscribers/latest?limit=500&order_by=login').catch(() => []),
  ]);
  topo.data = data;
  topo.subs = subs || [];
  return data;
}

/** Onglet Arbre reseau : le vrai arbre editable au glisser-deposer. C'est la
 *  meme vue que construisait l'onglet Topologie ; elle vit desormais ici, et
 *  Topologie ne garde que le tableau des liens. */
async function loadNetwork() {
  const data = await fetchTopo();
  const compte = document.getElementById('net-count');
  if (compte) {
    compte.textContent = data.counts.nodes + ' equipement(s), ' + data.counts.links + ' lien(s)';
  }
  renderTopoCanvas();
  renderTopoPanel();
}

/* --------------------------------------------------------------- abonnes */

/** Libelle de la limite appliquee, et d'ou elle vient.
 *  Afficher le plan RADIUS quand une surcharge existe serait mensonger : ce
 *  n'est pas ce que le routeur applique. */
function limitCell(r) {
  const down = r.effective_down_mbps;
  const up = r.effective_up_mbps;
  if (!down && !up) return '<span style="color:var(--faint)">-</span>';

  const texte = esc(mbps(down || 0) + ' / ' + mbps(up || 0));
  if (r.limit_source === 'plan') return texte;

  const marque = r.limit_source === 'boost'
    ? '<span class="boost-pill" style="margin-left:.4rem">boost</span>'
    : '<span class="badge warn" style="margin-left:.4rem">impose</span>';
  const plan = r.plan_down_mbps
    ? 'Plan : ' + mbps(r.plan_down_mbps) + ' / ' + mbps(r.plan_up_mbps || 0)
    : 'Aucun plan RADIUS';
  const note = r.policy_note || r.boost_reason;
  const couleur = r.limit_source === 'boost' ? '#a78bfa' : 'var(--warn)';

  return '<span title="' + esc(plan + (note ? ' — ' + note : '')) + '">' +
    '<span style="color:' + couleur + '">' + texte + '</span>' + marque + '</span>';
}

async function loadSubscribers() {
  let query = state.subSearch ? '&search=' + encodeURIComponent(state.subSearch) : '';
  if (state.subPop) query += '&pop_id=' + encodeURIComponent(state.subPop);
  const bloatQuery = state.subPop ? '&pop_id=' + encodeURIComponent(state.subPop) : '';
  const [rows, pops, boosts, bloat] = await Promise.all([
    api('/subscribers/latest?limit=200' + query),
    api('/pops'),
    api('/shaping/boosts').catch(() => []),
    api('/bufferbloat?minutes=60' + bloatQuery).catch(() => null),
  ]);

  // Note de bufferbloat par abonne : latence a vide vs sous charge, calculee en
  // correlant RTT et debit deja collectes.
  const bloatParId = {};
  if (bloat) (bloat.subscribers || []).forEach((b) => { bloatParId[b.subscriber_id] = b; });

  // Le filtre PoP repond a "voir les connexions depuis un PoP".
  const select = document.getElementById('sub-pop');
  if (select.dataset.filled !== String(pops.length)) {
    select.innerHTML = '<option value="">Tous les PoPs</option>' +
      pops.map((p) => '<option value="' + p.id + '">' + esc(p.name) +
        ' (' + p.subscriber_count + ')</option>').join('');
    select.dataset.filled = String(pops.length);
    select.value = state.subPop || '';
  }

  const parLogin = {};
  (boosts || []).forEach((b) => { if (b.scope === 'subscriber') parLogin[b.target_key] = b; });

  let compte = rows.length + ' session(s)' + (state.subPop ? ' sur ce PoP' : '');
  if (bloat && bloat.summary && bloat.summary.measured) {
    const dist = bloat.summary.distribution || {};
    const mauvais = (dist.D || 0) + (dist.F || 0);
    compte += ' · bufferbloat : ' + bloat.summary.measured + ' mesure(s)' +
      (mauvais ? ', ' + mauvais + ' degrade(s)' : ', tous bons') +
      (bloat.summary.worst_bloat_ms ? ' (pire +' + bloat.summary.worst_bloat_ms + ' ms)' : '');
  }
  document.getElementById('sub-count').textContent = compte;

  const host = document.getElementById('subscribers-table');
  if (!rows.length) {
    host.innerHTML = '<div class="empty">' +
      (state.subSearch || state.subPop ? 'Aucune session ne correspond au filtre.' :
        'Aucune mesure. Connectez un PoP et ouvrez une session PPPoE.') + '</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Login PPPoE</th><th>PoP</th>' +
    '<th class="num" title="Debit reellement applique">Limite</th>' +
    '<th class="num">Download</th><th style="width:140px">vs limite</th>' +
    '<th class="num">Upload</th><th class="num">Latence</th>' +
    '<th title="Latence ajoutee sous charge (A+ imperceptible, F injouable)">Bufferbloat</th>' +
    '<th>Boost</th>' +
    '<th class="num">Session</th><th class="num">Mesure</th>' +
    '<th class="sticky-actions"></th>' +
    '</tr></thead><tbody>' +
    rows.map((r) => {
      // La jauge se compare a la limite APPLIQUEE, pas au plan commercial :
      // un abonne bride a 512 kbps qui en consomme 400 est a 78 %, pas a 0,08 %.
      const limiteDown = (r.effective_down_mbps || 0) * 1e6;
      return '<tr class="clickable" data-sub="' + r.subscriber_id + '">' +
        '<td class="login">' + esc(r.pppoe_login) + '</td>' +
        '<td>' + esc(r.pop_name || '-') + '</td>' +
        '<td class="num">' + limitCell(r) + '</td>' +
        '<td class="num" style="color:var(--down)">' + esc(bpsText(r.tx_bps)) + '</td>' +
        '<td>' + meter(r.tx_bps, limiteDown) + '</td>' +
        '<td class="num" style="color:var(--up)">' + esc(bpsText(r.rx_bps)) + '</td>' +
        '<td class="num">' + rtt(r.rtt_ms) + '</td>' +
        '<td>' + bloatBadge(bloatParId[r.subscriber_id]) + '</td>' +
        '<td>' + (parLogin[r.pppoe_login]
          ? '<span class="boost-pill" title="' +
            esc(parLogin[r.pppoe_login].boost_reason || '') + '">' +
            esc(Math.max(0, Math.round(parLogin[r.pppoe_login].seconds_left / 60))) +
            ' min</span>'
          : '<span style="color:var(--faint)">-</span>') + '</td>' +
        '<td class="num">' + esc(uptime(r.session_uptime_s)) + '</td>' +
        '<td class="num" style="color:var(--faint)">' + esc(clock(r.ts)) + '</td>' +
        '<td class="sticky-actions"><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm" data-bw="' + esc(r.pppoe_login) + '">Debit</button>' +
          '<button class="sm" data-boost="' + esc(r.pppoe_login) + '">Boost</button>' +
        '</div></td>' +
        '</tr>';
    }).join('') + '</tbody></table>';

  host.querySelectorAll('tr[data-sub]').forEach((tr) => {
    // Un clic sur un bouton d'action ne doit pas aussi ouvrir la fiche.
    tr.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      openSubscriber(tr.dataset.sub);
    });
  });
  host.querySelectorAll('[data-bw]').forEach((b) => {
    const ligne = rows.find((r) => r.pppoe_login === b.dataset.bw);
    b.addEventListener('click', () => openBandwidthEditor('subscriber', ligne));
  });
  host.querySelectorAll('[data-boost]').forEach((b) => {
    const ligne = rows.find((r) => r.pppoe_login === b.dataset.boost);
    b.addEventListener('click', () => openBoostEditor(ligne));
  });
}

async function openSubscriber(id) {
  const root = document.getElementById('drawer-root');
  root.innerHTML = '<div class="drawer-backdrop"></div><div class="drawer">' +
    '<div class="empty">Chargement...</div></div>';
  root.querySelector('.drawer-backdrop').addEventListener('click', closeDrawer);

  try {
    const data = await api('/subscribers/' + id + '/metrics?minutes=60&bucket_seconds=30');
    const s = data.subscriber;
    root.querySelector('.drawer').innerHTML =
      '<div class="drawer-head"><h3>' + esc(s.pppoe_login) + '</h3>' +
      '<button class="sm" id="drawer-close">Fermer</button></div>' +
      '<div class="grid stats" style="margin-bottom:1rem">' +
        statCard('', 'PoP', esc(s.pop_name || '-'), '', '') +
        statCard('', 'Limite appliquee',
          s.effective_down_mbps
            ? esc(mbps(s.effective_down_mbps) + ' / ' + mbps(s.effective_up_mbps || 0))
            : '-',
          '',
          s.limit_source === 'boost'
            ? '<span class="boost-pill">boost</span>'
            : s.limit_source === 'override'
              ? '<span class="badge warn">impose</span> plan : ' +
                esc(mbps(s.plan_down_mbps || 0))
              : esc(s.plan_source || 'plan RADIUS')) +
        statCard('', 'Cible de la file', esc(s.last_ip ? s.last_ip + '/32' : '-'), '',
          s.last_ip
            ? 'adresse de la session'
            : '<span style="color:var(--warn)">hors ligne : aucune file</span>') +
        statCard('', 'Bufferbloat',
          data.bufferbloat ? esc(data.bufferbloat.grade) : 'n/d', '',
          data.bufferbloat
            ? 'a vide ' + esc(data.bufferbloat.idle_ms) + ' ms, sous charge ' +
              esc(data.bufferbloat.loaded_ms) + ' ms'
            : 'charge insuffisante pour mesurer') +
      '</div>' +
      (data.bufferbloat
        ? '<div class="notice"><b>Latence sous charge.</b> La latence passe de ' +
          '<b>' + esc(data.bufferbloat.idle_ms) + ' ms</b> a vide a <b>' +
          esc(data.bufferbloat.loaded_ms) + ' ms</b> quand le lien se remplit, ' +
          'soit <b>+' + esc(data.bufferbloat.bloat_ms) + ' ms</b> de bufferbloat ' +
          '(note ' + esc(data.bufferbloat.grade) + ').' +
          '<span class="hint">Deduit en correlant RTT (sonde active) et debit du ' +
          'meme echantillon, sur ' + esc(data.bufferbloat.samples) + ' point(s). ' +
          'Shaper legerement sous la capacite du lien fait tomber ce chiffre : la ' +
          'file se forme alors dans CAKE, ou elle est geree, pas dans le buffer radio.' +
          '</span></div>'
        : data.points.some((p) => p.rtt_ms_avg !== null && p.rtt_ms_avg !== undefined)
          ? '<div class="notice">Latence sur la fenetre : moyenne ' +
            rtt(Math.max(...data.points.map((p) => p.rtt_ms_avg || 0))) +
            ', pire ' + rtt(Math.max(...data.points.map((p) => p.rtt_ms_max || 0))) +
            '<span class="hint">Sonde active depuis le PoP. Pas encore assez de ' +
            'charge sur la fenetre pour en deduire un bufferbloat.</span></div>'
          : '') +
      '<h2>Derniere heure</h2><div class="card"><div id="sub-chart"></div></div>';
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    renderThroughput(document.getElementById('sub-chart'),
      data.points.map((p) => ({ bucket: p.bucket, tx_bps: p.tx_bps_max, rx_bps: p.rx_bps_max, subscribers: p.samples })));
  } catch (err) {
    root.querySelector('.drawer').innerHTML =
      '<div class="drawer-head"><h3>Erreur</h3><button class="sm" id="drawer-close">Fermer</button></div>' +
      '<div class="notice err">' + esc(err.message) + '</div>';
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  }
}
function closeDrawer() {
  state.link = null;
  state.linkLive = null;
  document.getElementById('drawer-root').innerHTML = '';
}

/* ------------------------------------------------------------------ PoPs */

async function loadPops() {
  const pops = await api('/pops');
  const host = document.getElementById('pops-table');
  if (!pops.length) {
    host.innerHTML = '<div class="empty">Aucun site. Ils apparaissent des qu\'un ' +
      'routeur remonte des sessions.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Site</th><th>Routeur</th><th class="num">Abonnes</th>' +
    '<th class="num">Backhauls</th><th></th></tr></thead><tbody>' +
    pops.map((p) => '<tr>' +
      '<td><strong>' + esc(p.name) + '</strong></td>' +
      '<td class="login">' + esc(p.router_host || '-') + '</td>' +
      '<td class="num">' + p.subscriber_count + '</td>' +
      '<td class="num">' + p.backhaul_count + '</td>' +
      '<td><div class="actions" style="justify-content:flex-end">' +
        '<button class="sm danger" data-del-pop="' + p.id + '">Supprimer</button>' +
      '</div></td></tr>').join('') + '</tbody></table>';

  host.querySelectorAll('[data-del-pop]').forEach((b) => {
    const pop = pops.find((p) => String(p.id) === b.dataset.delPop);
    b.addEventListener('click', async () => {
      if (!confirm('Supprimer definitivement "' + pop.name + '" ?\n\n' +
          pop.subscriber_count + ' abonne(s) et ' + pop.backhaul_count +
          ' backhaul(s) seront effaces, ainsi que TOUT leur historique de mesures.\n\n' +
          'Retirez aussi le routeur de l\'inventaire, sinon le site sera recree ' +
          'au prochain cycle.')) return;
      try {
        await api('/pops/' + pop.id + '?confirm=true', { method: 'DELETE' });
        await loadRouters();
      } catch (err) { alert(err.message); }
    });
  });
}

async function loadRouters() {
  await loadPops();
  await loadAntennas();
  const data = await api('/pops/routers');
  state.routers = data.routers;

  const notice = document.getElementById('pops-notice');
  let html = '';
  if (!data.secrets_available) {
    html += '<div class="notice warn"><strong>Ajout depuis l\'interface indisponible.</strong> ' +
      esc(data.secrets_reason || '') +
      '<span class="hint">Sans cle, l\'API refuse d\'ecrire un mot de passe de routeur : ' +
      'il ne sera jamais stocke en clair. Normalement la cle est generee toute seule ' +
      'au premier demarrage dans <code>APP_SECRET_KEY_FILE</code> ; verifiez que ce ' +
      'chemin est inscriptible (avec Docker, un volume doit etre monte sur ' +
      '<code>/app/data</code>). Sinon, renseignez <code>APP_SECRET_KEY</code> ' +
      'puis redemarrez.</span></div>';
  }
  (data.skipped || []).forEach((skip) => {
    // Ancien format (chaine) ou nouveau ({name, reason}) : on gere les deux.
    const nom = typeof skip === 'string' ? null : skip.name;
    const raison = typeof skip === 'string' ? skip : skip.reason;
    html += '<div class="notice err"><strong>Routeur ignore.</strong> ' + esc(raison) +
      (nom ? '<div class="actions" style="margin-top:.5rem">' +
        '<button class="sm danger" data-hide-file="' + esc(nom) + '">Retirer definitivement</button>' +
        '</div><span class="hint">« Retirer » ecarte ce routeur de l\'inventaire ' +
        'sans toucher au fichier, et l\'avertissement disparait.</span>' : '') +
      '</div>';
  });
  // Routeurs fichier retires a la main : proposer de les restaurer.
  (data.hidden || []).forEach((h) => {
    html += '<div class="notice"><strong>' + esc(h.name) + '</strong> est retire de ' +
      'l\'inventaire fichier.' +
      '<div class="actions" style="margin-top:.5rem">' +
      '<button class="sm" data-restore-file="' + esc(h.name) + '">Restaurer</button>' +
      '</div></div>';
  });
  notice.innerHTML = html;
  notice.querySelectorAll('[data-hide-file]').forEach((b) =>
    b.addEventListener('click', () => hideFileRouter(b.dataset.hideFile)));
  notice.querySelectorAll('[data-restore-file]').forEach((b) =>
    b.addEventListener('click', () => restoreFileRouter(b.dataset.restoreFile)));
  document.getElementById('btn-save').disabled = !data.secrets_available;

  const host = document.getElementById('routers-table');
  if (!state.routers.length) {
    host.innerHTML = '<div class="empty">Aucun routeur. Utilisez le formulaire ci-dessous.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>PoP</th><th>Adresse</th><th>Compte</th><th>Source</th>' +
    '<th>Etat</th><th>Modele</th><th></th></tr></thead><tbody>' +
    state.routers.map((r) => {
      let badge = '<span class="badge">jamais teste</span>';
      if (r.last_error) badge = '<span class="badge crit" title="' + esc(r.last_error) + '">en echec</span>';
      else if (r.last_ok_at) badge = '<span class="badge ok">joignable</span>';
      else if (r.source === 'file') badge = '<span class="badge ok">actif</span>';
      if (r.enabled === false) badge = '<span class="badge">desactive</span>';

      return '<tr>' +
        '<td><strong>' + esc(r.name) + '</strong>' +
          (r.pop_name ? '<br><span style="color:var(--faint);font-size:.75rem">' + esc(r.pop_name) + '</span>' : '') + '</td>' +
        '<td class="login">' + esc(r.host) + ':' + esc(r.port) + '</td>' +
        '<td class="login">' + esc(r.username) + '</td>' +
        '<td>' + (r.source === 'file'
          ? '<span class="badge file">inventaire fichier</span>'
          : '<span class="badge">interface</span>') + '</td>' +
        '<td>' + badge + '</td>' +
        '<td style="font-size:.76rem;color:var(--muted)">' +
          esc(r.board_name || '-') + (r.routeros_version ? ' &middot; ' + esc(r.routeros_version) : '') + '</td>' +
        '<td><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm" data-config="' + esc(r.name) +
            '" title="Voir la config complete (/export) que le controleur lit">Config</button>' +
          (r.editable
            ? '<button class="sm" data-probe="' + r.id + '">Tester</button>' +
              '<button class="sm" data-toggle="' + r.id + '">' + (r.enabled ? 'Desactiver' : 'Activer') + '</button>' +
              '<button class="sm danger" data-del="' + r.id + '">Retirer</button>'
            : '<span style="font-size:.72rem;color:var(--faint);margin-right:.4rem">routers.yml</span>' +
              '<button class="sm danger" data-hide-file="' + esc(r.name) +
              '" title="Ecarter ce routeur fichier sans editer le YAML">Retirer</button>') +
        '</div></td></tr>';
    }).join('') + '</tbody></table>' +
    '<div id="router-export"></div>';

  host.querySelectorAll('[data-config]').forEach((b) =>
    b.addEventListener('click', () => showRouterExport(b.dataset.config)));
  host.querySelectorAll('[data-probe]').forEach((b) =>
    b.addEventListener('click', () => probeRouter(b.dataset.probe, b)));
  host.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', () => deleteRouter(b.dataset.del)));
  host.querySelectorAll('[data-toggle]').forEach((b) =>
    b.addEventListener('click', () => toggleRouter(b.dataset.toggle)));
  host.querySelectorAll('[data-hide-file]').forEach((b) =>
    b.addEventListener('click', () => hideFileRouter(b.dataset.hideFile)));
}

/** Affiche le /export complet d'un routeur + ce que le controleur en tire
 *  (adresses, tunnels, commentaires). C'est la vue "tout percevoir" de la config. */
async function showRouterExport(name) {
  const host = document.getElementById('router-export');
  if (!host) return;
  host.innerHTML = '<div class="muted">Lecture de la config de ' + esc(name) + '…</div>';
  try {
    const r = await api('/topology/routers/' + encodeURIComponent(name) + '/export');
    const p = r.parsed || {};
    const tuns = (p.tunnels || []).map((t) =>
      esc(t.type + ' ' + (t.name || '') + ' → ' + t.remote_address)).join(', ') || '—';
    const adrs = (p.addresses || []).length;
    const coms = Object.keys(p.comments || {}).length;
    host.innerHTML =
      '<div class="notice" style="margin-top:.6rem"><b>Config de ' + esc(name) + '</b> — ' +
        adrs + ' adresse(s), ' + (p.tunnels || []).length + ' tunnel(s), ' + coms +
        ' commentaire(s). <b>Tunnels :</b> ' + tuns +
        (r.export ? '' : '<span class="hint">L\'API n\'a pas renvoyé d\'export sur cette ' +
          'version : la découverte se rabat sur le structuré (/ip/address, voisins).</span>') +
      '</div>' +
      (r.export
        ? '<pre class="export-pre">' + esc(r.export) + '</pre>'
        : '');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

/** Ecarte un routeur de l'inventaire fichier (source de verite intacte).
 *  Le fichier gagne par defaut ; ce masquage explicite est la seule facon,
 *  cote interface, de retirer un routeur fichier — et il est reversible. */
async function hideFileRouter(name) {
  if (!confirm('Retirer "' + name + '" de l\'inventaire ?\n\n' +
    'Le routeur est ecarte (interrogation et avertissements), sans modifier ' +
    'config/routers.yml. Vous pourrez le restaurer.')) return;
  try {
    await api('/pops/routers/file/' + encodeURIComponent(name), { method: 'DELETE' });
    await loadRouters();
  } catch (err) { alert(err.message); }
}

async function restoreFileRouter(name) {
  try {
    await api('/pops/routers/file/' + encodeURIComponent(name) + '/restore', { method: 'POST' });
    await loadRouters();
  } catch (err) { alert(err.message); }
}

/** Analyse la config des equipements et (re)construit l'arbre reseau.
 *  C'est l'action centrale de l'onglet : ajouter un routeur ou une antenne,
 *  puis lire sa conf via l'API pour en deduire l'arbre — sans le dessiner a la
 *  main. Chaque ajout la relance automatiquement. */
async function buildTreeFromConfig(silencieux) {
  const notice = document.getElementById('build-notice');
  const bouton = document.getElementById('btn-build-tree');
  if (bouton) bouton.disabled = true;
  if (!silencieux && notice) {
    notice.innerHTML = '<div class="notice">Analyse de la configuration sur chaque equipement ' +
      '(/ip/neighbor, /interface, capacite radio)...</div>';
  }
  try {
    const r = await api('/topology/discover', { method: 'POST' });
    const compte = document.getElementById('build-count');
    if (compte) compte.textContent = r.nodes + ' equipement(s), ' + r.links + ' lien(s)';
    if (notice) {
      notice.innerHTML = '<div class="notice ok"><strong>Arbre construit.</strong> ' +
        r.nodes + ' equipement(s) et ' + r.links + ' lien(s) deduits de la configuration.' +
        (r.warnings && r.warnings.length
          ? '<span class="hint">' + r.warnings.map(esc).join('<br>') + '</span>' : '') +
        '<span class="hint">Ouvrez l\'onglet Topologie pour voir et reorganiser ' +
        'l\'arbre au glisser-deposer.</span></div>';
    }
    return r;
  } catch (err) {
    if (notice) notice.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return null;
  } finally {
    if (bouton) bouton.disabled = false;
  }
}

function formPayload() {
  const form = document.getElementById('router-form');
  const data = new FormData(form);
  return {
    name: (data.get('name') || '').trim(),
    host: (data.get('host') || '').trim(),
    password: data.get('password') || '',
    port: Number(data.get('port') || 8728),
    username: (data.get('username') || 'qos-ro').trim(),
    role: data.get('role') || 'pop',
    pop_name: (data.get('pop_name') || '').trim() || null,
    use_ssl: document.getElementById('f-ssl').checked,
    timeout_s: Number(data.get('timeout_s') || 5),
    pppoe_interface_pattern: data.get('pppoe_interface_pattern') || '<pppoe-{login}>',
    enabled: true,
  };
}

function showFormResult(html) { document.getElementById('form-result').innerHTML = html; }

async function testConnection() {
  const button = document.getElementById('btn-test');
  const payload = formPayload();
  if (!payload.name || !payload.host || !payload.password) {
    showFormResult('<div class="notice err">Nom, adresse et mot de passe sont requis pour tester.</div>');
    return;
  }
  button.disabled = true;
  showFormResult('<div class="notice">Connexion a ' + esc(payload.host) + ':' + esc(payload.port) + '...</div>');
  try {
    const result = await api('/pops/routers/test', { method: 'POST', body: JSON.stringify(payload) });
    if (result.reachable) {
      showFormResult('<div class="notice ok"><strong>Connexion etablie.</strong> ' +
        esc(result.identity || 'routeur') + ' &middot; ' + esc(result.board_name || '?') +
        ' &middot; RouterOS ' + esc(result.version || '?') +
        '<span class="hint">' + esc(result.ppp_active_sessions) + ' session(s) PPPoE active(s), dont ' +
        esc(result.correlated_sessions) + ' avec compteurs correles' +
        (result.ppp_active_sessions > 0 && result.correlated_sessions === 0
          ? ' — aucun debit ne pourra etre calcule, verifiez le motif d\'interface PPPoE.'
          : '.') + '</span></div>');
    } else {
      showFormResult('<div class="notice err"><strong>Echec.</strong> <code>' + esc(result.error) + '</code>' +
        '<span class="hint">' + esc(result.hint || '') + '</span></div>');
    }
  } catch (err) {
    showFormResult('<div class="notice err">' + esc(err.message) + '</div>');
  } finally {
    button.disabled = false;
  }
}

async function saveRouter(event) {
  event.preventDefault();
  const button = document.getElementById('btn-save');
  button.disabled = true;
  try {
    const created = await api('/pops/routers', { method: 'POST', body: JSON.stringify(formPayload()) });
    showFormResult('<div class="notice ok"><strong>' + esc(created.name) +
      ' enregistre.</strong><span class="hint">Sa configuration est analysee tout de ' +
      'suite pour construire l\'arbre ; il est ensuite interroge a chaque cycle, sans ' +
      'redemarrage.</span></div>');
    document.getElementById('router-form').reset();
    document.getElementById('f-username').value = 'qos-ro';
    document.getElementById('f-port').value = '8728';
    await loadRouters();
    // Ajouter un routeur, c'est vouloir le voir dans l'arbre : on analyse sa
    // conf dans la foulee plutot que d'attendre un clic ou le prochain cycle.
    await buildTreeFromConfig(false);
  } catch (err) {
    showFormResult('<div class="notice err">' + esc(err.message) + '</div>');
  } finally {
    button.disabled = false;
  }
}

async function probeRouter(id, button) {
  const original = button.textContent;
  button.disabled = true; button.textContent = '...';
  try {
    const result = await api('/pops/routers/' + id + '/probe', { method: 'POST' });
    if (!result.reachable) {
      alert('Echec : ' + result.error + '\n\n' + (result.hint || ''));
    }
  } catch (err) {
    alert(err.message);
  } finally {
    button.disabled = false; button.textContent = original;
    await loadRouters();
  }
}

async function deleteRouter(id) {
  const router = state.routers.find((r) => String(r.id) === String(id));
  if (!confirm('Retirer "' + (router ? router.name : id) + '" de l\'inventaire ?\n\n' +
    'Les metriques deja collectees sont conservees.')) return;
  try {
    await api('/pops/routers/' + id, { method: 'DELETE' });
    await loadRouters();
  } catch (err) { alert(err.message); }
}

async function toggleRouter(id) {
  const router = state.routers.find((r) => String(r.id) === String(id));
  if (!router) return;
  try {
    await api('/pops/routers/' + id, {
      method: 'PATCH', body: JSON.stringify({ enabled: !router.enabled }),
    });
    await loadRouters();
  } catch (err) { alert(err.message); }
}


/* ------------------------------------------------- connexion a distance */

const REMOTE_STATUS = {
  ok: '<span class="badge ok">joignable</span>',
  error: '<span class="badge crit">en echec</span>',
  disabled: '<span class="badge">desactive</span>',
  unknown: '<span class="badge">jamais teste</span>',
};

/** Page facon LibreQoS : la joignabilite de chaque integration distante en un
 *  coup d'oeil (RouterOS, airOS, UISP, RADIUS), plus le detail par equipement. */
async function loadRemote() {
  const data = await api('/remote/status');
  const integrations = data.integrations || [];

  const totalOk = integrations.reduce((a, i) => a + (i.summary.ok || 0), 0);
  const totalDev = integrations.reduce((a, i) => a + (i.summary.total || 0), 0);
  document.getElementById('remote-count').textContent =
    totalDev + ' equipement(s) distant(s), ' + totalOk + ' joignable(s)';

  document.getElementById('remote-integrations').innerHTML = integrations.map((i) => {
    const s = i.summary || { total: 0, ok: 0, error: 0 };
    const etat = !i.configured
      ? '<span class="badge">non configure</span>'
      : s.error
        ? '<span class="badge crit">' + s.error + ' en echec</span>'
        : s.total
          ? '<span class="badge ok">' + s.ok + '/' + s.total + ' joignable(s)</span>'
          : '<span class="badge ok">actif</span>';
    return '<div class="card">' +
      '<div class="node-head" style="margin-bottom:.5rem">' +
        '<div class="node-title">' + esc(i.label) + '</div>' + etat + '</div>' +
      '<div class="child" style="border:0;padding:.2rem 0;font-size:.76rem;color:var(--muted)">' +
        esc(i.transport) + '</div>' +
      (i.endpoint ? '<div class="child" style="border:0;padding:.2rem 0;font-size:.76rem">' +
        '<span class="name" style="color:var(--faint)">Endpoint</span>' +
        '<span class="host">' + esc(i.endpoint) + '</span></div>' : '') +
      (i.provider ? '<div class="child" style="border:0;padding:.2rem 0;font-size:.76rem">' +
        '<span class="name" style="color:var(--faint)">Fournisseur</span>' +
        '<span class="host">' + esc(i.provider) + '</span></div>' : '') +
      (i.note ? '<div class="child" style="border:0;padding:.2rem 0;font-size:.74rem;color:var(--faint)">' +
        esc(i.note) + '</div>' : '') +
      '</div>';
  }).join('');

  const devices = [];
  integrations.forEach((i) => (i.devices || []).forEach((d) => devices.push({ ...d, kind: i.label })));
  const host = document.getElementById('remote-devices');
  if (!devices.length) {
    host.innerHTML = '<div class="empty">Aucun equipement distant enregistre. ' +
      'Ajoutez-en dans l\'onglet Equipements.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Equipement</th><th>Integration</th><th>Adresse</th>' +
    '<th>Etat</th><th>Detail</th><th class="num">Derniere connexion OK</th>' +
    '</tr></thead><tbody>' +
    devices.map((d) => '<tr>' +
      '<td><strong>' + esc(d.name) + '</strong></td>' +
      '<td>' + esc(d.kind) + (d.source === 'file'
        ? ' <span class="badge file">fichier</span>' : '') + '</td>' +
      '<td class="login">' + esc(d.host) + '</td>' +
      '<td>' + (REMOTE_STATUS[d.status] || esc(d.status)) + '</td>' +
      '<td style="font-size:.76rem;color:var(--muted)">' + esc(d.detail || '') + '</td>' +
      '<td class="num" style="color:var(--faint)">' +
        (d.last_ok_at ? esc(clock(d.last_ok_at)) : '-') + '</td>' +
      '</tr>').join('') + '</tbody></table>';
}

/* ---------------------------------------------------- antennes Ubiquiti */

async function loadAntennas() {
  const data = await api('/pops/antennas');
  state.antennas = data.antennas;

  const notice = document.getElementById('antennas-notice');
  notice.innerHTML = !data.secrets_available
    ? '<div class="notice warn"><strong>Mot de passe non stockable.</strong> ' +
      esc(data.secrets_reason || '') + '<span class="hint">Vous pouvez tout de meme ' +
      'ajouter une antenne dont le <code>/status.cgi</code> est ouvert en lecture ' +
      '(sans mot de passe).</span></div>'
    : '';
  document.getElementById('a-btn-save').disabled = false;

  const host = document.getElementById('antennas-table');
  if (!state.antennas.length) {
    host.innerHTML = '<div class="empty">Aucune antenne. Utilisez le formulaire ci-dessous.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Lien</th><th>Adresse</th><th>PoP</th>' +
    '<th class="num">Capacite lue</th><th>Etat</th><th></th></tr></thead><tbody>' +
    state.antennas.map((a) => {
      let badge = '<span class="badge">jamais lue</span>';
      if (a.last_error) badge = '<span class="badge crit" title="' + esc(a.last_error) + '">en echec</span>';
      else if (a.last_ok_at) badge = '<span class="badge ok">joignable</span>';
      if (a.enabled === false) badge = '<span class="badge">desactivee</span>';
      return '<tr>' +
        '<td><strong>' + esc(a.name) + '</strong>' +
          (a.device_key ? '<br><span style="color:var(--faint);font-size:.72rem">' +
            esc(a.device_key) + '</span>' : '') + '</td>' +
        '<td class="login">' + esc(a.host) + '</td>' +
        '<td>' + esc(a.pop_name) + '</td>' +
        '<td class="num">' + (a.last_capacity_mbps != null ? esc(mbps(a.last_capacity_mbps)) :
          '<span style="color:var(--faint)">-</span>') + '</td>' +
        '<td>' + badge + '</td>' +
        '<td><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm" data-a-probe="' + a.id + '">Tester</button>' +
          '<button class="sm" data-a-toggle="' + a.id + '">' +
            (a.enabled ? 'Desactiver' : 'Activer') + '</button>' +
          '<button class="sm danger" data-a-del="' + a.id + '">Retirer</button>' +
        '</div></td></tr>';
    }).join('') + '</tbody></table>';

  host.querySelectorAll('[data-a-probe]').forEach((b) =>
    b.addEventListener('click', () => probeAntenna(b.dataset.aProbe, b)));
  host.querySelectorAll('[data-a-del]').forEach((b) =>
    b.addEventListener('click', () => deleteAntenna(b.dataset.aDel)));
  host.querySelectorAll('[data-a-toggle]').forEach((b) =>
    b.addEventListener('click', () => toggleAntenna(b.dataset.aToggle)));
}

function antennaPayload() {
  const data = new FormData(document.getElementById('antenna-form'));
  const nominal = data.get('nominal_capacity_mbps');
  return {
    name: (data.get('name') || '').trim(),
    pop_name: (data.get('pop_name') || '').trim(),
    host: (data.get('host') || '').trim(),
    username: (data.get('username') || 'ubnt').trim(),
    password: data.get('password') || null,
    device_key: (data.get('device_key') || '').trim() || null,
    nominal_capacity_mbps: nominal ? Number(nominal) : null,
    verify_tls: document.getElementById('a-tls').checked,
    timeout_s: Number(data.get('timeout_s') || 10),
    enabled: true,
  };
}

function showAntennaResult(html) {
  document.getElementById('antenna-result').innerHTML = html;
}

function antennaCapacityLine(result) {
  return '<div class="notice ok"><strong>Antenne joignable.</strong> Capacite lue : ' +
    esc(mbps(result.capacity_mbps || 0)) +
    (result.capacity_down_mbps != null
      ? ' (down ' + esc(mbps(result.capacity_down_mbps)) + ' / up ' +
        esc(mbps(result.capacity_up_mbps || 0)) + ')'
      : '') +
    '<span class="hint">' +
    (result.signal_dbm != null ? 'Signal ' + esc(result.signal_dbm) + ' dBm. ' : '') +
    (result.mac ? 'MAC ' + esc(result.mac) + '.' : '') + '</span></div>';
}

async function testAntenna() {
  const button = document.getElementById('a-btn-test');
  const payload = antennaPayload();
  if (!payload.name || !payload.host) {
    showAntennaResult('<div class="notice err">Nom et adresse sont requis pour tester.</div>');
    return;
  }
  button.disabled = true;
  showAntennaResult('<div class="notice">Lecture de ' + esc(payload.host) + '...</div>');
  try {
    const result = await api('/pops/antennas/test', { method: 'POST', body: JSON.stringify(payload) });
    showAntennaResult(result.reachable
      ? antennaCapacityLine(result)
      : '<div class="notice err"><strong>Echec.</strong> <code>' + esc(result.error) + '</code>' +
        '<span class="hint">' + esc(result.hint || '') + '</span></div>');
  } catch (err) {
    showAntennaResult('<div class="notice err">' + esc(err.message) + '</div>');
  } finally {
    button.disabled = false;
  }
}

async function saveAntenna(event) {
  event.preventDefault();
  const button = document.getElementById('a-btn-save');
  button.disabled = true;
  try {
    const created = await api('/pops/antennas', { method: 'POST', body: JSON.stringify(antennaPayload()) });
    showAntennaResult('<div class="notice ok"><strong>' + esc(created.name) +
      ' enregistree.</strong><span class="hint">Sa capacite est lue et rattachee a ' +
      'l\'arbre tout de suite, puis a chaque cycle, sans redemarrage.</span></div>');
    document.getElementById('antenna-form').reset();
    document.getElementById('a-username').value = 'ubnt';
    document.getElementById('a-timeout').value = '10';
    await loadAntennas();
    await buildTreeFromConfig(false);
  } catch (err) {
    showAntennaResult('<div class="notice err">' + esc(err.message) + '</div>');
  } finally {
    button.disabled = false;
  }
}

async function probeAntenna(id, button) {
  const original = button.textContent;
  button.disabled = true; button.textContent = '...';
  try {
    const result = await api('/pops/antennas/' + id + '/probe', { method: 'POST' });
    if (!result.reachable) alert('Echec : ' + result.error + '\n\n' + (result.hint || ''));
  } catch (err) {
    alert(err.message);
  } finally {
    button.disabled = false; button.textContent = original;
    await loadAntennas();
  }
}

async function deleteAntenna(id) {
  const antenna = state.antennas.find((a) => String(a.id) === String(id));
  if (!confirm('Retirer "' + (antenna ? antenna.name : id) + '" ?\n\n' +
    'Les metriques deja collectees sont conservees.')) return;
  try {
    await api('/pops/antennas/' + id, { method: 'DELETE' });
    await loadAntennas();
  } catch (err) { alert(err.message); }
}

async function toggleAntenna(id) {
  const antenna = state.antennas.find((a) => String(a.id) === String(id));
  if (!antenna) return;
  try {
    await api('/pops/antennas/' + id, {
      method: 'PATCH', body: JSON.stringify({ enabled: !antenna.enabled }),
    });
    await loadAntennas();
  } catch (err) { alert(err.message); }
}


/* ------------------------------------------------------------- topologie */

const KIND_LABEL = {
  gateway: 'Gateway', core: 'Coeur', pop: 'PoP', radio: 'Radio',
  sector: 'Secteur', cpe: 'CPE', client: 'Client', unknown: 'Inconnu', subscriber: 'Abonnes',
};
const KIND_COLOR = {
  gateway: 'var(--accent)', core: 'var(--accent)', pop: 'var(--down)',
  radio: 'var(--up)', sector: 'var(--up)', cpe: 'var(--muted)', client: '#a78bfa',
  unknown: 'var(--faint)', subscriber: '#a78bfa',
};

/* ------------------------------------------------- editeur d'arbre reseau */

const KIND_ORDER = ['gateway', 'core', 'pop', 'radio', 'sector', 'cpe', 'client', 'unknown'];
const NODE_W = 176;
const NODE_H = 48;

/** Etat de l'editeur, conserve entre deux rafraichissements : disposition,
 *  case selectionnee, et si l'on montre les liens sans debit. */
const topo = {
  data: null, subs: [], model: null, selected: null, dragging: false,
  rateOnly: true, linkMode: false, linkSource: null,
};

async function loadTopology() {
  // Onglet Topologie : le tableau technique des liens. L'arbre visuel, lui, vit
  // dans l'onglet Arbre reseau (meme donnees, partagees via fetchTopo).
  const data = await fetchTopo();
  renderTopologyLinks(data.links);
}

/** Rang d'un role : plus petit = plus en amont. Sert a orienter un lien quand
 *  aucun parent n'a ete force a la main (la decouverte de voisinage est
 *  symetrique : elle dit "adjacents", pas "lequel est au-dessus"). */
const TOPO_RANG = { gateway: 0, core: 1, pop: 2, radio: 3, sector: 3, cpe: 4, client: 4, unknown: 5 };

/** Construit l'arbre : parent force (parent_override) prioritaire, sinon
 *  orientation par role. Un seul parent par case, cycles coupes. */
/** Un lien est-il assez SUR pour dessiner une adjacence directe dans l'arbre ?
 *
 *  - manuel (pose par l'operateur) ou UISP/radio declare : oui.
 *  - lien porte par un port local vu par UN SEUL voisin (point-a-point) : oui,
 *    c'est un vrai cable/lien radio.
 *  - port vu par PLUSIEURS voisins (interface_links > 1) : NON. C'est un segment
 *    partage (switch, VLAN de gestion) ou MNDP/LLDP montre tout le monde : rien
 *    ne prouve qui est relie a qui. On ne fabrique pas ce maillage.
 */
function topoLinkConfident(l) {
  const key = String(l.key || '');
  if (key.indexOf('manual:') === 0 || l.discovered_by === 'manual') return true;
  // Lien deduit de la config (sous-reseau /30 point-a-point) : preuve directe.
  let a = l.attributes;
  if (typeof a === 'string') { try { a = JSON.parse(a); } catch (e) { a = null; } }
  if (a && a.config_link) return true;
  if (!l.interface) return true;               // UISP / radio declare, sans port
  const peers = Number(l.interface_links) || 0;
  return peers <= 1;                            // point-a-point seulement
}

function topoBuildModel(data) {
  const nodes = new Map();
  data.nodes.forEach((n) => {
    if (n.hidden) return;
    nodes.set(n.key, { ...n, children: [], parentKey: null, edge: null, depth: 0 });
  });

  // Liens exploitables (les deux extremites visibles), orientes par role.
  const oriented = [];
  data.links.forEach((l) => {
    if (!nodes.has(l.source_key) || !nodes.has(l.target_key)) return;
    const ra = TOPO_RANG[nodes.get(l.source_key).kind] ?? 5;
    const rb = TOPO_RANG[nodes.get(l.target_key).kind] ?? 5;
    const conf = topoLinkConfident(l);
    if (rb < ra) {
      oriented.push({ parentKey: l.target_key, childKey: l.source_key, link: l,
        inverted: true, confident: conf });
    } else {
      oriented.push({ parentKey: l.source_key, childKey: l.target_key, link: l,
        inverted: false, confident: conf });
    }
  });

  // Index des liens par paire, pour retrouver le debit d'un rattachement force.
  const linkByPair = new Map();
  oriented.forEach((e) => {
    linkByPair.set(e.parentKey + '\u0000' + e.childKey, e);
    linkByPair.set(e.childKey + '\u0000' + e.parentKey, { ...e, inverted: !e.inverted });
  });

  const wouldCycle = (childKey, parentKey) => {
    let cur = parentKey;
    const seen = new Set();
    while (cur) {
      if (cur === childKey) return true;
      if (seen.has(cur)) return true;
      seen.add(cur);
      cur = nodes.get(cur)?.parentKey || null;
    }
    return false;
  };

  const attach = (childKey, parentKey, edge) => {
    const child = nodes.get(childKey);
    const parent = nodes.get(parentKey);
    if (!child || !parent || childKey === parentKey || child.parentKey) return;
    if (wouldCycle(childKey, parentKey)) return;
    child.parentKey = parentKey;
    child.edge = edge || null;
    parent.children.push(child);
  };

  // 1) parents forces a la main.
  nodes.forEach((n) => {
    if (n.parent_override && nodes.has(n.parent_override)) {
      const edge = linkByPair.get(n.parent_override + '\u0000' + n.key) || null;
      attach(n.key, n.parent_override, edge);
    }
  });
  // 2) par role, en DEUX passes pour ne perdre aucune vraie adjacence :
  //   a) d'abord les liens SURS (point-a-point, UISP, manuels) : ils priment
  //      toujours et forment l'ossature fiable de l'arbre.
  //   b) puis, pour un noeud encore SANS parent, on retombe sur son meilleur
  //      lien de segment partage plutot que de le laisser orphelin -- on prefere
  //      un rattachement probable, marque INCERTAIN (trait pointille), a un trou.
  // Un noeud n'a jamais qu'UN parent (attach ne l'ecrit qu'une fois) : pas de
  // maillage, mais plus de routeur detache a tort non plus.
  const rang = (k) => TOPO_RANG[nodes.get(k)?.kind] ?? 5;
  oriented.filter((e) => e.confident).forEach((e) => attach(e.childKey, e.parentKey, e));
  oriented
    .filter((e) => !e.confident && !nodes.get(e.childKey)?.parentKey)
    // Meilleur parent d'abord : le plus haut dans la hierarchie (coeur/gateway
    // avant un PoP voisin), pour eviter de rattacher a un frere par hasard.
    .sort((a, b) => rang(a.parentKey) - rang(b.parentKey))
    .forEach((e) => attach(e.childKey, e.parentKey, { ...e, uncertain: true }));

  // Rattache les abonnes a leur PoP : un noeud agrege repliable par PoP plutot
  // que 500 cases. Le debit de l'arete est la somme du trafic des abonnes.
  const parPop = new Map();
  (topo.subs || []).forEach((s) => {
    if (!s.pop_name) return;
    if (!parPop.has(s.pop_name)) parPop.set(s.pop_name, []);
    parPop.get(s.pop_name).push(s);
  });
  if (parPop.size) {
    [...nodes.values()].forEach((n) => {
      const abonnes = parPop.get(n.name);
      if (!abonnes || !abonnes.length) return;
      const tx = abonnes.reduce((a, s) => a + (Number(s.tx_bps) || 0), 0);
      const rx = abonnes.reduce((a, s) => a + (Number(s.rx_bps) || 0), 0);
      const synth = {
        key: 'abos:' + n.key, name: abonnes.length + ' abonne(s)', kind: 'subscriber',
        synthetic: true, parentKey: n.key, children: [], edge: null,
        synthRates: (tx || rx) ? { down: tx, up: rx, cap: 0 } : null,
        count: abonnes.length, addresses: [], fresh: true,
      };
      nodes.set(synth.key, synth);
      n.children.push(synth);
    });
  }

  // Pour un noeud reste SANS parent, on retient les liens de segment partage
  // qu'on a refuse d'auto-tracer : le panneau proposera de le rattacher a la
  // main a ces candidats probables, plutot que de le laisser orphelin sans
  // explication.
  nodes.forEach((n) => {
    if (n.parentKey || n.synthetic) return;
    const cands = [];
    const vus = new Set();
    oriented.forEach((e) => {
      if (e.childKey !== n.key || e.confident) return;
      const p = nodes.get(e.parentKey);
      if (p && !vus.has(p.key)) { vus.add(p.key); cands.push({ key: p.key, name: p.name }); }
    });
    if (cands.length) n.unsureParents = cands;
  });

  const roots = [...nodes.values()].filter((n) => !n.parentKey);
  return { nodesByKey: nodes, roots };
}

/** Range les cases : position enregistree si elle existe, sinon disposition
 *  automatique en arbre couche (parent a gauche, enfants a droite). */
function topoAutoLayout(model) {
  const COL = 268;   // large : laisse la place au debit sur l'arete
  const ROWH = 74;
  const MX = 26;
  const MY = 22;
  let leaf = 0;
  const rowOf = new Map();
  const place = (node, depth, guard) => {
    if (guard.has(node.key)) return;   // securite anti-boucle
    guard.add(node.key);
    node.depth = depth;
    if (!node.children.length) {
      rowOf.set(node.key, leaf++);
    } else {
      node.children.forEach((c) => place(c, depth + 1, guard));
      const rows = node.children.map((c) => rowOf.get(c.key)).filter((r) => r !== undefined);
      rowOf.set(node.key, rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : leaf++);
    }
  };
  const guard = new Set();
  model.roots.forEach((r) => place(r, 0, guard));

  model.nodesByKey.forEach((n) => {
    const autoX = MX + n.depth * COL;
    const autoY = MY + (rowOf.get(n.key) || 0) * ROWH;
    n.x = n.pos_x != null ? Number(n.pos_x) : autoX;
    n.y = n.pos_y != null ? Number(n.pos_y) : autoY;
  });
}

/** Debit d'une arete, oriente vers l'enfant (descendant = vers le bas de l'arbre). */
function topoEdgeRates(edge) {
  if (!edge || !edge.link) return null;
  const l = edge.link;
  const down = edge.inverted ? l.rx_bps : l.tx_bps;
  const up = edge.inverted ? l.tx_bps : l.rx_bps;
  if ((down === null || down === undefined) && (up === null || up === undefined)) return null;
  const cap = (l.port_capacity_mbps || l.capacity_mbps || 0) * 1e6;
  return { down: down || 0, up: up || 0, cap };
}

function renderTopoCanvas() {
  const host = document.getElementById('topo-canvas');
  const data = topo.data;
  if (!data || !data.nodes.length) {
    host.innerHTML = '<div class="empty">Aucun equipement decouvert.<br>' +
      'Lancez la decouverte : elle lit /ip/neighbor sur chaque PoP pour ' +
      'construire l\'arbre.</div>';
    return;
  }
  const model = topoBuildModel(data);
  topoAutoLayout(model);
  topo.model = model;

  let maxX = 0;
  let maxY = 0;
  model.nodesByKey.forEach((n) => {
    maxX = Math.max(maxX, n.x + NODE_W);
    maxY = Math.max(maxY, n.y + NODE_H);
  });
  const W = Math.max(host.clientWidth - 2, maxX + 30);
  const H = Math.max(host.clientHeight - 2, maxY + 30);

  const parts = ['<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '">'];

  // Aretes d'abord (derriere les cases).
  parts.push('<g class="topo-edges">');
  model.nodesByKey.forEach((n) => {
    if (!n.parentKey) return;
    const p = model.nodesByKey.get(n.parentKey);
    if (!p) return;
    const rates = n.synthRates || topoEdgeRates(n.edge);
    // Un lien FORCE (parent pose a la main) ou MANUEL est toujours dessine :
    // sinon un lien qu'on vient de creer disparaitrait sous "debit seulement".
    const linkKey = (n.edge && n.edge.link && n.edge.link.key) || null;
    const manual = !!linkKey && String(linkKey).indexOf('manual:') === 0;
    const forced = n.parent_override && n.parent_override === p.key;
    if (!rates && topo.rateOnly && !forced && !manual) return;

    const x1 = p.x + NODE_W;
    const y1 = p.y + NODE_H / 2;
    const x2 = n.x;
    const y2 = n.y + NODE_H / 2;
    const dx = Math.max(28, Math.abs(x2 - x1) / 2);
    const d = 'M ' + x1 + ' ' + y1 + ' C ' + (x1 + dx) + ' ' + y1 + ', ' +
      (x2 - dx) + ' ' + y2 + ', ' + x2 + ' ' + y2;

    let cls = 'topo-edge';
    if (!rates) {
      cls += (forced || manual) ? ' forced' : ' faint';
    } else if (rates.cap) {
      cls += ' ' + severity(pct(Math.max(rates.down, rates.up), rates.cap));
    }
    // Adjacence probable (segment partage), pas prouvee point-a-point : pointille.
    if (n.edge && n.edge.uncertain && !forced && !manual) cls += ' uncertain';
    // Zone de clic large et transparente derriere le trait : un lien se
    // supprime en cliquant dessus (les abonnes agreges n'ont pas de lien reel).
    if (!n.synthetic) {
      parts.push('<path class="topo-edge-hit" d="' + d + '" data-edge-child="' + esc(n.key) +
        '" data-edge-link="' + esc(linkKey || '') + '"><title>Cliquer pour retirer ce lien' +
        '</title></path>');
    }
    parts.push('<path class="' + cls + '" d="' + d + '"></path>');

    if (rates) {
      const mx = (x1 + x2) / 2;
      const my = (y1 + y2) / 2 - 4;
      // Format compact (640M / 1.2G) pour tenir dans le court intervalle entre
      // deux cases ; le tableau des liens plus bas donne la valeur complete.
      const texte = '↓' + bpsShort(rates.down) + ' ↑' + bpsShort(rates.up);
      const w = texte.length * 6.0 + 10;
      parts.push('<rect x="' + (mx - w / 2) + '" y="' + (my - 11) + '" width="' + w +
        '" height="15" rx="4" fill="var(--surface)" opacity="0.9"></rect>');
      parts.push('<text class="topo-edge-label" x="' + mx + '" y="' + my +
        '" text-anchor="middle"><tspan class="d">&#8595;' + esc(bpsShort(rates.down)) +
        '</tspan> <tspan class="u">&#8593;' + esc(bpsShort(rates.up)) + '</tspan></text>');
    }
  });
  parts.push('</g>');

  // Cases.
  parts.push('<g class="topo-nodes">');
  model.nodesByKey.forEach((n) => {
    const color = KIND_COLOR[n.kind] || 'var(--faint)';
    let cls = 'topo-node';
    if (n.synthetic) cls += ' synthetic';
    if (topo.selected === n.key) cls += ' selected';
    if (topo.linkSource === n.key) cls += ' linksrc';
    if (n.fresh === false) cls += ' stale';
    // Rassemble toutes les adresses de l'equipement plutot que d'en montrer une.
    const meta = n.synthetic
      ? (n.synthRates ? bpsText(n.synthRates.down) + ' / ' + bpsText(n.synthRates.up) : 'abonnes')
      : (n.addresses && n.addresses.length ? n.addresses.join(', ')
        : (n.address || n.platform || ''));
    parts.push(
      '<g class="' + cls + '" data-node="' + esc(n.key) +
        (n.synthetic ? '" data-synthetic="1' : '') + '" transform="translate(' +
        n.x + ',' + n.y + ')">' +
        '<rect class="box" width="' + NODE_W + '" height="' + NODE_H + '" rx="8"></rect>' +
        '<rect class="accent" x="0" y="0" width="5" height="' + NODE_H +
          '" fill="' + color + '"></rect>' +
        '<text class="role" x="13" y="18" fill="' + color + '">' +
          esc(ICONE[n.kind] || '?') + '</text>' +
        '<text class="title" x="13" y="31">' + esc(topoTrim(n.name, 20)) + '</text>' +
        (meta ? '<text class="meta" x="13" y="42">' + esc(topoTrim(meta, 26)) + '</text>' : '') +
      '</g>');
  });
  parts.push('</g></svg>');

  host.innerHTML = parts.join('');
  bindTopoDrag(host.querySelector('svg'), model);
  bindTopoEdges(host.querySelector('svg'));
}

/** Clic sur une arete : retirer le lien. Un lien decouvert ou manuel porte une
 *  cle (on le masque) ; un simple rattachement force sans lien reel se detache
 *  en effacant le parent force. */
function bindTopoEdges(svg) {
  if (!svg) return;
  svg.querySelectorAll('[data-edge-child]').forEach((el) => {
    el.addEventListener('click', async (e) => {
      e.stopPropagation();
      const child = el.dataset.edgeChild;
      const linkKey = el.dataset.edgeLink;
      const node = topo.model && topo.model.nodesByKey.get(child);
      if (!confirm('Retirer ce lien de l\'arbre ?')) return;
      try {
        if (linkKey) {
          await api('/topology/links/' + encodeURIComponent(linkKey), { method: 'DELETE' });
        }
        // Detache aussi le rattachement force, sinon la case resterait sous ce
        // parent alors qu'on vient d'en couper le lien.
        if (node && node.parent_override) {
          await api('/topology/nodes/' + encodeURIComponent(child) + '/parent',
            { method: 'PATCH', body: JSON.stringify({ parent_key: null }) });
        }
        await loadNetwork();
      } catch (err) { alert(err.message); }
    });
  });
}

/** Clic sur une case : selection normale, ou choix d'extremite en mode lien. */
function topoNodeClick(key) {
  if (topo.linkMode) { topoLinkPick(key); return; }
  topoSelect(key);
}

/** Mode "creer un lien" : premier clic = parent, second = enfant. Le lien est
 *  pose (adjacence manuelle) ET l'enfant est rattache sous le parent, pour que
 *  l'arbre le montre tout de suite. */
async function topoLinkPick(key) {
  if (!topo.linkSource) {
    topo.linkSource = key;
    renderTopoCanvas();
    setTopoLinkNotice();
    return;
  }
  const source = topo.linkSource;
  const target = key;
  topo.linkSource = null;
  if (source === target) { renderTopoCanvas(); setTopoLinkNotice(); return; }
  // Anti-boucle : l'enfant ne peut pas etre un ancetre du parent.
  if (topo.model && topoDescendants(topo.model, target).has(source)) {
    alert('Impossible : cela creerait une boucle (l\'enfant est deja au-dessus du parent).');
    renderTopoCanvas();
    setTopoLinkNotice();
    return;
  }
  try {
    await api('/topology/links',
      { method: 'POST', body: JSON.stringify({ source_key: source, target_key: target }) });
    await api('/topology/nodes/' + encodeURIComponent(target) + '/parent',
      { method: 'PATCH', body: JSON.stringify({ parent_key: source }) });
    await loadNetwork();
    setTopoLinkNotice();
  } catch (err) { alert(err.message); }
}

/** Bandeau d'aide du mode lien. */
function setTopoLinkNotice() {
  const notice = document.getElementById('topo-notice');
  if (!notice) return;
  if (!topo.linkMode) { notice.innerHTML = ''; return; }
  notice.innerHTML = '<div class="notice"><b>Mode lien.</b> ' +
    (topo.linkSource
      ? 'Cliquez la case <b>enfant</b> a rattacher (ou re-cliquez pour annuler).'
      : 'Cliquez la case <b>parent</b>, puis la case <b>enfant</b>.') +
    ' Cliquez « Creer un lien » pour quitter ce mode.</div>';
}

function topoTrim(text, n) {
  const s = String(text || '');
  return s.length > n ? s.slice(0, n - 1) + '\u2026' : s;
}

/** Glisser une case la deplace ; la deposer sur une autre la rattache. Tout se
 *  fait au pointeur (souris ou tactile), et on distingue un clic (selection)
 *  d'un vrai deplacement par la distance parcourue. */
function bindTopoDrag(svg, model) {
  if (!svg) return;
  svg.querySelectorAll('.topo-node').forEach((g) => {
    g.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0) return;
      ev.preventDefault();
      const key = g.dataset.node;
      const node = model.nodesByKey.get(key);
      // Le noeud "abonnes" est un agregat synthetique : ni deplacable ni
      // rattachable, il suit son PoP.
      if (!node || node.synthetic) return;

      // En mode "creer un lien", un clic choisit une extremite : pas de drag.
      if (topo.linkMode) {
        const pick = () => { window.removeEventListener('pointerup', pick); topoNodeClick(key); };
        window.addEventListener('pointerup', pick);
        return;
      }

      const rect = svg.getBoundingClientRect();
      const start = { x: ev.clientX, y: ev.clientY };
      const origin = { x: node.x, y: node.y };
      let moved = false;
      let dropTarget = null;
      topo.dragging = true;
      g.classList.add('dragging');
      g.parentNode.appendChild(g);   // passe au premier plan

      const descendants = topoDescendants(model, key);

      const onMove = (e) => {
        const nx = origin.x + (e.clientX - start.x);
        const ny = origin.y + (e.clientY - start.y);
        if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 4) moved = true;
        node.x = nx;
        node.y = ny;
        g.setAttribute('transform', 'translate(' + nx + ',' + ny + ')');

        // Cible de rattachement : la case survolee par le CENTRE de celle qu'on
        // traine, hors elle-meme et hors ses descendants (cela ferait un cycle).
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        let cible = null;
        model.nodesByKey.forEach((other) => {
          if (other.key === key || descendants.has(other.key)) return;
          if (cx >= other.x && cx <= other.x + NODE_W && cy >= other.y && cy <= other.y + NODE_H) {
            cible = other.key;
          }
        });
        if (cible !== dropTarget) {
          if (dropTarget) svg.querySelector('[data-node="' + cssEsc(dropTarget) + '"]')
            ?.classList.remove('drop-target');
          dropTarget = cible;
          if (dropTarget) svg.querySelector('[data-node="' + cssEsc(dropTarget) + '"]')
            ?.classList.add('drop-target');
        }
      };

      const onUp = async () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        g.classList.remove('dragging');
        if (dropTarget) svg.querySelector('[data-node="' + cssEsc(dropTarget) + '"]')
          ?.classList.remove('drop-target');
        topo.dragging = false;

        if (!moved) { topoNodeClick(key); return; }
        try {
          if (dropTarget && dropTarget !== node.parentKey) {
            await api('/topology/nodes/' + encodeURIComponent(key) + '/parent',
              { method: 'PATCH', body: JSON.stringify({ parent_key: dropTarget }) });
            await api('/topology/nodes/' + encodeURIComponent(key) + '/layout',
              { method: 'PATCH', body: JSON.stringify({ x: node.x, y: node.y }) });
            await loadTopology();
          } else {
            await api('/topology/nodes/' + encodeURIComponent(key) + '/layout',
              { method: 'PATCH', body: JSON.stringify({ x: node.x, y: node.y }) });
            // Reporter la position dans les donnees en memoire, sinon le
            // prochain rendu la recalculerait en automatique et la case
            // reviendrait a sa place.
            const brut = (topo.data.nodes || []).find((d) => d.key === key);
            if (brut) { brut.pos_x = node.x; brut.pos_y = node.y; }
            renderTopoCanvas();   // redessine les aretes vers la nouvelle position
          }
        } catch (err) { alert(err.message); await loadTopology(); }
      };

      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    });
  });
}

/** Cle CSS sure pour un selecteur d'attribut (les cles contiennent des ':'). */
function cssEsc(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return String(value).replace(/["\\]/g, '\\$&');
}

function topoDescendants(model, key) {
  const out = new Set();
  const walk = (k) => {
    const node = model.nodesByKey.get(k);
    if (!node) return;
    node.children.forEach((c) => { out.add(c.key); walk(c.key); });
  };
  walk(key);
  return out;
}

function topoSelect(key) {
  topo.selected = topo.selected === key ? null : key;
  renderTopoCanvas();
  renderTopoPanel();
}

/** Panneau de la case selectionnee : role, rattachement force, masquage, debit. */
/** attributes d'un noeud, que /topology le rende en objet ou en JSON brut. */
function topoAttrs(node) {
  let a = node && node.attributes;
  if (typeof a === 'string') { try { a = JSON.parse(a); } catch (e) { a = null; } }
  return a && typeof a === 'object' ? a : {};
}

// Mots trop generiques pour caracteriser un equipement (identite par defaut).
const TOPO_GENERIC_TOKENS = new Set(['', 'mikrotik', 'routeros', 'routerboard', 'chr']);

/** Jeu de mots-cles normalise d'un noeud (nom + identite), trie, sans les mots
 *  generiques. "CCR DS" et "DS-CCR" donnent la MEME signature : c'est ce qui
 *  permet de REPERER un doublon probable meme quand l'ordre des mots differe. */
function topoNameTokens(node) {
  const brut = ((node.name || '') + ' ' + (topoAttrs(node).identity || '')).toLowerCase();
  const toks = brut.split(/[^a-z0-9]+/).filter((t) => t && !TOPO_GENERIC_TOKENS.has(t));
  return [...new Set(toks)].sort();
}

/** Doublons PROBABLES du noeud : d'autres cases dont les mots-cles sont les memes
 *  (ordre indifferent). On ne fusionne PAS tout seul -- deux extremites d'un lien
 *  ("CCR-DS" / "DS-CCR") peuvent etre deux vrais routeurs -- mais on le SIGNALE
 *  pour une fusion en un clic si c'est bien le meme materiel. */
function topoDuplicateSuggestions(node) {
  const mine = topoNameTokens(node);
  if (mine.length < 2 || !topo.model) return [];
  const sig = mine.join(' ');
  const out = [];
  topo.model.nodesByKey.forEach((n) => {
    if (n.key === node.key || n.synthetic) return;
    if (topoNameTokens(n).join(' ') === sig) out.push({ key: n.key, name: n.name });
  });
  return out;
}

/** Les autres cases de l'arbre, pour proposer une cible de fusion manuelle.
 *  Triees par nom, la case courante exclue. */
function topoOtherNodes(selfKey) {
  if (!topo.model) return [];
  return [...topo.model.nodesByKey.values()]
    .filter((n) => n.key !== selfKey)
    .sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
}

function renderTopoPanel() {
  const host = document.getElementById('topo-panel');
  if (!host) return;
  const node = topo.selected && topo.model ? topo.model.nodesByKey.get(topo.selected) : null;
  if (!node) {
    host.innerHTML = '<div class="muted">Cliquez une case pour la corriger : role, ' +
      'rattachement, visibilite. Glissez-la pour la ranger, deposez-la sur une ' +
      'autre pour la rattacher.</div>';
    return;
  }
  const parent = node.parentKey ? topo.model.nodesByKey.get(node.parentKey) : null;
  const attrs = topoAttrs(node);
  const dups = topoDuplicateSuggestions(node);
  host.innerHTML =
    '<h4>' + esc(node.name) +
      (attrs.unreachable ? ' <span class="badge warn" title="' + esc(attrs.error || '') +
        '">injoignable</span>' : '') + '</h4>' +
    // Doublon probable (memes mots-cles, ordre different) : signale, pas fusionne
    // d'office. Un clic replie l'autre case dans celle-ci si c'est le meme materiel.
    (dups.length
      ? '<div class="notice" style="margin:.5rem 0"><b>Doublon probable</b> — mêmes ' +
        'mots-clés que : ' +
        dups.map((d) => '<button class="sm primary" data-merge-into="' + esc(d.key) + '">' +
          'Fusionner ' + esc(topoTrim(d.name, 18)) + '</button>').join(' ') +
        '<span class="hint">Même équipement ? Fusionnez. Sinon (deux bouts d\'un lien, ' +
        'p.ex. CCR↔DS), laissez : ce sont deux vrais routeurs.</span></div>'
      : '') +
    '<div class="kv"><span>Role</span><span>' + esc(KIND_LABEL[node.kind] || '?') + '</span></div>' +
    ((node.addresses && node.addresses.length)
      ? '<div class="kv"><span>Adresse(s)</span><span>' + esc(node.addresses.join(', ')) + '</span></div>'
      : (node.address ? '<div class="kv"><span>Adresse</span><span>' + esc(node.address) + '</span></div>' : '')) +
    (attrs.serial
      ? '<div class="kv"><span>N° serie</span><span>' + esc(attrs.serial) + '</span></div>' : '') +
    (node.merged_count > 1
      ? '<div class="kv"><span>Fusion</span><span>' + esc(node.merged_count) +
        ' vues reconciliees</span></div>' : '') +
    (node.platform ? '<div class="kv"><span>Plateforme</span><span>' + esc(topoTrim(node.platform, 18)) + '</span></div>' : '') +
    '<div class="kv"><span>Parent</span><span>' + esc(parent ? topoTrim(parent.name, 16) : 'racine') +
      (node.parent_override ? ' *' : '') + '</span></div>' +
    '<div class="kv"><span>Vu</span><span>' + (node.fresh ? 'recemment' : 'ancien') + '</span></div>' +
    // Rattachement INCERTAIN (vu via un segment partage, pas prouve
    // point-a-point) : on le signale et on offre de le confirmer/verrouiller.
    (node.edge && node.edge.uncertain && parent
      ? '<div class="notice" style="margin:.5rem 0">Rattachement <b>probable</b> à <b>' +
        esc(topoTrim(parent.name, 18)) + '</b>, vu via un segment partagé (switch / VLAN ' +
        'de gestion) — pas une adjacence directe prouvée. ' +
        '<button class="sm ghost" data-attach="' + esc(parent.key) + '">Confirmer</button>' +
        '<span class="hint">Confirmer verrouille ce parent ; ou glissez la case sous le bon ' +
        'parent. Trait pointillé = lien incertain.</span></div>'
      : '') +
    // Noeud vraiment orphelin (aucun lien) : propose ses candidats de segment.
    (!node.parentKey && node.unsureParents && node.unsureParents.length
      ? '<div class="notice" style="margin:.5rem 0">Aucun lien direct sûr. Vu via un ' +
        'segment partagé vers : ' +
        node.unsureParents.map((c) =>
          '<button class="sm ghost" data-attach="' + esc(c.key) + '">' +
          esc(topoTrim(c.name, 18)) + '</button>').join(' ') +
        '<span class="hint">Cliquez pour rattacher à la main.</span></div>'
      : '') +
    '<div class="stack field"><label>Role</label>' +
      '<select id="topo-kind">' + KIND_ORDER.map((k) =>
        '<option value="' + k + '"' + (k === node.kind ? ' selected' : '') + '>' +
        esc(KIND_LABEL[k]) + '</option>').join('') + '</select></div>' +
    // Fusion manuelle : le dernier mot quand l'app n'a pas pu prouver que deux
    // cases sont le meme routeur (nom generique, pas de MAC commune).
    '<div class="stack field"><label>Meme equipement que…</label>' +
      '<select id="topo-merge-target"><option value="">— fusionner cette case dans —</option>' +
      topoOtherNodes(node.key).map((o) =>
        '<option value="' + esc(o.key) + '">' + esc(topoTrim(o.name, 24)) +
        ' · ' + esc(KIND_LABEL[o.kind] || '?') + '</option>').join('') +
      '</select></div>' +
    (node.manual_aliases && node.manual_aliases.length
      ? '<div class="kv"><span>Fusions manuelles</span><span class="topo-unmerge">' +
        node.manual_aliases.map((a) =>
          '<button class="sm ghost" data-unmerge="' + esc(a) + '" title="' + esc(a) +
          '">Separer ' + esc(topoTrim(a, 16)) + '</button>').join(' ') + '</span></div>'
      : '') +
    '<div class="actions" style="margin-top:.7rem">' +
      '<button class="sm" id="topo-merge">Fusionner</button>' +
      (node.parent_override
        ? '<button class="sm" id="topo-detach">Rattachement auto</button>' : '') +
      '<button class="sm" id="topo-hide">Masquer</button>' +
    '</div>';

  document.getElementById('topo-kind').addEventListener('change', async (e) => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '?kind=' + e.target.value,
        { method: 'PATCH' });
      await loadTopology();
    } catch (err) { alert(err.message); }
  });
  document.getElementById('topo-merge').addEventListener('click', async () => {
    const cible = document.getElementById('topo-merge-target').value;
    if (!cible) { alert('Choisissez la case dans laquelle fusionner celle-ci.'); return; }
    try {
      await api('/topology/merge', { method: 'POST',
        body: JSON.stringify({ alias_key: node.key, canonical_key: cible }) });
      topo.selected = cible;  // la case fusionnee disparait : on suit la canonique
      await loadTopology();
    } catch (err) { alert(err.message); }
  });
  host.querySelectorAll('[data-unmerge]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await api('/topology/merge/' + encodeURIComponent(b.dataset.unmerge), { method: 'DELETE' });
      await loadTopology();
    } catch (err) { alert(err.message); }
  }));
  host.querySelectorAll('[data-attach]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '/parent',
        { method: 'PATCH', body: JSON.stringify({ parent_key: b.dataset.attach }) });
      await loadTopology();
    } catch (err) { alert(err.message); }
  }));
  host.querySelectorAll('[data-merge-into]').forEach((b) => b.addEventListener('click', async () => {
    // On replie l'autre case (alias) dans celle que l'operateur regarde (canonique).
    try {
      await api('/topology/merge', { method: 'POST',
        body: JSON.stringify({ alias_key: b.dataset.mergeInto, canonical_key: node.key }) });
      await loadTopology();
    } catch (err) { alert(err.message); }
  }));
  const detach = document.getElementById('topo-detach');
  if (detach) detach.addEventListener('click', async () => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '/parent',
        { method: 'PATCH', body: JSON.stringify({ parent_key: null }) });
      await loadTopology();
    } catch (err) { alert(err.message); }
  });
  document.getElementById('topo-hide').addEventListener('click', async () => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '/visibility',
        { method: 'PATCH', body: JSON.stringify({ hidden: true }) });
      topo.selected = null;
      await loadTopology();
    } catch (err) { alert(err.message); }
  });
}

/** Remet toute la disposition en automatique : efface positions ET
 *  rattachements forces, sur chaque case. */
async function resetTopoLayout() {
  if (!topo.data || !confirm('Remettre la disposition automatique ?\n\n' +
    'Les positions et rattachements poses a la main seront effaces.')) return;
  try {
    await Promise.all((topo.data.nodes || []).map((n) => Promise.all([
      api('/topology/nodes/' + encodeURIComponent(n.key) + '/layout',
        { method: 'PATCH', body: JSON.stringify({ x: null, y: null }) }),
      n.parent_override
        ? api('/topology/nodes/' + encodeURIComponent(n.key) + '/parent',
            { method: 'PATCH', body: JSON.stringify({ parent_key: null }) })
        : Promise.resolve(),
    ])));
    await loadTopology();
  } catch (err) { alert(err.message); }
}

/** Debit mesure d'un lien, dans le sens du tableau : la fleche part de la
 *  colonne "Depuis" et va vers la colonne "Vers". Aucune heuristique ici, on
 *  montre les compteurs tels que le routeur les tient. */
function linkRates(l) {
  if (l.rx_bps === null && l.tx_bps === null) {
    return '<span style="color:var(--faint)" title="Aucun compteur exploitable pour ce lien : ' +
      'soit il vient d\'UISP et n\'a pas de port local, soit la premiere mesure ' +
      'n\'a pas encore eu de seconde lecture.">pas de mesure</span>';
  }
  const perime = l.measure_fresh === false;
  return '<span class="d" title="Le routeur emet vers ' + esc(l.target_name || '?') + '">&rarr; ' +
      esc(bpsText(l.tx_bps || 0)) + '</span> ' +
    '<span class="u" title="Le routeur recoit depuis ' + esc(l.target_name || '?') + '">&larr; ' +
      esc(bpsText(l.rx_bps || 0)) + '</span>' +
    (perime ? ' <span class="badge warn" title="Derniere mesure : ' +
      esc(clock(l.measured_at)) + '">perime</span>' : '');
}

/** Charge du port dans sa direction la plus chargee : c'est celle-la qui sature
 *  en premier, une moyenne des deux sens masquerait un lien deja plein. */
function linkLoad(l) {
  const plafond = (l.port_capacity_mbps || l.capacity_mbps || 0) * 1e6;
  if (!plafond || (l.rx_bps === null && l.tx_bps === null)) return '';
  return meter(Math.max(l.rx_bps || 0, l.tx_bps || 0), plafond);
}

function renderTopologyLinks(allLinks) {
  const host = document.getElementById('topo-links');
  // Meme filtre que le canvas : "liens a debit seulement" masque le bruit des
  // adjacences sans compteur (radio UISP sans port, seconde lecture en attente).
  const hasRate = (l) => l.rx_bps !== null || l.tx_bps !== null;
  const links = topo.rateOnly ? allLinks.filter(hasRate) : allLinks;
  const compte = document.getElementById('topo-links-count');
  if (compte) compte.textContent = links.length + ' lien(s)';
  if (!links.length) {
    host.innerHTML = '<div class="empty">' +
      (allLinks.length && topo.rateOnly
        ? 'Aucun lien avec un debit mesure. Decochez "Liens a debit seulement" ' +
          'pour voir les adjacences sans compteur.'
        : 'Aucun lien.') + '</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Depuis</th><th>Interface</th><th>Vers</th><th>Type</th>' +
    '<th class="num">Debit mesure</th><th>Charge</th>' +
    '<th class="num">Capacite</th><th class="num">Debit impose</th><th></th></tr></thead><tbody>' +
    links.map((l) => {
      const impose = l.max_down_mbps || l.max_up_mbps;
      const partage = (l.interface_links || 0) > 1;
      return '<tr>' +
        '<td>' + esc(l.source_name || l.source_key) + '</td>' +
        '<td class="login">' + esc(l.interface || '-') +
          (partage ? ' <span class="badge warn" title="' + esc(l.interface_links) +
            ' voisins sur ce port : le debit est celui du port, pas de ce seul voisin.">' +
            'partage</span>' : '') + '</td>' +
        '<td>' + esc(l.target_name || l.target_key) +
          ' <span class="badge">' + esc(KIND_LABEL[l.target_kind] || '?') + '</span></td>' +
        '<td>' + esc(l.kind) + '</td>' +
        '<td class="num">' + linkRates(l) + '</td>' +
        '<td style="min-width:120px">' + linkLoad(l) + '</td>' +
        '<td class="num">' + (l.capacity_mbps ? esc(mbps(l.capacity_mbps)) : '-') + '</td>' +
        '<td class="num">' + (impose
          ? '<span style="color:var(--warn)">' +
            esc(mbps(l.max_down_mbps || 0) + ' / ' + mbps(l.max_up_mbps || 0)) + '</span>'
          : '<span style="color:var(--faint)">auto</span>') + '</td>' +
        '<td><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm" data-link-detail="' + esc(l.key) + '">Debit</button>' +
          '<button class="sm" data-edit-link="' + esc(l.key) + '">Bande passante</button>' +
        '</div></td></tr>';
    }).join('') + '</tbody></table>';

  host.querySelectorAll('[data-edit-link]').forEach((b) => {
    const lien = links.find((l) => l.key === b.dataset.editLink);
    b.addEventListener('click', () => openBandwidthEditor('link', lien));
  });
  host.querySelectorAll('[data-link-detail]').forEach((b) => {
    b.addEventListener('click', () => openLink(b.dataset.linkDetail));
  });
}

/* --------------------------------------------------- debit d'un lien */

const FENETRES = [[15, '15 min'], [60, '1 h'], [360, '6 h'], [1440, '24 h']];

/** Tiroir "debit de ce lien" : l'historique collecte, plus un bouton qui va
 *  chercher la mesure INSTANTANEE sur le routeur. Les deux repondent a la meme
 *  question a deux echelles de temps, et l'origine du chiffre est toujours
 *  affichee. */
async function openLink(key, minutes, silencieux) {
  const fenetre = minutes || 60;
  const root = document.getElementById('drawer-root');
  if (!silencieux) {
    root.innerHTML = '<div class="drawer-backdrop"></div><div class="drawer">' +
      '<div class="empty">Chargement...</div></div>';
    root.querySelector('.drawer-backdrop').addEventListener('click', closeDrawer);
  } else if (!root.querySelector('.drawer')) {
    return;  // le tiroir a ete ferme entre-temps
  }
  state.link = { key: key, minutes: fenetre };

  try {
    const data = await api('/topology/links/' + encodeURIComponent(key) +
      '/throughput?minutes=' + fenetre + '&bucket=' + (fenetre <= 60 ? 30 : 300));
    const l = data.link;
    const voisin = l.target_name || l.target_key;
    const plafond = (l.port_capacity_mbps || l.capacity_mbps || 0) * 1e6;
    const pointe = data.series.reduce(
      (m, p) => Math.max(m, p.tx_peak_bps || 0, p.rx_peak_bps || 0), 0);

    root.querySelector('.drawer').innerHTML =
      '<div class="drawer-head"><h3>' + esc(l.source_name || l.source_key) +
        ' <span style="color:var(--faint)">&rarr;</span> ' + esc(voisin) + '</h3>' +
      '<button class="sm" id="drawer-close">Fermer</button></div>' +
      '<div class="grid stats" style="margin-bottom:1rem">' +
        statCard('', 'Vers ' + voisin, bpsText(l.tx_bps || 0), '',
          esc(l.interface || '') + (l.running === false ? ' &middot; port down' : '')) +
        statCard('', 'Depuis ' + voisin, bpsText(l.rx_bps || 0), '',
          l.measured_at ? 'mesure ' + esc(clock(l.measured_at)) : 'jamais mesure') +
        statCard('', 'Capacite du port', plafond ? bpsText(plafond) : '-', '',
          plafond
            ? 'charge ' + Math.round(pct(Math.max(l.rx_bps || 0, l.tx_bps || 0), plafond)) + ' %'
            : 'capacite du port inconnue') +
        statCard('', 'Pointe sur la fenetre', pointe ? bpsText(pointe) : '-', '',
          data.series.length + ' point(s)') +
      '</div>' +
      '<div class="notice"><b>Origine du chiffre.</b> ' + esc(data.measurement.note) +
        '<span class="hint">rx et tx sont ceux du routeur : &rarr; il emet vers ' +
        esc(voisin) + ', &larr; il recoit depuis ' + esc(voisin) + '.</span></div>' +
      '<div class="actions" style="margin:.8rem 0">' +
        FENETRES.map(([m, libelle]) =>
          '<button class="sm' + (m === fenetre ? ' primary' : '') +
            '" data-link-window="' + m + '">' + libelle + '</button>').join('') +
        '<button class="sm" id="link-live">Mesurer maintenant</button>' +
        '<button class="sm" id="link-bw">Bande passante</button>' +
      '</div>' +
      '<div id="link-live-result"></div>' +
      '<div class="card"><div id="link-chart"></div></div>';

    // Une mesure instantanee reste affichee : elle porte son horodatage, la
    // remplacer par du vide a chaque cycle serait la perdre sous les yeux.
    if (state.linkLive && state.linkLive.key === key) {
      document.getElementById('link-live-result').innerHTML = state.linkLive.html;
    }

    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    document.getElementById('link-bw').addEventListener('click', () => openBandwidthEditor('link', l));
    document.getElementById('link-live').addEventListener('click', () => measureLink(key));
    root.querySelectorAll('[data-link-window]').forEach((b) => {
      b.addEventListener('click', () => openLink(key, Number(b.dataset.linkWindow)));
    });

    renderThroughput(
      document.getElementById('link-chart'),
      data.series.map((p) => ({ bucket: p.bucket, tx_bps: p.tx_peak_bps, rx_bps: p.rx_peak_bps })),
      { labels: { down: 'Vers ' + voisin, up: 'Depuis ' + voisin, extra: null } },
    );
  } catch (err) {
    root.querySelector('.drawer').innerHTML =
      '<div class="drawer-head"><h3>Erreur</h3><button class="sm" id="drawer-close">Fermer</button></div>' +
      '<div class="notice err">' + esc(err.message) + '</div>';
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  }
}

/** Demande au routeur le debit qu'il mesure a l'instant. Lecture pure :
 *  /interface/monitor-traffic ne modifie aucune configuration. */
async function measureLink(key) {
  const host = document.getElementById('link-live-result');
  const bouton = document.getElementById('link-live');
  if (bouton) { bouton.disabled = true; bouton.textContent = 'Mesure...'; }
  try {
    const m = await api('/topology/links/' + encodeURIComponent(key) + '/live');
    const direct = m.source === 'monitor-traffic';
    const html = '<div class="notice' + (direct ? ' ok' : '') + '">' +
      '<b>' + (direct ? 'Mesure instantanee' : 'Derniere mesure collectee') + '</b> ' +
      '<span style="color:var(--faint)">' + esc(clock(m.measured_at)) + '</span> &middot; ' +
      '&rarr; ' + esc(bpsText(m.tx_bps || 0)) + ' &middot; &larr; ' + esc(bpsText(m.rx_bps || 0)) +
      (direct ? '<span class="hint">Lue a l\'instant sur ' + esc(m.router_name || '?') + ' ' +
        'via /interface/monitor-traffic (lecture seule).</span>'
        : '<span class="hint">' + esc(m.detail || '') + '</span>') +
      '</div>';
    state.linkLive = { key: key, html: html };
    host.innerHTML = html;
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  } finally {
    if (bouton) { bouton.disabled = false; bouton.textContent = 'Mesurer maintenant'; }
  }
}

/** Editeur de bande passante : c'est ici qu'on "clique pour modifier". Il
 *  n'ecrit PAS sur le routeur : il enregistre l'intention, puis renvoie vers le
 *  plan, ou les commandes exactes sont visibles avant execution. */
async function openBandwidthEditor(scope, cible) {
  if (!cible) return;
  const nom = scope === 'link'
    ? (cible.target_name || cible.interface) : cible.pppoe_login;
  const cle = scope === 'link' ? cible.key : cible.pppoe_login;
  const capacite = cible.capacity_mbps;

  // Relire la surcharge en place : ouvrir sur des champs vides laisserait
  // croire qu'aucun plafond n'est pose.
  let actuelle = {};
  try {
    const politiques = await api('/shaping/policies?scope=' + scope);
    actuelle = politiques.find((p) => p.target_key === cle) || {};
  } catch (err) {
    console.warn('Politique non relue :', err);
  }

  // Pre-remplir dans l'unite la plus lisible : 0.512 Mbps s'affiche 512 kbps.
  const dep = bestUnit(actuelle.max_down_mbps);
  const mon = bestUnit(actuelle.max_up_mbps);

  const root = document.getElementById('drawer-root');
  root.innerHTML = '<div class="drawer-backdrop"></div><div class="drawer">' +
    '<div class="drawer-head"><h3>' + esc(nom) + '</h3>' +
    '<button class="sm" id="drawer-close">Fermer</button></div>' +
    (capacite ? '<div class="notice">Capacite mesuree : <strong>' + esc(mbps(capacite)) +
      '</strong><span class="hint">Le debit impose devrait rester sous cette valeur : ' +
      'c\'est ce qui fait que la file se forme dans CAKE, ou on la controle, ' +
      'plutot que dans le buffer de la radio.</span></div>' : '') +
    // Sur quoi la file sera reellement accrochee : l'exploitant doit pouvoir
    // relier ce qu'il saisit ici a la ligne qu'il verra dans /queue/simple.
    (scope === 'subscriber'
      ? (cible.last_ip
          ? '<div class="notice">La file visera <code>' + esc(cible.last_ip) +
            '/32</code><span class="hint">C\'est l\'adresse de la session en cours, ' +
            'relue sur le routeur au moment du plan. Elle est reecrite toute seule ' +
            'si l\'abonne se reconnecte avec une autre IP.</span></div>'
          : '<div class="notice warn">Aucune adresse connue pour cet abonne.' +
            '<span class="hint">La limite est enregistree, mais aucune file ne sera ' +
            'ecrite tant qu\'il n\'a pas de session ouverte : poser une file sur une ' +
            'ancienne adresse briderait le client qui l\'a recuperee entre-temps.</span>' +
            '</div>')
      : '') +
    '<form class="stack" id="bw-form">' +
      '<div class="row-2">' +
        '<div class="field"><label for="bw-down">Download</label>' +
          '<div style="display:flex;gap:.4rem">' +
            '<input id="bw-down" type="number" min="0" step="any" value="' +
            esc(dep.value) + '" placeholder="auto (plan ou capacite mesuree)">' +
            unitSelect('bw-down-unit', dep.unit) +
          '</div></div>' +
        '<div class="field"><label for="bw-up">Upload</label>' +
          '<div style="display:flex;gap:.4rem">' +
            '<input id="bw-up" type="number" min="0" step="any" value="' +
            esc(mon.value) + '" placeholder="auto">' +
            unitSelect('bw-up-unit', mon.unit) +
          '</div></div>' +
      '</div>' +
      '<div class="field"><label for="bw-note">Note</label>' +
        '<input id="bw-note" value="' + esc(actuelle.note || '') +
        '" placeholder="pourquoi ce plafond (optionnel)"></div>' +
      '<div id="bw-result"></div>' +
      '<div class="actions">' +
        '<button type="submit" class="primary">Enregistrer</button>' +
        '<button type="button" id="bw-clear">Revenir a auto</button>' +
      '</div>' +
    '</form>' +
    '<p class="empty" style="text-align:left;padding:.8rem 0 0">' +
      'Enregistrer ne touche a aucun routeur. Passez ensuite par l\'onglet ' +
      'Shaping pour voir les commandes exactes, puis les appliquer.</p>' +
    '</div>';

  root.querySelector('.drawer-backdrop').addEventListener('click', closeDrawer);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);

  document.getElementById('bw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/shaping/policies', {
        method: 'PUT',
        body: JSON.stringify({
          scope: scope, target_key: cle,
          // Converti en Mbps ici : la base ne connait qu'une seule unite.
          max_down_mbps: readRate('bw-down', 'bw-down-unit'),
          max_up_mbps: readRate('bw-up', 'bw-up-unit'),
          enabled: true,
          note: document.getElementById('bw-note').value || null,
        }),
      });
      document.getElementById('bw-result').innerHTML =
        '<div class="notice ok">Enregistre. Ouvrez l\'onglet Shaping pour voir ' +
        'les commandes qui en decoulent.</div>';
      await refresh();
    } catch (err) {
      document.getElementById('bw-result').innerHTML =
        '<div class="notice err">' + esc(err.message) + '</div>';
    }
  });

  document.getElementById('bw-clear').addEventListener('click', async () => {
    try {
      await api('/shaping/policies/' + scope + '/' + encodeURIComponent(cle), { method: 'DELETE' });
      closeDrawer();
      await refresh();
    } catch (err) { alert(err.message); }
  });
}

/* ----------------------------------------------------------------- boost */

const DUREES = [
  { label: '15 min', minutes: 15 },
  { label: '1 h', minutes: 60 },
  { label: '4 h', minutes: 240 },
  { label: '24 h', minutes: 1440 },
];
const FACTEURS = [2, 3, 5];

/** Coup de debit temporaire sur un abonne PPPoE. Il expire tout seul : un job
 *  verifie l'echeance et ramene la file au debit normal. */
function openBoostEditor(abonne) {
  if (!abonne) return;
  const planDown = abonne.plan_down_mbps || 0;
  const planUp = abonne.plan_up_mbps || 0;

  const root = document.getElementById('drawer-root');
  root.innerHTML = '<div class="drawer-backdrop"></div><div class="drawer">' +
    '<div class="drawer-head"><h3>Boost &middot; ' + esc(abonne.pppoe_login) + '</h3>' +
    '<button class="sm" id="drawer-close">Fermer</button></div>' +

    '<div class="notice">Plan actuel : <strong>' +
      esc(mbps(planDown)) + ' / ' + esc(mbps(planUp)) + '</strong>' +
      '<span class="hint">Le boost prime sur le plan et sur toute surcharge ' +
      'permanente, puis s\'efface a echeance sans intervention.</span></div>' +

    '<form class="stack" id="boost-form">' +
      '<div class="field"><label>Duree</label>' +
        '<div class="boost-choices" id="boost-durations">' +
        DUREES.map((d, i) => '<button type="button" data-minutes="' + d.minutes + '"' +
          (i === 1 ? ' class="active"' : '') + '>' + esc(d.label) + '</button>').join('') +
        '</div>' +
        '<input id="boost-minutes" type="number" min="1" max="10080" value="60" ' +
          'style="margin-top:.4rem" aria-label="duree en minutes">' +
        '<span class="help">en minutes</span></div>' +

      '<div class="field"><label>Debit</label>' +
        '<div class="boost-choices" id="boost-factors">' +
        FACTEURS.map((f) => '<button type="button" data-mult="' + f + '">x' + f +
          (planDown ? ' (' + esc(mbps(planDown * f)) + ')' : '') + '</button>').join('') +
        '</div></div>' +

      '<div class="row-2">' +
        '<div class="field"><label for="boost-down">Download</label>' +
          '<div style="display:flex;gap:.4rem">' +
            '<input id="boost-down" type="number" min="0" step="any" placeholder="inchange">' +
            unitSelect('boost-down-unit', 'mbps') +
          '</div></div>' +
        '<div class="field"><label for="boost-up">Upload</label>' +
          '<div style="display:flex;gap:.4rem">' +
            '<input id="boost-up" type="number" min="0" step="any" placeholder="inchange">' +
            unitSelect('boost-up-unit', 'mbps') +
          '</div></div>' +
      '</div>' +

      '<div class="field"><label for="boost-reason">Motif</label>' +
        '<input id="boost-reason" placeholder="geste commercial, depannage... (optionnel)"></div>' +

      '<div id="boost-result"></div>' +
      '<div class="actions">' +
        '<button type="submit" class="primary">Lancer le boost</button>' +
        '<button type="button" id="boost-clear" class="danger">Retirer le boost en cours</button>' +
      '</div>' +
    '</form>';

  root.querySelector('.drawer-backdrop').addEventListener('click', closeDrawer);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);

  const champMinutes = document.getElementById('boost-minutes');
  document.querySelectorAll('#boost-durations button').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#boost-durations button')
        .forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      champMinutes.value = b.dataset.minutes;
    });
  });
  document.querySelectorAll('#boost-factors button').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#boost-factors button')
        .forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      const facteur = Number(b.dataset.mult);
      [['boost-down', planDown], ['boost-up', planUp]].forEach(([id, plan]) => {
        const choix = bestUnit(plan * facteur);
        document.getElementById(id).value = choix.value;
        document.getElementById(id + '-unit').value = choix.unit;
      });
    });
  });

  document.getElementById('boost-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const down = readRate('boost-down', 'boost-down-unit');
    const up = readRate('boost-up', 'boost-up-unit');
    if (!down && !up) {
      document.getElementById('boost-result').innerHTML =
        '<div class="notice err">Choisissez un facteur ou saisissez un debit.</div>';
      return;
    }
    try {
      const r = await api('/shaping/boosts', {
        method: 'POST',
        body: JSON.stringify({
          login: abonne.pppoe_login,
          duration_minutes: Number(champMinutes.value) || 60,
          down_mbps: down,
          up_mbps: up,
          reason: document.getElementById('boost-reason').value || null,
          apply_now: true,
        }),
      });
      const applique = r.applied || {};
      document.getElementById('boost-result').innerHTML =
        '<div class="notice ' + (applique.ok === false ? 'warn' : 'ok') + '">' +
        '<strong>Boost actif jusqu\'a ' +
        esc(new Date(r.boost.expires_at).toLocaleString('fr-FR')) + '.</strong>' +
        '<span class="hint">' +
        (applique.ok === false
          ? esc(applique.detail || '')
          : (applique.applied || 0) + ' commande(s) poussee(s) sur le routeur.') +
        '</span></div>';
      await refresh();
    } catch (err) {
      document.getElementById('boost-result').innerHTML =
        '<div class="notice err">' + esc(err.message) + '</div>';
    }
  });

  document.getElementById('boost-clear').addEventListener('click', async () => {
    try {
      await api('/shaping/boosts/' + encodeURIComponent(abonne.pppoe_login), { method: 'DELETE' });
      closeDrawer();
      await refresh();
    } catch (err) {
      document.getElementById('boost-result').innerHTML =
        '<div class="notice err">' + esc(err.message) + '</div>';
    }
  });
}

/* --------------------------------------------------------------- shaping */

async function loadShaping() {
  const select = document.getElementById('shaping-router');
  if (!select.options.length) {
    const inventaire = await api('/pops/routers');
    select.innerHTML = inventaire.routers
      .map((r) => '<option value="' + esc(r.name) + '">' + esc(r.name) + '</option>').join('');
  }
  await refreshEnforcement();
  await loadAudit();
}

async function refreshEnforcement() {
  const etat = await api('/shaping/enforcement');
  const toggle = document.getElementById('enforcement-toggle');
  const label = document.getElementById('enforcement-label');

  toggle.checked = etat.enabled;
  toggle.disabled = etat.locked;
  label.textContent = etat.locked
    ? 'enforcement verrouille'
    : etat.enabled ? 'ecriture AUTORISEE' : 'lecture seule';
  label.style.color = etat.enabled ? 'var(--warn)' : 'var(--muted)';
  document.getElementById('enforcement-switch').title = etat.locked
    ? 'ENFORCEMENT_LOCKED=true : la bascule est interdite depuis l\'interface'
    : 'Autoriser ou couper l\'ecriture sur les routeurs';

  const notice = document.getElementById('shaping-notice');
  if (etat.enabled) {
    notice.innerHTML = '<div class="notice warn"><strong>Ecriture autorisee.</strong> ' +
      'Les plans appliques modifient reellement les routeurs. Seules les files ' +
      'portant <code>freeqos:managed</code> sont concernees.' +
      (etat.last_change && etat.last_change.reason
        ? '<span class="hint">Motif : ' + esc(etat.last_change.reason) + '</span>' : '') +
      '</div>';
  } else {
    notice.innerHTML = '<div class="notice"><strong>Lecture seule.</strong> ' +
      'Les plans sont calcules et affiches, mais rien n\'est envoye. ' +
      'Basculez l\'interrupteur pour autoriser l\'ecriture.' +
      (etat.locked
        ? '<span class="hint">Verrouille par <code>ENFORCEMENT_LOCKED=true</code> : ' +
          'seul un redemarrage avec <code>ENFORCEMENT_ENABLED</code> modifie peut ' +
          'autoriser l\'ecriture.</span>'
        : '') + '</div>';
  }
}

async function toggleEnforcement(active) {
  const toggle = document.getElementById('enforcement-toggle');
  if (active && !confirm(
      "Autoriser l'ecriture sur les routeurs ?\n\n" +
      'A partir de maintenant, appliquer un plan modifiera reellement leur ' +
      'configuration. Seules les files marquees freeqos:managed sont touchees.')) {
    toggle.checked = false;
    return;
  }
  const motif = active
    ? (prompt('Motif (trace dans le journal, optionnel) :') || null)
    : null;
  toggle.disabled = true;
  try {
    await api('/shaping/enforcement', {
      method: 'PUT',
      body: JSON.stringify({ enabled: active, confirm: true, reason: motif }),
    });
  } catch (err) {
    alert(err.message);
  } finally {
    toggle.disabled = false;
    await refreshEnforcement();
    await refreshHealth();
  }
}

async function loadAudit() {
  const rows = await api('/shaping/audit?limit=40');
  const host = document.getElementById('shaping-audit');
  if (!rows.length) {
    host.innerHTML = '<div class="empty">Aucune commande envoyee.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Quand</th><th>Routeur</th><th>Commande</th>' +
    '<th>Mode</th><th>Etat</th></tr></thead><tbody>' +
    rows.map((r) => '<tr>' +
      '<td class="num" style="color:var(--faint)">' + esc(clock(r.ts)) + '</td>' +
      '<td>' + esc(r.router_name) + '</td>' +
      '<td class="login" style="font-size:.74rem">' + esc(r.command) + '</td>' +
      '<td>' + (r.dry_run ? '<span class="badge">simule</span>'
        : '<span class="badge warn">applique</span>') + '</td>' +
      '<td>' + (r.ok ? '<span class="badge ok">ok</span>'
        : '<span class="badge crit" title="' + esc(r.detail || '') + '">echec</span>') + '</td>' +
      '</tr>').join('') + '</tbody></table>';
}

async function inspectShaping() {
  const routeur = document.getElementById('shaping-router').value;
  const host = document.getElementById('shaping-state');
  host.innerHTML = '<div class="notice">Lecture de ' + esc(routeur) + '...</div>';
  try {
    const [etats, droits] = await Promise.all([
      api('/shaping/state?router=' + encodeURIComponent(routeur)),
      api('/shaping/capability?router=' + encodeURIComponent(routeur)).catch(() => null),
    ]);
    host.innerHTML = etats.map((e) => {
      if (!e.reachable) {
        return '<div class="notice err"><strong>' + esc(e.router) + '</strong> injoignable : ' +
          esc(e.error || '') + '</div>';
      }
      let bandeauDroits = '';
      if (droits) {
        if (droits.can_write === true) {
          bandeauDroits = '<div class="notice ok">Le compte <code>' +
            esc(droits.username) + '</code> peut ecrire : ' + esc(droits.detail) + '</div>';
        } else if (droits.can_write === false) {
          bandeauDroits = '<div class="notice err"><strong>Le compte <code>' +
            esc(droits.username) + '</code> ne peut pas ecrire.</strong> ' +
            esc(droits.detail) +
            '<span class="hint">Sur le routeur : <code>/user/group set ' +
            '[find name=' + esc(droits.group || '&lt;groupe&gt;') +
            '] policy=read,write,api,test</code></span></div>';
        } else {
          bandeauDroits = '<div class="notice warn">Droits du compte <code>' +
            esc(droits.username) + '</code> non verifiables : ' + esc(droits.detail) +
            '</div>';
        }
      }
      return '<div class="card" style="margin-bottom:1rem">' +
        '<div class="node-head" style="margin-bottom:.8rem">' +
          '<div class="node-title">' + esc(e.router) + '</div>' +
          '<div class="node-metrics">' +
            '<span>' + e.counts.simple_queues + ' file(s) simple(s)</span>' +
            '<span style="color:var(--down)">' + e.counts.managed + ' geree(s) par freeQoS</span>' +
            '<span style="color:var(--warn)">' + e.counts.foreign + ' tierce(s)</span>' +
          '</div></div>' +
        (e.counts.foreign
          ? '<div class="notice warn">' + e.counts.foreign + ' file(s) ne portent pas le ' +
            'marqueur <code>freeqos:managed</code> : posees a la main ou par RADIUS. ' +
            'Elles ne seront jamais modifiees ni supprimees.' +
            '<span class="hint">' +
            e.foreign_queues.slice(0, 8).map((q) => esc(q.name)).join(', ') +
            (e.foreign_queues.length > 8 ? '...' : '') + '</span></div>'
          : '<div class="notice ok">Aucune file tierce : le controleur est seul a shaper ' +
            'sur ce routeur.</div>') +
        bandeauDroits +
        '</div>';
    }).join('');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

async function computePlan() {
  const routeur = document.getElementById('shaping-router').value;
  const host = document.getElementById('shaping-plan');
  host.innerHTML = '<div class="notice">Calcul du plan pour ' + esc(routeur) + '...</div>';
  try {
    const plan = await api('/shaping/plan', {
      method: 'POST', body: JSON.stringify({ router: routeur }),
    });
    renderPlan(plan, routeur);
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

/** Abonnes volontairement laisses de cote. Les motifs identiques sont
 *  regroupes : "42 abonnes hors ligne" se lit, quarante-deux lignes non. */
function renderEcartes(ecartes) {
  if (!ecartes || !ecartes.length) return '';
  const parMotif = new Map();
  ecartes.forEach((s) => {
    if (!parMotif.has(s.reason)) parMotif.set(s.reason, []);
    parMotif.get(s.reason).push(s.login);
  });
  return '<div class="notice"><strong>' + ecartes.length +
    ' abonne(s) hors du plan.</strong> Ce n\'est pas une erreur : ce sont ceux ' +
    'pour lesquels il n\'y a rien a ecrire.' +
    [...parMotif.entries()].map(([motif, logins]) =>
      '<span class="hint"><b>' + esc(motif) + '</b> &mdash; ' +
      esc(logins.slice(0, 12).join(', ')) +
      (logins.length > 12 ? ' et ' + (logins.length - 12) + ' autre(s)' : '') +
      '</span>').join('') +
    '</div>';
}

function renderPlan(plan, routeur) {
  const host = document.getElementById('shaping-plan');
  const c = plan.counts;
  const total = c.add + c.set + c.remove;

  let html = '<h2>Plan pour ' + esc(routeur) + '</h2>';

  if (plan.conflicts.length) {
    html += '<div class="notice err"><strong>' + plan.conflicts.length +
      ' conflit(s) de nom.</strong> Ces files existent deja sans notre marqueur : ' +
      'elles appartiennent a quelqu\'un d\'autre et ne seront pas touchees.' +
      '<span class="hint">' + plan.conflicts.map((x) => esc(x.name)).join(', ') +
      '</span></div>';
  }

  // Un abonne absent du plan sans explication est indiscernable d'un abonne
  // correctement shape : on dit qui est ecarte et pourquoi.
  html += renderEcartes(plan.skipped);

  if (!total) {
    html += '<div class="notice ok">Rien a faire : la configuration du routeur ' +
      'correspond deja a l\'etat voulu (' + plan.unchanged + ' element(s) conformes).</div>';
    host.innerHTML = html;
    return;
  }

  html += '<div class="notice">' +
    '<strong>' + total + ' commande(s)</strong> : ' +
    c.add + ' creation(s), ' + c.set + ' modification(s), ' + c.remove + ' suppression(s). ' +
    plan.unchanged + ' element(s) deja conformes.' +
    '<span class="hint">Rien n\'est envoye tant que vous n\'avez pas applique.</span></div>';

  html += '<div class="table-wrap"><table><thead><tr><th>Action</th><th>Raison</th>' +
    '<th>Commande RouterOS</th></tr></thead><tbody>' +
    plan.actions.map((a) => {
      const couleur = a.verb === 'remove' ? 'crit' : a.verb === 'add' ? 'ok' : 'warn';
      return '<tr>' +
        '<td><span class="badge ' + couleur + '">' + esc(a.verb) + '</span> ' +
          esc(a.summary) + '</td>' +
        '<td style="color:var(--muted);font-size:.76rem">' + esc(a.reason) + '</td>' +
        '<td class="login" style="font-size:.72rem;white-space:nowrap">' +
          esc(a.command) + '</td></tr>';
    }).join('') + '</tbody></table></div>';

  html += '<div class="actions" style="margin-top:1rem">' +
    '<button id="btn-simulate">Simuler (dry-run)</button>' +
    '<button id="btn-apply" class="primary">Appliquer sur le routeur</button>' +
    '</div><div id="apply-result"></div>';

  host.innerHTML = html;
  document.getElementById('btn-simulate').addEventListener('click', () => applyPlan(routeur, true));
  document.getElementById('btn-apply').addEventListener('click', () => applyPlan(routeur, false));
}

async function applyPlan(routeur, dryRun) {
  if (!dryRun && !confirm(
      'Appliquer reellement sur ' + routeur + ' ?\n\n' +
      'Des commandes vont etre envoyees au routeur. Seules les files portant ' +
      'le marqueur freeqos:managed sont concernees.')) {
    return;
  }
  const host = document.getElementById('apply-result');
  host.innerHTML = '<div class="notice">Execution...</div>';
  try {
    const reponse = await api('/shaping/apply', {
      method: 'POST',
      body: JSON.stringify({ router: routeur, dry_run: dryRun, confirm: !dryRun }),
    });
    const r = reponse.result;
    host.innerHTML = '<div class="notice ' + (r.ok ? 'ok' : 'err') + '">' +
      '<strong>' + (r.dry_run ? 'Simulation' : 'Application') + ' : ' +
      r.applied + ' reussie(s), ' + r.failed + ' echec(s).</strong>' +
      (r.aborted_reason ? '<span class="hint">' + esc(r.aborted_reason) + '</span>' : '') +
      (r.failed ? '<span class="hint">' + r.results.filter((x) => !x.ok)
        .map((x) => esc(x.command) + ' -> ' + esc(x.detail)).join('<br>') + '</span>' : '') +
      '</div>';
    await loadAudit();
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

/* ---------------------------------------------------------------- sante */

async function refreshHealth() {
  const dot = document.getElementById('health-dot');
  const pill = document.getElementById('mode-pill');
  try {
    const res = await fetch('/health/ready');
    const body = await res.json();
    dot.className = 'dot' + (res.ok ? '' : ' stale');
    // Le mode d'ecriture est la premiere chose a savoir : l'afficher en dur
    // comme "lecture seule" alors que l'enforcement est actif serait mensonger.
    const mode = body.enforcement_enabled ? 'ECRITURE ACTIVE' : 'lecture seule';
    pill.textContent = 'hors-bande · ' + mode + ' · ' +
      body.collectors_active + ' routeur(s)' +
      (body.routers_skipped ? ' · ' + body.routers_skipped + ' ignore(s)' : '') +
      (body.timescaledb ? ' · timescale' : '');
    pill.style.color = body.enforcement_enabled ? 'var(--warn)' : '';
    pill.style.borderColor = body.enforcement_enabled ? 'rgba(210,153,34,.45)' : '';
    pill.title = body.stale_jobs && body.stale_jobs.length
      ? 'Jobs en retard : ' + body.stale_jobs.join(', ') : 'Cycles a l\'heure';
  } catch (err) {
    dot.className = 'dot down';
    pill.textContent = 'controleur injoignable';
  }
}

/* -------------------------------------------------------------- routage */

const LOADERS = {
  dashboard: loadDashboard,
  exec: loadExec,
  network: loadNetwork,
  subscribers: loadSubscribers,
  topology: loadTopology,
  shaping: loadShaping,
  pops: loadRouters,
  remote: loadRemote,
};

async function show(view) {
  if (!LOADERS[view]) view = 'dashboard';
  state.view = view;
  document.querySelectorAll('section').forEach((s) => s.classList.remove('active'));
  document.getElementById('view-' + view).classList.add('active');
  document.querySelectorAll('nav.tabs a').forEach((a) =>
    a.classList.toggle('active', a.dataset.view === view));
  await refresh();
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    await LOADERS[state.view]();
  } catch (err) {
    console.error('Rafraichissement impossible :', err);
  } finally {
    refreshing = false;
  }
}

function route() { show((location.hash || '#/dashboard').replace('#/', '')); }

window.addEventListener('hashchange', route);
window.addEventListener('resize', () => {
  if (state.view === 'dashboard' && state.lastPoints.length) {
    renderThroughput(document.getElementById('throughput-chart'), state.lastPoints);
  }
});

document.getElementById('range-select').addEventListener('change', (e) => {
  state.rangeMinutes = Number(e.target.value);
  loadThroughput();
});
document.getElementById('btn-test').addEventListener('click', testConnection);
document.getElementById('btn-inspect').addEventListener('click', inspectShaping);
document.getElementById('enforcement-toggle').addEventListener('change', (e) => {
  toggleEnforcement(e.target.checked);
});
document.getElementById('btn-plan').addEventListener('click', computePlan);
document.getElementById('btn-discover').addEventListener('click', async (e) => {
  e.target.disabled = true;
  const notice = document.getElementById('topo-notice');
  notice.innerHTML = '<div class="notice">Lecture de /ip/neighbor sur chaque PoP...</div>';
  try {
    const r = await api('/topology/discover', { method: 'POST' });
    notice.innerHTML = '<div class="notice ok">' + r.nodes + ' equipement(s), ' +
      r.links + ' lien(s) decouvert(s).' +
      (r.warnings.length ? '<span class="hint">' + r.warnings.map(esc).join('<br>') + '</span>' : '') +
      '</div>';
    await loadNetwork();
  } catch (err) {
    notice.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  } finally {
    e.target.disabled = false;
  }
});
document.getElementById('topo-rate-only').addEventListener('change', (e) => {
  topo.rateOnly = e.target.checked;
  if (topo.data) { renderTopoCanvas(); renderTopologyLinks(topo.data.links); }
});
document.getElementById('btn-topo-reset').addEventListener('click', resetTopoLayout);
document.getElementById('btn-topo-link').addEventListener('click', (e) => {
  topo.linkMode = !topo.linkMode;
  topo.linkSource = null;
  e.target.classList.toggle('primary', topo.linkMode);
  e.target.textContent = topo.linkMode ? 'Terminer' : 'Creer un lien';
  if (topo.data) renderTopoCanvas();
  setTopoLinkNotice();
});
document.getElementById('btn-build-tree').addEventListener('click', () => buildTreeFromConfig(false));
document.getElementById('btn-remote-refresh').addEventListener('click', loadRemote);
document.getElementById('exec-range').addEventListener('change', (e) => {
  state.execRange = Number(e.target.value);
  loadExec();
});
document.getElementById('rtt-toggle').addEventListener('change', (e) => toggleRtt(e.target.checked));
document.getElementById('router-form').addEventListener('submit', saveRouter);
document.getElementById('a-btn-test').addEventListener('click', testAntenna);
document.getElementById('antenna-form').addEventListener('submit', saveAntenna);

document.getElementById('sub-pop').addEventListener('change', (e) => {
  state.subPop = e.target.value;
  loadSubscribers();
});

let searchTimer = null;
document.getElementById('sub-search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.subSearch = e.target.value.trim(); loadSubscribers(); }, 250);
});

route();
refreshHealth();
// Les PoPs ne changent pas tout seuls : inutile de recharger ce formulaire
// pendant qu'un administrateur le remplit.
// Les vues d'edition ne se rafraichissent pas toutes seules : ce serait effacer
// un formulaire en cours de saisie, ou un plan qu'on est en train de lire.
const VUES_FIGEES = new Set(['pops', 'shaping']);
setInterval(() => {
  if (VUES_FIGEES.has(state.view)) return;
  // L'arbre porte le debit des liens : le laisser vivre pour ne pas afficher un
  // debit perime. Mais on ne rafraichit PAS pendant qu'on deplace une case,
  // qu'une case est selectionnee (panneau ouvert), ou qu'un menu est ouvert :
  // ce serait annuler le geste en cours.
  if (state.view === 'network' && (topo.dragging || topo.selected || topo.linkMode ||
      (document.activeElement && document.activeElement.tagName === 'SELECT'))) return;
  // Vue Files live : ne pas ecraser un champ de debit en cours de saisie.
  if (state.view === 'exec' && document.activeElement &&
      document.activeElement.tagName === 'INPUT') return;
  refresh();
  // Le tiroir d'un lien suit le meme rythme : on regarde un debit justement
  // quand il bouge.
  if (state.link) openLink(state.link.key, state.link.minutes, true);
}, 10000);
setInterval(refreshHealth, 15000);
