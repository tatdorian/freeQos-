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
    throw new Error(typeof detail === 'string' ? detail : validationText(detail));
  }
  return body;
}

/** Rend lisible une erreur de validation d'API.
 *
 *  FastAPI rend un TABLEAU d'objets ; affiche tel quel, l'exploitant recevait
 *  '[{"type":"string_too_short","loc":["body","router"],...}]' en pleine page.
 *  Un message d'erreur qu'il faut dechiffrer ne vaut guere mieux que pas de
 *  message du tout. */
function validationText(detail) {
  if (!Array.isArray(detail)) return JSON.stringify(detail);
  const lignes = detail.map((e) => {
    const champ = Array.isArray(e.loc) ? e.loc.filter((l) => l !== 'body').join('.') : '';
    return (champ ? champ + ' : ' : '') + (e.msg || e.type || 'value rejected');
  });
  return lignes.join(' ; ');
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
    return '<span class="badge" title="Not enough load over the period to ' +
      'measure this subscriber\'s bufferbloat.">n/a</span>';
  }
  return '<span class="badge ' + esc(v.severity) + '" title="Idle latency ' +
    esc(v.idle_ms) + ' ms, under load ' + esc(v.loaded_ms) + ' ms, over ' +
    esc(v.samples) + ' sample(s)">' + esc(v.grade) + ' &middot; +' +
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

/** Anciennete lisible ("il y a 4 min"). Rend '-' sur une date absente plutot
 *  qu'une valeur par defaut : ne pas savoir n'est pas la meme chose que zero. */
function depuis(ts) {
  if (!ts) return '-';
  const secondes = (Date.now() - new Date(ts).getTime()) / 1000;
  if (!isFinite(secondes) || secondes < 0) return '-';
  if (secondes < 90) return 'just now';
  const minutes = Math.round(secondes / 60);
  if (minutes < 90) return minutes + ' min ago';
  const heures = Math.round(minutes / 60);
  if (heures < 48) return heures + ' h ago';
  return Math.round(heures / 24) + ' d ago';
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
  const legende = opt.labels || { down: 'Download', up: 'Upload', extra: 'Subscribers' };
  container.innerHTML = '';
  if (!points || points.length === 0) {
    container.innerHTML = '<div class="empty">No measurement over this period.</div>';
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
  const lib = legende || { down: 'Download', up: 'Upload', extra: 'Subscribers' };
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
  view: 'dashboard', rangeMinutes: 60, execRange: 60, subSearch: '', subPop: '', subKind: '',
  routers: [], lastPoints: [], lastTree: [],
  // Lien suivi dans le tiroir, et derniere mesure instantanee affichee.
  link: null, linkLive: null,
  // Motif de la derniere bascule d'enforcement, rendu avec la carte du shaping.
  enforcementReason: null,
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
    statCard('down', 'Download', down.v, down.u, 'sum of active sessions') +
    statCard('up', 'Upload', up.v, up.u, 'sum of active sessions') +
    statCard('', 'Subscribers online', overview.online || 0, '',
      esc((overview.subscribers || 0) + ' known') ) +
    statCard('', 'Sold throughput', (overview.sold_down_mbps || 0).toFixed(0), 'Mbps',
      ratio === null ? 'no known plan' : ratio.toFixed(0) + '% used') +
    statCard('', 'Backhaul capacity', (overview.backhaul_capacity_mbps || 0).toFixed(0), 'Mbps',
      esc((overview.backhauls || 0) + ' link(s) measured'));

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
    host.innerHTML = '<div class="empty">No active session.<br>Connect a PoP in the Devices tab.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Login</th><th>PoP</th><th class="num">Download</th>' +
    '<th style="width:150px">vs limit</th><th class="num">Upload</th>' +
    '<th class="num">Latency</th></tr></thead><tbody>' +
    rows.map((r) => {
      const limiteDown = (r.effective_down_mbps || 0) * 1e6;
      return '<tr class="clickable" data-sub="' + r.subscriber_id + '">' +
        '<td class="login">' + esc(r.login) + '</td>' +
        '<td class="nowrap">' + esc(r.pop_name || '-') + '</td>' +
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
    host.innerHTML = '<div class="card"><div class="empty">No backhaul declared.<br>' +
      '<code>backhauls</code> section of the inventory.</div></div>';
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
          (b.online === false ? ' <span class="badge crit">offline</span>' : '') +
          '<span class="host">' + esc(pop.name || '') + '</span></div>' +
        '<div class="node-metrics"><span>' + esc(mbps(capacity)) + '</span></div>' +
      '</div>' +
      '<div class="child" style="border:0;padding:.25rem 0">' +
        '<span class="name" style="color:var(--muted)">Capacity vs nominal</span>' +
        (fade === null ? '<span class="pct">-</span>'
          : meter(capacity, nominal, fade < 50 ? 'crit' : fade < 80 ? 'warn' : 'ok')) +
      '</div>' +
      '<div class="child" style="border:0;padding:.25rem 0">' +
        '<span class="name" style="color:var(--muted)">Load vs capacity</span>' +
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

/** QoE 0..100 derivee du SEUL RTT : le PROXY latence, et rien d'autre.
 *
 *  Le vrai score est composite (bufferbloat + latence a vide) et il est calcule
 *  cote serveur -- app/services/qoe.py, la meme fonction que la heatmap et que
 *  la boucle fermee. On ne le recode donc PAS ici : `qoeOf()` va le chercher, et
 *  cette formule ne sert que de repli quand le serveur n'a rien pu conclure
 *  (abonne silencieux, sonde RTT coupee). Recoder la regle cote client
 *  garantirait qu'elles divergent un jour. */
function qoeScore(ms) {
  if (ms === null || ms === undefined) return null;
  return Math.max(0, Math.min(100, Math.round(100 - Math.max(0, ms - 10) * 0.6)));
}
/** Score composite calcule par le serveur pour cet abonne, s'il existe. */
function qoeOf(subscriberId) {
  const b = exec.bloatById[subscriberId];
  return b && b.qoe ? b.qoe : null;
}
/** Cellule QoO : le score composite du serveur, ou le proxy latence a defaut.
 *  L'infobulle dit toujours d'ou vient le chiffre. */
function qooCell(note, rttMs) {
  if (note) {
    const detail = note.grade
      ? 'bufferbloat ' + note.grade + ' (+' + note.bloat_ms + ' ms under load)'
      : 'latency proxy: no load to correlate';
    return sqCell(String(note.score), note.severity, detail);
  }
  const proxy = qoeScore(rttMs);
  return proxy == null ? sqCell('-', 'none')
    : sqCell(String(proxy), qoeSev(proxy), 'latency proxy: no load to correlate');
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
function sqCell(text, sev, title) {
  const attr = title ? ' title="' + esc(title) + '"' : '';
  return '<span class="sq ' + (sev || 'none') + '"' + attr + '></span>' + esc(text);
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
const LEAF_KINDS = new Set(['client', 'subscriber', 'cpe', 'static', 'candidate']);

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
    exec.nodes.length + ' node(s), ' + subs.length + ' circuit(s)';
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
    html += '<div class="notice err"><b>Chargement partiel.</b> ' +
      esc(error.message) + '</div>';
  }
  if (state.noNodes && !error) {
    html += '<div class="notice"><b>No node.</b> Connect a router in the ' +
      '<b>Devices</b> tab.</div>';
  }
  if (state.topoOnly && !error) {
    html += '<div class="notice"><b>Topology only: no subscriber measured.</b></div>';
  }
  if (rttState && !rttState.enabled) {
    html += '<div class="notice"><b>RTT probe off.</b> RTT, QoO and bufferbloat ' +
      'stay empty.</div>';
  }
  notice.innerHTML = html;
}

function renderHeatmap(host, heat) {
  if (!heat || !Array.isArray(heat.rows)) {
    host.innerHTML = '<div class="empty">Heatmap unavailable for now.</div>';
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
      let t = c.value !== null && c.value !== undefined
        ? new Date(c.ts).toLocaleTimeString('en-GB', { hour12: false }) + ' : ' +
          c.value + (row.unit ? ' ' + row.unit : '')
        : 'no measurement';
      // La ligne QoE dit si le pas repose sur une latence SOUS CHARGE reellement
      // mesuree ou sur le repli proxy : un chiffre qu'on croit mesure est pire
      // qu'un repli annonce.
      if (c.basis === 'latency') t += ' (latency proxy: no load to correlate)';
      else if (c.basis) t += ' (bufferbloat ' + (c.grade || '?') + ', +' + c.bloat_ms + ' ms)';
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
    const nom = s.pop_name || '(no PoP)';
    if (!parPop.has(nom)) {
      parPop.set(nom, { name: nom, circuits: 0, tx: 0, rx: 0, effDown: 0, effUp: 0,
        confDown: 0, confUp: 0, rttMax: null, qoe: null, subs: [] });
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
    // QoO du noeud = le PIRE de ses circuits, jamais la moyenne : dix abonnes en
    // A+ et un en F, ce n'est pas "presque A", c'est un abonne dont la visio ne
    // marche pas. Les scores viennent du serveur, ils ne sont pas recalcules ici.
    const note = qoeOf(s.subscriber_id);
    if (note && (n.qoe === null || note.score < n.qoe.score)) n.qoe = note;
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
    host.innerHTML = '<div class="empty">No active circuit.</div>';
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
      '<td class="num">' + qooCell(n.qoe, n.rttMax) + '</td>' +
      '<td class="num">' + naSq + '</td><td class="num">' + naSq + '</td><td class="num">' + naSq + '</td></tr>';

    const subRows = !open ? '' : n.subs.map((s) => {
      const eff = (Number(s.effective_down_mbps) || 0) * 1e6;
      const effU = (Number(s.effective_up_mbps) || 0) * 1e6;
      const csel = exec.selected && exec.selected.type === 'client' && exec.selected.id === s.subscriber_id;
      return '<tr class="sub-row' + (csel ? ' selected' : '') + '" data-client="' + s.subscriber_id + '">' +
        '<td></td><td class="login">' + esc(s.login) + '</td>' +
        '<td class="num"></td><td class="num"></td>' +
        '<td class="num">' + esc(mbps(s.effective_down_mbps || 0) + ' / ' + mbps(s.effective_up_mbps || 0)) + '</td>' +
        '<td class="num na">' + esc(mbps(s.plan_down_mbps || 0) + ' / ' + mbps(s.plan_up_mbps || 0)) + '</td>' +
        '<td class="num">' + sqCell(bpsText(s.tx_bps), severity(pct(s.tx_bps, eff))) + '</td>' +
        '<td class="num">' + sqCell(bpsText(s.rx_bps), severity(pct(s.rx_bps, effU))) + '</td>' +
        '<td class="num">' + rttSq(s.rtt_ms) + '</td>' +
        '<td class="num">' + qooCell(qoeOf(s.subscriber_id), s.rtt_ms) + '</td>' +
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
    const vide = '<div class="empty">Select a node or a client in the table.</div>';
    live.innerHTML = snap.innerHTML = det.innerHTML = vide;
    return;
  }

  const isClient = sel.type === 'client';
  const client = isClient ? exec.subsById[sel.id] : null;
  const node = isClient ? null : exec.nodes.find((n) => n.name === sel.name);
  const title = isClient ? client.login : node.name;
  const down = isClient ? (Number(client.tx_bps) || 0) : node.tx;
  const up = isClient ? (Number(client.rx_bps) || 0) : node.rx;
  const effDown = isClient ? (Number(client.effective_down_mbps) || 0) * 1e6 : node.effDown;
  const effUp = isClient ? (Number(client.effective_up_mbps) || 0) * 1e6 : node.effUp;
  const confDown = isClient ? (Number(client.plan_down_mbps) || 0) * 1e6 : node.confDown;
  const confUp = isClient ? (Number(client.plan_up_mbps) || 0) * 1e6 : node.confUp;
  const rttMs = isClient ? client.rtt_ms : node.rttMax;
  // Score composite calcule par le serveur (bufferbloat + latence a vide). Pour
  // un noeud, celui de son circuit le plus degrade.
  const note = isClient ? qoeOf(client.subscriber_id) : node.qoe;
  const qoe = note ? note.score : qoeScore(rttMs);

  // Noeud issu de la seule topologie (aucun abonne mesure) : tout ce qui est
  // "live" reste en n/d — on ne fabrique pas de zeros.
  const synth = !isClient && !!node.synthetic;
  const rttSq = (ms) => (ms === null || ms === undefined)
    ? sqCell('-', 'none') : sqCell(Math.round(ms) + 'ms', rttSevJs(ms));
  const qooSq = qooCell(note, rttMs);
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
      ? '<div class="empty">No measurement for this node yet ' +
        '(shown from the topology).</div>'
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
        '<div class="lq-note">Rate in Mbps. Save writes an override on this subscriber ' +
          '(visible afterwards in the Shaping plan). Retr / marks / drops: out-of-band, unavailable.</div>' +
        '<div class="actions" style="margin-top:.6rem">' +
          '<button class="sm" id="lq-open">Open in the tree</button></div>' +
        '<div id="lq-result"></div>'
      : sharedCapacityBlock(node) +
        '<div class="lq-note">' + (synth
          ? '<b>Node from the topology.</b> No subscriber measured here yet: ' +
            'its queues will appear on the next collection cycle. The parent rate ' +
            '(the shared envelope) is already set on its link, <b>Bandwidth</b> button ' +
            'in the tree.'
          : '<b>' + node.circuits + ' circuit(s).</b> A node is an ' +
            'aggregate: unfold it and select a client to force a rate. The parent ' +
            'rate (the shared envelope) is set on its link, <b>Bandwidth</b> ' +
            'button in the tree. Retr / marks / drops: out-of-band, unavailable.') + '</div>' +
        '<div class="actions" style="margin-top:.6rem">' +
          '<button class="sm" id="lq-open">Set the envelope in the tree</button></div>');

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
    return '<div class="lq-note">Shared envelope unknown: no backhaul measured ' +
      'for this PoP. Add its antenna (Devices tab) or set it on its link.</div>';
  }
  const ratio = soldDown / envDown;
  const sev = ratio <= 1 ? 'ok' : ratio <= 2 ? 'warn' : 'crit';
  return '<div class="lq-kv" style="margin-top:.6rem">' +
    '<span class="k">Shared capacity</span><span class="v">' + esc(mbps(envDown)) + '</span>' +
    '<span class="k">Sold (&Sigma; plans)</span><span class="v">' + esc(mbps(soldDown)) + '</span>' +
    '<span class="k">Flowing (measured)</span><span class="v">' + esc(mbps(measured)) + '</span>' +
    '<span class="k">Oversubscription</span><span class="v">' +
      '<span class="sq ' + sev + '"></span>' + ratio.toFixed(1) + '&times;</span>' +
    '</div>' +
    '<div class="lq-note">' + (ratio > 1
      ? 'Sold plans total <b>' + ratio.toFixed(1) + '&times;</b> the envelope: ' +
        'circuits share the parent under load (this is intended, CAKE arbitrates).'
      : 'Under the envelope: no oversubscription on this parent.') + '</div>';
}

/** Ce que la pose immediate a REELLEMENT fait, rendu tel quel.
 *
 *  On ne resume pas en "enregistre" : c'est precisement ce raccourci qui
 *  laissait croire un abonne bride alors que rien n'etait parti sur le
 *  routeur. Le motif rendu par le controleur est affiche sans reformulation. */
function poseText(pose) {
  if (!pose) return '<div class="notice ok">Saved.</div>';
  const ok = pose.state === 'file-posee' || pose.state === 'file-retiree';
  const classe = ok ? 'ok' : (pose.state === 'file-a-poser' ? 'warn' : 'err');
  const titres = {
    'file-posee': 'Cap written on the router',
    'file-retiree': 'Queue removed from the router',
    'file-a-poser': 'Saved, but NOTHING was written',
    'ecarte': 'No queue written',
    'conflit': 'Conflict on the router',
    'sans-routeur': 'No router carries this target',
    'erreur': 'Write failed',
  };
  return '<div class="notice ' + classe + '"><strong>' +
    esc(titres[pose.state] || pose.state || 'Saved') +
    (pose.applied ? ' (' + pose.applied + ' command(s))' : '') + '</strong>' +
    (pose.reason ? '<span class="hint">' + esc(pose.reason) + '</span>' : '') +
    (pose.router ? '<span class="hint">Router: ' + esc(pose.router) + '</span>' : '') +
    '</div>';
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
    const reponse = await api('/shaping/policies', {
      method: 'PUT',
      body: JSON.stringify({
        scope: 'subscriber', target_key: client.login,
        max_down_mbps: down === '' ? null : Number(down),
        max_up_mbps: up === '' ? null : Number(up),
        enabled: true, note: 'forced from Live queues',
      }),
    });
    if (host) host.innerHTML = poseText(reponse.enforcement);
    await loadExec();
  } catch (err) {
    if (host) host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

async function clearClientRate(client) {
  try {
    await api('/shaping/policies/subscriber/' + encodeURIComponent(client.login), { method: 'DELETE' });
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
    host.innerHTML = '<div class="empty">No downstream traffic to draw.</div>';
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
    '" transform="rotate(90 ' + (srcX + srcW + 4) + ' ' + (M + 12) + ')">Network</text>');

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

/* ---------------------------------------------------------------- trafic
 *
 *  OU LE CONTROLEUR SE PLACE, ET POURQUOI CET ONGLET DIT "point de mesure".
 *
 *  Il n'est jamais sur le chemin des paquets. Le volume ne peut donc venir que
 *  de ce que les routeurs exportent deja : quelques dizaines de kbit/s de
 *  resumes plutot qu'un miroir de port qui recopierait chaque octet sur le lien
 *  de collecte, dans les deux sens, a travers le coeur.
 *
 *  Les exporteurs se declarent AUX DEUX EXTREMITES -- en amont du coeur (sortie
 *  internet) et au PoP -- et jamais au milieu. Le meme octet est donc vu deux
 *  fois : les additionner doublerait la consommation de chacun. Le point de
 *  mesure fait partie de la mesure, et la lecture n'en retient qu'un.
 */

const FLOW = { minutes: 60, vantage: '', pop: '', category: '', search: '', open: new Set() };

const VANTAGE_LABEL = {
  edge: 'upstream of the core',
  pop: 'at the PoP',
  unknown: 'undeclared',
};

function flowNotice(html) {
  document.getElementById('flow-notice').innerHTML = html || '';
}

/** Ce qui EMPECHE de mesurer, et le geste qui le corrige.
 *
 *  Un tableau vide a plusieurs causes opposees -- collecteur coupe, aucun
 *  routeur declare, ecriture interdite, export pose mais port non publie -- et
 *  un ecran vide les confond toutes. Nommer le fait ne suffit pas : "configurez
 *  l'export ci-dessous" laisse chercher OU et COMMENT. Chaque cas porte donc
 *  son bouton, qui mene exactement au geste suivant. */
function flowDiagnostic(etat, exportEtat) {
  if (!etat.enabled) {
    return '<div class="notice err"><b>NetFlow collector off.</b> ' +
      '<code>NETFLOW_ENABLED=true</code> then restart.</div>';
  }
  if (!etat.listening) {
    return '<div class="notice err"><b>The collector is not listening.</b> ' +
      esc(etat.last_error || 'unknown cause') + '</div>';
  }
  if (!etat.packets_received) {
    const routeurs = (exportEtat && exportEtat.routers) || [];
    const poses = routeurs.filter((r) => r.configured).length;
    if (!routeurs.length) {
      return '<div class="notice warn"><b>No datagram received on ' +
        esc(etat.bind) + '.</b> No router is declared: nothing can ' +
        'export. <a href="#/pops">Devices tab</a></div>';
    }
    if (!poses) {
      return '<div class="notice warn"><b>No datagram received on ' +
        esc(etat.bind) + '.</b> ' + esc(routeurs.length) +
        ' router(s) declared, none exports yet. The export is set up ' +
        'automatically on each router at the next pass.</div>';
    }
    // POSE MAIS MUET : le routeur envoie, et rien n'arrive. Le coupable le plus
    // frequent n'est pas le routeur, c'est le chemin -- port UDP non publie par
    // Docker, pare-feu, ou adresse annoncee injoignable depuis le PoP.
    return '<div class="notice err"><b>Export configured on ' + esc(poses) +
      ' router(s), but no datagram reaches ' + esc(etat.bind) + '.</b> ' +
      'The path is at fault, not the configuration: is port <code>' +
      esc(String(etat.bind).split(':').pop()) + '/udp</code> published? ' +
      'A firewall between the PoP and this collector?</div>';
  }
  if (etat.flows_seen && !etat.flows_matched) {
    return '<div class="notice warn"><b>Flows received, none matched to a subscriber.</b> ' +
      esc(etat.declared_prefixes) + ' prefix(es) declared.</div>';
  }
  if (etat.orphan_records && !etat.templates_known) {
    return '<div class="notice warn"><b>Flows received, templates never sent</b> ' +
      '(RouterOS : <code>template-refresh</code>).</div>';
  }
  return '';
}

async function loadTraffic() {
  FLOW.minutes = Number(document.getElementById('flow-range').value) || 60;
  FLOW.vantage = document.getElementById('flow-vantage').value || '';

  const suffixe = '?minutes=' + FLOW.minutes + (FLOW.vantage ? '&vantage=' + FLOW.vantage : '');
  const [etat, top, hotes, exporteurs, points] = await Promise.all([
    api('/netflow/status'),
    api('/netflow/top' + suffixe + '&limit=25').catch(() => null),
    api('/netflow/hosts?limit=60').catch(() => ({ hosts: [], vlans: [] })),
    api('/netflow/exporters').catch(() => []),
    api('/netflow/vantages?minutes=' + FLOW.minutes).catch(() => null),
  ]);

  const exportEtat = await api('/netflow/export').catch(() => null);
  flowNotice(flowDiagnostic(etat, exportEtat));
  renderVantages(points, top);
  renderFlowStats(etat, top);
  renderFlowTop(top);
  renderFlowHosts(hotes);
  renderFlowExporters(exporteurs);
  await loadFlowPairs();

  const compte = document.getElementById('flow-count');
  if (compte) {
    compte.textContent = etat.listening
      ? etat.bind + ' · ' + etat.packets_received + ' datagram(s)'
      : 'collector stopped';
  }
}

/** Les deux points de mesure, cote a cote : la sortie internet et les PoP.
 *
 *  Ils voient le meme trafic a deux endroits. Les montrer ensemble dit tout
 *  de suite si l'un manque, et lequel sert au decompte (on ne compte le meme
 *  octet qu'une fois). */
const VANTAGE_CARD = {
  edge: {
    title: 'Internet edge',
    sub: 'Upstream of the core, where internet arrives',
    icon: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>' +
      '<path d="M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
    missing: 'No router exports here yet. Give your internet gateway the ' +
      '<b>Gateway</b> role in Devices: its export is then set up automatically.',
  },
  pop: {
    title: 'PoPs',
    sub: 'At each point of presence, next to the subscribers',
    icon: '<rect x="3" y="5" width="18" height="6" rx="2"/><rect x="3" y="13" width="18" height="6" rx="2"/>' +
      '<path d="M7 8h.01M7 16h.01"/>',
    missing: 'No PoP exports yet. The export is set up automatically on each ' +
      'PoP router once writing is enabled.',
  },
};

function renderVantages(data, top) {
  const hote = document.getElementById('flow-vantages');
  if (!hote) return;
  if (!data || !data.points) { hote.innerHTML = ''; return; }
  const compte = (top && top.vantage) || data.accounting;
  hote.innerHTML = data.points.map((p) => {
    const carte = VANTAGE_CARD[p.vantage];
    if (!carte) return '';
    const t = p.totals || {};
    const compteIci = p.vantage === compte;
    const etat = p.active
      ? (compteIci ? '<span class="badge file">Counting</span>'
        : '<span class="badge ok">Receiving</span>')
      : (p.exporters ? '<span class="badge warn">Silent</span>'
        : '<span class="badge">No exporter</span>');
    return '<div class="vantage' + (compteIci ? ' counting' : '') + '">' +
      '<div class="vantage-head">' +
        '<span class="vantage-icon"><svg viewBox="0 0 24 24">' + carte.icon + '</svg></span>' +
        '<div class="vantage-title">' + carte.title +
          '<span class="hint">' + carte.sub + '</span></div>' +
        etat +
      '</div>' +
      (p.exporters || p.active
        ? '<div class="vantage-figures">' +
            '<div class="d"><span>Down</span><b>' + bytesText(t.down_bytes || 0) + '</b></div>' +
            '<div class="u"><span>Up</span><b>' + bytesText(t.up_bytes || 0) + '</b></div>' +
            '<div><span>Subscribers</span><b>' + esc(t.subscribers || 0) + '</b></div>' +
            '<div><span>Exporters</span><b>' + esc(p.exporters) + '</b></div>' +
          '</div>'
        : '<div class="vantage-note">' + carte.missing + '</div>') +
    '</div>';
  }).join('');
}

/** Drapeau d'un pays a partir de son code ISO (FR -> drapeau francais).
 *  Aucune image : les indicateurs regionaux Unicode suffisent. */
function drapeau(code) {
  const c = String(code || '').trim().toUpperCase();
  if (!/^[A-Z]{2}$/.test(c)) return '';
  return '<span class="flag" title="' + c + '">' +
    String.fromCodePoint(...[...c].map((l) => 0x1F1E6 + l.charCodeAt(0) - 65)) + '</span>';
}

/** Ville, pays, et drapeau d'une adresse localisee ; vide si inconnue. */
function lieu(r) {
  if (!r || !(r.country || r.city)) return '';
  return '<span class="geo">' + drapeau(r.country) +
    esc([r.city, r.country].filter(Boolean).join(', ')) + '</span>';
}

/** Lien vers la carte (OpenStreetMap), ouvert dans un nouvel onglet. */
function lienCarte(lat, lon) {
  if (lat === null || lat === undefined || lon === null || lon === undefined) return '';
  const la = Number(lat).toFixed(4);
  const lo = Number(lon).toFixed(4);
  return '<a href="//www.openstreetmap.org/?mlat=' + la + '&mlon=' + lo +
    '#map=9/' + la + '/' + lo + '" target="_blank" rel="noopener">Open map</a>';
}

function renderFlowStats(etat, top) {
  const totaux = (top && top.totals) || {};
  const descendant = Number(totaux.down_bytes) || 0;
  const montant = Number(totaux.up_bytes) || 0;
  const vus = etat.flows_seen || 0;
  const rattaches = etat.flows_matched || 0;
  const part = vus ? Math.round((rattaches / vus) * 100) : 0;
  document.getElementById('flow-stats').innerHTML =
    statCard('down', 'Downstream', bytesText(descendant), '',
      'over ' + FLOW.minutes + ' min') +
    statCard('up', 'Upstream', bytesText(montant), '',
      'over ' + FLOW.minutes + ' min') +
    statCard('', 'Subscribers seen', String(totaux.subscribers || 0), '',
      (top && top.vantage ? 'counted ' + esc(VANTAGE_LABEL[top.vantage] || top.vantage) : '')) +
    statCard(part < 50 ? 'warn' : '', 'Flows matched', String(part), '%',
      rattaches + ' of ' + vus + ' since startup');
}

function renderFlowTop(top) {
  const host = document.getElementById('flow-top');
  const lignes = (top && top.subscribers) || [];
  if (!lignes.length) {
    host.innerHTML = '<div class="empty">No volume measured over this period.</div>';
    return;
  }
  host.innerHTML = '<table><thead><tr><th>Subscriber</th><th>PoP</th><th>Kind</th>' +
    '<th class="num">Down</th><th class="num">Up</th>' +
    '<th class="num">Plan</th><th class="num">Flows</th><th>Seen</th></tr></thead><tbody>' +
    lignes.map((r) =>
      '<tr><td><a href="#" data-flow-sub="' + esc(r.subscriber_id) + '">' +
        esc(r.login) + '</a></td>' +
      '<td class="nowrap">' + esc(r.pop_name || '-') + '</td>' +
      '<td>' + kindBadge(r.kind) + '</td>' +
      '<td class="num">' + bytesText(r.down_bytes) + '</td>' +
      '<td class="num">' + bytesText(r.up_bytes) + '</td>' +
      '<td class="num">' + (r.plan_down_mbps
        ? esc(r.plan_down_mbps) + '/' + esc(r.plan_up_mbps || '?') + ' Mbps' : '-') + '</td>' +
      '<td class="num">' + esc(r.flows) + '</td>' +
      '<td>' + esc(depuis(r.last_ts)) + '</td></tr>').join('') +
    '</tbody></table>';
  host.querySelectorAll('[data-flow-sub]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      openSubscriber(Number(a.dataset.flowSub));
    });
  });
}

/** Un debit a partir d'un volume et d'une duree.
 *
 *  Des octets seuls ne disent pas si c'est un filet continu ou une rafale :
 *  « 3 Kio » sur une heure et « 3 Kio » sur deux secondes n'appellent pas la
 *  meme reaction. */
function debit(octets, secondes) {
  if (!secondes || secondes <= 0) return null;
  return (Number(octets) || 0) * 8 / secondes;
}

function debitText(octets, secondes) {
  const v = debit(octets, secondes);
  return v === null ? '<span class="hint">-</span>' : bpsText(v);
}

/** QUI PARLE A QUI : une ligne par conversation.
 *
 *  Le tableau par usage agrege, celui par adresse fond tous les clients
 *  ensemble. Le couple (client, destination) est la seule forme qui montre la
 *  conversation -- et c'est ce qu'on vient chercher quand une famille d'usage
 *  pese sans qu'on sache pourquoi. */
/** Remplit un selecteur avec ce qui EXISTE dans les donnees.
 *
 *  Proposer tous les PoPs de l'inventaire et toutes les familles du catalogue
 *  ferait choisir des filtres qui ne rendent rien -- et on chercherait la panne
 *  plutot que le filtre. */
function remplirFacette(id, libelle, valeurs, choisi) {
  const select = document.getElementById(id);
  const avant = select.value;
  select.innerHTML = '<option value="">' + esc(libelle) + '</option>' +
    (valeurs || []).map((v) =>
      '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
  select.value = choisi || avant || '';
}

async function loadFlowPairs() {
  const hote = document.getElementById('flow-pairs');
  const parametres = '?minutes=' + FLOW.minutes + '&limit=500' +
    (FLOW.pop ? '&pop=' + encodeURIComponent(FLOW.pop) : '') +
    (FLOW.category ? '&category=' + encodeURIComponent(FLOW.category) : '') +
    (FLOW.search ? '&q=' + encodeURIComponent(FLOW.search) : '');

  let data;
  try {
    data = await api('/netflow/pairs' + parametres);
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const facettes = data.facets || {};
  remplirFacette('flow-pairs-pop', 'All PoPs', facettes.pops, FLOW.pop);
  remplirFacette('flow-pairs-category', 'All categories', facettes.categories, FLOW.category);
  const direct = new Set((data.live || []).map((c) => c[0] + '|' + c[1]));
  const octetsDirect = data.live_bytes || {};
  const fenetre = data.window_seconds || 0;
  const periode = FLOW.minutes * 60;
  const lignes = data.pairs || [];

  // UN BLOC PAR CLIENT. La ligne de tete donne sa consommation totale ; on
  // deplie pour voir avec quelles adresses elle se fait, et a quel debit.
  const clients = new Map();
  lignes.forEach((r) => {
    const cle = String(r.client);
    if (!clients.has(cle)) {
      clients.set(cle, { tete: r, lignes: [], down: 0, up: 0, direct: 0, vivants: 0 });
    }
    const c = clients.get(cle);
    c.lignes.push(r);
    c.down += Number(r.down_bytes || 0);
    c.up += Number(r.up_bytes || 0);
    const paire = r.client + '|' + r.address;
    if (direct.has(paire)) {
      c.vivants += 1;
      c.direct += Number(octetsDirect[paire] || 0);
    }
  });
  const groupes = Array.from(clients.values())
    .sort((a, b) => (b.down + b.up) - (a.down + a.up));

  const filtre = [FLOW.search, FLOW.pop, FLOW.category].filter(Boolean).length;
  document.getElementById('flow-pairs-count').textContent =
    groupes.length + ' client(s) · ' + lignes.length + ' conversation(s) · ' +
    direct.size + ' live' + (filtre ? ' · ' + filtre + ' filtre(s)' : '');

  if (!groupes.length) {
    hote.innerHTML = '<div class="empty">' + (filtre
      ? 'No conversation matches these filters.'
      : 'No conversation over this period.') + '</div>';
    return;
  }

  const conversation = (r) => {
    const paire = r.client + '|' + r.address;
    return '<tr>' +
      '<td><a href="#" data-pair-ip="' + esc(r.address) + '"><code>' +
        esc(r.address) + '</code></a>' +
        (domaine(r.hostname)
          ? '<br><b style="font-size:.75rem">' + esc(domaine(r.hostname)) + '</b>' : '') +
        (r.hostname ? '<br><span class="hint">' + esc(r.hostname) + '</span>' : '') +
        (lieu(r) ? '<br>' + lieu(r) : '') + '</td>' +
      '<td>' + (r.service
        ? '<a href="#" data-pair-service="' + esc(r.service) + '">' + esc(r.service) + '</a>'
        : '<span class="hint">unidentified</span>') + '</td>' +
      '<td>' + (r.category
        ? '<a href="#" data-pair-cat="' + esc(r.category) + '">' +
          svcBadge(r.category) + '</a>'
        : svcBadge(null)) + '</td>' +
      '<td class="num">' + esc(r.port || '-') + '</td>' +
      '<td>' + esc(protoName(r.protocol)) + '</td>' +
      '<td class="num">' + bytesText(r.down_bytes) + '</td>' +
      '<td class="num">' + bytesText(r.up_bytes) + '</td>' +
      '<td class="num">' +
        debitText(Number(r.down_bytes || 0) + Number(r.up_bytes || 0), periode) + '</td>' +
      '<td class="num">' + (direct.has(paire)
        ? '<b>' + debitText(octetsDirect[paire] || 0, fenetre) + '</b>'
        : '<span class="hint">-</span>') + '</td>' +
      '</tr>';
  };

  hote.innerHTML = groupes.map((c) => {
    const r = c.tete;
    const ouvert = FLOW.open.has(String(r.client));
    return '<details class="flow-client" data-flow-client="' + esc(r.client) + '"' +
        (ouvert ? ' open' : '') + '>' +
      '<summary>' +
        '<span class="login">' + clientCell(r) + '</span>' +
        (r.pop_name ? ' <span class="hint">' + esc(r.pop_name) + '</span>' : '') +
        '<span class="spacer"></span>' +
        '<span class="flow-client-sum">' +
          '<span>&darr; ' + bytesText(c.down) + '</span>' +
          '<span>&uarr; ' + bytesText(c.up) + '</span>' +
          '<span>' + debitText(c.down + c.up, periode) + ' avg</span>' +
          '<span>' + (c.vivants
            ? '<b>' + debitText(c.direct, fenetre) + '</b> live'
            : '<span class="hint">idle</span>') + '</span>' +
          '<span class="hint">' + c.lignes.length + ' address(es)</span>' +
        '</span>' +
      '</summary>' +
      '<div class="table-wrap"><table><thead><tr><th>Destination</th><th>Service</th>' +
        '<th>Category</th><th class="num">Port</th><th>Proto</th>' +
        '<th class="num">Down</th><th class="num">Up</th>' +
        '<th class="num">Avg rate</th><th class="num">Live</th>' +
      '</tr></thead><tbody>' + c.lignes.map(conversation).join('') +
      '</tbody></table></div></details>';
  }).join('');

  // Les blocs ouverts le restent quand la liste se recharge (filtre, periode).
  hote.querySelectorAll('[data-flow-client]').forEach((d) => {
    d.addEventListener('toggle', () => {
      if (d.open) FLOW.open.add(d.dataset.flowClient);
      else FLOW.open.delete(d.dataset.flowClient);
    });
  });
  hote.querySelectorAll('[data-svc-sub]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      openSubscriber(Number(a.dataset.svcSub));
    });
  });
  hote.querySelectorAll('[data-pair-ip]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      openPairAddress(a.dataset.pairIp);
    });
  });
  // Chaque valeur du tableau est un filtre : on clique ce qu'on voit plutot
  // que de le retrouver dans un selecteur.
  hote.querySelectorAll('[data-pair-cat]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const valeur = a.getAttribute('data-pair-cat');
      FLOW.category = FLOW.category === valeur ? '' : valeur;
      loadFlowPairs();
    });
  });
  hote.querySelectorAll('[data-pair-service]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      document.getElementById('flow-pairs-search').value = a.dataset.pairService;
      FLOW.search = a.dataset.pairService;
      loadFlowPairs();
    });
  });
}

/** La fiche complete d'une adresse, depuis l'onglet Trafic. */
async function openPairAddress(address) {
  const hote = document.getElementById('flow-pair-detail');
  hote.innerHTML = '<div class="ip-card">Reading <code>' + esc(address) + '</code>...</div>';
  try {
    const fiche = await api('/netflow/destinations/' + encodeURIComponent(address) +
      '?minutes=' + Math.max(FLOW.minutes, 1440));
    hote.innerHTML = ipCard(fiche, Math.max(FLOW.minutes, 1440) * 60, false);
    brancherLiensServices(hote);
    hote.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

/** Les adresses vues qui ne correspondent a aucune fiche.
 *
 *  A LIRE COMME UNE PISTE. Une imprimante, une camera ou l'equipement d'un
 *  autre operateur laissent exactement la meme trace qu'un client, et rien dans
 *  un flux ne dit quel debit a ete vendu. Le bouton ne cree donc rien : il
 *  emmene vers le formulaire de declaration, ou un humain saisit le plan. */
function renderFlowHosts(data) {
  const host = document.getElementById('flow-hosts');
  const lignes = (data && data.hosts) || [];
  if (!lignes.length) {
    host.innerHTML = '<div class="empty">No unmatched address. ' +
      'Either everything is declared, or nothing talks on your client VLANs.</div>';
    return;
  }
  host.innerHTML = '<table><thead><tr><th>Address</th><th>VLAN</th><th>PoP</th>' +
    '<th>Exporter</th><th class="num">Down</th><th class="num">Up</th>' +
    '<th>Seen</th><th></th></tr></thead><tbody>' +
    lignes.map((h) =>
      '<tr><td><code>' + esc(h.address) + '</code></td>' +
      '<td>' + (h.vlan_id ? esc(h.vlan_id) : '<span class="faint">-</span>') + '</td>' +
      '<td>' + esc(h.pop_name || '-') + '</td>' +
      '<td>' + esc(h.exporter || '-') + '</td>' +
      '<td class="num">' + bytesText(h.down_bytes) + '</td>' +
      '<td class="num">' + bytesText(h.up_bytes) + '</td>' +
      '<td>' + esc(depuis(h.last_seen)) + '</td>' +
      '<td><button class="sm" data-declare-host="' + esc(h.address) + '"' +
        ' data-declare-vlan="' + esc(h.vlan_id || '') + '"' +
        ' data-declare-pop="' + esc(h.pop_name || '') + '">Declare</button></td>' +
      '</tr>').join('') + '</tbody></table>';
  host.querySelectorAll('[data-declare-host]').forEach((b) => {
    b.addEventListener('click', () => {
      location.hash = '#/subscribers';
      // Le panneau de saisie est replie par defaut : l'ouvrir, sinon le
      // formulaire pre-rempli serait invisible et le geste paraitrait sans effet.
      const panneau = document.getElementById('sc-panel');
      if (panneau) panneau.hidden = false;
      scRemplirFormulaire({
        reference: '',
        address: b.dataset.declareHost,
        vlan: b.dataset.declareVlan ? Number(b.dataset.declareVlan) : null,
        pop_name: b.dataset.declarePop || '',
      });
      scNotice('<span class="warn">Address taken from observed traffic. ' +
        'Enter the reference and the subscribed rate: nobody can guess those.</span>');
    });
  });
}

function renderFlowExporters(rows) {
  const host = document.getElementById('flow-exporters');
  if (!rows || !rows.length) {
    host.innerHTML = '<div class="empty">No exporter. Declare one below, ' +
      'or configure the export on a router: it will appear on its own, marked ' +
      '<code>unknown</code>.</div>';
    return;
  }
  host.innerHTML = '<table><thead><tr><th>Address</th><th>Name</th><th>Vantage</th>' +
    '<th>PoP</th><th class="num">Sampling</th><th class="num">Datagrams</th>' +
    '<th class="num">Flows</th><th>Version</th><th>Seen</th><th></th></tr></thead><tbody>' +
    rows.map((e) => {
      const inconnu = e.vantage === 'unknown';
      return '<tr><td><code>' + esc(e.address) + '</code></td>' +
        '<td>' + esc(e.name || '-') + '</td>' +
        '<td><span class="badge ' + (inconnu ? 'warn' : 'ok') + '">' +
          esc(VANTAGE_LABEL[e.vantage] || e.vantage) + '</span></td>' +
        '<td class="nowrap">' + esc(e.pop_name || '-') + '</td>' +
        '<td class="num">' + (e.sampling_rate > 1 ? '1:' + esc(e.sampling_rate) : 'all') + '</td>' +
        '<td class="num">' + esc(e.packets_seen) + '</td>' +
        '<td class="num">' + esc(e.flows_seen) + '</td>' +
        '<td>' + esc(e.last_version || '-') + '</td>' +
        '<td>' + esc(depuis(e.last_seen)) + '</td>' +
        '<td><button class="sm" data-exp-edit="' + esc(e.address) + '"' +
          ' data-exp-name="' + esc(e.name || '') + '"' +
          ' data-exp-vantage="' + esc(e.vantage) + '"' +
          ' data-exp-pop="' + esc(e.pop_name || '') + '"' +
          ' data-exp-sampling="' + esc(e.sampling_rate) + '">Edit</button> ' +
          '<button class="sm" data-exp-del="' + esc(e.id) + '">Remove</button></td>' +
        '</tr>';
    }).join('') + '</tbody></table>';

  host.querySelectorAll('[data-exp-edit]').forEach((b) => {
    b.addEventListener('click', () => {
      document.getElementById('exp-address').value = b.dataset.expEdit;
      document.getElementById('exp-name').value = b.dataset.expName;
      document.getElementById('exp-vantage').value =
        b.dataset.expVantage === 'edge' ? 'edge' : 'pop';
      document.getElementById('exp-pop').value = b.dataset.expPop;
      document.getElementById('exp-sampling').value = b.dataset.expSampling;
    });
  });
  host.querySelectorAll('[data-exp-del]').forEach((b) => {
    b.addEventListener('click', async () => {
      if (!confirm('Remove this exporter from the list? Its flows will go back to ' +
                   '"undeclared" if it keeps sending.')) return;
      try {
        await api('/netflow/exporters/' + b.dataset.expDel, { method: 'DELETE' });
        await loadTraffic();
      } catch (err) { alert(err.message); }
    });
  });
}

async function declareExporter(event) {
  event.preventDefault();
  const charge = {
    address: document.getElementById('exp-address').value.trim(),
    name: document.getElementById('exp-name').value.trim() || null,
    vantage: document.getElementById('exp-vantage').value,
    pop_name: document.getElementById('exp-pop').value.trim() || null,
    sampling_rate: Number(document.getElementById('exp-sampling').value) || 1,
  };
  const sortie = document.getElementById('exporter-result');
  try {
    await api('/netflow/exporters', { method: 'POST', body: JSON.stringify(charge) });
    sortie.innerHTML = '<div class="notice ok">Exporter declared.</div>';
    document.getElementById('exporter-form').reset();
    await loadTraffic();
  } catch (err) {
    sortie.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

/* ----------------------------------------------------------------- API */

/** Les points d'entree que cette application expose, et la cle pour y entrer.
 *
 *  Le contrat est celui de Preseem : un integrateur qui parlait deja a Preseem
 *  change l'URL de base et la cle, rien d'autre. */
const API_ENDPOINTS = [
  ['PUT', '/model/v1/accounts/{id}', 'Customer'],
  ['PUT', '/model/v1/packages/{id}', 'Package'],
  ['PUT', '/model/v1/sites/{id}', 'Site'],
  ['PUT', '/model/v1/access_points/{id}', 'Radio sector'],
  ['PUT', '/model/v1/services/{id}', 'Sold line'],
  ['GET', '/model/v1/{collection}', 'Read a collection'],
  ['DELETE', '/model/v1/{collection}/{id}', 'Remove a record'],
  ['GET', '/usage/v1/services', 'Usage, every service'],
  ['GET', '/usage/v1/services/{id}', 'Usage of one service'],
];

async function loadApi() {
  document.getElementById('api-base').textContent = location.origin;
  document.getElementById('api-endpoints').innerHTML =
    '<table><thead><tr><th>Method</th><th>Path</th><th>Object</th></tr></thead><tbody>' +
    API_ENDPOINTS.map(([verbe, chemin, objet]) =>
      '<tr><td><b>' + esc(verbe) + '</b></td>' +
      '<td class="login">' + esc(chemin) + '</td>' +
      '<td>' + esc(objet) + '</td></tr>').join('') +
    '</tbody></table>';
  document.getElementById('api-sample').textContent =
    'curl -u <key>: -X PUT ' + location.origin + '/model/v1/services/abo-42 \\\n' +
    "  -H 'content-type: application/json' \\\n" +
    '  -d \'{"name":"Dupont","address":"10.20.0.10/32","download_mbps":100,' +
    '"upload_mbps":20,"account":"cli-7","package":"fibre-100"}\'';
  await loadApiKeys();
  const lignes = document.querySelectorAll('#keys-table tbody tr').length;
  document.getElementById('api-count').textContent = lignes + ' key(s)';
}

/* ------------------------------------------------------------ cles d'API */

async function loadApiKeys() {
  const host = document.getElementById('keys-table');
  if (!host) return;
  let rows;
  try {
    rows = await api('/api-keys');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  if (!rows.length) {
    host.innerHTML = '<div class="empty">No key.</div>';
    return;
  }
  host.innerHTML = '<table><thead><tr><th>Name</th><th>Prefix</th><th>Scopes</th>' +
    '<th>State</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>' +
    rows.map((k) =>
      '<tr><td>' + esc(k.name) + '</td>' +
      '<td><code>fqos_' + esc(k.prefix) + '_…</code></td>' +
      '<td>' + esc((k.scopes || []).join(', ')) + '</td>' +
      '<td><span class="badge ' + (k.enabled ? 'ok' : 'warn') + '">' +
        (k.enabled ? 'enabled' : 'disabled') + '</span></td>' +
      '<td>' + esc(clock(k.created_at)) + '</td>' +
      '<td>' + (k.last_used_at ? esc(depuis(k.last_used_at)) :
        '<span class="faint">never</span>') + '</td>' +
      '<td><button class="sm" data-key-toggle="' + esc(k.id) + '"' +
        ' data-key-enabled="' + (k.enabled ? '1' : '') + '">' +
        (k.enabled ? 'Disable' : 'Re-enable') + '</button> ' +
        '<button class="sm" data-key-del="' + esc(k.id) + '">Revoke</button></td>' +
      '</tr>').join('') + '</tbody></table>';

  host.querySelectorAll('[data-key-toggle]').forEach((b) => {
    b.addEventListener('click', async () => {
      try {
        await api('/api-keys/' + b.dataset.keyToggle, {
          method: 'PATCH',
          body: JSON.stringify({ enabled: !b.dataset.keyEnabled }),
        });
        await loadApiKeys();
      } catch (err) { alert(err.message); }
    });
  });
  host.querySelectorAll('[data-key-del]').forEach((b) => {
    b.addEventListener('click', async () => {
      if (!confirm('Revoke this key?')) return;
      try {
        await api('/api-keys/' + b.dataset.keyDel, { method: 'DELETE' });
        await loadApiKeys();
      } catch (err) { alert(err.message); }
    });
  });
}

/** Cree la cle et affiche son secret UNE SEULE FOIS.
 *
 *  Il n'existe nulle part ailleurs : la base n'en garde que l'empreinte. C'est
 *  le seul message de cette interface qui a le droit de crier -- une page
 *  rechargee sans avoir copie le secret oblige a tout recommencer. */
async function createApiKey(event) {
  event.preventDefault();
  const sortie = document.getElementById('key-result');
  const portee = document.getElementById('key-scope').value;
  try {
    const cle = await api('/api-keys', {
      method: 'POST',
      body: JSON.stringify({
        name: document.getElementById('key-name').value.trim(),
        scopes: portee === 'write' ? ['read', 'write'] : ['read'],
      }),
    });
    sortie.innerHTML = '<div class="notice ok"><strong>Copy this key: it will not ' +
      'be shown again.</strong>' +
      '<pre style="user-select:all;white-space:pre-wrap;word-break:break-all">' +
      esc(cle.secret) + '</pre>' +
      '<code>curl -u ' + esc(cle.secret) + ': ' + esc(location.origin) +
      '/model/v1/services</code></div>';
    document.getElementById('key-form').reset();
    await loadApiKeys();
  } catch (err) {
    sortie.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

/* ---------------------------------------------------------- arbre reseau */

const ICONE = {
  gateway: 'GW', core: 'CORE', pop: 'POP', radio: 'RF',
  sector: 'SECT', cpe: 'CPE', client: 'CLI', unknown: '?', subscriber: 'ABO',
  static: 'FIXE', candidate: '?IP',
};

/** Combien d'equipements et de liens porte ce graphe.
 *
 *  ``counts`` est un resume produit par le serveur : quand il manque, on
 *  affichait 'undefined equipement(s)'. Les listes, elles, sont toujours la --
 *  ce sont meme elles qui sont dessinees. */
function topoCompte(data) {
  const c = (data && data.counts) || {};
  return {
    noeuds: c.nodes != null ? c.nodes : ((data && data.nodes) || []).length,
    liens: c.links != null ? c.links : ((data && data.links) || []).length,
  };
}

/** Charge le graphe et les abonnes une seule fois, partage entre l'arbre
 *  editable et le tableau des liens, qui vivent tous deux dans l'onglet
 *  Arbre reseau. */
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
 *  tableau des liens, qui avait son propre onglet, vit replie juste dessous. */
async function loadNetwork() {
  const data = await fetchTopo();
  const compte = document.getElementById('net-count');
  if (compte) {
    const n = topoCompte(data);
    compte.textContent = n.noeuds + ' device(s), ' + n.liens + ' link(s)';
  }
  renderDecouverte(data);
  renderTopoCanvas();
  topoAjusterUneFois();
  renderTopoPanel();
  // Le tableau des liens, replie juste dessous : meme donnee, lue en lignes.
  await loadTopology();
}

/** Ce que la DERNIERE decouverte a a dire, qu'elle vienne du job periodique ou
 *  du bouton. Un arbre de cases isolees sans explication n'aide personne : ces
 *  avertissements disent precisement ce qui manque pour les relier. */
function renderDecouverte(data) {
  const hote = document.getElementById('topo-notice');
  if (!hote) return;
  const avertissements = data.warnings || [];

  // Un arbre vide a DEUX causes opposees : rien a decouvrir, ou rien n'a encore
  // ete decouvert. Les confondre laisse chercher au mauvais endroit.
  // Les deux causes restent distinguees -- c'est le renseignement utile --
  // mais sans le mode d'emploi : le bouton qui relance l'analyse est juste
  // au-dessus, et l'onglet Equipements est dans la barre.
  if (!topoCompte(data).noeuds) {
    hote.innerHTML = '<div class="notice' + (data.discovered_at ? '' : ' err') + '">' +
      (data.discovered_at
        ? '<strong>No device discovered.</strong> Last run: ' +
          esc(clock(data.discovered_at)) + '.'
        : '<strong>Discovery has never run.</strong>') +
      '</div>';
    return;
  }
  if (!avertissements.length) { hote.innerHTML = ''; return; }
  hote.innerHTML = '<div class="notice"><strong>' + esc(avertissements.length) +
    ' remark(s) from the last run' +
    (data.discovered_at ? ' (' + esc(clock(data.discovered_at)) + ')' : '') + '.</strong>' +
    avertissements.map((a) => '<span class="hint">' + esc(a) + '</span>').join('') +
    '</div>';
}

/* --------------------------------------------------------------- abonnes */

/** Libelle de la limite appliquee, et d'ou elle vient.
 *  Afficher le plan RADIUS quand une surcharge existe serait mensonger : ce
 *  n'est pas ce que le routeur applique. */
/** Le site d'un abonne, en disant s'il vient d'un VLAN.
 *
 *  Un VLAN qui porte des clients EST un site : chez un operateur radio il
 *  porte un village ou un relais, le routeur n'en est que la tete. Le dire
 *  evite la question suivante -- "et ce site-la, il est sur quel routeur ?". */
function popCell(r, siteParNom) {
  const nom = r.pop_name || '-';
  const site = siteParNom && siteParNom[r.pop_name];
  if (!site || site.kind !== 'vlan') return esc(nom);
  const titre = 'VLAN ' + (site.vlan_id !== null && site.vlan_id !== undefined
    ? site.vlan_id + ' ' : '') + '(' + (site.vlan_interface || '?') + ')' +
    (site.router_name ? ' on ' + site.router_name : '');
  return '<span title="' + esc(titre) + '">' + esc(nom) +
    '<span class="badge" style="margin-left:.35rem">VLAN' +
    (site.vlan_id !== null && site.vlan_id !== undefined ? ' ' + site.vlan_id : '') +
    '</span></span>';
}

/** Le plafond est-il TENU par le routeur ? Pastille, couleur, explication.
 *
 *  C'EST LA CORRECTION DE FOND DE CETTE PAGE. Elle affichait "100 kbps impose"
 *  sur un abonne mesure a 497 kbps : elle montrait une INTENTION enregistree en
 *  base en la presentant comme un FAIT applique sur le reseau. Desormais la
 *  colonne dit laquelle des deux on regarde. */
function limitProof(etat) {
  if (!etat) return '';
  if (etat.verdict === 'sans-plafond') return '';
  if (etat.enforced) {
    // Discret : le cas normal ne doit pas crier. La pastille sert surtout a
    // montrer que la verification a bien eu lieu.
    return '<span class="badge ok" style="margin-left:.35rem" title="' +
      esc(etat.detail || 'cap in force') + '">held</span>';
  }
  const libelles = {
    'contourne': 'fasttrack',
    'file-absente': 'no queue',
    'file-masquee': 'queue shadowed',
    'file-desactivee': 'queue disabled',
    'debit-different': 'different rate',
  };
  // Impossible a confondre avec "impose" : c'est exactement la confusion qu'on
  // repare. Un plafond enregistre que le reseau ne tient pas doit se lire comme
  // un defaut, pas comme un reglage.
  return '<span class="badge crit" style="margin-left:.35rem" title="' +
    esc(etat.detail || '') + '">NOT HELD &middot; ' +
    esc(libelles[etat.verdict] || etat.verdict) + '</span>';
}

/** Libelle de la limite appliquee, et d'ou elle vient.
 *  Afficher le plan RADIUS quand une surcharge existe serait mensonger : ce
 *  n'est pas ce que le routeur applique. */
function limitCell(r, etat) {
  const down = r.effective_down_mbps;
  const up = r.effective_up_mbps;
  if (!down && !up) return '<span style="color:var(--faint)">-</span>';

  const texte = esc(mbps(down || 0) + ' / ' + mbps(up || 0));
  const sceau = limitProof(etat);
  if (r.limit_source === 'plan') return texte + sceau;

  const marque = r.limit_source === 'boost'
    ? '<span class="boost-pill" style="margin-left:.4rem">boost</span>'
    : '<span class="badge warn" style="margin-left:.4rem">forced</span>';
  const plan = r.plan_down_mbps
    ? 'Plan: ' + mbps(r.plan_down_mbps) + ' / ' + mbps(r.plan_up_mbps || 0)
    : 'No RADIUS plan';
  const note = r.policy_note || r.boost_reason;
  const couleur = r.limit_source === 'boost' ? '#a78bfa' : 'var(--warn)';

  return '<span title="' + esc(plan + (note ? ' — ' + note : '')) + '">' +
    '<span style="color:' + couleur + '">' + texte + '</span>' + marque + sceau + '</span>';
}

/** Ce qui empeche les plafonds de tenir, en haut de la liste des abonnes.
 *
 *  Le fasttrack passe avant tout le reste : tant qu'il est actif, AUCUNE file
 *  simple du routeur ne bride quoi que ce soit. Poser des plafonds un par un
 *  sans le savoir, c'est passer la journee a corriger ce qui n'est pas casse. */
function renderLimitsAlert(plafonds) {
  const hote = document.getElementById('limits-alert');
  if (!hote) return;
  if (!plafonds) { hote.innerHTML = ''; return; }

  const morceaux = [];
  (plafonds.routers || []).forEach((rt) => {
    const ft = rt.fasttrack || {};
    if (ft.active === true) {
      morceaux.push('<div class="notice err"><strong>' + esc(rt.router) +
        ': fasttrack bypasses the queues.</strong>' +
        '<span class="hint">' + esc(ft.detail || '') + '</span>' +
        (ft.remedy ? '<span class="hint">Run on the router: <code>' +
          esc(ft.remedy) + '</code></span>' : '') + '</div>');
    } else if (ft.active === null) {
      morceaux.push('<div class="notice warn"><strong>' + esc(rt.router) +
        ': fasttrack not verified.</strong><span class="hint">' +
        esc(ft.detail || '') + '</span></div>');
    }
    if (rt.error) {
      morceaux.push('<div class="notice warn"><strong>' + esc(rt.router) +
        ': caps not verified.</strong><span class="hint">' +
        esc(rt.error) + '</span></div>');
    }
  });

  // Le detail des files qui fuient, hors fasttrack (deja dit plus haut).
  const fuites = [];
  (plafonds.routers || []).forEach((rt) => {
    (rt.queues || []).forEach((q) => {
      // 'sans-plafond' n'est pas une fuite : la file existe et ne borne rien,
      // ce qui est voulu tant que la capacite du lien est inconnue.
      if (q.verdict === 'sans-plafond' || q.verdict === 'contourne') return;
      if (!q.enforced) fuites.push({ routeur: rt.router, q: q });
    });
  });
  if (fuites.length) {
    morceaux.push('<div class="notice warn"><strong>' + fuites.length +
      ' decided cap(s) are not applied by the network.</strong>' +
      fuites.slice(0, 8).map((f) => '<span class="hint"><code>' +
        esc(f.q.login || f.q.name) + '</code> on ' + esc(f.routeur) + ': ' +
        esc(f.q.detail || f.q.verdict) + '</span>').join('') +
      (fuites.length > 8 ? '<span class="hint">... and ' + (fuites.length - 8) +
        ' other(s).</span>' : '') + '</div>');
  }
  hote.innerHTML = morceaux.join('');
}

async function loadSubscribers() {
  let query = state.subSearch ? '&search=' + encodeURIComponent(state.subSearch) : '';
  if (state.subPop) query += '&pop_id=' + encodeURIComponent(state.subPop);
  if (state.subKind) query += '&kind=' + encodeURIComponent(state.subKind);
  const bloatQuery = state.subPop ? '&pop_id=' + encodeURIComponent(state.subPop) : '';
  // include_unmeasured : la liste doit montrer les abonnes QU'IL Y A sur le
  // PoP, pas seulement ceux qui ont deja produit une mesure. Un abonne declare
  // et jamais vu est facture comme les autres ; l'omettre le rendait
  // indiscernable d'un abonne qui n'existe pas.
  const [rows, pops, boosts, bloat] = await Promise.all([
    api('/subscribers/latest?limit=200&include_unmeasured=true' + query),
    api('/pops'),
    api('/shaping/boosts').catch(() => []),
    api('/bufferbloat?minutes=60' + bloatQuery).catch(() => null),
  ]);

  // L'ETAT REEL DES PLAFONDS NE BLOQUE PAS LA LISTE.
  //
  // Il se lit SUR LES ROUTEURS, un par un : sur un parc de plusieurs PoPs, ou
  // avec un routeur injoignable, la reponse peut prendre des secondes. Tant
  // qu'il etait attendu avec le reste, la liste des abonnes ne s'affichait
  // pas du tout -- on remplacait un affichage trompeur par une page vide, ce
  // qui est pire. Il arrive donc APRES, et vient decorer une liste deja
  // lisible (cf. annoterLesPlafonds, plus bas).
  const plafondParLogin = state.plafonds || {};

  // Note de bufferbloat par abonne : latence a vide vs sous charge, calculee en
  // correlant RTT et debit deja collectes.
  const bloatParId = {};
  if (bloat) (bloat.subscribers || []).forEach((b) => { bloatParId[b.subscriber_id] = b; });

  // Ce que chaque site EST : site de routeur ou site de VLAN. Sert a la
  // colonne PoP, qui doit dire d'ou vient le site sans le faire chercher.
  const siteParNom = {};
  pops.forEach((p) => { siteParNom[p.name] = p; });

  // Le filtre PoP repond a "voir les connexions depuis un PoP".
  const select = document.getElementById('sub-pop');
  if (select.dataset.filled !== String(pops.length)) {
    // Un site issu d'un VLAN se signale : l'exploitant doit savoir qu'il
    // regarde un VLAN d'un routeur et non un site a lui.
    select.innerHTML = '<option value="">All PoPs</option>' +
      pops.map((p) => '<option value="' + p.id + '">' +
        (p.kind === 'vlan' ? 'VLAN · ' : '') + esc(p.name) +
        ' (' + p.subscriber_count + ')</option>').join('');
    select.dataset.filled = String(pops.length);
    select.value = state.subPop || '';
  }

  const parLogin = {};
  (boosts || []).forEach((b) => { if (b.scope === 'subscriber') parLogin[b.target_key] = b; });

  const statiques = rows.filter((r) => r.kind === 'static').length;
  const sansMesure = rows.filter((r) => !r.ts).length;
  // L'effectif declare du (ou des) PoP concerne : il dit si la liste est
  // complete ou tronquee par la limite, ce qu'un simple total ne dit pas.
  const effectif = pops
    .filter((p) => !state.subPop || String(p.id) === String(state.subPop))
    .reduce((a, p) => a + (p.subscriber_count || 0), 0);
  let compte = rows.length + ' subscriber(s)' +
    (effectif > rows.length ? ' of ' + effectif + ' declared' : '') +
    (statiques ? ' incl. ' + statiques + ' static-IP' : '') +
    (sansMesure ? ' · ' + sansMesure + ' unmeasured' : '') +
    (state.subPop ? ' on this PoP' : '');
  if (bloat && bloat.summary && bloat.summary.measured) {
    const dist = bloat.summary.distribution || {};
    const mauvais = (dist.D || 0) + (dist.F || 0);
    compte += ' · bufferbloat: ' + bloat.summary.measured + ' measured' +
      (mauvais ? ', ' + mauvais + ' degraded' : ', all good') +
      (bloat.summary.worst_bloat_ms ? ' (worst +' + bloat.summary.worst_bloat_ms + ' ms)' : '');
  } else if (bloat && bloat.rtt_enabled === false) {
    // Sonde coupee : sans RTT la note ne PEUT pas exister. Le dire, plutot que
    // de laisser une colonne vide passer pour un reseau sain.
    compte += ' · bufferbloat unavailable: latency probe off';
  }
  document.getElementById('sub-count').textContent = compte;
  state.subCompte = compte;

  const host = document.getElementById('subscribers-table');
  if (!rows.length) {
    host.innerHTML = '<div class="empty">' +
      (state.subSearch || state.subPop || state.subKind
        ? 'No subscriber matches the filter.'
        : 'No subscriber. This list carries every subscriber of the PoPs, measured or ' +
          'not: empty, it means no PPPoE session has been seen yet and ' +
          'no static-IP client is declared.') + '</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Subscriber</th><th>Kind</th><th>PoP</th>' +
    '<th class="num" title="Rate actually applied">Limit</th>' +
    '<th class="num">Download</th><th style="width:140px">vs limit</th>' +
    '<th class="num">Upload</th><th class="num">Latency</th>' +
    '<th title="Latency added under load (A+ imperceptible, F unplayable)">Bufferbloat</th>' +
    '<th>Boost</th>' +
    '<th class="num">Session</th><th class="num">Sample</th>' +
    '<th class="sticky-actions"></th>' +
    '</tr></thead><tbody>' +
    rows.map((r) => {
      // La jauge se compare a la limite APPLIQUEE, pas au plan commercial :
      // un abonne bride a 512 kbps qui en consomme 400 est a 78 %, pas a 0,08 %.
      const limiteDown = (r.effective_down_mbps || 0) * 1e6;
      // AUCUNE MESURE N'EST PAS UN DEBIT NUL. Afficher 0 bps ferait passer un
      // abonne jamais vu pour un abonne silencieux -- deux situations qui
      // n'appellent pas du tout le meme geste.
      const mesure = !!r.ts;
      const trou = '<span style="color:var(--faint)">-</span>';
      return '<tr class="clickable" data-sub="' + r.subscriber_id + '">' +
        '<td class="login">' + esc(r.login) +
          (mesure ? '' : '<span class="hint" style="display:block" title="No sample: ' +
            'this subscriber was never measured, or its PoP is no longer collected">never measured</span>') +
          '</td>' +
        '<td>' + kindBadge(r.kind) + '</td>' +
        '<td>' + popCell(r, siteParNom) + '</td>' +
        '<td class="num" data-limite="' + esc(r.login) + '">' +
          limitCell(r, plafondParLogin[r.login]) + '</td>' +
        '<td class="num" style="color:var(--down)">' +
          (mesure ? esc(bpsText(r.tx_bps)) : trou) + '</td>' +
        '<td>' + (mesure ? meter(r.tx_bps, limiteDown) : '') + '</td>' +
        '<td class="num" style="color:var(--up)">' +
          (mesure ? esc(bpsText(r.rx_bps)) : trou) + '</td>' +
        '<td class="num">' + (mesure ? rtt(r.rtt_ms) : trou) + '</td>' +
        '<td>' + bloatBadge(bloatParId[r.subscriber_id]) + '</td>' +
        '<td>' + (parLogin[r.login]
          ? '<span class="boost-pill" title="' +
            esc(parLogin[r.login].boost_reason || '') + '">' +
            esc(Math.max(0, Math.round(parLogin[r.login].seconds_left / 60))) +
            ' min</span>'
          : '<span style="color:var(--faint)">-</span>') + '</td>' +
        '<td class="num">' + (mesure ? esc(uptime(r.session_uptime_s)) : trou) + '</td>' +
        '<td class="num" style="color:var(--faint)">' +
          (mesure ? esc(clock(r.ts))
            : '<span title="Last known session">' + esc(depuis(r.last_seen)) + '</span>') +
          '</td>' +
        '<td class="sticky-actions"><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm" data-bw="' + esc(r.login) + '">Rate</button>' +
          '<button class="sm" data-boost="' + esc(r.login) + '">Boost</button>' +
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
    const ligne = rows.find((r) => r.login === b.dataset.bw);
    b.addEventListener('click', () => openBandwidthEditor('subscriber', ligne));
  });
  host.querySelectorAll('[data-boost]').forEach((b) => {
    const ligne = rows.find((r) => r.login === b.dataset.boost);
    b.addEventListener('click', () => openBoostEditor(ligne));
  });

  // La verification des plafonds part MAINTENANT, sans etre attendue : la
  // liste est deja a l'ecran, elle se decorera quand les routeurs auront
  // repondu. Une page vide n'est pas une reponse plus honnete qu'une page
  // incomplete -- c'est l'absence de reponse.
  annoterLesPlafonds(rows);
}

/** Va lire sur les routeurs si les plafonds tiennent, puis decore la liste.
 *
 *  Detache a dessein : cette lecture touche chaque routeur (calcul du plan,
 *  files, pare-feu). Elle peut prendre plusieurs secondes sur un parc etendu,
 *  et un PoP injoignable ne doit pas faire disparaitre la liste des abonnes. */
async function annoterLesPlafonds(rows) {
  let data;
  try {
    data = await api('/shaping/limits');
  } catch (err) {
    // Silencieux a l'ecran : l'absence de verification n'est pas une panne de
    // la page. Les pastilles restent simplement absentes, et Reglages > Shaping
    // porte le message complet.
    console.warn('Caps not verified:', err);
    return;
  }

  const parLogin = {};
  (data.routers || []).forEach((rt) => {
    (rt.queues || []).forEach((q) => { if (q.login) parLogin[q.login] = q; });
  });
  state.plafonds = parLogin;

  // La liste a pu changer entre-temps (filtre, rafraichissement) : on ne
  // touche qu'aux lignes encore affichees, en les retrouvant par leur login.
  const host = document.getElementById('subscribers-table');
  if (host) {
    host.querySelectorAll('td[data-limite]').forEach((cell) => {
      const ligne = rows.find((r) => r.login === cell.dataset.limite);
      if (ligne) cell.innerHTML = limitCell(ligne, parLogin[ligne.login]);
    });
  }

  const compteur = document.getElementById('sub-count');
  if (compteur && state.subCompte) {
    compteur.textContent = state.subCompte +
      (data.leaking ? ' \u00b7 ' + data.leaking + ' cap(s) NOT HELD' : '');
  }
  renderLimitsAlert(data);
}

/* ---------------------------------------------------------------- nature
   Un coup d'oeil doit suffire a savoir de quel type est un abonne : le
   diagnostic n'est pas le meme. Un PPPoE absent s'est deconnecte ; un client
   a IP fixe sans mesure n'a simplement pas encore de file posee. */
function kindBadge(kind) {
  if (kind === 'static') {
    return '<span class="badge" title="Static-IP client, declared by hand. ' +
      'No session: its address comes from its record.">static IP</span>';
  }
  return '<span class="badge ok" title="Session PPPoE decouverte sur le routeur">PPPoE</span>';
}

/* =====================================================================
   Inventaire des clients a IP fixe
   ===================================================================== */

/** Fiche en cours d'edition. null = le formulaire cree une nouvelle fiche. */
let scEdition = null;

function scNotice(html) {
  document.getElementById('sc-notice').innerHTML = html;
}

/** Remplit les menus "Attached PoP" avec TOUS les PoP disponibles.
 *
 *  Deux sources, reunies : les routeurs declares (un routeur sans PoP
 *  explicite a son propre nom pour PoP -- c'est ce que fait le collecteur) et
 *  les PoP deja connus en base (sites VLAN, PoP decouverts). Un menu plutot
 *  qu'une saisie libre : une faute de frappe rattachait le client ou
 *  l'antenne a un PoP qui n'existe pas. */
let POPS_CONNUS = [];

async function remplirMenusPop() {
  const [inventaire, pops] = await Promise.all([
    api('/pops/routers').catch(() => null),
    api('/pops').catch(() => null),
  ]);
  if (inventaire || pops) {
    const noms = new Set();
    ((inventaire && inventaire.routers) || []).forEach((r) => {
      if (r.enabled === false) return;
      const nom = r.pop_name || r.name;
      if (nom) noms.add(nom);
    });
    (Array.isArray(pops) ? pops : []).forEach((p) => { if (p.name) noms.add(p.name); });
    const liste = [...noms].sort((a, b) => a.localeCompare(b));
    const inchangee = liste.join('\n') === POPS_CONNUS.join('\n');
    POPS_CONNUS = liste;
    // Reconstruire un menu deja ouvert le refermerait : on ne touche a rien
    // quand la liste n'a pas bouge.
    if (inchangee && remplirMenusPop.fait) return;
  }
  remplirMenusPop.fait = true;
  document.querySelectorAll('select[data-pop-select]').forEach((select) => {
    choisirPop(select, select.value);
  });
}

/** Selectionne un PoP, en l'ajoutant au menu s'il n'y figure pas : une fiche
 *  existante rattachee a un PoP disparu doit rester lisible et modifiable. */
function choisirPop(select, valeur) {
  if (typeof select === 'string') select = document.getElementById(select);
  const noms = POPS_CONNUS.slice();
  if (valeur && !noms.includes(valeur)) noms.push(valeur);
  select.innerHTML = '<option value="">' +
    (noms.length ? 'Choose a PoP (' + noms.length + ' available)' : 'No PoP available yet') +
    '</option>' +
    noms.map((n) => '<option value="' + esc(n) + '">' + esc(n) + '</option>').join('');
  select.value = valeur || (noms.length === 1 ? noms[0] : '');
}

function scRemplirFormulaire(fiche) {
  scEdition = fiche;
  const v = (id, valeur) => { document.getElementById(id).value = valeur === null || valeur === undefined ? '' : valeur; };
  v('sc-reference', fiche ? fiche.reference : '');
  v('sc-label', fiche ? fiche.label : '');
  choisirPop('sc-pop', fiche ? fiche.pop_name : '');
  v('sc-address', fiche ? fiche.address : '');
  v('sc-vlan', fiche ? fiche.vlan : '');
  v('sc-sector', fiche ? fiche.sector_key : '');
  v('sc-down', fiche ? fiche.plan_down_mbps : '');
  v('sc-up', fiche ? fiche.plan_up_mbps : '');
  v('sc-cpe', fiche ? fiche.cpe_mac : '');
  v('sc-note', fiche ? fiche.note : '');
  document.getElementById('sc-enabled').checked = fiche ? !!fiche.enabled : true;
  document.getElementById('sc-submit').textContent = fiche ? 'Save' : 'Declare';
  document.getElementById('sc-cancel').hidden = !fiche;
  // Le meme formulaire sert a ajouter et a modifier. Sans titre qui change, on
  // croyait ajouter un client alors qu'on en ecrasait un autre -- et la
  // reference saisie remplacait silencieusement celle qu'on venait d'ouvrir.
  const titre = document.getElementById('sc-form-title');
  if (titre) titre.textContent = fiche ? 'Edit ' + (fiche.reference || 'the client')
    : 'Add a client';
  scNotice('');
}

/** Ouvre le formulaire pre-rempli a partir d'un candidat detecte.
 *
 *  On reprend ce que l'observation SAIT (adresse, VLAN, PoP) et rien d'autre.
 *  La reference et le plan restent vides a dessein : l'IP ne doit pas servir
 *  d'identite (elle changera), et le debit souscrit ne se devine pas -- c'est
 *  precisement ce qu'aucune detection ne pourra jamais fournir. */
function scDepuisCandidat(candidat) {
  scRemplirFormulaire(null);
  const v = (id, valeur) => {
    document.getElementById(id).value = valeur === null || valeur === undefined ? '' : valeur;
  };
  v('sc-address', candidat.address);
  v('sc-vlan', candidat.vlan_id);
  choisirPop('sc-pop', candidat.pop_name);
  scNotice('<span class="badge">Address, VLAN and PoP taken from detection &middot; ' +
    'reference and rate to enter</span>');
  const reference = document.getElementById('sc-reference');
  reference.focus();
  reference.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

async function loadCandidates() {
  const bloc = document.getElementById('sc-candidates-block');
  if (bloc && bloc.tagName === 'DETAILS' && !bloc.open) return;
  const host = document.getElementById('sc-candidates');
  let data;
  try {
    data = await api('/static-clients/candidates');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  if (!data.enabled) {
    host.innerHTML = '<div class="empty">Detection disabled ' +
      '(setting <code>vlan_detect_enabled</code>).</div>';
    return;
  }
  const rows = data.candidates || [];
  if (!rows.length) {
    host.innerHTML = '<div class="empty">No undeclared address on the routed VLANs.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Address</th><th>MAC</th><th class="num">VLAN</th>' +
    '<th>Interface</th><th>PoP</th><th>Router</th>' +
    '<th>Seen</th><th>Since</th><th class="sticky-actions"></th>' +
    '</tr></thead><tbody>' +
    rows.map((c, i) =>
      '<tr>' +
      '<td class="login"><code>' + esc(c.address) + '</code></td>' +
      '<td style="color:var(--faint)">' + esc(c.mac || '-') + '</td>' +
      '<td class="num">' + esc(c.vlan_id === null || c.vlan_id === undefined ? '-' : c.vlan_id) + '</td>' +
      '<td>' + esc(c.vlan_interface || '-') + '</td>' +
      '<td>' + esc(c.pop_name || '-') + '</td>' +
      '<td>' + esc(c.router_name || '-') + '</td>' +
      '<td>' + esc(depuis(c.last_seen)) + '</td>' +
      '<td style="color:var(--faint)">' + esc(depuis(c.first_seen)) + '</td>' +
      '<td class="sticky-actions"><div class="actions" style="justify-content:flex-end">' +
        '<button class="sm primary" data-sc-declare="' + i + '">Declare</button>' +
      '</div></td>' +
      '</tr>').join('') + '</tbody></table>';

  host.querySelectorAll('[data-sc-declare]').forEach((b) => {
    b.addEventListener('click', () => scDepuisCandidat(rows[Number(b.dataset.scDeclare)]));
  });
}

/** Recensement d'un PoP : TOUS les clients, quelle que soit leur trace.
 *
 *  Les candidats ci-dessus sortent de la detection periodique. Ce panneau lit
 *  les routeurs EN DIRECT et croise sept sources : ARP, baux DHCP, sessions
 *  PPPoE, table de ponts, routes statiques, files deja posees, voisinage. Il
 *  repond a la question que la liste des candidats ne repond pas : "combien de
 *  clients ce PoP porte-t-il, et lesquels ne sont pas dans mon inventaire ?".
 *
 *  Les remarques sont affichees AVANT la table, a dessein : une liste courte se
 *  lirait comme un PoP vide alors qu'elle signale souvent une source illisible
 *  ou un adressage porte par un autre routeur. */
async function scRecensement() {
  const hote = document.getElementById('sc-recensement-out');
  hote.innerHTML = '<div class="empty">Reading the routers (about fifteen tables per PoP)...</div>';
  let data;
  try {
    data = await api('/pops/census');
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }

  const pops = data.pops || [];
  if (!pops.length) {
    hote.innerHTML = '<div class="empty">No router collected.</div>';
    return;
  }
  const declarations = [];
  hote.innerHTML = pops.map((pop) => {
    const c = pop.counts || {};
    const erreurs = (pop.errors || []).map((e) =>
      '<div class="notice err">' + esc(e) + '</div>').join('');
    const remarques = (pop.remarks || []).map((r) =>
      '<div class="notice"><span class="hint">' + esc(r) + '</span></div>').join('');
    const lignes = (pop.clients || []).map((cl) => {
      const declare = cl.declared
        ? '<span class="badge">' + esc(cl.declared.reference) + '</span>'
        : (cl.login
          ? '<span class="badge">PPPoE ' + esc(cl.login) + '</span>'
          : '<button class="sm primary" data-sc-census="' + (declarations.push({
              address: cl.address, vlan_id: cl.vlan_id, pop_name: pop.pop_name,
            }) - 1) + '">Declare</button>');
      const vlan = cl.vlan_id === null || cl.vlan_id === undefined
        ? '-'
        : '<span title="' + esc(cl.vlan_source || '') + '">' + esc(cl.vlan_id) + '</span>';
      const nom = cl.hostname || cl.identity || cl.comment || '';
      return '<tr>' +
        '<td class="login"><code>' + esc(cl.address) + '</code>' +
          ((cl.routed_prefixes || []).length
            ? ' <span class="badge" title="bloc route derriere cette adresse">+ ' +
              esc(cl.routed_prefixes.join(', ')) + '</span>' : '') +
        '</td>' +
        '<td style="color:var(--faint)">' + esc(cl.mac || '-') + '</td>' +
        '<td class="num">' + vlan + '</td>' +
        '<td>' + esc(cl.interface || '-') +
          ((cl.ports || []).length ? ' <span class="hint">' + esc(cl.ports.join(', ')) + '</span>' : '') +
        '</td>' +
        '<td style="color:var(--faint)">' + esc((cl.sources || []).join(' + ')) + '</td>' +
        '<td>' + esc(nom || '-') + '</td>' +
        '<td>' + esc(cl.router || '-') + '</td>' +
        '<td class="sticky-actions"><div class="actions" style="justify-content:flex-end">' +
          declare + '</div></td>' +
        '</tr>';
    }).join('');

    return '<div class="notice" style="margin-top:.6rem">' +
      '<strong>' + esc(pop.pop_name) + '</strong> &mdash; ' +
      esc(c.clients || 0) + ' client(s) located, incl. ' +
      esc(c.pppoe || 0) + ' over PPPoE. ' +
      '<b>' + esc(c.non_declares || 0) + ' undeclared in the inventory.</b>' +
      '</div>' + erreurs + remarques +
      (lignes
        ? '<div class="table-wrap"><table><thead><tr>' +
          '<th>Address</th><th>MAC</th><th class="num">VLAN</th><th>Interface / port</th>' +
          '<th>Seen by</th><th>Known name</th><th>Router</th><th class="sticky-actions"></th>' +
          '</tr></thead><tbody>' + lignes + '</tbody></table></div>'
        : '<div class="empty">No client located on this PoP.</div>');
  }).join('');

  hote.querySelectorAll('[data-sc-census]').forEach((b) => {
    b.addEventListener('click', () => scDepuisCandidat(declarations[Number(b.dataset.scCensus)]));
  });
}

/** Diagnostic : POURQUOI une entree ARP a ete ecartee, ligne par ligne.
 *
 *  Une adresse est retenue par deux chemins : son interface est une VLAN
 *  declaree sans serveur PPPoE, OU son adresse tombe dans un sous-reseau que le
 *  PoP dessert (/ip/address). Le second chemin est ce qui rend visible un
 *  client derriere un pont en filtrage VLAN, que le nom de l'interface seul
 *  ferait disparaitre. Les sous-reseaux retenus sont affiches : s'ils manquent,
 *  c'est la que se trouve la reponse. */
async function scDiagnostic() {
  const hote = document.getElementById('sc-diag-out');
  hote.innerHTML = '<div class="empty">Reading /ip/arp on the routers...</div>';
  let data;
  try {
    data = await api('/static-clients/candidates/diagnostic');
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  if (!data.routers || !data.routers.length) {
    hote.innerHTML = '<div class="empty">No router collected.</div>';
    return;
  }
  hote.innerHTML = data.routers.map((r) => {
    if (r.error) {
      return '<div class="notice err"><strong>' + esc(r.router) + '</strong> : ' +
        esc(r.error) + '</div>';
    }
    const horsVlan = Object.entries(r.interfaces_hors_vlan || {});
    const motifs = Object.entries(r.by_reason || {})
      .filter(([m]) => m !== 'retenu')
      .map(([m, n]) => '<span class="hint">' + esc(n) + ' &times; ' + esc(m) + '</span>')
      .join('');
    return '<div class="notice">' +
      '<strong>' + esc(r.router) + '</strong> &mdash; ' + esc(r.kept) + ' kept out of ' +
      esc(r.arp_rows) + ' ARP entry(ies).' +
      '<span class="hint">Declared VLANs: ' +
        esc((r.vlans_declares || []).join(', ') || 'none') +
        ((r.interfaces_pppoe || []).length
          ? ' &middot; excluded (PPPoE): ' + esc(r.interfaces_pppoe.join(', ')) : '') +
      '</span>' + motifs +
      (horsVlan.length
        ? '<div class="notice err" style="margin-top:.5rem">' +
          '<strong>Dropped: neither a declared VLAN nor a served subnet.</strong> ' +
          horsVlan.map(([nom, n]) => '<code>' + esc(nom) + '</code> (' + esc(n) + ')').join(', ') +
          '<span class="hint">Served subnets: ' +
          esc((r.reseaux_clients || []).join(', ') || 'none read from /ip/address') +
          '</span>' +
          '</div>'
        : '') +
      '</div>';
  }).join('');
}

/** Lit le formulaire. Les champs vides deviennent null plutot que "" : une
 *  chaine vide se lirait comme une valeur posee, un null comme une absence. */
function scPayload() {
  const txt = (id) => {
    const brut = (document.getElementById(id).value || '').trim();
    return brut === '' ? null : brut;
  };
  const nb = (id) => {
    const brut = txt(id);
    return brut === null ? null : Number(brut);
  };
  return {
    reference: txt('sc-reference'),
    label: txt('sc-label'),
    pop_name: txt('sc-pop'),
    address: txt('sc-address'),
    vlan: nb('sc-vlan'),
    sector_key: txt('sc-sector'),
    plan_down_mbps: nb('sc-down'),
    plan_up_mbps: nb('sc-up'),
    cpe_mac: txt('sc-cpe'),
    note: txt('sc-note'),
    enabled: document.getElementById('sc-enabled').checked,
  };
}

/** Ce qui est DECLARE sur chaque VLAN.
 *
 *  Le tableau rend la SAISIE, pas une decouverte. C'est la nuance qui compte :
 *  un client sur VLAN routee n'ouvre pas de session, RADIUS ne le decrit pas,
 *  et rien sur le reseau ne dit quel debit lui a ete vendu. Ce que les flux
 *  montrent, ce sont des adresses qui parlent -- une imprimante et un client
 *  professionnel y ont exactement la meme apparence. */
async function loadVlanClients() {
  const host = document.getElementById('sc-vlans');
  if (!host) return;
  let corps;
  try {
    corps = await api('/static-clients/vlans');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const lignes = corps.vlans || [];
  const orphelins = (corps.unmatched || []).filter((h) => h.vlan_id).length;
  if (!lignes.length) {
    // Le compte d'adresses orphelines reste : c'est un RENSEIGNEMENT (des
    // machines parlent sur des VLAN sans etre declarees), pas un mode d'emploi.
    // Le renvoi au formulaire, lui, disait « ci-dessous » alors qu'il est
    // desormais au-dessus -- et n'apprenait rien.
    host.innerHTML = '<div class="empty">No client declared on a VLAN.' +
      (orphelins
        ? ' ' + esc(orphelins) + ' address(es) do talk on VLANs without ' +
          'being declared.'
        : '') +
      '</div>';
    return;
  }
  const parVlan = new Map();
  (corps.unmatched || []).forEach((h) => {
    if (!h.vlan_id) return;
    parVlan.set(h.vlan_id, (parVlan.get(h.vlan_id) || 0) + 1);
  });
  host.innerHTML = '<table><thead><tr><th>VLAN</th><th>PoP</th>' +
    '<th class="num">Clients</th><th class="num">Active</th>' +
    '<th class="num">Sold (down)</th><th class="num">Sold (up)</th>' +
    '<th>Origin</th><th class="num">Undeclared</th></tr></thead><tbody>' +
    lignes.map((v) => {
      const vus = parVlan.get(v.vlan) || 0;
      return '<tr><td><b>' + esc(v.vlan) + '</b></td>' +
        '<td>' + esc((v.pops || []).filter(Boolean).join(', ') || '-') + '</td>' +
        '<td class="num">' + esc(v.clients) + '</td>' +
        '<td class="num">' + esc(v.actifs) + '</td>' +
        '<td class="num">' + esc(Math.round(v.vendu_down_mbps || 0)) + ' Mbps</td>' +
        '<td class="num">' + esc(Math.round(v.vendu_up_mbps || 0)) + ' Mbps</td>' +
        '<td>' + (v.depuis_api
          ? '<span class="badge">' + esc(v.depuis_api) + ' via API</span> '
          : '') + '<span class="badge ok">' + esc(v.clients - v.depuis_api) +
          ' by hand</span></td>' +
        '<td class="num">' + (vus
          ? '<span class="badge warn">' + esc(vus) + '</span>'
          : '<span class="faint">0</span>') + '</td></tr>';
    }).join('') + '</tbody></table>';
}

async function loadStaticClients() {
  const host = document.getElementById('sc-table');
  let fiches;
  try {
    fiches = await api('/static-clients');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }

  // Listes de suggestion : saisir une cle de secteur de memoire est le meilleur
  // moyen de rattacher un client a un noeud qui n'existe pas.
  // La liste vient des ROUTEURS COLLECTES, pas de la table des PoP : celle-ci
  // contient aussi les PoP nes d'une faute de frappe, et les proposer
  // reproduirait l'erreur qu'on cherche a empecher.
  await remplirMenusPop();
  try {
    const graphe = await api('/topology');
    document.getElementById('sc-sector-list').innerHTML = ((graphe && graphe.nodes) || [])
      .filter((n) => n.kind === 'sector' || n.kind === 'radio' || n.kind === 'pop')
      .map((n) => '<option value="' + esc(n.key) + '">' + esc(n.name || '') + '</option>')
      .join('');
  } catch (err) { /* le champ reste libre */ }

  if (!fiches.length) {
    host.innerHTML = '<div class="empty">No static-IP client declared.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Reference</th><th>Name</th><th>PoP</th>' +
    '<th>Address</th><th class="num">VLAN</th><th>Sector</th>' +
    '<th class="num">Plan</th>' +
    '<th title="Last time this address talked, seen in /ip/arp. ' +
    'A silent client, or one reachable by another path, stays empty: ' +
    'not knowing is not the same as being absent.">Seen active</th>' +
    '<th>State</th>' +
    '<th title="The queue actually written on the router, and what is missing when ' +
    'it is not. Read from the routers, after the table is shown.">Queue</th>' +
    '<th class="sticky-actions"></th>' +
    '</tr></thead><tbody>' +
    fiches.map((f) =>
      '<tr>' +
      '<td class="login">' + esc(f.reference) + '</td>' +
      '<td>' + esc(f.label || '-') + '</td>' +
      '<td>' + esc(f.pop_name) + '</td>' +
      '<td><code>' + esc(f.address) + '</code></td>' +
      '<td class="num">' + esc(f.vlan === null || f.vlan === undefined ? '-' : f.vlan) + '</td>' +
      '<td>' + esc(f.sector_key || '-') + '</td>' +
      '<td class="num">' + esc(
        f.plan_down_mbps || f.plan_up_mbps
          ? mbps(f.plan_down_mbps || 0) + ' / ' + mbps(f.plan_up_mbps || 0)
          : '-') + '</td>' +
      '<td' + (f.last_seen_at
        ? ' title="' + esc((f.seen_mac || '') + ' on ' + (f.seen_vlan_interface || '')) + '"'
        : '') + '>' + esc(depuis(f.last_seen_at)) + '</td>' +
      '<td>' + (f.enabled
        ? '<span class="badge ok">active</span>'
        : '<span class="badge warn" title="Fiche conservee, file retiree au plan suivant">suspendu</span>') + '</td>' +
      '<td class="sc-file" data-sc-file="' + esc(f.reference) + '">' +
        '<span class="hint">reading...</span></td>' +
      '<td class="sticky-actions"><div class="actions" style="justify-content:flex-end">' +
        '<button class="sm" data-sc-edit="' + esc(f.id) + '">Edit</button>' +
        '<button class="sm" data-sc-del="' + esc(f.id) + '">Remove</button>' +
      '</div></td>' +
      '</tr>').join('') + '</tbody></table>';

  host.querySelectorAll('[data-sc-edit]').forEach((b) => {
    b.addEventListener('click', () => {
      const fiche = fiches.find((f) => String(f.id) === b.dataset.scEdit);
      scRemplirFormulaire(fiche);
      // Le formulaire est au-dessus de ce tableau : sans ce recentrage, cliquer
      // « Modifier » ne montrerait rien du tout depuis le bas de la liste.
      const reference = document.getElementById('sc-reference');
      reference.focus();
      reference.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  });
  host.querySelectorAll('[data-sc-del]').forEach((b) => {
    b.addEventListener('click', () => scSupprimer(b.dataset.scDel, fiches));
  });
  scEtatDesFiles(host);
}

/** Remplit la colonne "File" APRES l'affichage du tableau.
 *
 *  Cette lecture interroge chaque routeur concerne (un plan par routeur) : la
 *  faire avant l'affichage retarderait tout l'inventaire pour une colonne. Une
 *  cellule qui reste en "lecture..." est donc un routeur lent, pas une erreur. */
async function scEtatDesFiles(host) {
  let data;
  try {
    data = await api('/static-clients/enforcement');
  } catch (err) {
    host.querySelectorAll('[data-sc-file]').forEach((cell) => {
      cell.innerHTML = '<span class="hint" title="' + esc(err.message) + '">illisible</span>';
    });
    return;
  }
  const parReference = {};
  (data.clients || []).forEach((ligne) => { parReference[ligne.reference] = ligne; });
  host.querySelectorAll('[data-sc-file]').forEach((cell) => {
    const ligne = parReference[cell.dataset.scFile];
    if (!ligne) { cell.innerHTML = '<span class="hint">-</span>'; return; }
    const detail = (ligne.reason || '') + (ligne.router ? ' (' + ligne.router + ')' : '');
    cell.innerHTML = '<span title="' + esc(detail) + '">' + scBadgeEtat(ligne.state) + '</span>';
  });
}

/** Etats de file rendus par l'API, et ce qu'ils veulent dire pour l'exploitant.
 *
 *  Ils repondent tous a la meme question, celle qu'on se pose apres avoir
 *  declare un client : est-ce qu'il est bride, et sinon qu'est-ce qui manque ? */
const SC_ETATS = {
  'file-posee': ['ok', 'Queue written'],
  'file-retiree': ['', 'Queue removed'],
  'file-a-poser': ['warn', 'Queue pending'],
  'ecarte': ['warn', 'No queue'],
  'sans-routeur': ['crit', 'PoP with no router'],
  'conflit': ['crit', 'Conflict'],
  'erreur': ['crit', 'Error'],
};

function scBadgeEtat(etat) {
  const [classe, libelle] = SC_ETATS[etat] || ['', etat || '?'];
  return '<span class="badge ' + classe + '">' + esc(libelle) + '</span>';
}

/** Ce que la declaration a REELLEMENT fait sur le routeur.
 *
 *  Une fiche enregistree ne dit rien de la file : entre "elle est posee" et
 *  "elle ne le sera jamais parce que le PoP ne correspond a aucun routeur", il
 *  n'y avait aucune difference visible. C'est ce rapport qui la fait. */
function scEnforcement(rapport) {
  if (!rapport) return '';
  const routeur = rapport.router ? ' on <code>' + esc(rapport.router) + '</code>' : '';
  const rapproche = rapport.pop_resolution === 'normalise'
    ? '<span class="hint">PoP entered <code>' + esc(rapport.pop_declared || '') +
      '</code>, matched to <code>' + esc(rapport.pop_name || '') + '</code>.</span>'
    : '';
  return ' ' + scBadgeEtat(rapport.state) + routeur +
    '<span class="hint">' + esc(rapport.reason || '') + '</span>' + rapproche;
}

async function scEnregistrer(event) {
  event.preventDefault();
  const bouton = document.getElementById('sc-submit');
  bouton.disabled = true;
  try {
    const payload = scPayload();
    let fiche;
    if (scEdition) {
      fiche = await api('/static-clients/' + encodeURIComponent(scEdition.id), {
        method: 'PATCH', body: JSON.stringify(payload),
      });
      scNotice('<span class="badge ok">Record updated</span>' + scEnforcement(fiche.enforcement));
    } else {
      fiche = await api('/static-clients', { method: 'POST', body: JSON.stringify(payload) });
      scNotice('<span class="badge ok">Client declared</span>' + scEnforcement(fiche.enforcement));
    }
    scRemplirFormulaire(null);
    // Declarer un client le retire de la liste des candidats : les deux
    // tableaux doivent etre relus ensemble, sinon il apparait aux deux endroits.
    await Promise.all([loadStaticClients(), loadVlanClients(), loadCandidates()]);
    // La fiche modifiee change le plan : le tableau des abonnes doit suivre.
    await loadSubscribers();
  } catch (err) {
    scNotice('<span class="badge crit">' + esc(err.message) + '</span>');
  } finally {
    bouton.disabled = false;
  }
}

async function scSupprimer(id, fiches) {
  const fiche = (fiches || []).find((f) => String(f.id) === String(id));
  const nom = fiche ? (fiche.label || fiche.reference) : id;
  if (!confirm('Remove "' + nom + '" from the inventory?\n\n' +
      'Its measurement history is kept. Its queue is removed from the router ' +
      'immediately, and only that one.')) return;
  try {
    await api('/static-clients/' + encodeURIComponent(id), { method: 'DELETE' });
    if (scEdition && String(scEdition.id) === String(id)) scRemplirFormulaire(null);
    // Retirer une fiche peut faire REAPPARAITRE son adresse en candidat.
    await Promise.all([loadStaticClients(), loadVlanClients(), loadCandidates()]);
    await loadSubscribers();
  } catch (err) {
    scNotice('<span class="badge crit">' + esc(err.message) + '</span>');
  }
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
      '<div class="drawer-head"><h3>' + esc(s.login) + '</h3>' +
      '<button class="sm" id="drawer-close">Close</button></div>' +
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
              ? '<span class="badge warn">forced</span> plan: ' +
                esc(mbps(s.plan_down_mbps || 0))
              : esc(s.plan_source || 'RADIUS plan')) +
        statCard('', 'Queue target', esc(s.last_ip ? s.last_ip + '/32' : '-'), '',
          s.last_ip
            ? 'session address'
            : '<span style="color:var(--warn)">offline: no queue</span>') +
        statCard('', 'Bufferbloat',
          data.bufferbloat ? esc(data.bufferbloat.grade) : 'n/a', '',
          data.bufferbloat
            ? 'idle ' + esc(data.bufferbloat.idle_ms) + ' ms, under load ' +
              esc(data.bufferbloat.loaded_ms) + ' ms'
            : 'not enough load to measure') +
      '</div>' +
      (data.bufferbloat
        ? '<div class="notice"><b>Latency under load.</b> Latency goes from ' +
          '<b>' + esc(data.bufferbloat.idle_ms) + ' ms</b> idle to <b>' +
          esc(data.bufferbloat.loaded_ms) + ' ms</b> when the link fills up, ' +
          'that is <b>+' + esc(data.bufferbloat.bloat_ms) + ' ms</b> of bufferbloat ' +
          '(grade ' + esc(data.bufferbloat.grade) + ', ' +
          esc(data.bufferbloat.samples) + ' point(s)).</div>'
        : data.points.some((p) => p.rtt_ms_avg !== null && p.rtt_ms_avg !== undefined)
          ? '<div class="notice">Latency over the window: average ' +
            rtt(Math.max(...data.points.map((p) => p.rtt_ms_avg || 0))) +
            ', worst ' + rtt(Math.max(...data.points.map((p) => p.rtt_ms_max || 0))) +
            '</div>'
          : '') +
      '<h2>Last hour</h2><div class="card"><div id="sub-chart"></div></div>';
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    renderThroughput(document.getElementById('sub-chart'),
      data.points.map((p) => ({ bucket: p.bucket, tx_bps: p.tx_bps_max, rx_bps: p.rx_bps_max, subscribers: p.samples })));
  } catch (err) {
    root.querySelector('.drawer').innerHTML =
      '<div class="drawer-head"><h3>Error</h3><button class="sm" id="drawer-close">Close</button></div>' +
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
    host.innerHTML = '<div class="empty">No site. They appear as soon as a ' +
      'router reports sessions.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Site</th><th>Router</th><th class="num">Subscribers</th>' +
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
          pop.subscriber_count + ' subscriber(s) and ' + pop.backhaul_count +
          ' backhaul(s) will be erased, along with ALL their measurement history.\n\n' +
          'Remove the router from the inventory too, otherwise the site is recreated ' +
          'on the next cycle.')) return;
      try {
        await api('/pops/' + pop.id + '?confirm=true', { method: 'DELETE' });
        await loadRouters();
      } catch (err) { alert(err.message); }
    });
  });
}

/** Sante des routeurs : ce qu'ils disent d'eux-memes, en direct.
 *
 *  Lue apres l'inventaire et sans le bloquer : c'est une commande par routeur,
 *  et un routeur lent ne doit pas retarder la page ou l'on vient justement
 *  d'ajouter un equipement. */
async function loadRoutersHealth() {
  const host = document.getElementById('routers-health');
  let data;
  try {
    data = await api('/pops/health');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const routeurs = data.routers || [];
  if (!routeurs.length) {
    host.innerHTML = '<div class="empty">No router collected.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Router</th><th>PoP</th><th>Model</th>' +
    '<th class="num">CPU</th><th class="num">Memory</th>' +
    '<th class="num">Uptime</th><th>Version</th></tr></thead><tbody>' +
    routeurs.map((r) => {
      if (!r.reachable) {
        return '<tr>' +
          '<td class="login"><b>' + esc(r.router) + '</b></td>' +
          '<td class="nowrap">' + esc(r.pop_name || '-') + '</td>' +
          '<td colspan="5"><span class="badge crit">unreachable</span>' +
            '<span class="hint">' + esc(r.error || '') + '</span></td>' +
          '</tr>';
      }
      // Les seuils disent ce qui EMPECHE d'appliquer, pas ce qui est "beau" :
      // au-dela de 80 % de CPU, RouterOS commence a retarder ses reponses API.
      const cpu = r.cpu_load_pct;
      const ram = r.memory_used_pct;
      const badge = (v, chaud, brulant) => v === null || v === undefined
        ? '<span class="hint">-</span>'
        : '<span class="badge ' + (v >= brulant ? 'crit' : v >= chaud ? 'warn' : 'ok') + '">' +
          esc(Math.round(v)) + ' %</span>';
      return '<tr>' +
        '<td class="login"><b>' + esc(r.router) + '</b>' +
          (r.identity && r.identity !== r.router
            ? '<span class="hint" style="display:block">' + esc(r.identity) + '</span>'
            : '') + '</td>' +
        '<td class="nowrap">' + esc(r.pop_name || '-') + '</td>' +
        '<td>' + esc(r.board_name || '-') +
          (r.cpu_count ? ' <span class="hint">' + esc(r.cpu_count) + ' core(s)</span>' : '') +
          '</td>' +
        '<td class="num">' + badge(cpu, 70, 85) + '</td>' +
        // Sans memoire totale, RouterOS ne permet aucun pourcentage : on montre
        // alors la memoire libre seule, plutot qu'un tiret suivi d'un chiffre
        // qui se lirait comme un nombre negatif.
        '<td class="num">' +
          (ram === null || ram === undefined
            ? (r.free_memory
              ? '<span class="hint">' + esc(bytesText(r.free_memory)) + ' libres</span>'
              : '<span class="hint">-</span>')
            : badge(ram, 80, 90) +
              (r.free_memory ? '<span class="hint" style="display:block">' +
                esc(bytesText(r.free_memory)) + ' libres</span>' : '')) + '</td>' +
        '<td class="num">' + esc(uptime(r.uptime_s)) + '</td>' +
        '<td style="color:var(--faint)">' + esc(r.version || '-') + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
}

/** Octets en unite lisible. Les memoires de routeur se comptent en Mio. */
function bytesText(octets) {
  const n = Number(octets) || 0;
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + ' Gio';
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(0) + ' Mio';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' Kio';
  return n + ' o';
}

async function loadRouters() {
  await loadPops();
  await loadAntennas();
  // La sante interroge les routeurs un par un : lancee sans attendre, pour ne
  // pas retarder la page ou l'on vient d'ajouter un equipement.
  loadRoutersHealth();
  const data = await api('/pops/routers');
  state.routers = data.routers;

  const notice = document.getElementById('pops-notice');
  let html = '';
  if (!data.secrets_available) {
    html += '<div class="notice warn"><b>Adding from the interface is unavailable.</b> ' +
      esc(data.secrets_reason || '') +
      '<span class="hint">Check <code>APP_SECRET_KEY_FILE</code>.</span></div>';
  }
  (data.skipped || []).forEach((skip) => {
    // Ancien format (chaine) ou nouveau ({name, reason, source, ...}) : les deux.
    const nom = typeof skip === 'string' ? null : skip.name;
    const raison = typeof skip === 'string' ? skip : skip.reason;
    const source = typeof skip === 'string' ? null : skip.source;
    // « Retirer » agit sur l'inventaire FICHIER. Le proposer pour un routeur
    // declare en base enverrait l'exploitant vers un bouton sans effet.
    const retirable = nom && source !== 'db';
    html += '<div class="notice err"><strong>' +
      (nom ? esc(nom) + ': dropped from collection.' : 'Incomplete inventory.') +
      '</strong> ' + esc(raison) +
      '<span class="hint">Nothing is read from this router.</span>' +
      (source === 'db'
        ? '<span class="hint">Declared in the database: fix its record below.</span>'
        : '') +
      (retirable ? '<div class="actions" style="margin-top:.5rem">' +
        '<button class="sm danger" data-hide-file="' + esc(nom) + '">Remove for good</button>' +
        '</div>' : '') +
      '</div>';
  });
  // Routeurs fichier retires a la main : proposer de les restaurer.
  (data.hidden || []).forEach((h) => {
    html += '<div class="notice"><strong>' + esc(h.name) + '</strong> is removed from ' +
      'the file inventory.' +
      '<div class="actions" style="margin-top:.5rem">' +
      '<button class="sm" data-restore-file="' + esc(h.name) + '">Restore</button>' +
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
    host.innerHTML = '<div class="empty">No router. Use the form below.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>PoP</th><th>Address</th><th>Account</th><th>Source</th>' +
    '<th>State</th><th>Model</th><th></th></tr></thead><tbody>' +
    state.routers.map((r) => {
      let badge = '<span class="badge">never tested</span>';
      if (r.last_error) {
        // Un secret illisible ou une fiche invalide ne sont pas une panne du
        // routeur : il est ECARTE de la collecte, ce qui se corrige ici et non
        // sur l'equipement. Les confondre envoie chercher au mauvais endroit.
        const ecarte = /secret illisible|fiche invalide/.test(r.last_error);
        badge = '<span class="badge crit" title="' + esc(r.last_error) + '">' +
          (ecarte ? 'dropped' : 'failing') + '</span>';
      }
      else if (r.last_ok_at) badge = '<span class="badge ok">reachable</span>';
      else if (r.source === 'file') badge = '<span class="badge ok">active</span>';
      // HORS COLLECTE : present dans l'inventaire, mais absent des collecteurs.
      // C'est le signal le plus direct, et le seul qui ne depende pas de
      // deviner la cause : rien n'est lu sur ce routeur, donc il n'a ni case
      // decouverte dans l'arbre, ni abonnes, ni detection de clients.
      if (r.active === false && r.enabled !== false) {
        badge = '<span class="badge crit" title="' + esc(r.last_error ||
          'This router is in the inventory but is not collected.') +
          '">not collected</span>';
      }
      if (r.enabled === false) badge = '<span class="badge">disabled</span>';

      return '<tr>' +
        '<td><strong>' + esc(r.name) + '</strong>' +
          (r.pop_name ? '<br><span style="color:var(--faint);font-size:.75rem">' + esc(r.pop_name) + '</span>' : '') + '</td>' +
        '<td class="login">' + esc(r.host) + ':' + esc(r.port) + '</td>' +
        '<td class="login">' + esc(r.username) + '</td>' +
        '<td>' + (r.source === 'file'
          ? '<span class="badge file">file inventory</span>'
          : '<span class="badge">interface</span>') + '</td>' +
        '<td>' + badge + '</td>' +
        '<td style="font-size:.76rem;color:var(--muted)">' +
          esc(r.board_name || '-') + (r.routeros_version ? ' &middot; ' + esc(r.routeros_version) : '') + '</td>' +
        '<td><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm" data-config="' + esc(r.name) +
            '" title="See the full config (/export) the controller reads">Config</button>' +
          (r.editable
            ? '<button class="sm" data-probe="' + r.id + '">Test</button>' +
              '<button class="sm" data-toggle="' + r.id + '">' + (r.enabled ? 'Disable' : 'Enable') + '</button>' +
              '<button class="sm danger" data-del="' + r.id + '">Remove</button>'
            : '<span style="font-size:.72rem;color:var(--faint);margin-right:.4rem">routers.yml</span>' +
              '<button class="sm danger" data-hide-file="' + esc(r.name) +
              '" title="Drop this file router without editing the YAML">Remove</button>') +
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
  host.innerHTML = '<div class="muted">Reading the config of ' + esc(name) + '…</div>';
  try {
    const r = await api('/topology/routers/' + encodeURIComponent(name) + '/export');
    const p = r.parsed || {};
    const tuns = (p.tunnels || []).map((t) =>
      esc(t.type + ' ' + (t.name || '') + ' → ' + t.remote_address)).join(', ') || '—';
    const adrs = (p.addresses || []).length;
    const coms = Object.keys(p.comments || {}).length;
    host.innerHTML =
      '<div class="notice" style="margin-top:.6rem"><b>Config of ' + esc(name) + '</b> — ' +
        adrs + ' address(es), ' + (p.tunnels || []).length + ' tunnel(s), ' + coms +
        ' comment(s). <b>Tunnels:</b> ' + tuns +
        (r.export ? '' : '<span class="hint">The API returned no export on this ' +
          'version: discovery falls back to the structured data (/ip/address, neighbours).</span>') +
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
  if (!confirm('Remove "' + name + '" from the inventory?\n\n' +
    'The router is dropped (polling and warnings), without changing ' +
    'config/routers.yml. You will be able to restore it.')) return;
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
    notice.innerHTML = '<div class="notice">Analysis running...</div>';
  }
  try {
    const r = await api('/topology/discover', { method: 'POST' });
    const compte = document.getElementById('build-count');
    if (compte) compte.textContent = r.nodes + ' device(s), ' + r.links + ' link(s)';
    if (notice) {
      notice.innerHTML = '<div class="notice ok"><b>Tree built.</b> ' +
        r.nodes + ' device(s), ' + r.links + ' link(s).' +
        (r.warnings && r.warnings.length
          ? '<span class="hint">' + r.warnings.map(esc).join('<br>') + '</span>' : '') +
        '</div>';
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
    // Vide = a deduire. On envoie null plutot que "" : une chaine vide se
    // lirait comme une valeur posee.
    loopback: (data.get('loopback') || '').trim() || null,
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
    showFormResult('<div class="notice err">Name, address and password are required to test.</div>');
    return;
  }
  button.disabled = true;
  showFormResult('<div class="notice">Connecting to ' + esc(payload.host) + ':' + esc(payload.port) + '...</div>');
  try {
    const result = await api('/pops/routers/test', { method: 'POST', body: JSON.stringify(payload) });
    if (result.reachable) {
      showFormResult('<div class="notice ok"><strong>Connexion etablie.</strong> ' +
        esc(result.identity || 'router') + ' &middot; ' + esc(result.board_name || '?') +
        ' &middot; RouterOS ' + esc(result.version || '?') +
        '<span class="hint">' + esc(result.ppp_active_sessions) + ' active PPPoE session(s), incl. ' +
        esc(result.correlated_sessions) + ' with correlated counters' +
        (result.ppp_active_sessions > 0 && result.correlated_sessions === 0
          ? ' — no rate can be computed, check the PPPoE interface pattern.'
          : '.') + '</span></div>');
    } else {
      showFormResult('<div class="notice err"><strong>Failed.</strong> <code>' + esc(result.error) + '</code>' +
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
    showFormResult('<div class="notice ok"><b>' + esc(created.name) +
      ' enregistre.</b></div>');
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
      alert('Failed: ' + result.error + '\n\n' + (result.hint || ''));
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
  if (!confirm('Remove "' + (router ? router.name : id) + '" from the inventory?\n\n' +
    'The metrics already collected are kept.')) return;
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


/* -------------------------------------------------------------- services */

/** QUI SE CONNECTE A QUOI.
 *
 *  Le reste de l'interface compte des octets. Cette page nomme l'autre bout :
 *  quel service, quelle famille, quelle organisation. Elle ne lit jamais le
 *  contenu -- le trafic est chiffre, il le reste -- seulement l'adresse, son
 *  nom inverse et, si l'exploitant l'a autorise, le registre.
 *
 *  ELLE EST AUSSI L'ENDROIT OU L'ON RESTREINT, et ce n'est pas un hasard :
 *  decider de brider un trafic se fait en regardant ce trafic, pas dans un
 *  onglet separe ou l'on aurait perdu de vue ce que la regle designe. */
const SVC = {
  minutes: 60,
  category: '',
  search: '',
  catalogue: null,
  detail: null,
};

/** Familles, avec la teinte qui leur va. Le streaming est la raison d'etre de
 *  cette page : il porte la couleur la plus visible. */
const SVC_CATEGORIES = {
  'streaming': 'crit',
  'social networks': 'warn',
  'gaming': 'warn',
  'voice / video': 'ok',
  'cdn': '',
  'cloud': '',
  'updates': '',
  'dns': '',
  'messaging': '',
};

const PROTOCOLES = { 1: 'icmp', 6: 'tcp', 17: 'udp', 47: 'gre', 50: 'esp', 58: 'icmpv6' };

function protoName(numero) {
  const n = Number(numero) || 0;
  return PROTOCOLES[n] || (n ? String(n) : '-');
}

/** Le client d'une ligne : son login s'il est declare, son adresse sinon.
 *
 *  UNE MACHINE SANS FICHE RESTE VISIBLE. Elle n'a pas d'abonne -- un poste de
 *  supervision, une camera, un routeur -- mais elle joint bien quelque chose,
 *  et c'est la question posee. La masquer faisait disparaitre le ping qu'on
 *  venait de lancer pour verifier que la mesure marche. */
function clientCell(ligne) {
  if (ligne.login) {
    return '<a href="#" data-svc-sub="' + esc(ligne.subscriber_id) + '">' +
      esc(ligne.login) + '</a>';
  }
  return '<code>' + esc(ligne.client || '?') + '</code>' +
    ' <span class="hint">undeclared</span>';
}

/** Le domaine sous lequel un nom inverse est enregistre.
 *
 *  'lfbn-lyo-1-878-160.w86-194.abo.wanadoo.fr' ne dit rien a personne ;
 *  'wanadoo.fr' dit Orange. C'est la forme qu'on reconnait d'un coup d'oeil.
 *  Les suffixes a deux etiquettes (co.uk, com.au) comptent pour un. */
const SUFFIXES_COMPOSES = new Set([
  'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'net.uk',
  'com.au', 'net.au', 'org.au', 'com.br', 'com.mx', 'com.ar',
  'co.nz', 'co.jp', 'ne.jp', 'co.in', 'com.cn', 'co.za', 'com.tr',
]);

function domaine(nom) {
  if (!nom) return null;
  const parts = String(nom).trim().replace(/\.$/, '').toLowerCase().split('.').filter(Boolean);
  if (parts.length < 2) return null;
  if (parts.length >= 3 && SUFFIXES_COMPOSES.has(parts.slice(-2).join('.'))) {
    return parts.slice(-3).join('.');
  }
  return parts.slice(-2).join('.');
}

function svcBadge(categorie) {
  if (!categorie) return '<span class="badge">unidentified</span>';
  return '<span class="badge ' + (SVC_CATEGORIES[categorie] || '') + '">' +
    esc(categorie) + '</span>';
}

/** Le service, avec la raison de le croire.
 *
 *  LA SOURCE COMPTE AUTANT QUE LE VERDICT. "Netflix d'apres son nom inverse"
 *  et "Netflix d'apres un bloc publie" ne se contestent pas de la meme facon,
 *  et un exploitant qui va bloquer ce trafic a le droit de savoir laquelle des
 *  deux il regarde. */
function svcName(ligne) {
  if (!ligne.service) {
    return '<span class="hint">unidentified</span>';
  }
  const source = ligne.source ? ' <span class="hint">' + esc(ligne.source) + '</span>' : '';
  return '<b>' + esc(ligne.service) + '</b>' + source;
}

async function loadServices() {
  SVC.minutes = Number(document.getElementById('svc-range').value) || 60;
  SVC.category = document.getElementById('svc-category').value || '';
  SVC.search = document.getElementById('svc-search').value.trim();

  if (SVC.catalogue === null) {
    // Le catalogue ne bouge pas d'un rafraichissement a l'autre : une seule
    // lecture par session suffit, et elle remplit les deux listes du
    // formulaire de restriction.
    SVC.catalogue = await api('/netflow/catalogue').catch(() => ({ services: [], categories: [] }));
    renderCatalogue(SVC.catalogue);
    fillRuleChoices(SVC.catalogue);
    fillCategoryFilter(SVC.catalogue);
  }

  const suffixe = '?minutes=' + SVC.minutes +
    (SVC.category ? '&category=' + encodeURIComponent(SVC.category) : '') +
    (SVC.search ? '&q=' + encodeURIComponent(SVC.search) : '');

  const [etat, intel, dest, live, regles] = await Promise.all([
    api('/netflow/status'),
    api('/netflow/intel').catch(() => null),
    api('/netflow/destinations' + suffixe + '&limit=120')
      .catch(() => ({ destinations: [], services: [] })),
    api('/netflow/connections?limit=80').catch(() => ({ connections: [] })),
    api('/traffic-rules').catch(() => ({ rules: [] })),
  ]);

  renderServiceNotice(etat, intel);
  renderServiceStats(etat, intel, dest);
  renderLiveConnections(live);
  renderServiceTable(dest.services || []);
  renderDestinations(dest.destinations || []);
  renderRules(regles);

  document.getElementById('svc-count').textContent = etat.listening
    ? (dest.destinations || []).length + ' address(es) over ' + SVC.minutes + ' min'
    : 'collector stopped';
}

/** Ce qui empeche cette page de repondre, dit en toutes lettres.
 *
 *  Un tableau vide se lit "il n'y a rien". Or il veut souvent dire "je ne peux
 *  pas savoir" : collecteur coupe, suivi des destinations desactive,
 *  enrichissement a l'arret. Les trois appellent des gestes differents. */
function renderServiceNotice(etat, intel) {
  const hote = document.getElementById('svc-notice');
  const messages = [];
  if (!etat.enabled) {
    messages.push('<div class="notice warn"><b>NetFlow collector off.</b></div>');
  } else if (!etat.listening) {
    messages.push('<div class="notice err"><b>The collector is not listening.</b> ' +
      esc(etat.last_error || 'port busy or insufficient privileges') + '</div>');
  } else if (!etat.packets_received) {
    messages.push('<div class="notice warn"><b>No datagram received on ' +
      esc(etat.bind) + '.</b> Traffic tab &gt; Export on the routers.</div>');
  }
  if (etat.enabled && etat.track_destinations === false) {
    messages.push('<div class="notice warn"><b>Destination tracking disabled.</b></div>');
  }
  if (intel && intel.enabled === false) {
    messages.push('<div class="notice warn"><b>Identification disabled.</b></div>');
  }
  if (intel && intel.pending > 0) {
    messages.push('<div class="notice">' + esc(intel.pending) +
      ' address(es) waiting for a name.</div>');
  }
  hote.innerHTML = messages.join('');
}

function renderServiceStats(etat, intel, dest) {
  const services = (dest.services || []);
  const total = services.reduce((s, r) => s + Number(r.down_bytes || 0) + Number(r.up_bytes || 0), 0);
  const nomme = services
    .filter((r) => r.service)
    .reduce((s, r) => s + Number(r.down_bytes || 0) + Number(r.up_bytes || 0), 0);
  const streaming = services
    .filter((r) => r.category === 'streaming')
    .reduce((s, r) => s + Number(r.down_bytes || 0) + Number(r.up_bytes || 0), 0);
  const partNommee = total ? Math.round((nomme / total) * 100) : 0;

  document.getElementById('svc-stats').innerHTML =
    statCard('down', 'Streaming', bytesText(streaming), '',
      total ? Math.round((streaming / total) * 100) + ' % of identified traffic' : 'nothing to measure') +
    statCard('', 'Named traffic', String(partNommee), '%',
      'the rest has neither a known prefix nor a reverse name') +
    statCard('', 'Known addresses', String((intel && intel.resolved) || 0), '',
      ((intel && intel.named) || 0) + ' matched to a service') +
    statCard(etat.destinations_dropped ? 'warn' : '', 'Current window',
      String(etat.destinations_window || 0), '',
      etat.destinations_dropped
        ? esc(etat.destinations_dropped) + ' dropped: cap reached'
        : 'subscriber/destination pairs');
}

/** La fenetre EN COURS, lue dans la memoire du collecteur.
 *
 *  C'est la seule vue en direct du produit. Elle se vide a chaque ecriture de
 *  fenetre puis se remplit : le dire evite qu'un tableau momentanement vide ne
 *  soit lu comme une panne. */
function renderLiveConnections(data) {
  const hote = document.getElementById('svc-live');
  const lignes = (data && data.connections) || [];
  if (!lignes.length) {
    hote.innerHTML = '<div class="empty">No connection in the current window.</div>';
    return;
  }
  hote.innerHTML = '<table><thead><tr><th>Client</th><th>Destination</th>' +
    '<th>Service</th><th>Category</th><th class="num">Port</th><th>Proto</th>' +
    '<th class="num">Down</th><th class="num">Up</th><th></th>' +
    '</tr></thead><tbody>' +
    lignes.map((c) =>
      '<tr><td class="login">' + clientCell(c) + '</td>' +
      '<td><a href="#" data-svc-ip="' + esc(c.address) + '"><code>' + esc(c.address) +
        '</code></a>' + (c.hostname
          ? '<br><span class="hint">' + esc(c.hostname) + '</span>' : '') + '</td>' +
      '<td>' + svcName(c) + '</td>' +
      '<td>' + svcBadge(c.category) + '</td>' +
      '<td class="num">' + esc(c.port || '-') + '</td>' +
      '<td>' + esc(protoName(c.protocol)) + '</td>' +
      '<td class="num">' + bytesText(c.down_bytes) + '</td>' +
      '<td class="num">' + bytesText(c.up_bytes) + '</td>' +
      '<td>' + (c.pending ? '<span class="hint">to be named</span>' : '') + '</td></tr>').join('') +
    '</tbody></table>';
  brancherLiensServices(hote);
}

function renderServiceTable(services) {
  const hote = document.getElementById('svc-services');
  if (!services.length) {
    hote.innerHTML = '<div class="empty">No traffic measured over this period.</div>';
    return;
  }
  const total = services.reduce((s, r) => s + Number(r.down_bytes || 0) + Number(r.up_bytes || 0), 0);
  hote.innerHTML = '<table><thead><tr><th>Service</th><th>Category</th>' +
    '<th class="num">Addresses</th><th class="num">Clients</th>' +
    '<th class="num">Down</th><th class="num">Up</th><th>Share</th>' +
    '<th></th></tr></thead><tbody>' +
    services.map((r) => {
      const somme = Number(r.down_bytes || 0) + Number(r.up_bytes || 0);
      return '<tr>' +
        '<td><b>' + esc(r.service || 'unidentified') + '</b></td>' +
        '<td>' + svcBadge(r.category) + '</td>' +
        '<td class="num">' + esc(r.addresses) + '</td>' +
        '<td class="num">' + esc(r.clients) + '</td>' +
        '<td class="num">' + bytesText(r.down_bytes) + '</td>' +
        '<td class="num">' + bytesText(r.up_bytes) + '</td>' +
        '<td style="min-width:140px">' + meter(somme, total || 1, '') + '</td>' +
        '<td>' + (r.service
          ? '<button class="sm" data-svc-restrict="' + esc(r.service) + '">Restreindre</button>'
          : '') + '</td></tr>';
    }).join('') + '</tbody></table>';
  hote.querySelectorAll('[data-svc-restrict]').forEach((b) => {
    b.addEventListener('click', () => prefillRule(b.dataset.svcRestrict));
  });
}

function renderDestinations(lignes) {
  const hote = document.getElementById('svc-destinations');
  if (!lignes.length) {
    hote.innerHTML = '<div class="empty">No destination reached.</div>';
    return;
  }
  hote.innerHTML = '<table><thead><tr><th>Address</th><th>Reverse name</th>' +
    '<th>Service</th><th>Category</th><th class="num">Clients</th>' +
    '<th class="num">Down</th><th class="num">Up</th><th>Seen</th>' +
    '</tr></thead><tbody>' +
    lignes.map((d) =>
      '<tr><td><a href="#" data-svc-ip="' + esc(d.address) + '"><code>' +
        esc(d.address) + '</code></a></td>' +
      '<td class="login">' + (d.hostname
        ? esc(d.hostname) : '<span class="hint">-</span>') + '</td>' +
      '<td>' + svcName(d) + '</td>' +
      '<td>' + svcBadge(d.category) + '</td>' +
      '<td class="num">' + esc(d.clients) + '</td>' +
      '<td class="num">' + bytesText(d.down_bytes) + '</td>' +
      '<td class="num">' + bytesText(d.up_bytes) + '</td>' +
      '<td>' + esc(depuis(d.last_seen)) + '</td></tr>').join('') +
    '</tbody></table>';
  brancherLiensServices(hote);
}

function brancherLiensServices(hote) {
  hote.querySelectorAll('[data-svc-ip]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      openDestination(a.dataset.svcIp);
    });
  });
  hote.querySelectorAll('[data-svc-sub]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      openSubscriber(Number(a.dataset.svcSub));
    });
  });
}

/** La fiche complete d'une adresse : ce qu'on sait, et QUI la joint. */
async function openDestination(address) {
  const hote = document.getElementById('svc-detail');
  hote.innerHTML = '<div class="ip-card">Reading <code>' + esc(address) + '</code>...</div>';
  let fiche;
  try {
    fiche = await api('/netflow/destinations/' + encodeURIComponent(address) +
      '?minutes=' + Math.max(SVC.minutes, 1440));
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  SVC.detail = fiche;
  renderDestinationCard(fiche);
  hote.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/** La fiche complete d'une adresse, rendue en HTML.
 *
 *  UNE SEULE IMPLEMENTATION pour les deux onglets. Elle etait ecrite dans
 *  Services ; la rendre depuis Trafic en la recopiant aurait garanti que les
 *  deux divergent au premier ajout de champ.
 *
 *  ``actions`` est faux quand la carte est ouverte hors de Services : les
 *  boutons portent des identifiants, et deux cartes ouvertes en meme temps
 *  auraient produit des doublons -- le second ecouteur ne se serait jamais
 *  branche, sans rien dire. */
function ipCard(fiche, periodeSecondes, actions) {
  const intel = fiche.intel || {};
  const catalogue = fiche.catalogue || {};
  const totaux = fiche.totals || {};
  const fait = (libelle, valeur) =>
    '<div><span>' + libelle + '</span>' + (valeur || '<span class="hint">-</span>') + '</div>';

  const service = intel.service || catalogue.service;
  const famille = intel.category || catalogue.category;
  const source = intel.source || catalogue.source;
  const octets = Number(totaux.down_bytes || 0) + Number(totaux.up_bytes || 0);
  // La localisation n'est remplie que si l'exploitant l'a autorisee : la
  // demander envoie a un tiers l'adresse que son client a jointe.
  const position = (intel.latitude !== null && intel.latitude !== undefined)
    ? Number(intel.latitude).toFixed(3) + ', ' + Number(intel.longitude).toFixed(3)
    : null;

  return '<div class="ip-card">' +
    '<h3><code>' + esc(fiche.address) + '</code> ' + svcBadge(famille) + '</h3>' +
    '<div class="ip-facts">' +
      fait('Service', service ? '<b>' + esc(service) + '</b>' : null) +
      fait('Recognised by', source && source !== 'inconnu' ? esc(source) : null) +
      fait('Domain', domaine(intel.hostname)
        ? '<b>' + esc(domaine(intel.hostname)) + '</b>' : null) +
      fait('Reverse name', intel.hostname ? esc(intel.hostname) : null) +
      fait('Organisation', intel.org ? esc(intel.org) : null) +
      fait('AS', intel.asn ? 'AS' + esc(intel.asn) : null) +
      fait('Country', intel.country ? drapeau(intel.country) + esc(intel.country) : null) +
      fait('City', intel.city ? esc(intel.city) : null) +
      fait('Region', intel.region ? esc(intel.region) : null) +
      fait('Coordinates', position
        ? '<code>' + esc(position) + '</code> ' + lienCarte(intel.latitude, intel.longitude)
        : null) +
      fait('Announced prefix', esc(intel.network || catalogue.matched_prefix || '')) +
      fait('Analysed', intel.resolved_at ? esc(depuis(intel.resolved_at)) : null) +
      fait('Clients', esc(totaux.clients || 0)) +
      fait('Down', bytesText(totaux.down_bytes)) +
      fait('Up', bytesText(totaux.up_bytes)) +
      fait('Average bandwidth', debitText(octets, periodeSecondes)) +
      fait('First seen', totaux.first_seen ? esc(depuis(totaux.first_seen)) : null) +
      fait('Last seen', totaux.last_seen ? esc(depuis(totaux.last_seen)) : null) +
    '</div>' +
    (actions
      ? '<div class="actions" style="margin-top:.7rem">' +
        '<button class="sm" id="svc-detail-resolve">Analyse again</button>' +
        '<button class="sm" id="svc-detail-restrict">Restrict this address</button>' +
        '<span class="mode">' + esc(intel.attempts || 0) + ' tentative(s)</span>' +
        '</div>'
      : '') +
    '<h3 style="margin-top:.9rem">Who reaches this address</h3>' +
    ((fiche.clients || []).length
      ? '<div class="table-wrap"><table><thead><tr><th>Client</th><th>PoP</th>' +
        '<th class="num">Port</th><th>Proto</th><th>Usage</th>' +
        '<th class="num">Down</th><th class="num">Up</th>' +
        '<th class="num">Avg rate</th><th>Seen</th>' +
        '</tr></thead><tbody>' +
        fiche.clients.map((s) => '<tr>' +
          '<td class="login">' + clientCell(s) +
            (s.kind === 'static' ? ' <span class="badge">static IP</span>' : '') + '</td>' +
          '<td class="nowrap">' + esc(s.pop_name || '-') + '</td>' +
          '<td class="num">' + esc(s.port || '-') + '</td>' +
          '<td>' + esc(protoName(s.protocol)) + '</td>' +
          '<td>' + esc(s.app || '-') + '</td>' +
          '<td class="num">' + bytesText(s.down_bytes) + '</td>' +
          '<td class="num">' + bytesText(s.up_bytes) + '</td>' +
          '<td class="num">' + debitText(
            Number(s.down_bytes || 0) + Number(s.up_bytes || 0), periodeSecondes) + '</td>' +
          '<td>' + esc(depuis(s.last_seen)) + '</td></tr>').join('') +
        '</tbody></table></div>'
      : '<div class="empty">Nobody reached this address over the period.</div>') +
    '</div>';
}

function renderDestinationCard(fiche) {
  document.getElementById('svc-detail').innerHTML =
    ipCard(fiche, Math.max(SVC.minutes, 1440) * 60, true);

  const detail = document.getElementById('svc-detail');
  brancherLiensServices(detail);
  document.getElementById('svc-detail-resolve').addEventListener('click', async () => {
    try {
      await api('/netflow/destinations/' + encodeURIComponent(fiche.address) + '/resolve',
        { method: 'POST' });
      await openDestination(fiche.address);
    } catch (err) {
      detail.innerHTML += '<div class="notice err">' + esc(err.message) + '</div>';
    }
  });
  document.getElementById('svc-detail-restrict').addEventListener('click', () => {
    prefillRule(null, fiche.address);
  });
}

/* ------------------------------------------------------- restrictions */

function renderRules(data) {
  const hote = document.getElementById('svc-rules');
  const regles = (data && data.rules) || [];
  const etat = (data && data.status) || {};
  if (!regles.length) {
    hote.innerHTML = '<div class="empty">No restriction.</div>';
    return;
  }
  hote.innerHTML = '<table><thead><tr><th>Rule</th><th>Effect</th><th>Targets</th>' +
    '<th>For whom</th><th>State</th><th>Last applied</th><th></th>' +
    '</tr></thead><tbody>' +
    regles.map((r) => {
      const criteres = [].concat(r.services || [], r.categories || [],
        (r.prefixes || []).length ? [(r.prefixes || []).length + ' prefix(es)'] : []);
      return '<tr>' +
        '<td><b>' + esc(r.name) + '</b>' +
          (r.note ? '<br><span class="hint">' + esc(r.note) + '</span>' : '') + '</td>' +
        '<td>' + (r.action === 'limit'
          ? '<span class="badge warn">cap ' +
            (r.limit_down_mbps ? esc(mbps(r.limit_down_mbps)) : '-') + ' / ' +
            (r.limit_up_mbps ? esc(mbps(r.limit_up_mbps)) : '-') + '</span>'
          : '<span class="badge crit">blocked</span>') + '</td>' +
        '<td>' + (criteres.length ? esc(criteres.join(', ')) : '<span class="hint">-</span>') +
          (r.protocol ? ' <span class="hint">' + esc(r.protocol) +
            (r.ports ? ':' + esc(r.ports) : '') + '</span>' : '') + '</td>' +
        '<td>' + (r.scope === 'subscribers'
          ? esc((r.logins || []).length) + ' subscriber(s)' : 'everyone') + '</td>' +
        '<td>' + (r.enabled
          ? '<span class="badge ok">active</span>' : '<span class="badge">suspended</span>') +
          '</td>' +
        '<td>' + (r.last_applied_at
          ? esc(r.last_state || '') + ' <span class="hint">' +
            esc(depuis(r.last_applied_at)) + '</span>'
          : '<span class="hint">never applied</span>') + '</td>' +
        '<td class="actions">' +
          '<button class="sm" data-rule-preview="' + esc(r.id) + '">What it targets</button>' +
          '<button class="sm" data-rule-toggle="' + esc(r.id) + '" data-rule-on="' +
            (r.enabled ? '1' : '0') + '">' + (r.enabled ? 'Suspend' : 'Enable') + '</button>' +
          '<button class="sm danger" data-rule-del="' + esc(r.id) + '">Delete</button>' +
        '</td></tr>';
    }).join('') + '</tbody></table>';

  hote.querySelectorAll('[data-rule-del]').forEach((b) => {
    b.addEventListener('click', () => deleteRule(Number(b.dataset.ruleDel)));
  });
  hote.querySelectorAll('[data-rule-toggle]').forEach((b) => {
    b.addEventListener('click', () =>
      toggleRule(Number(b.dataset.ruleToggle), b.dataset.ruleOn !== '1'));
  });
  hote.querySelectorAll('[data-rule-preview]').forEach((b) => {
    b.addEventListener('click', () => previewRule(Number(b.dataset.rulePreview)));
  });
}

function ruleNotice(html) {
  document.getElementById('svc-rule-result').innerHTML = html;
}

function applyNotice(html) {
  document.getElementById('svc-apply-result').innerHTML = html;
}

/** Ce que la levee a vraiment retire des routeurs.
 *
 *  Suspendre ou supprimer une regle la LEVE aussitot. Dire "c'est fait" sans
 *  regarder le rapport ferait croire qu'un trafic repasse alors qu'un routeur
 *  injoignable -- ou l'ecriture coupee -- le bloque encore. */
function liftNotice(action, rapport, fait) {
  fait = fait || 'lifted on the routers';
  if (!rapport || rapport.state === 'levee' || rapport.state === 'posee') {
    return '<div class="notice ok">Rule ' + action + (rapport ? ' and ' + fait : '') +
      '.</div>';
  }
  // Seulement ce qui bloque, routeur par routeur : pas la liste des commandes.
  const details = (rapport.routers || [])
    .filter((r) => r.state !== 'posee')
    .map((r) => esc(r.router) + ': ' + esc(r.reason)).join('<br>');
  return '<div class="notice warn">Rule ' + action + ', but <b>not ' + esc(fait) +
    ' everywhere</b>' + (rapport.reason ? ': ' + esc(rapport.reason) : '') +
    (details ? '<br>' + details : '') +
    '<br><span class="hint">The automatic pass will retry.</span></div>';
}

async function deleteRule(id) {
  try {
    const reponse = await api('/traffic-rules/' + id, { method: 'DELETE' });
    applyNotice(liftNotice('deleted', reponse && reponse.lift));
    await loadServices();
  } catch (err) {
    applyNotice('<div class="notice err">' + esc(err.message) + '</div>');
  }
}

async function toggleRule(id, enabled) {
  try {
    const regle = await api('/traffic-rules/' + id, {
      method: 'PATCH', body: JSON.stringify({ enabled }),
    });
    applyNotice(enabled
      ? liftNotice('enabled', regle && regle.apply, 'applied on the routers')
      : liftNotice('suspended', regle && regle.lift));
    await loadServices();
  } catch (err) {
    applyNotice('<div class="notice err">' + esc(err.message) + '</div>');
  }
}

/** Ce que la regle vise A CET INSTANT.
 *
 *  Une regle est un critere, pas une liste : elle grossit toute seule a mesure
 *  que NetFlow decouvre des serveurs. Avant de la poser, il faut pouvoir
 *  regarder ce qu'elle couvre reellement -- surtout quand le critere est une
 *  famille entiere ou un CDN, qui porte tout le monde. */
async function previewRule(id) {
  try {
    const vue = await api('/traffic-rules/' + id + '/preview?limit=40');
    applyNotice('<div class="ip-card"><h3>' + esc(vue.rule.name) + '</h3>' +
      '<div class="ip-facts">' +
        '<div><span>Target addresses</span><b>' + esc(vue.address_count) + '</b></div>' +
        '<div><span>Target clients</span>' + (vue.rule.scope === 'subscribers'
          ? esc(vue.client_count) + ' prefix(es)' : 'everyone') + '</div>' +
        '<div><span>Routers</span>' +
          esc((vue.routers || []).join(', ') || 'none') + '</div>' +
      '</div>' +
      '<p class="empty" style="text-align:left;padding:.6rem 0 .3rem">Sample:</p>' +
      '<div class="login" style="font-size:.75rem;line-height:1.6">' +
        esc((vue.addresses || []).join('  ')) +
        (vue.address_count > (vue.addresses || []).length
          ? ' <span class="hint">... and ' +
            esc(vue.address_count - vue.addresses.length) + ' more</span>' : '') +
      '</div></div>');
  } catch (err) {
    applyNotice('<div class="notice err">' + esc(err.message) + '</div>');
  }
}

function fillCategoryFilter(catalogue) {
  const select = document.getElementById('svc-category');
  select.innerHTML = '<option value="">All categories</option>' +
    (catalogue.categories || []).map((c) =>
      '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
}

function fillRuleChoices(catalogue) {
  document.getElementById('svc-rule-services').innerHTML =
    (catalogue.services || []).map((s) =>
      '<option value="' + esc(s.key) + '" title="' + esc(s.note || '') + '">' +
        esc(s.label) + ' (' + esc(s.prefixes) + ' bloc' + (s.prefixes > 1 ? 's' : '') + ')' +
      '</option>').join('');
  document.getElementById('svc-rule-categories').innerHTML =
    (catalogue.categories || []).map((c) =>
      '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
}

function renderCatalogue(catalogue) {
  const services = catalogue.services || [];
  document.getElementById('svc-catalogue').innerHTML = !services.length
    ? '<div class="empty">Empty catalogue.</div>'
    : '<table><thead><tr><th>Service</th><th>Category</th><th class="num">Published prefixes</th>' +
      '<th>Reverse names</th><th>Worth knowing</th></tr></thead><tbody>' +
      services.map((s) => '<tr>' +
        '<td><b>' + esc(s.label) + '</b><br><span class="hint">' + esc(s.key) + '</span></td>' +
        '<td>' + svcBadge(s.category) + '</td>' +
        '<td class="num">' + esc(s.prefixes) +
          (s.prefixes ? '' : ' <span class="hint">reverse name only</span>') + '</td>' +
        '<td class="login" style="font-size:.72rem">' + esc((s.rdns || []).join(' ')) + '</td>' +
        '<td style="font-size:.74rem;color:var(--muted)">' + esc(s.note || '') + '</td>' +
        '</tr>').join('') + '</tbody></table>';
}

/** Ouvre le formulaire deja rempli depuis un service ou une adresse.
 *
 *  Le geste naturel est "je vois ce trafic, je veux le brider". Obliger a
 *  retrouver le service dans une liste de trente entrees apres l'avoir vu a
 *  l'ecran serait une perte seche. */
function prefillRule(service, address) {
  const bloc = document.getElementById('svc-rule-block');
  bloc.open = true;
  if (service) {
    const select = document.getElementById('svc-rule-services');
    Array.from(select.options).forEach((o) => { o.selected = (o.value === service); });
    document.getElementById('svc-rule-name').value = 'Restriction ' + service;
  }
  if (address) {
    document.getElementById('svc-rule-prefixes').value = address;
    document.getElementById('svc-rule-name').value = 'Restriction ' + address;
  }
  bloc.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function selectedValues(id) {
  return Array.from(document.getElementById(id).selectedOptions).map((o) => o.value);
}

function lignesNonVides(id) {
  return document.getElementById(id).value
    .split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
}

async function submitRule(event) {
  event.preventDefault();
  const action = document.getElementById('svc-rule-action').value;
  const scope = document.getElementById('svc-rule-scope').value;
  const corps = {
    name: document.getElementById('svc-rule-name').value.trim(),
    action,
    services: selectedValues('svc-rule-services'),
    categories: selectedValues('svc-rule-categories'),
    prefixes: lignesNonVides('svc-rule-prefixes'),
    scope,
    logins: scope === 'subscribers' ? lignesNonVides('svc-rule-logins') : [],
    protocol: document.getElementById('svc-rule-protocol').value || null,
    ports: document.getElementById('svc-rule-ports').value.trim() || null,
    note: document.getElementById('svc-rule-note').value.trim() || null,
  };
  if (action === 'limit') {
    corps.limit_down_mbps = readRate('svc-rule-down', 'svc-rule-down-unit');
    corps.limit_up_mbps = readRate('svc-rule-up', 'svc-rule-up-unit');
  }
  try {
    const regle = await api('/traffic-rules', { method: 'POST', body: JSON.stringify(corps) });
    ruleNotice(liftNotice('<b>' + esc(regle.name) + '</b> saved', regle.apply,
      'applied on the routers'));
    document.getElementById('svc-rule-form').reset();
    document.getElementById('svc-rule-limits').hidden = true;
    document.getElementById('svc-rule-logins-field').hidden = true;
    await loadServices();
  } catch (err) {
    ruleNotice('<div class="notice err">' + esc(err.message) + '</div>');
  }
}

/* ---------------------------------------------------- antennes Ubiquiti */

async function loadAntennas() {
  const data = await api('/pops/antennas');
  state.antennas = data.antennas;
  remplirMenusPop();

  const notice = document.getElementById('antennas-notice');
  notice.innerHTML = !data.secrets_available
    ? '<div class="notice warn"><strong>Password cannot be stored.</strong> ' +
      esc(data.secrets_reason || '') + '<span class="hint">You can still ' +
      'add an antenna whose <code>/status.cgi</code> is open for reading ' +
      '(no password).</span></div>'
    : '';
  document.getElementById('a-btn-save').disabled = false;

  const host = document.getElementById('antennas-table');
  if (!state.antennas.length) {
    host.innerHTML = '<div class="empty">No antenna. Use the form below.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>Link</th><th>Address</th><th>PoP</th>' +
    '<th class="num">Capacity read</th><th>State</th><th></th></tr></thead><tbody>' +
    state.antennas.map((a) => {
      let badge = '<span class="badge">never read</span>';
      if (a.last_error) badge = '<span class="badge crit" title="' + esc(a.last_error) + '">failing</span>';
      else if (a.last_ok_at) badge = '<span class="badge ok">reachable</span>';
      if (a.enabled === false) badge = '<span class="badge">disabled</span>';
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
          '<button class="sm" data-a-probe="' + a.id + '">Test</button>' +
          '<button class="sm" data-a-toggle="' + a.id + '">' +
            (a.enabled ? 'Disable' : 'Enable') + '</button>' +
          '<button class="sm danger" data-a-del="' + a.id + '">Remove</button>' +
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
  return '<div class="notice ok"><strong>Antenna reachable.</strong> Capacity read: ' +
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
    showAntennaResult('<div class="notice err">Name and address are required to test.</div>');
    return;
  }
  button.disabled = true;
  showAntennaResult('<div class="notice">Reading ' + esc(payload.host) + '...</div>');
  try {
    const result = await api('/pops/antennas/test', { method: 'POST', body: JSON.stringify(payload) });
    showAntennaResult(result.reachable
      ? antennaCapacityLine(result)
      : '<div class="notice err"><strong>Failed.</strong> <code>' + esc(result.error) + '</code>' +
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
      ' saved.</strong><span class="hint">Its capacity is read and attached to ' +
      'the tree right away, then on every cycle, without a restart.</span></div>');
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
    if (!result.reachable) alert('Failed: ' + result.error + '\n\n' + (result.hint || ''));
  } catch (err) {
    alert(err.message);
  } finally {
    button.disabled = false; button.textContent = original;
    await loadAntennas();
  }
}

async function deleteAntenna(id) {
  const antenna = state.antennas.find((a) => String(a.id) === String(id));
  if (!confirm('Remove "' + (antenna ? antenna.name : id) + '"?\n\n' +
    'The metrics already collected are kept.')) return;
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
  gateway: 'Gateway', core: 'Core', pop: 'PoP', radio: 'Radio',
  sector: 'Sector', cpe: 'CPE', client: 'Client', unknown: 'Unknown', subscriber: 'Subscribers',
  // Nature a part entiere : ce noeud est DECLARE, pas decouvert.
  static: 'Static-IP client',
  // Ni infrastructure, ni abonne : une adresse vue, rien de plus.
  candidate: 'Detected, undeclared',
};
const KIND_COLOR = {
  gateway: 'var(--accent)', core: 'var(--accent)', pop: 'var(--down)',
  radio: 'var(--up)', sector: 'var(--up)', cpe: 'var(--muted)', client: '#a78bfa',
  unknown: 'var(--faint)', subscriber: '#a78bfa', static: '#f0abfc',
  candidate: 'var(--warn)',
};

/* ------------------------------------------------- editeur d'arbre reseau */

const KIND_ORDER = [
  'gateway', 'core', 'pop', 'radio', 'sector', 'cpe', 'static', 'candidate', 'client', 'unknown',
];
const NODE_W = 176;
const NODE_H = 48;

/** Etat de l'editeur, conserve entre deux rafraichissements : disposition,
 *  case selectionnee, et si l'on montre les liens sans debit. */
const topo = {
  data: null, subs: [], model: null, selected: null, dragging: false,
  rateOnly: true, linkMode: false, linkSource: null,
  // Agregats d'abonnes ouverts, par cle. Replie par defaut : un PoP
  // d'operateur porte des centaines d'abonnes.
  abosOuverts: new Set(),
  // Branches repliees, par cle. Un reseau d'operateur compte des dizaines de
  // PoPs : pouvoir en fermer une est ce qui rend les autres lisibles.
  replies: new Set(),
  // Echelle d'affichage. Un arbre large ne tient pas sur un ecran ; le reduire
  // pour en voir la forme vaut mieux que de defiler a l'aveugle.
  zoom: 1,
  // L'arbre a-t-il deja ete cadre une fois ? Au-dela, l'echelle appartient a
  // l'exploitant : la recalculer a chaque rafraichissement effacerait son
  // reglage toutes les trente secondes.
  ajuste: false,
};

// Au-dela, l'arbre cesse d'etre lisible et ne renseigne plus sur rien :
// le detail se lit dans l'onglet Abonnes, qui est fait pour ca.
const TOPO_ABOS_MAX = 25;

// Hauteur minimale du cadre. En dessous, l'arbre se lit par le trou d'une
// serrure : mieux vaut un peu de fond libre sous un tout petit reseau.
const TOPO_CADRE_MIN = 360;

/** Le tableau technique des liens. Il avait son propre onglet ; il vit
 *  desormais replie sous l'arbre reseau, qui montre la MEME donnee en cases.
 *
 *  Il n'est charge que si le bloc est ouvert : c'est une lecture
 *  supplementaire de l'inventaire pour une question qu'on ne se pose pas a
 *  chaque rafraichissement. */
async function loadTopology() {
  const bloc = document.getElementById('net-links-block');
  if (bloc && !bloc.open) return;
  const [data, inventaire] = await Promise.all([
    topo.data ? Promise.resolve(topo.data) : fetchTopo(),
    api('/pops/routers').catch(() => null),
  ]);
  renderTopologySources(data, inventaire);
  renderTopologyLinks(data.links, data.nodes);
}

/** Les cles des noeuds qui sont des routeurs INTERROGES par le controleur.
 *
 *  Sans cette distinction, la colonne "Vers" melange deux natures que tout
 *  oppose : un equipement que le controleur LIT par API (il en tire ses liens,
 *  ses abonnes, ses files) et un equipement qu'un voisin VOIT simplement en
 *  face. Les deux s'affichaient avec le meme badge de role. */
function topoInterroges(nodes) {
  const cles = new Set();
  (nodes || []).forEach((n) => {
    if (topoAttrs(n).managed === true) cles.add(n.key);
  });
  return cles;
}

/** Combien de vues distinctes chaque case regroupe, indexe par cle.
 *
 *  La reconciliation replie en UNE case plusieurs observations du meme
 *  equipement (vu par deux voisins, en IPv4 et IPv6, sous deux casses). C'est
 *  ce qu'on veut -- mais quand elle se trompe, elle replie deux equipements
 *  DIFFERENTS, et la case absorbe des liens qui ne lui appartiennent pas. Rien
 *  ne le signalait la ou on le remarque : plusieurs lignes du tableau pointant
 *  vers un meme nom sont soit un equipement joignable par plusieurs chemins
 *  (normal), soit une fusion abusive (a defaire) -- et l'ecran ne permettait
 *  pas de trancher. */
function topoFusions(nodes) {
  const parCle = new Map();
  (nodes || []).forEach((n) => {
    const compte = Number(n.merged_count) || 0;
    if (compte > 1) parCle.set(n.key, { compte, membres: n.members || [] });
  });
  return parCle;
}

/** Dit QUI a produit ce tableau, et pourquoi certains routeurs n'y sont pas.
 *
 *  Un tableau dont toutes les lignes portent le meme nom dans la colonne
 *  "Depuis" pose une question a laquelle l'ecran ne repondait pas : ce routeur
 *  est-il le seul declare, le seul joignable, ou le seul dont les compteurs ont
 *  ete lus ? Les trois causes sont opposees -- l'une n'appelle aucune action,
 *  les deux autres si -- et rien ne les distinguait. Le detail de l'echec
 *  n'existait que dans le panneau d'une case de l'onglet Arbre reseau, qu'il
 *  fallait penser a ouvrir.
 */
function renderTopologySources(data, inventaire) {
  const host = document.getElementById('topo-sources');
  if (!host) return;
  const declares = (inventaire && inventaire.routers) || [];
  const ecartes = (inventaire && inventaire.skipped) || [];
  // Un routeur "producteur" est un routeur dont au moins un lien a ete
  // decouvert : c'est la preuve qu'on a vraiment lu sa configuration.
  const producteurs = new Set(
    (data.links || []).map((l) => l.discovered_by).filter((n) => n && n !== 'manual'));
  const muets = declares.filter((r) => !producteurs.has(r.name));

  // Les cases posees pour un routeur dont la lecture a echoue portent l'erreur.
  const injoignables = {};
  (data.nodes || []).forEach((n) => {
    const a = topoAttrs(n);
    if (a.unreachable && n.router_name) injoignables[n.router_name] = a.error || 'read failed';
  });

  if (!declares.length && !ecartes.length) { host.innerHTML = ''; return; }

  let html = '';
  if (!muets.length && !ecartes.length) {
    host.innerHTML = '<div class="notice ok" style="margin-bottom:.8rem">' +
      '<strong>' + producteurs.size + ' router(s) polled: ' +
      esc(declares.map((r) => r.name).sort().join(', ')) + '</strong>' +
      '<span class="hint">All are read by API. A cable between two of them ' +
      'counts as ONE row, carried by one of the two ends: that is why the ' +
      '<b>From</b> column may name only one. The ' +
      '<span class="badge ok">polled</span> badge in the <b>To</b> column marks ' +
      'the other end. Devices WITHOUT that badge are seen from across, not read: ' +
      'add them under <b>Devices</b> to poll them in turn.</span></div>';
    return;
  }

  html += '<div class="notice err" style="margin-bottom:.8rem"><strong>' +
    producteurs.size + ' router(s) polled out of ' + (declares.length + ecartes.length) +
    ' declared.</strong><span class="hint">Only a POLLED router produces rows ' +
    'here. A device that only appears in the <b>To</b> column is seen by a ' +
    'neighbour, not read: it brings neither its own links, nor its subscribers, nor its queues.' +
    '</span><ul style="margin:.5rem 0 0;padding-left:1.1rem">';
  muets.forEach((r) => {
    const raison = injoignables[r.name];
    html += '<li><b>' + esc(r.name) + '</b> (' + esc(r.host) + ') — ' +
      (raison
        ? 'unreachable: <code>' + esc(String(raison).slice(0, 200)) + '</code>'
        : 'declared, but no link discovered. Check the API account (policy ' +
          '<code>read,api,test</code>) and the port.') + '</li>';
  });
  ecartes.forEach((e) => {
    html += '<li><b>' + esc(e.name || '(invalid record)') + '</b> — dropped: ' +
      esc(String(e.reason || '').slice(0, 200)) + '</li>';
  });
  html += '</ul></div>';
  host.innerHTML = html;
}

/** Rang d'un role : plus petit = plus en amont. Sert a choisir par ou entrer
 *  dans le graphe et a departager deux parents possibles (la decouverte de
 *  voisinage est symetrique : elle dit "adjacents", pas "lequel est au-dessus"). */
// Un client a IP fixe pend au meme niveau qu'un CPE : c'est une feuille du
// reseau, sous un secteur ou, a defaut de secteur declare, sous son PoP.
const TOPO_RANG = {
  gateway: 0, core: 1, pop: 2, radio: 3, sector: 3,
  cpe: 4, static: 4, candidate: 4, client: 4, unknown: 5,
};

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

/** Construit l'arbre a partir du graphe d'adjacence.
 *
 *  POURQUOI UN PARCOURS EN LARGEUR, ET PAS UNE ORIENTATION LIEN PAR LIEN
 *  --------------------------------------------------------------------
 *  La decouverte ne dit que "A et B sont voisins". Orienter chaque lien
 *  isolement (en comparant les roles de ses deux bouts) ne peut pas marcher :
 *  entre deux equipements de MEME role -- deux PoPs relies par un lien de
 *  secours, cas tres courant -- il n'y a rien a comparer, et l'un devenait
 *  arbitrairement le parent de l'autre. Pire, le premier lien rencontre dans le
 *  tableau gagnait : un PoP deja rattache a un voisin ignorait ensuite son
 *  vrai lien vers le coeur. Resultat : une CHAINE (coeur > PoP Nord > PoP Sud)
 *  la ou il fallait un arbre (coeur > PoP Nord, PoP Sud).
 *
 *  On construit donc le voisinage NON ORIENTE, puis on derive l'arbre par un
 *  parcours en largeur qui part du haut de la hierarchie. Chaque case est
 *  rattachee par le chemin le plus COURT depuis le sommet : un PoP relie au
 *  coeur pend du coeur, jamais d'un PoP frere, quel que soit l'ordre des liens.
 *
 *  Deux tours : d'abord les seules adjacences SURES (ossature fiable), puis on
 *  autorise les segments partages pour ne laisser personne orphelin -- ces
 *  rattachements-la sont marques incertains (trait pointille).
 */
function topoBuildModel(data) {
  const nodes = new Map();
  data.nodes.forEach((n) => {
    if (n.hidden) return;
    nodes.set(n.key, { ...n, children: [], parentKey: null, edge: null, depth: 0 });
  });

  const rang = (key) => TOPO_RANG[nodes.get(key)?.kind] ?? 5;
  const nom = (key) => String(nodes.get(key)?.name || '');

  // Voisinage NON ORIENTE : chaque lien est inscrit dans les deux sens. C'est
  // le parcours, plus bas, qui decidera du sens. ``inverted`` dit de quel cote
  // du lien se trouve l'enfant, pour que le debit affiche sur l'arete soit bien
  // oriente vers le bas de l'arbre (cf. topoEdgeRates).
  const voisins = new Map();
  const ajouter = (de, vers, link, inverted, confident) => {
    if (!voisins.has(de)) voisins.set(de, []);
    voisins.get(de).push({ key: vers, link, inverted, confident });
  };
  data.links.forEach((l) => {
    if (!nodes.has(l.source_key) || !nodes.has(l.target_key)) return;
    if (l.source_key === l.target_key) return;
    const conf = topoLinkConfident(l);
    // parent = source, enfant = target  -> sens direct
    ajouter(l.source_key, l.target_key, l, false, conf);
    // parent = target, enfant = source  -> sens inverse
    ajouter(l.target_key, l.source_key, l, true, conf);
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
    if (!child || !parent || childKey === parentKey || child.parentKey) return false;
    if (wouldCycle(childKey, parentKey)) return false;
    child.parentKey = parentKey;
    child.edge = edge || null;
    parent.children.push(child);
    return true;
  };

  // 1) Parents forces a la main : ils priment sur toute heuristique.
  nodes.forEach((n) => {
    if (!n.parent_override || !nodes.has(n.parent_override)) return;
    const via = (voisins.get(n.parent_override) || []).find((v) => v.key === n.key);
    attach(n.key, n.parent_override, via ? { parentKey: n.parent_override, childKey: n.key,
      link: via.link, inverted: via.inverted, confident: via.confident } : null);
  });

  // 1bis) Parents PROUVES PAR LA CONFIGURATION.
  //
  //   La route par defaut d'un routeur dit ou part ce qu'il ne sait pas
  //   router : c'est la relation hierarchique elle-meme, pas une deduction.
  //   Elle passe donc avant le calcul de plus court chemin, qui n'est qu'une
  //   approximation -- utile la ou la config ne dit rien (equipements non
  //   geres, voisins decouverts), fausse des qu'un anneau relie deux PoPs
  //   entre eux autant qu'au coeur.
  //
  //   Elle reste APRES le parent force a la main : l'operateur garde le
  //   dernier mot sur le controleur, comme partout ailleurs.
  nodes.forEach((n) => {
    if (!n.config_parent || !nodes.has(n.config_parent)) return;
    const via = (voisins.get(n.config_parent) || []).find((v) => v.key === n.key);
    attach(n.key, n.config_parent, via
      ? { parentKey: n.config_parent, childKey: n.key, link: via.link,
          inverted: via.inverted, confident: true }
      : null);
  });

  // 2) Le reste est derive par plus court chemin depuis le haut de la
  //    hierarchie. Un lien SUR coute 1, un segment partage coute tres cher :
  //    une adjacence prouvee est donc toujours preferee, et un rattachement
  //    incertain ne sert qu'en dernier recours (il sera dessine en pointille).
  //    Comme on fige les cases par distance croissante, chaque case est
  //    rattachee par le chemin le plus court depuis le sommet -- un PoP relie
  //    au coeur pend du coeur, jamais d'un PoP frere.
  const POIDS_SUR = 1;
  const POIDS_INCERTAIN = 1000;

  const parRangPuisNom = (a, b) => {
    const ra = rang(a);
    const rb = rang(b);
    if (ra !== rb) return ra - rb;
    return nom(a).localeCompare(nom(b));
  };
  const ordreVoisins = (a, b) => {
    if (a.confident !== b.confident) return a.confident ? -1 : 1;
    return parRangPuisNom(a.key, b.key);
  };

  const vus = new Set();

  /** Parcourt toute la composante connexe de ``source`` et y pose les parents. */
  const parcourirComposante = (source) => {
    const dist = new Map([[source, 0]]);
    const aTraiter = new Map([[source, 0]]);
    const fige = new Set();
    const candidat = new Map();   // cle -> meilleur rattachement connu

    while (aTraiter.size) {
      // Extraction du minimum. Les graphes de PoPs tiennent en quelques
      // centaines de cases : un balayage lineaire suffit, et reste lisible.
      let cle = null;
      let meilleure = Infinity;
      aTraiter.forEach((d, k) => {
        if (d < meilleure || (d === meilleure && cle !== null && parRangPuisNom(k, cle) < 0)) {
          meilleure = d;
          cle = k;
        }
      });
      aTraiter.delete(cle);
      if (fige.has(cle)) continue;
      fige.add(cle);
      vus.add(cle);

      // Distance definitive : on peut rattacher. Le parent retenu est deja fige
      // (propriete du plus court chemin), donc l'arbre se construit de haut en bas.
      const choix = candidat.get(cle);
      if (choix) {
        const edge = { parentKey: choix.parent, childKey: cle, link: choix.via.link,
          inverted: choix.via.inverted, confident: choix.via.confident };
        if (!choix.via.confident) edge.uncertain = true;
        attach(cle, choix.parent, edge);
      }

      (voisins.get(cle) || []).slice().sort(ordreVoisins).forEach((v) => {
        if (fige.has(v.key)) return;
        const d = meilleure + (v.confident ? POIDS_SUR : POIDS_INCERTAIN);
        const connue = dist.has(v.key) ? dist.get(v.key) : Infinity;
        if (d >= connue) return;   // a egalite on garde le premier, deja trie
        dist.set(v.key, d);
        aTraiter.set(v.key, d);
        candidat.set(v.key, { parent: cle, via: v });
      });
    }
  };

  // Par ou entrer : le role le plus haut present, et a role egal le mieux
  // connecte (c'est le coeur, pas une feuille) -- puis le nom, pour la stabilite.
  const parRangPuisDegre = (a, b) => {
    const ra = rang(a);
    const rb = rang(b);
    if (ra !== rb) return ra - rb;
    const da = (voisins.get(a) || []).length;
    const db = (voisins.get(b) || []).length;
    if (da !== db) return db - da;
    return nom(a).localeCompare(nom(b));
  };

  // Une passe par composante connexe : chacune recoit sa propre racine, celle
  // qui est le plus haut dans la hierarchie.
  const restants = () => [...nodes.keys()].filter((k) => !nodes.get(k).synthetic && !vus.has(k));
  let aPlacer = restants();
  while (aPlacer.length) {
    parcourirComposante(aPlacer.sort(parRangPuisDegre)[0]);
    aPlacer = restants();
  }

  // Rattache les abonnes a leur PoP : un noeud agrege par PoP plutot que 500
  // cases. Le debit de l'arete est la somme du trafic des abonnes.
  //
  // L'AGREGAT SE DEPLIE. Il s'annoncait "repliable" sans que rien ne le deplie :
  // on lisait un compte, jamais QUI. Or c'est la question qu'on se pose devant
  // un PoP qui sature. Un clic ouvre donc la liste, et un second la referme.
  // Le repli reste le defaut, parce qu'un PoP d'operateur porte des centaines
  // d'abonnes et qu'aucun arbre ne se lit avec des centaines de cases.
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
      const cle = 'abos:' + n.key;
      const ouvert = topo.abosOuverts.has(cle);
      const synth = {
        key: cle,
        name: ouvert ? 'Subscribers of ' + n.name : abonnes.length + ' subscriber(s)',
        kind: 'subscriber',
        synthetic: true, parentKey: n.key, children: [], edge: null,
        synthRates: (tx || rx) ? { down: tx, up: rx, cap: 0 } : null,
        count: abonnes.length, addresses: [], fresh: true,
        expandable: true, expanded: ouvert,
      };
      nodes.set(synth.key, synth);
      n.children.push(synth);
      if (!ouvert) return;

      // Deplie : une case par abonne, sous l'agregat. BORNE, parce qu'un PoP
      // charge en compte des centaines et qu'un arbre illisible ne renseigne
      // sur rien. Le reste se lit dans l'onglet Abonnes, qui est fait pour ca.
      const montres = abonnes.slice(0, TOPO_ABOS_MAX);
      montres.forEach((s) => {
        const feuille = {
          key: cle + '|' + s.login,
          name: s.login,
          kind: s.kind === 'static' ? 'static' : 'cpe',
          synthetic: true, parentKey: synth.key, children: [], edge: null,
          subscriber: s,
          synthRates: (s.tx_bps || s.rx_bps)
            ? { down: Number(s.tx_bps) || 0, up: Number(s.rx_bps) || 0, cap: 0 } : null,
          addresses: s.last_ip ? [String(s.last_ip)] : [], fresh: true,
        };
        nodes.set(feuille.key, feuille);
        synth.children.push(feuille);
      });
      if (abonnes.length > montres.length) {
        const reste = {
          key: cle + '|…',
          name: '+ ' + (abonnes.length - montres.length) + ' more',
          kind: 'subscriber', synthetic: true, parentKey: synth.key,
          children: [], edge: null, synthRates: null, addresses: [], fresh: true,
        };
        nodes.set(reste.key, reste);
        synth.children.push(reste);
      }
    });
  }

  // Pour un noeud reste SANS parent, on liste ses voisins connus : le panneau
  // proposera de le rattacher a la main, plutot que de le laisser orphelin sans
  // explication.
  nodes.forEach((n) => {
    if (n.parentKey || n.synthetic) return;
    const cands = [];
    const dejaVus = new Set();
    (voisins.get(n.key) || []).forEach((v) => {
      const p = nodes.get(v.key);
      if (!p || p.key === n.key || dejaVus.has(p.key)) return;
      dejaVus.add(p.key);
      cands.push({ key: p.key, name: p.name });
    });
    if (cands.length) n.unsureParents = cands;
  });

  const roots = [...nodes.values()].filter((n) => !n.parentKey);
  return { nodesByKey: nodes, roots };
}

/** Ordre d'affichage de deux cases soeurs : hierarchie d'abord, puis le nom.
 *  Deterministe, donc l'arbre ne se reorganise pas a chaque rafraichissement. */
function topoOrdreFratrie(a, b) {
  const ra = TOPO_RANG[a.kind] ?? 5;
  const rb = TOPO_RANG[b.kind] ?? 5;
  if (ra !== rb) return ra - rb;
  return String(a.name || '').localeCompare(String(b.name || ''));
}

/** Range les cases : position enregistree si elle existe, sinon disposition
 *  automatique en arbre couche (parent a gauche, enfants a droite).
 *
 *  Chaque feuille prend une ligne a elle ; un parent est CENTRE sur ses enfants.
 *  Comme les sous-arbres occupent des plages de lignes disjointes (parcours en
 *  profondeur), deux cases ne peuvent pas se superposer. */
function topoAutoLayout(model) {
  const COL = 268;   // large : laisse la place au debit sur l'arete
  const ROWH = 74;
  const MX = 26;
  const MY = 22;
  let leaf = 0;
  const rowOf = new Map();

  model.nodesByKey.forEach((n) => { n.children.sort(topoOrdreFratrie); n.replie = false; });

  // Replier une branche cache TOUT ce qui pend dessous : sans cela ses enfants
  // garderaient une ligne (et un trait) alors que le repli dit justement qu'on
  // ne veut pas les voir.
  const cacher = (node, vus) => {
    node.children.forEach((c) => {
      if (vus.has(c.key)) return;
      vus.add(c.key);
      c.replie = true;
      cacher(c, vus);
    });
  };

  const place = (node, depth, guard) => {
    if (guard.has(node.key)) return;   // securite anti-boucle
    guard.add(node.key);
    node.depth = depth;
    // ``topo.replies`` peut manquer quand la disposition est appelee hors de
    // l'interface (harnais de test) : on ne veut pas que l'arbre en depende.
    const replie = !!(topo.replies && topo.replies.has(node.key));
    if (!node.children.length || replie) {
      if (replie) cacher(node, new Set([node.key]));
      rowOf.set(node.key, leaf++);
      return;
    }
    node.children.forEach((c, i) => {
      // De l'air entre deux sous-arbres voisins. Colles, les feuilles de l'un
      // touchent celles de l'autre et plus rien ne dit ou une branche finit.
      // Deux feuilles simples restent cote a cote : une liste d'abonnes ne
      // gagne rien a etre aeree, elle se lit justement comme une liste.
      if (i > 0 && (c.children.length || node.children[i - 1].children.length)) leaf += 0.6;
      place(c, depth + 1, guard);
    });
    const rows = node.children.map((c) => rowOf.get(c.key)).filter((r) => r !== undefined);
    // Centre sur la PLAGE des enfants (premier..dernier) : c'est ce qui donne
    // l'allure d'arbre, la moyenne tasserait le parent vers le sous-arbre le
    // plus fourni.
    rowOf.set(node.key, rows.length ? (Math.min(...rows) + Math.max(...rows)) / 2 : leaf++);
  };
  const guard = new Set();
  model.roots.sort(topoOrdreFratrie).forEach((r, i) => {
    // Deux racines ne sont pas le meme reseau : une ligne vide les separe.
    if (i > 0) leaf += 1;
    place(r, 0, guard);
  });
  // Filet : une case qu'aucune racine n'atteint garde une ligne a elle, plutot
  // que de s'empiler a l'origine avec les autres. Une case repliee sous une
  // autre, elle, n'en veut pas : c'est le repli qui l'a retiree de l'arbre.
  model.nodesByKey.forEach((n) => {
    if (!rowOf.has(n.key) && !n.replie) rowOf.set(n.key, leaf++);
  });

  // Positions des cases SANS equipement (abonnes de l'arbre). Elles n'ont pas
  // de ligne en base a porter leur pos_x : sans cette carte, elles
  // reviendraient a leur place automatique au premier rechargement, ce qui
  // revient a ne pas pouvoir les deplacer du tout.
  const libres = (topo.data && topo.data.layout) || {};

  model.nodesByKey.forEach((n) => {
    if (n.replie) return;   // rien a placer pour ce qu'on ne dessine pas
    const autoX = MX + n.depth * COL;
    const autoY = MY + rowOf.get(n.key) * ROWH;
    const libre = libres[n.key];
    const px = n.pos_x != null ? n.pos_x : (libre ? libre.pos_x : null);
    const py = n.pos_y != null ? n.pos_y : (libre ? libre.pos_y : null);
    n.x = px != null ? Number(px) : autoX;
    n.y = py != null ? Number(py) : autoY;
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
    // Rendre la hauteur a la feuille de style : sans cela le cadre garderait
    // celle du dernier arbre dessine, et un message de trois mots flotterait
    // au milieu d'une zone vide de 700 px.
    host.style.height = '';
    host.innerHTML = '<div class="empty">No device discovered.</div>';
    return;
  }
  const model = topoBuildModel(data);
  topoAutoLayout(model);
  topo.model = model;

  const etendue = topoEtendue(model);
  // LA TOILE SUIT LE CONTENU.
  //
  // Le quadrillage etait peint sur le conteneur qui DEFILE : il s'arretait donc
  // a la partie visible, et l'arbre finissait sur du vide des qu'il depassait.
  // Peint dans le SVG, a la taille du dessin, il s'etend exactement aussi loin
  // que les cases -- et le cadre garde au minimum sa propre taille pour qu'un
  // petit arbre ne flotte pas sur un fond tronque.
  const zoom = topo.zoom || 1;
  // Le CADRE se cale sur le dessin, dans les deux sens : il grandit avec
  // l'arbre jusqu'a la hauteur de la fenetre, et ne laisse pas une grande
  // zone vide sous un arbre de trois cases. La hauteur est calculee a partir
  // du CONTENU et jamais de la taille courante du cadre : la deduire de
  // ``clientHeight`` la rendrait collante (une fois grande, elle le resterait
  // apres un repli).
  const hMax = Math.max(TOPO_CADRE_MIN, window.innerHeight - 200);
  const cadre = Math.min(Math.max(etendue.h * zoom + 2, TOPO_CADRE_MIN), hMax);
  host.style.height = cadre + 'px';
  const W = Math.max((host.clientWidth - 2) / zoom, etendue.w);
  const H = Math.max((cadre - 2) / zoom, etendue.h);

  const parts = [
    '<svg width="' + (W * zoom) + '" height="' + (H * zoom) +
      '" viewBox="0 0 ' + W + ' ' + H + '">',
    '<defs><pattern id="topo-grille" width="26" height="26" patternUnits="userSpaceOnUse">' +
      '<path d="M 26 0 L 0 0 0 26" fill="none" stroke="var(--border)" stroke-width="1">' +
      '</path></pattern></defs>',
    '<rect class="topo-fond" width="' + W + '" height="' + H + '" fill="url(#topo-grille)">' +
      '</rect>',
  ];

  // Aretes d'abord (derriere les cases).
  parts.push('<g class="topo-edges">');
  model.nodesByKey.forEach((n) => {
    if (n.replie || !n.parentKey) return;
    const p = model.nodesByKey.get(n.parentKey);
    if (!p) return;
    const rates = n.synthRates || topoEdgeRates(n.edge);
    // Un lien FORCE (parent pose a la main) ou MANUEL est toujours dessine :
    // sinon un lien qu'on vient de creer disparaitrait sous "debit seulement".
    const linkKey = (n.edge && n.edge.link && n.edge.link.key) || null;
    const manual = !!linkKey && String(linkKey).indexOf('manual:') === 0;
    const forced = n.parent_override && n.parent_override === p.key;
    // Le trait d'un abonne est un RATTACHEMENT, pas un cable : "liens a debit
    // seulement" filtre les adjacences decouvertes sans compteur, pas
    // l'appartenance d'un abonne a son PoP. Le masquer laissait sa case flotter
    // a cote de l'arbre, sans rien pour dire de qui elle depend.
    if (!rates && topo.rateOnly && !forced && !manual && !n.synthetic) return;

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
        '" data-edge-link="' + esc(linkKey || '') + '"><title>Click to remove this link' +
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
    if (n.replie) return;
    const color = KIND_COLOR[n.kind] || 'var(--faint)';
    let cls = 'topo-node';
    if (n.synthetic) cls += ' synthetic';
    if (topo.selected === n.key) cls += ' selected';
    if (topo.linkSource === n.key) cls += ' linksrc';
    if (n.fresh === false) cls += ' stale';
    // Rassemble toutes les adresses de l'equipement plutot que d'en montrer une.
    // Une case d'abonne montre son adresse ET son debit : c'est ce qu'on
    // cherche quand on ouvre la liste d'un PoP qui sature.
    const meta = n.subscriber
      ? [n.addresses.join(', '),
         n.synthRates ? bpsText(n.synthRates.down) + ' / ' + bpsText(n.synthRates.up) : '']
        .filter(Boolean).join(' · ')
      : n.synthetic
        ? (n.synthRates ? bpsText(n.synthRates.down) + ' / ' + bpsText(n.synthRates.up) : 'subscribers')
        : (n.addresses && n.addresses.length ? n.addresses.join(', ')
          : (n.address || n.platform || ''));
    // Pastille de repli. Elle porte le COMPTE de ce qu'elle cache : une branche
    // fermee doit dire ce qu'il y a dessous, sinon replier revient a faire
    // disparaitre du reseau sans le signaler.
    const replie = !!(topo.replies && topo.replies.has(n.key));
    const pliable = !!n.children.length && !n.synthetic;
    const sous = pliable ? topoDescendants(model, n.key).size : 0;
    const pastille = pliable
      ? '<g class="topo-toggle' + (replie ? ' replie' : '') + '" data-fold="' + esc(n.key) + '">' +
          '<rect x="' + (NODE_W - 36) + '" y="' + (NODE_H / 2 - 9) +
            '" width="30" height="18" rx="9"></rect>' +
          '<text x="' + (NODE_W - 21) + '" y="' + (NODE_H / 2 + 4) + '" text-anchor="middle">' +
            (replie ? '+' + sous : '\u2212') + '</text>' +
          '<title>' + (replie ? 'Unfold ' + sous + ' box(es)' : 'Fold this branch') +
          '</title>' +
        '</g>'
      : '';
    parts.push(
      '<g class="' + cls + '" data-node="' + esc(n.key) +
        (n.synthetic ? '" data-synthetic="1' : '') + '" transform="translate(' +
        n.x + ',' + n.y + ')">' +
        '<rect class="box" width="' + NODE_W + '" height="' + NODE_H + '" rx="8"></rect>' +
        '<rect class="accent" x="0" y="0" width="5" height="' + NODE_H +
          '" fill="' + color + '"></rect>' +
        '<text class="role" x="13" y="18" fill="' + color + '">' +
          esc(ICONE[n.kind] || '?') +
          // Le chevron dit que la case s'ouvre, et dans quel sens elle va.
          (n.expandable ? (n.expanded ? '  \u25be open' : '  \u25b8 show') : '') + '</text>' +
        '<text class="title" x="13" y="31">' + esc(topoTrim(n.name, pliable ? 15 : 20)) +
          '</text>' +
        (meta ? '<text class="meta" x="13" y="42">' + esc(topoTrim(meta, 26)) + '</text>' : '') +
        pastille +
      '</g>');
  });
  parts.push('</g></svg>');

  host.innerHTML = parts.join('');
  bindTopoDrag(host.querySelector('svg'), model);
  bindTopoEdges(host.querySelector('svg'));
  bindTopoFolds(host.querySelector('svg'));
  renderTopoLegend(model);
}

/** Rectangle occupe par les cases visibles, marge comprise. */
function topoEtendue(model) {
  let w = 0;
  let h = 0;
  model.nodesByKey.forEach((n) => {
    if (n.replie) return;
    w = Math.max(w, n.x + NODE_W);
    h = Math.max(h, n.y + NODE_H);
  });
  return { w: w + 40, h: h + 40 };
}

/** Legende des couleurs, limitee aux natures REELLEMENT presentes dans
 *  l'arbre : une legende qui annonce des roles absents fait chercher des cases
 *  qui n'existent pas. */
function renderTopoLegend(model) {
  const host = document.getElementById('topo-legend');
  if (!host) return;
  const vus = new Set();
  model.nodesByKey.forEach((n) => { if (!n.replie) vus.add(n.kind); });
  const ordre = [...vus].sort(
    (a, b) => ((TOPO_RANG[a] ?? 5) - (TOPO_RANG[b] ?? 5)) || a.localeCompare(b));
  host.innerHTML = ordre.map((k) =>
    '<span class="chip"><i style="background:' + (KIND_COLOR[k] || 'var(--faint)') + '"></i>' +
    esc(KIND_LABEL[k] || k) + '</span>').join('');
}

/** Clic sur la pastille : replier ou deplier la branche. Le ``pointerdown`` est
 *  arrete net, sinon le glisser-deposer de la case demarrerait sous le doigt et
 *  le clic ne serait jamais reconnu. */
function bindTopoFolds(svg) {
  if (!svg) return;
  svg.querySelectorAll('[data-fold]').forEach((el) => {
    el.addEventListener('pointerdown', (e) => { e.stopPropagation(); });
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const key = el.dataset.fold;
      if (topo.replies.has(key)) topo.replies.delete(key);
      else topo.replies.add(key);
      renderTopoCanvas();
    });
  });
}

/** Echelle de l'arbre : la reduire montre la forme d'ensemble, l'agrandir rend
 *  les etiquettes lisibles. Bornee, pour qu'on ne perde jamais le dessin. */
function setTopoZoom(z) {
  topo.zoom = Math.min(2, Math.max(0.4, Math.round(z * 20) / 20));
  const etiquette = document.getElementById('topo-zoom-level');
  if (etiquette) etiquette.textContent = Math.round(topo.zoom * 100) + ' %';
  if (topo.data) renderTopoCanvas();
}

/** A LA PREMIERE OUVERTURE, cadre l'arbre entier s'il deborde.
 *
 *  Arriver sur un arbre coupe a droite, sans que rien ne dise qu'il continue,
 *  est le pire accueil : on croit voir le reseau alors qu'on en voit un
 *  morceau. Une seule fois, ensuite l'echelle appartient a l'exploitant. */
function topoAjusterUneFois() {
  if (topo.ajuste || !topo.model) return;
  topo.ajuste = true;
  topoFit();
}

/** Ajuste l'echelle pour que tout l'arbre tienne dans le cadre, sans jamais
 *  grossir au-dela de la taille reelle (agrandir un petit arbre le rendrait
 *  flou sans rien apprendre). */
function topoFit() {
  const host = document.getElementById('topo-canvas');
  if (!host || !topo.model) return;
  const etendue = topoEtendue(topo.model);
  if (!etendue.w || !etendue.h) return;
  setTopoZoom(Math.min(
    (host.clientWidth - 8) / etendue.w, (host.clientHeight - 8) / etendue.h, 1));
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
      if (!confirm('Remove this link from the tree?')) return;
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
    alert('Impossible: that would create a loop (the child is already above the parent).');
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
  notice.innerHTML = '<div class="notice">' +
    (topo.linkSource ? '<b>Child</b> box?' : '<b>Parent</b> box, then <b>child</b> box.') +
    '</div>';
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
      if (!node) return;

      // LES CASES D'ABONNES SE DEPLACENT, MAIS NE SE RATTACHENT PAS.
      //
      // Elles n'ont pas d'equipement derriere elles : les glisser sous une
      // autre case ne voudrait rien dire -- un abonne pend a son PoP, c'est la
      // collecte qui le dit, pas un glisser-deposer. Les ranger, en revanche,
      // est exactement ce qu'on veut pouvoir faire : un arbre ou chacun met ses
      // clients la ou il les a sur le terrain se lit d'un coup d'oeil.
      const libre = !!node.synthetic;

      // L'agregat d'abonnes se DEPLIE au clic : c'est la seule facon de savoir
      // QUI est derriere un compte, sans imposer des centaines de cases par
      // defaut. Il se deplace quand meme -- c'est le MOUVEMENT qui distingue
      // les deux gestes, pas la nature de la case.
      const deplier = () => {
        if (topo.abosOuverts.has(key)) topo.abosOuverts.delete(key);
        else topo.abosOuverts.add(key);
        renderTopoCanvas();
      };

      // En mode "creer un lien", un clic choisit une extremite : pas de drag.
      if (topo.linkMode) {
        const pick = () => { window.removeEventListener('pointerup', pick); topoNodeClick(key); };
        window.addEventListener('pointerup', pick);
        return;
      }

      const rect = svg.getBoundingClientRect();
      // Les coordonnees du dessin ne sont plus celles de l'ecran des que
      // l'echelle change : sans cette division, la case fuirait le pointeur.
      const z = topo.zoom || 1;
      const start = { x: ev.clientX, y: ev.clientY };
      const origin = { x: node.x, y: node.y };
      let moved = false;
      let dropTarget = null;
      topo.dragging = true;
      g.classList.add('dragging');
      g.parentNode.appendChild(g);   // passe au premier plan

      const descendants = topoDescendants(model, key);

      const onMove = (e) => {
        const nx = origin.x + (e.clientX - start.x) / z;
        const ny = origin.y + (e.clientY - start.y) / z;
        if (!moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 4) moved = true;
        node.x = nx;
        node.y = ny;
        g.setAttribute('transform', 'translate(' + nx + ',' + ny + ')');

        // Une case libre ne se rattache pas : inutile de chercher une cible,
        // et surtout de la souligner comme si le depot allait faire quelque
        // chose.
        if (libre) return;

        // Cible de rattachement : la case survolee par le CENTRE de celle qu'on
        // traine, hors elle-meme et hors ses descendants (cela ferait un cycle).
        const cx = (e.clientX - rect.left) / z;
        const cy = (e.clientY - rect.top) / z;
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

        if (!moved) {
          if (node.expandable) deplier();
          else topoNodeClick(key);
          return;
        }
        try {
          if (dropTarget && dropTarget !== node.parentKey) {
            await api('/topology/nodes/' + encodeURIComponent(key) + '/parent',
              { method: 'PATCH', body: JSON.stringify({ parent_key: dropTarget }) });
            await api('/topology/nodes/' + encodeURIComponent(key) + '/layout',
              { method: 'PATCH', body: JSON.stringify({ x: node.x, y: node.y }) });
            await loadNetwork();
          } else {
            await api('/topology/nodes/' + encodeURIComponent(key) + '/layout',
              { method: 'PATCH', body: JSON.stringify({ x: node.x, y: node.y }) });
            // Reporter la position dans les donnees en memoire, sinon le
            // prochain rendu la recalculerait en automatique et la case
            // reviendrait a sa place.
            const brut = (topo.data.nodes || []).find((d) => d.key === key);
            if (brut) { brut.pos_x = node.x; brut.pos_y = node.y; }
            if (libre) {
              if (!topo.data.layout) topo.data.layout = {};
              topo.data.layout[key] = { pos_x: node.x, pos_y: node.y };
            }
            renderTopoCanvas();   // redessine les aretes vers la nouvelle position
          }
        } catch (err) { alert(err.message); await loadNetwork(); }
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
    host.innerHTML = '<div class="muted">No box selected.</div>';
    return;
  }
  const parent = node.parentKey ? topo.model.nodesByKey.get(node.parentKey) : null;
  const attrs = topoAttrs(node);
  const dups = topoDuplicateSuggestions(node);
  host.innerHTML =
    '<h4>' + esc(node.name) +
      (attrs.unreachable ? ' <span class="badge warn" title="' + esc(attrs.error || '') +
        '">unreachable</span>' : '') + '</h4>' +
    // Doublon probable (memes mots-cles, ordre different) : signale, pas fusionne
    // d'office. Un clic replie l'autre case dans celle-ci si c'est le meme materiel.
    (dups.length
      ? '<div class="notice" style="margin:.5rem 0"><b>Probable duplicate</b> — same ' +
        'keywords as: ' +
        dups.map((d) => '<button class="sm primary" data-merge-into="' + esc(d.key) + '">' +
          'Merge ' + esc(topoTrim(d.name, 18)) + '</button>').join(' ') +
        '<span class="hint">Same device? Merge. Otherwise (two ends of one link, ' +
        'e.g. CCR\u2194DS), leave it: these are two real routers.</span></div>'
      : '') +
    '<div class="kv"><span>Role</span><span>' + esc(KIND_LABEL[node.kind] || '?') + '</span></div>' +
    // Le loopback EST l'identite du routeur : il merite la ligne juste sous le
    // role, et l'origine de la deduction doit etre visible pour que l'operateur
    // sache s'il peut lui faire confiance ou s'il doit la declarer.
    (attrs.excluded
      ? '<div class="notice err" style="margin:.5rem 0"><b>Dropped from collection.</b> ' +
        esc(attrs.error || '') + '<span class="hint">This box is drawn from the ' +
        'inventory: nothing was read from this device. Fix its record in the ' +
        'Devices tab.</span></div>'
      : '') +
    (attrs.loopback
      ? '<div class="kv"><span>Loopback</span><span title="The router identity in the ' +
        'topology, unique by construction. Origin: ' + esc(attrs.loopback_source || '?') +
        '"><code>' + esc(attrs.loopback) + '</code>' +
        (attrs.loopback_source && attrs.loopback_source !== 'declare'
          ? ' <span class="badge warn">inferred</span>' : '') +
        '</span></div>'
      : (attrs.managed
        ? '<div class="kv"><span>Loopback</span><span class="na" title="Without a loopback, ' +
          'this router identity falls back to its MAC and interface addresses, ' +
          'which are less reliable. Declare it in its record.">not found</span></div>'
        : '')) +
    ((node.addresses && node.addresses.length)
      ? '<div class="kv"><span>Address(es)</span><span>' + esc(node.addresses.join(', ')) + '</span></div>'
      : (node.address ? '<div class="kv"><span>Address</span><span>' + esc(node.address) + '</span></div>' : '')) +
    (attrs.serial
      ? '<div class="kv"><span>Serial no.</span><span>' + esc(attrs.serial) + '</span></div>' : '') +
    // QUOI a ete replie, pas seulement COMBIEN. Un compte seul ne permet pas de
    // juger : "4 vues reconciliees" est parfaitement normal pour un equipement
    // vu par quatre ports, et parfaitement faux pour quatre equipements
    // distincts qu'on vient de confondre. Les nommer laisse trancher.
    (node.merged_count > 1
      ? '<div class="kv"><span>Merge</span><span title="Observations folded into this ' +
        'single box.">' + esc(node.merged_count) + ' reconciled views</span></div>' +
        '<div class="notice" style="margin:.5rem 0"><b>This box groups ' +
        esc(node.merged_count) + ' observations:</b>' +
        '<ul style="margin:.35rem 0 0;padding-left:1.1rem">' +
        (node.members || []).map((k) => '<li><code>' + esc(k) + '</code></li>').join('') +
        '</ul><span class="hint">Same device seen on several ports or under ' +
        'several addresses: that is normal. <b>Different</b> devices: ' +
        'reconciliation got it wrong, and this box absorbs links that are not ' +
        'its own. Declare a distinct loopback for each one under ' +
        '<b>Devices</b> — that is what tells them apart.</span></div>'
      : '') +
    (node.platform ? '<div class="kv"><span>Platform</span><span>' + esc(topoTrim(node.platform, 18)) + '</span></div>' : '') +
    '<div class="kv"><span>Parent</span><span>' + esc(parent ? topoTrim(parent.name, 16) : 'root') +
      (node.parent_override ? ' *' : '') +
      // D'ou vient ce rattachement : pose a la main, PROUVE par la table de
      // routage, ou seulement deduit du graphe. L'operateur doit pouvoir faire
      // la difference avant de s'y fier.
      (node.parent_override
        ? ' <span class="badge">by hand</span>'
        : (node.config_parent && parent && node.config_parent === parent.key
          ? ' <span class="badge ok" title="Its default route exits towards this node' +
            (attrs.config_parent_via ? ', via ' + esc(attrs.config_parent_via) : '') +
            '">route</span>'
          : (parent ? ' <span class="badge warn" title="Inferred from the graph, for lack of ' +
            'a usable default route">inferred</span>' : ''))) +
      '</span></div>' +
    '<div class="kv"><span>Seen</span><span>' + (node.fresh ? 'recently' : 'long ago') + '</span></div>' +
    // Rattachement INCERTAIN (vu via un segment partage, pas prouve
    // point-a-point) : on le signale et on offre de le confirmer/verrouiller.
    (node.edge && node.edge.uncertain && parent
      ? '<div class="notice" style="margin:.5rem 0"><b>Probable</b> attachment to <b>' +
        esc(topoTrim(parent.name, 18)) + '</b>, seen over a shared segment (switch / management ' +
        'VLAN) — not a proven direct adjacency. ' +
        '<button class="sm ghost" data-attach="' + esc(parent.key) + '">Confirm</button>' +
        '<span class="hint">Confirming locks this parent; or drag the box under the right ' +
        'one. A dashed line means an uncertain link.</span></div>'
      : '') +
    // Noeud vraiment orphelin (aucun lien) : propose ses candidats de segment.
    (!node.parentKey && node.unsureParents && node.unsureParents.length
      ? '<div class="notice" style="margin:.5rem 0">No reliable direct link. Seen over a ' +
        'shared segment towards: ' +
        node.unsureParents.map((c) =>
          '<button class="sm ghost" data-attach="' + esc(c.key) + '">' +
          esc(topoTrim(c.name, 18)) + '</button>').join(' ') +
        '<span class="hint">Click to attach by hand.</span></div>'
      : '') +
    '<div class="stack field"><label>Role</label>' +
      '<select id="topo-kind">' + KIND_ORDER.map((k) =>
        '<option value="' + k + '"' + (k === node.kind ? ' selected' : '') + '>' +
        esc(KIND_LABEL[k]) + '</option>').join('') + '</select></div>' +
    // Fusion manuelle : le dernier mot quand l'app n'a pas pu prouver que deux
    // cases sont le meme routeur (nom generique, pas de MAC commune).
    '<div class="stack field"><label>Same device as…</label>' +
      '<select id="topo-merge-target"><option value="">— merge this box into —</option>' +
      topoOtherNodes(node.key).map((o) =>
        '<option value="' + esc(o.key) + '">' + esc(topoTrim(o.name, 24)) +
        ' · ' + esc(KIND_LABEL[o.kind] || '?') + '</option>').join('') +
      '</select></div>' +
    (node.manual_aliases && node.manual_aliases.length
      ? '<div class="kv"><span>Manual merges</span><span class="topo-unmerge">' +
        node.manual_aliases.map((a) =>
          '<button class="sm ghost" data-unmerge="' + esc(a) + '" title="' + esc(a) +
          '">Split ' + esc(topoTrim(a, 16)) + '</button>').join(' ') + '</span></div>'
      : '') +
    '<div class="actions" style="margin-top:.7rem">' +
      '<button class="sm" id="topo-merge">Merge</button>' +
      (node.parent_override
        ? '<button class="sm" id="topo-detach">Auto attachment</button>' : '') +
      '<button class="sm" id="topo-hide">Masquer</button>' +
    '</div>';

  document.getElementById('topo-kind').addEventListener('change', async (e) => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '?kind=' + e.target.value,
        { method: 'PATCH' });
      await loadNetwork();
    } catch (err) { alert(err.message); }
  });
  document.getElementById('topo-merge').addEventListener('click', async () => {
    const cible = document.getElementById('topo-merge-target').value;
    if (!cible) { alert('Choose the box to merge this one into.'); return; }
    try {
      await api('/topology/merge', { method: 'POST',
        body: JSON.stringify({ alias_key: node.key, canonical_key: cible }) });
      topo.selected = cible;  // la case fusionnee disparait : on suit la canonique
      await loadNetwork();
    } catch (err) { alert(err.message); }
  });
  host.querySelectorAll('[data-unmerge]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await api('/topology/merge/' + encodeURIComponent(b.dataset.unmerge), { method: 'DELETE' });
      await loadNetwork();
    } catch (err) { alert(err.message); }
  }));
  host.querySelectorAll('[data-attach]').forEach((b) => b.addEventListener('click', async () => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '/parent',
        { method: 'PATCH', body: JSON.stringify({ parent_key: b.dataset.attach }) });
      await loadNetwork();
    } catch (err) { alert(err.message); }
  }));
  host.querySelectorAll('[data-merge-into]').forEach((b) => b.addEventListener('click', async () => {
    // On replie l'autre case (alias) dans celle que l'operateur regarde (canonique).
    try {
      await api('/topology/merge', { method: 'POST',
        body: JSON.stringify({ alias_key: b.dataset.mergeInto, canonical_key: node.key }) });
      await loadNetwork();
    } catch (err) { alert(err.message); }
  }));
  const detach = document.getElementById('topo-detach');
  if (detach) detach.addEventListener('click', async () => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '/parent',
        { method: 'PATCH', body: JSON.stringify({ parent_key: null }) });
      await loadNetwork();
    } catch (err) { alert(err.message); }
  });
  document.getElementById('topo-hide').addEventListener('click', async () => {
    try {
      await api('/topology/nodes/' + encodeURIComponent(node.key) + '/visibility',
        { method: 'PATCH', body: JSON.stringify({ hidden: true }) });
      topo.selected = null;
      await loadNetwork();
    } catch (err) { alert(err.message); }
  });
}

/** Remet toute la disposition en automatique : efface positions ET
 *  rattachements forces, sur chaque case. */
async function resetTopoLayout() {
  if (!topo.data || !confirm('Restore the automatic layout?\n\n' +
    'Hand-placed positions and attachments will be erased.')) return;
  // Meme geste, meme promesse : l'echelle repart elle aussi sur le cadrage
  // automatique, comme a la premiere ouverture.
  topo.ajuste = false;
  const remiseAZero = (cle) => api('/topology/nodes/' + encodeURIComponent(cle) + '/layout',
    { method: 'PATCH', body: JSON.stringify({ x: null, y: null }) });
  try {
    await Promise.all((topo.data.nodes || []).map((n) => Promise.all([
      remiseAZero(n.key),
      n.parent_override
        ? api('/topology/nodes/' + encodeURIComponent(n.key) + '/parent',
            { method: 'PATCH', body: JSON.stringify({ parent_key: null }) })
        : Promise.resolve(),
    ])));
    // Les cases d'abonnes aussi : elles ne sont pas dans 'nodes' (elles n'ont
    // pas d'equipement derriere elles), et les oublier aurait laisse une
    // disposition "automatique" ou les clients restaient ranges a la main.
    await Promise.all(Object.keys(topo.data.layout || {}).map(remiseAZero));
    // refresh() et non loadTopology() : c'est la vue ACTIVE qu'il faut
    // redessiner, sinon l'arbre garde a l'ecran des positions qui n'existent
    // plus en base.
    await refresh();
  } catch (err) { alert(err.message); }
}

/** Retire de l'arbre ce qu'aucune decouverte ne revoit depuis un moment.
 *
 *  Le graphe n'efface jamais rien tout seul, pour qu'un equipement
 *  momentanement invisible -- fade radio, redemarrage, lecture en echec -- ne
 *  disparaisse pas. Le revers : une adresse de gestion changee, un lien de test
 *  demonte ou un voisin croise pendant une migration y restent pour toujours.
 *  Il n'existait aucun geste pour les retirer, sinon masquer les cases une par
 *  une. En voici un, explicite et borne. */
async function forgetStaleNodes() {
  const heures = prompt(
    'Forget the devices discovery no longer sees.\n\n' +
    'For how many HOURS must a device have been gone before it is ' +
    'removed from the tree?\n\n' +
    'Routers from your inventory are never affected, even when ' +
    'unreachable: their box is declared, not discovered. Nor are links placed ' +
    'by hand. Whatever still exists comes back on the next discovery.',
    '24');
  if (heures === null) return;
  const minutes = Math.round(Number(heures) * 60);
  if (!Number.isFinite(minutes) || minutes < 5) {
    alert('Invalid duration: at least 5 minutes (0.1 hour).');
    return;
  }
  try {
    const r = await api('/topology/forget-stale?confirm=true&older_than_minutes=' + minutes,
      { method: 'POST' });
    // Le redessin AVANT le message : loadNetwork reecrit #topo-notice avec les
    // remarques de la derniere decouverte, et effacerait le compte-rendu.
    await loadNetwork();
    const notice = document.getElementById('topo-notice');
    if (notice) {
      notice.insertAdjacentHTML('afterbegin',
        '<div class="notice ok"><b>' + esc(r.forgotten_nodes) +
        ' box(es) and ' + esc(r.forgotten_links) + ' link(s) forgotten.</b>' +
        '<span class="hint">' + esc(r.detail) + '</span></div>');
    }
  } catch (err) { alert(err.message); }
}

/** Debit mesure d'un lien, dans le sens du tableau : la fleche part de la
 *  colonne "Depuis" et va vers la colonne "Vers". Aucune heuristique ici, on
 *  montre les compteurs tels que le routeur les tient. */
function linkRates(l) {
  if (l.rx_bps === null && l.tx_bps === null) {
    return '<span style="color:var(--faint)" title="No usable counter for this link: ' +
      'either it comes from UISP with no local port, or the first sample ' +
      'has not had a second reading yet.">no measurement</span>';
  }
  const perime = l.measure_fresh === false;
  return '<span class="d" title="The router sends towards ' + esc(l.target_name || '?') + '">&rarr; ' +
      esc(bpsText(l.tx_bps || 0)) + '</span> ' +
    '<span class="u" title="The router receives from ' + esc(l.target_name || '?') + '">&larr; ' +
      esc(bpsText(l.rx_bps || 0)) + '</span>' +
    (perime ? ' <span class="badge warn" title="Last sample: ' +
      esc(clock(l.measured_at)) + '">perime</span>' : '');
}

/** Charge du port dans sa direction la plus chargee : c'est celle-la qui sature
 *  en premier, une moyenne des deux sens masquerait un lien deja plein. */
function linkLoad(l) {
  const plafond = (l.port_capacity_mbps || l.capacity_mbps || 0) * 1e6;
  if (!plafond || (l.rx_bps === null && l.tx_bps === null)) return '';
  return meter(Math.max(l.rx_bps || 0, l.tx_bps || 0), plafond);
}

function renderTopologyLinks(allLinks, allNodes) {
  const host = document.getElementById('topo-links');
  const interroges = topoInterroges(allNodes);
  const fusions = topoFusions(allNodes);
  // Meme filtre que le canvas : "liens a debit seulement" masque le bruit des
  // adjacences sans compteur (radio UISP sans port, seconde lecture en attente).
  const hasRate = (l) => l.rx_bps !== null || l.tx_bps !== null;
  // UN CABLE, UNE LIGNE. Deux routeurs geres relies par un cable se voient
  // mutuellement : la decouverte produit donc deux liens pour un seul cable, et
  // le second porte 'mirror_of'. On l'ecarte de ce tableau -- le port d'en face
  // est montre sur la ligne qui reste, colonne Interface.
  const visibles = allLinks.filter((l) => !topoAttrs(l).mirror_of);
  const links = topo.rateOnly ? visibles.filter(hasRate) : visibles;
  const compte = document.getElementById('topo-links-count');
  // DIRE CE QUI EST MASQUE. Le filtre "liens a debit seulement" est actif par
  // defaut : un lien decouvert mais dont les compteurs ne sont pas encore lus
  // disparaissait du tableau sans laisser de trace, et le compte affiche
  // paraissait etre le total.
  const masques = visibles.length - links.length;
  if (compte) {
    compte.textContent = links.length + ' link(s)' +
      (masques > 0 ? ' · ' + masques + ' with no measured rate, hidden' : '');
  }
  if (!links.length) {
    host.innerHTML = '<div class="empty">' +
      (allLinks.length && topo.rateOnly
        ? 'No link with a measured rate. Untick "Measured links only" ' +
          'to see adjacencies without counters.'
        : 'No link.') + '</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>From</th><th>Interface</th><th>To</th><th>Type</th>' +
    '<th class="num">Measured rate</th><th>Load</th>' +
    '<th class="num">Capacity</th><th class="num">Forced rate</th><th></th></tr></thead><tbody>' +
    links.map((l) => {
      const impose = l.max_down_mbps || l.max_up_mbps;
      const partage = (l.interface_links || 0) > 1;
      const attrs = topoAttrs(l);
      return '<tr>' +
        '<td>' + esc(l.source_name || l.source_key) + '</td>' +
        '<td class="login">' + esc(l.interface || '-') +
          // Port d'en face : le cable est vu des deux cotes, on garde les deux
          // noms plutot que d'en perdre un en repliant les doublons.
          (attrs.peer_interface
            ? ' <span style="color:var(--faint)" title="Port of ' +
              esc(attrs.peer_router || 'the device across') +
              ', at the other end of the same cable.">&#8596; ' +
              esc(attrs.peer_interface) + '</span>'
            : '') +
          (partage ? ' <span class="badge warn" title="' + esc(l.interface_links) +
            ' neighbours on this port: the rate is the port\'s, not this one neighbour\'s.">' +
            'shared</span>' : '') + '</td>' +
        '<td>' + esc(l.target_name || l.target_key) +
          ' <span class="badge">' + esc(KIND_LABEL[l.target_kind] || '?') + '</span>' +
          // UN CABLE ENTRE DEUX ROUTEURS INTERROGES N'A QU'UNE LIGNE : celle du
          // bout canonique. Sans ce badge, le routeur d'en face n'apparaissait
          // nulle part dans la colonne "Depuis" et semblait ne pas etre lu --
          // dans un reseau en etoile, tous les PoPs disparaissaient ainsi
          // derriere le coeur, et l'operateur concluait qu'un seul routeur
          // etait detecte.
          (interroges.has(l.target_key)
            ? ' <span class="badge ok" title="This router is polled by API. ' +
              'The cable opposite is seen from both ends and counts as one row.">' +
              'polled</span>'
            : '') +
          // Plusieurs lignes vers un meme nom : equipement joignable par
          // plusieurs chemins, ou fusion abusive de la reconciliation ? Ce
          // badge donne de quoi trancher, en nommant ce qui a ete replie.
          (fusions.has(l.target_key)
            ? ' <span class="badge warn" title="This box groups ' +
              esc(fusions.get(l.target_key).compte) + ' observations reconciled into one ' +
              'single device:&#10;' +
              esc(fusions.get(l.target_key).membres.join('\n')) +
              '&#10;&#10;If these are DIFFERENT devices, the merge is wrong: ' +
              'open the box in the Network tree tab to undo it.">' +
              esc(fusions.get(l.target_key).compte) + ' views</span>'
            : '') + '</td>' +
        '<td>' + esc(l.kind) + '</td>' +
        '<td class="num">' + linkRates(l) + '</td>' +
        '<td style="min-width:120px">' + linkLoad(l) + '</td>' +
        '<td class="num">' + (l.capacity_mbps ? esc(mbps(l.capacity_mbps)) : '-') + '</td>' +
        '<td class="num">' + (impose
          ? '<span style="color:var(--warn)">' +
            esc(mbps(l.max_down_mbps || 0) + ' / ' + mbps(l.max_up_mbps || 0)) + '</span>'
          : '<span style="color:var(--faint)">auto</span>') + '</td>' +
        '<td><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm" data-link-detail="' + esc(l.key) + '">Rate</button>' +
          '<button class="sm" data-edit-link="' + esc(l.key) + '">Bandwidth</button>' +
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
      '<button class="sm" id="drawer-close">Close</button></div>' +
      '<div class="grid stats" style="margin-bottom:1rem">' +
        statCard('', 'To ' + voisin, bpsText(l.tx_bps || 0), '',
          esc(l.interface || '') + (l.running === false ? ' &middot; port down' : '')) +
        statCard('', 'From ' + voisin, bpsText(l.rx_bps || 0), '',
          l.measured_at ? 'sampled ' + esc(clock(l.measured_at)) : 'never sampled') +
        statCard('', 'Port capacity', plafond ? bpsText(plafond) : '-', '',
          plafond
            ? 'load ' + Math.round(pct(Math.max(l.rx_bps || 0, l.tx_bps || 0), plafond)) + ' %'
            : 'port capacity unknown') +
        statCard('', 'Peak over the window', pointe ? bpsText(pointe) : '-', '',
          data.series.length + ' point(s)') +
      '</div>' +
      '<div class="notice"><b>Where this figure comes from.</b> ' + esc(data.measurement.note) +
        '<span class="hint">rx and tx are the router\'s: &rarr; it sends towards ' +
        esc(voisin) + ', &larr; it receives from ' + esc(voisin) + '.</span></div>' +
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
      { labels: { down: 'To ' + voisin, up: 'From ' + voisin, extra: null } },
    );
  } catch (err) {
    root.querySelector('.drawer').innerHTML =
      '<div class="drawer-head"><h3>Error</h3><button class="sm" id="drawer-close">Close</button></div>' +
      '<div class="notice err">' + esc(err.message) + '</div>';
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  }
}

/** Demande au routeur le debit qu'il mesure a l'instant. Lecture pure :
 *  /interface/monitor-traffic ne modifie aucune configuration. */
async function measureLink(key) {
  const host = document.getElementById('link-live-result');
  const bouton = document.getElementById('link-live');
  if (bouton) { bouton.disabled = true; bouton.textContent = 'Measuring...'; }
  try {
    const m = await api('/topology/links/' + encodeURIComponent(key) + '/live');
    const direct = m.source === 'monitor-traffic';
    const html = '<div class="notice' + (direct ? ' ok' : '') + '">' +
      '<b>' + (direct ? 'Instant sample' : 'Last collected sample') + '</b> ' +
      '<span style="color:var(--faint)">' + esc(clock(m.measured_at)) + '</span> &middot; ' +
      '&rarr; ' + esc(bpsText(m.tx_bps || 0)) + ' &middot; &larr; ' + esc(bpsText(m.rx_bps || 0)) +
      (direct ? '<span class="hint">Read just now from ' + esc(m.router_name || '?') + ' ' +
        'via /interface/monitor-traffic (read-only).</span>'
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
    ? (cible.target_name || cible.interface) : cible.login;
  const cle = scope === 'link' ? cible.key : cible.login;
  const capacite = cible.capacity_mbps;

  // Relire la surcharge en place : ouvrir sur des champs vides laisserait
  // croire qu'aucun plafond n'est pose.
  let actuelle = {};
  try {
    const politiques = await api('/shaping/policies?scope=' + scope);
    actuelle = politiques.find((p) => p.target_key === cle) || {};
  } catch (err) {
    console.warn('Policy not re-read:', err);
  }

  // Pre-remplir dans l'unite la plus lisible : 0.512 Mbps s'affiche 512 kbps.
  const dep = bestUnit(actuelle.max_down_mbps);
  const mon = bestUnit(actuelle.max_up_mbps);

  const root = document.getElementById('drawer-root');
  root.innerHTML = '<div class="drawer-backdrop"></div><div class="drawer">' +
    '<div class="drawer-head"><h3>' + esc(nom) + '</h3>' +
    '<button class="sm" id="drawer-close">Close</button></div>' +
    (capacite ? '<div class="notice">Measured capacity: <strong>' + esc(mbps(capacite)) +
      '</strong><span class="hint">The forced rate should stay under this value: ' +
      'that is what makes the queue build inside CAKE, where it is controlled, ' +
      'rather than in the radio buffer.</span></div>' : '') +
    // Sur quoi la file sera reellement accrochee : l'exploitant doit pouvoir
    // relier ce qu'il saisit ici a la ligne qu'il verra dans /queue/simple.
    (scope === 'subscriber'
      ? (cible.last_ip
          ? '<div class="notice">The queue will target <code>' + esc(cible.last_ip) +
            '/32</code><span class="hint">That is the address of the current session, ' +
            're-read from the router when the plan is built. It is rewritten on its own ' +
            'if the subscriber reconnects with another IP.</span></div>'
          : '<div class="notice warn">No address known for this subscriber.' +
            '<span class="hint">The limit is saved, but no queue will be ' +
            'written until a session is open: writing a queue on an ' +
            'old address would throttle whoever picked it up in the meantime.</span>' +
            '</div>')
      : '') +
    '<form class="stack" id="bw-form">' +
      '<div class="row-2">' +
        '<div class="field"><label for="bw-down">Download</label>' +
          '<div style="display:flex;gap:.4rem">' +
            '<input id="bw-down" type="number" min="0" step="any" value="' +
            esc(dep.value) + '" placeholder="auto (plan or measured capacity)">' +
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
        '" placeholder="why this cap (optional)"></div>' +
      '<div id="bw-result"></div>' +
      '<div class="actions">' +
        '<button type="submit" class="primary">Save</button>' +
        '<button type="button" id="bw-clear">Back to auto</button>' +
      '</div>' +
    '</form>' +
    '<p class="empty" style="text-align:left;padding:.8rem 0 0">' +
      'Saving WRITES the cap on the router immediately. The exact result ' +
      'of the write is shown here: if nothing could be sent ' +
      '(enforcement off, subscriber offline), it says so.</p>' +
    '</div>';

  root.querySelector('.drawer-backdrop').addEventListener('click', closeDrawer);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);

  document.getElementById('bw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const reponse = await api('/shaping/policies', {
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
      document.getElementById('bw-result').innerHTML = poseText(reponse.enforcement);
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
    '<div class="drawer-head"><h3>Boost &middot; ' + esc(abonne.login) + '</h3>' +
    '<button class="sm" id="drawer-close">Close</button></div>' +

    '<div class="notice">Current plan: <strong>' +
      esc(mbps(planDown)) + ' / ' + esc(mbps(planUp)) + '</strong>' +
      '<span class="hint">A boost overrides the plan and any permanent ' +
      'override, then clears itself when it expires.</span></div>' +

    '<form class="stack" id="boost-form">' +
      '<div class="field"><label>Duration</label>' +
        '<div class="boost-choices" id="boost-durations">' +
        DUREES.map((d, i) => '<button type="button" data-minutes="' + d.minutes + '"' +
          (i === 1 ? ' class="active"' : '') + '>' + esc(d.label) + '</button>').join('') +
        '</div>' +
        '<input id="boost-minutes" type="number" min="1" max="10080" value="60" ' +
          'style="margin-top:.4rem" aria-label="duration in minutes">' +
        '<span class="help">in minutes</span></div>' +

      '<div class="field"><label>Rate</label>' +
        '<div class="boost-choices" id="boost-factors">' +
        FACTEURS.map((f) => '<button type="button" data-mult="' + f + '">x' + f +
          (planDown ? ' (' + esc(mbps(planDown * f)) + ')' : '') + '</button>').join('') +
        '</div></div>' +

      '<div class="row-2">' +
        '<div class="field"><label for="boost-down">Download</label>' +
          '<div style="display:flex;gap:.4rem">' +
            '<input id="boost-down" type="number" min="0" step="any" placeholder="unchanged">' +
            unitSelect('boost-down-unit', 'mbps') +
          '</div></div>' +
        '<div class="field"><label for="boost-up">Upload</label>' +
          '<div style="display:flex;gap:.4rem">' +
            '<input id="boost-up" type="number" min="0" step="any" placeholder="unchanged">' +
            unitSelect('boost-up-unit', 'mbps') +
          '</div></div>' +
      '</div>' +

      '<div class="field"><label for="boost-reason">Reason</label>' +
        '<input id="boost-reason" placeholder="goodwill gesture, troubleshooting... (optional)"></div>' +

      '<div id="boost-result"></div>' +
      '<div class="actions">' +
        '<button type="submit" class="primary">Start the boost</button>' +
        '<button type="button" id="boost-clear" class="danger">Remove the running boost</button>' +
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
        '<div class="notice err">Choose a factor or enter a rate.</div>';
      return;
    }
    try {
      const r = await api('/shaping/boosts', {
        method: 'POST',
        body: JSON.stringify({
          login: abonne.login,
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
        '<strong>Boost active until ' +
        esc(new Date(r.boost.expires_at).toLocaleString('en-GB')) + '.</strong>' +
        '<span class="hint">' +
        (applique.ok === false
          ? esc(applique.detail || '')
          : (applique.applied || 0) + ' command(s) pushed to the router.') +
        '</span></div>';
      await refresh();
    } catch (err) {
      document.getElementById('boost-result').innerHTML =
        '<div class="notice err">' + esc(err.message) + '</div>';
    }
  });

  document.getElementById('boost-clear').addEventListener('click', async () => {
    try {
      await api('/shaping/boosts/' + encodeURIComponent(abonne.login), { method: 'DELETE' });
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
    select.innerHTML = '<option value="">All PoPs</option>' + inventaire.routers
      .map((r) => '<option value="' + esc(r.name) + '">' + esc(r.name) + '</option>').join('');
  }
  await refreshEnforcement();
  await loadPoints();
  // PAS d'await : la verification des plafonds lit chaque routeur. Attendue
  // ici, un PoP lent gelait la vue entiere -- et le verrou de rafraichissement
  // avec elle, donc la page ne repartait plus jamais.
  loadLimits();
  // Le journal n'est lu que si le detail technique est ouvert : c'est une
  // lecture de plus pour une question qu'on ne se pose pas tous les jours.
  if (document.getElementById('shaping-technique').open) await loadAudit();
}

/** Etats d'un point de shaping, et ce qu'ils veulent dire sur le reseau. */
const POINT_ETATS = {
  'file-posee': ['ok', 'throttled'],
  'file-a-poser': ['warn', 'pending'],
  'ecarte': ['warn', 'no queue'],
  'conflit': ['crit', 'conflict'],
  'posee-a-la-main': ['', 'manual queue'],
};

function pointBadge(etat) {
  const [classe, libelle] = POINT_ETATS[etat] || ['', etat || '?'];
  return '<span class="badge ' + classe + '">' + esc(libelle) + '</span>';
}

function pointDebit(point) {
  if (point.limit) return esc(point.limit);
  const bas = point.down_mbps, haut = point.up_mbps;
  if (bas === null && haut === null) return '<span class="hint">-</span>';
  const fmt = (v) => (v === null || v === undefined ? '0' : mbps(v));
  return fmt(bas) + ' &darr; / ' + fmt(haut) + ' &uarr;';
}

/** Une ligne de la carte, et ses enfants en dessous.
 *
 *  L'arbre est celui que RouterOS applique : un abonne pend sous le lien qu'il
 *  traverse, et partage donc son plafond avec ses voisins. L'afficher a plat
 *  obligerait a le reconstruire de tete. */
function renderPoint(point, profondeur) {
  const decalage = 'padding-left:' + (profondeur * 1.1 + 0.2) + 'rem';
  const nature = point.kind === 'lien'
    ? '<span class="badge">link</span>'
    : (point.detail && point.detail.nature === 'static'
      ? '<span class="badge">static-IP client</span>'
      : '<span class="badge">subscriber</span>');
  const cible = point.target
    ? '<code>' + esc(point.target) + '</code>'
    : '<span class="hint">no target</span>';
  // Le motif s'affiche pour ce qui ne bride PAS et ne bridera pas tout seul :
  // c'est l'information qu'on est venu chercher, et une infobulle la cacherait.
  // "A poser" n'en a pas besoin -- le bandeau du haut dit deja que la boucle
  // passe, et le repeter sur chaque ligne noierait les deux qui comptent.
  const motif = (point.state === 'ecarte' || point.state === 'conflit')
    ? '<span class="hint">' + esc(point.reason || '') + '</span>' : '';
  const ligne =
    '<tr>' +
    '<td style="' + decalage + '">' + (profondeur ? '<span class="hint">&#8627; </span>' : '') +
      '<b>' + esc(point.label) + '</b> ' + nature + motif + '</td>' +
    '<td>' + cible + '</td>' +
    '<td class="num">' + pointDebit(point) + '</td>' +
    '<td>' + esc(point.source || '') + '</td>' +
    '<td title="' + esc(point.reason || '') + '">' + pointBadge(point.state) + '</td>' +
    '</tr>';
  return ligne + (point.children || []).map((e) => renderPoint(e, profondeur + 1)).join('');
}

/** La carte du shaping : ou ca bride sur le reseau, et a combien.
 *
 *  Les commandes partent toutes seules (boucle de reconciliation) : cette page
 *  ne fait que regarder. Le bandeau du haut dit quand la derniere passe a eu
 *  lieu -- sans lui, une page qui n'a aucun bouton se lit comme une page qui ne
 *  fait rien. */
/** Verifie SUR LE ROUTEUR que chaque plafond decide s'applique vraiment.
 *
 *  La carte du shaping repond a "ou ca bride". Ce panneau repond a la question
 *  qui vient juste apres, et qu'aucun ecran ne posait : "est-ce que ca bride
 *  VRAIMENT ?". Trois etats etaient jusqu'ici confondus -- ce que le controleur
 *  veut poser, ce qu'il a ecrit, ce que le reseau applique. Le troisieme est le
 *  seul que l'abonne ressente. */
/** Rend lisible un ``max-limit`` RouterOS ("100000/100000", "100M/500M").
 *
 *  RouterOS ecrit MONTANT/DESCENDANT, du point de vue de la cible. Laisser les
 *  bits par seconde bruts oblige a compter les zeros pour repondre a "est-ce
 *  que c'est le bon chiffre ?" -- exactement la question posee ici. */
function maxLimitText(brut) {
  if (brut === null || brut === undefined || brut === '') return '-';
  const morceaux = String(brut).split('/');
  if (morceaux.length !== 2) return esc(String(brut));
  const lire = (m) => {
    const texte = String(m).trim().toLowerCase();
    const mult = { k: 1e3, m: 1e6, g: 1e9 }[texte.slice(-1)];
    const n = mult ? parseFloat(texte) * mult : parseFloat(texte);
    return Number.isFinite(n) ? n : null;
  };
  const up = lire(morceaux[0]);
  const down = lire(morceaux[1]);
  if (up === null || down === null) return esc(String(brut));
  const mot = (n) => (n > 0 ? bpsText(n) : 'illimite');
  return '<span title="' + esc(String(brut)) + '">&uarr; ' + esc(mot(up)) +
    ' &nbsp;&darr; ' + esc(mot(down)) + '</span>';
}

async function loadLimits() {
  const host = document.getElementById('shaping-limits');
  if (!host) return;
  const routeur = document.getElementById('shaping-router').value;
  if (!host.innerHTML) {
    host.innerHTML = '<div class="empty">Checking on the routers...</div>';
  }
  let data;
  try {
    data = await api('/shaping/limits' + (routeur ? '?router=' + encodeURIComponent(routeur) : ''));
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }

  const total = (data.enforced || 0) + (data.leaking || 0);
  const sansPlafond = (data.routers || []).reduce((a, rt) => a + (rt.uncapped || 0), 0);
  // Une file a 0/0 ne borne rien : il n'y a RIEN a tenir. La compter parmi les
  // plafonds non tenus gonflait l'alarme d'un site neuf et noyait la seule
  // ligne qui comptait.
  const note = sansPlafond
    ? '<span class="hint">' + sansPlafond + ' queue(s) carry no cap ' +
      '(link capacity unknown): they are counted on neither side of ' +
      'the tally.</span>'
    : '';
  const entete = data.leaking
    ? '<div class="notice err"><strong>' + data.leaking + ' cap(s) out of ' + total +
      ' are NOT held by the network.</strong><span class="hint">A queue that exists and ' +
      'carries the right rate may throttle nothing: that is what this table goes and checks, ' +
      'straight on the router.</span>' + note + '</div>'
    : (total
        ? '<div class="notice ok"><strong>All ' + total + ' decided caps are held by ' +
          'the network.</strong><span class="hint">Checked queue by queue on the router: ' +
          'rate matching, queue enabled, not shadowed, and no fasttrack to bypass it.' +
          '</span>' + note + '</div>'
        : (sansPlafond
            ? '<div class="notice"><strong>No cap to hold on this scope.</strong>' +
              note + '</div>'
            : '<div class="empty">No cap to check: no queue is expected ' +
              'on this scope yet.</div>'));

  const routeurs = (data.routers || []).map((rt) => {
    if (rt.error) {
      return '<div class="notice warn"><strong>' + esc(rt.router) + '</strong>' +
        '<span class="hint">' + esc(rt.error) + '</span></div>';
    }
    const ft = rt.fasttrack || {};
    let bandeau = '';
    if (ft.active === true) {
      bandeau = '<div class="notice err"><strong>Fasttrack on: no simple queue on this ' +
        'router throttles anything.</strong><span class="hint">' + esc(ft.detail || '') +
        '</span>' + (ft.remedy ? '<span class="hint"><code>' + esc(ft.remedy) + '</code></span>'
          : '') + '</div>';
    } else if (ft.active === null) {
      bandeau = '<div class="notice warn"><strong>Fasttrack not verified.</strong>' +
        '<span class="hint">' + esc(ft.detail || '') + '</span></div>';
    }
    const fuites = (rt.queues || []).filter((q) => !q.enforced && q.verdict !== 'sans-plafond');
    const libres = (rt.queues || []).filter((q) => q.verdict === 'sans-plafond');
    const tableau = fuites.length
      ? '<div class="table-wrap"><table><thead><tr><th>Queue</th><th>Subscriber</th>' +
        '<th>Target</th><th class="num">Wanted</th><th class="num">On the router</th>' +
        '<th>Why it does not throttle</th></tr></thead><tbody>' +
        fuites.map((q) => '<tr>' +
          '<td class="login">' + esc(q.name) + '</td>' +
          '<td>' + esc(q.login || '-') + '</td>' +
          '<td>' + esc(q.target || '-') + '</td>' +
          '<td class="num">' + maxLimitText(q.wanted) + '</td>' +
          '<td class="num">' + maxLimitText(q.seen) + '</td>' +
          '<td>' + esc(q.detail || q.verdict) + '</td>' +
          '</tr>').join('') + '</tbody></table></div>'
      : '<p class="empty" style="text-align:left">Every cap on this router is held.</p>';
    // Informatif, pas une alerte : ces files existent et ne bornent rien, ce
    // qui est le comportement voulu tant que la capacite du lien est inconnue.
    const sans = libres.length
      ? '<p class="empty" style="text-align:left">' + libres.length +
        ' queue(s) with no cap: ' +
        libres.map((q) => '<code>' + esc(q.name) + '</code>').join(', ') + '. ' +
        'The capacity of these links is unknown, so nothing is bounded there. ' +
        'Declare it so they carry an envelope.</p>'
      : '';
    return '<div class="card" style="margin-bottom:.8rem">' +
      '<h3 style="margin:0 0 .4rem">' + esc(rt.router) +
      '<span class="hint" style="display:inline;font-weight:400;margin-left:.5rem">' +
      (rt.enforced || 0) + ' held, ' + (rt.leaking || 0) + ' not held' +
      (rt.uncapped ? ', ' + rt.uncapped + ' with no cap' : '') + '</span></h3>' +
      bandeau + tableau + sans + '</div>';
  }).join('');

  host.innerHTML = entete + routeurs;
}

async function loadPoints() {
  const host = document.getElementById('shaping-points');
  const routeur = document.getElementById('shaping-router').value;
  if (!host.innerHTML) host.innerHTML = '<div class="empty">Reading the routers...</div>';
  let data;
  try {
    data = await api('/shaping/points' + (routeur ? '?router=' + encodeURIComponent(routeur) : ''));
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const passe = data.last_reconcile;
  const cadence = Math.round(data.reconcile_interval_s || 0);
  let bandeau;
  if (!data.enforcement_enabled) {
    bandeau = '<div class="notice"><strong>Automatic apply idle.</strong> ' +
      'The map below is computed and kept up to date, but nothing is written while ' +
      'enforcement is off. The points marked <b>pending</b> will go out as soon as ' +
      'you flip the switch.</div>';
  } else {
    bandeau = '<div class="notice ok"><strong>Automatic apply on.</strong> ' +
      'Commands go out on their own, every ' + esc(cadence) + ' s' +
      (passe && passe.at
        ? ' &middot; last pass ' + esc(depuis(passe.at)) + ': ' +
          esc(passe.applied || 0) + ' command(s) on ' + esc((passe.routers || []).length) +
          ' router(s)'
        : ' &middot; first pass still to come') +
      ((passe && (passe.errors || []).length)
        ? '<span class="hint">' + esc(passe.errors.join(' | ')) + '</span>' : '') +
      (state.enforcementReason
        ? '<span class="hint">Writing allowed, reason: ' +
          esc(state.enforcementReason) + '</span>' : '') +
      '</div>';
  }

  const routeurs = data.routers || [];
  const corps = routeurs.map((r) => {
    if (r.error) {
      return '<div class="notice err"><strong>' + esc(r.router) + '</strong> : ' +
        esc(r.error) + '</div>';
    }
    const c = r.counts || {};
    const entete = '<h2 style="margin-top:1rem">' + esc(r.router) +
      ' <span class="hint">' + esc(r.pop_name || '') + '</span></h2>' +
      '<div class="hint" style="margin-bottom:.4rem">' +
      esc(c['file-posee'] || 0) + ' point(s) throttled &middot; ' +
      esc(c['file-a-poser'] || 0) + ' pending &middot; ' +
      esc(c['ecarte'] || 0) + ' with no queue &middot; ' +
      esc(c['posee-a-la-main'] || 0) + ' manual queue(s)' +
      ((c['conflit'] || 0) ? ' &middot; ' + esc(c['conflit']) + ' conflict(s)' : '') +
      '</div>';
    if (!(r.points || []).length) {
      return entete + '<div class="empty">No shaping point on this router.</div>';
    }
    return entete + '<div class="table-wrap"><table><thead><tr>' +
      '<th>Network point</th><th>Target</th><th class="num">Cap</th>' +
      '<th>Where the cap comes from</th><th>State</th>' +
      '</tr></thead><tbody>' +
      r.points.map((p) => renderPoint(p, 0)).join('') +
      '</tbody></table></div>';
  }).join('');

  host.innerHTML = bandeau + (corps || '<div class="empty">No router collected.</div>');
}

async function refreshEnforcement() {
  const etat = await api('/shaping/enforcement');
  const toggle = document.getElementById('enforcement-toggle');
  const label = document.getElementById('enforcement-label');

  toggle.checked = etat.enabled;
  toggle.disabled = etat.locked;
  label.textContent = etat.locked
    ? 'enforcement locked'
    : etat.enabled ? 'writing ALLOWED' : 'read-only';
  label.style.color = etat.enabled ? 'var(--warn)' : 'var(--muted)';
  document.getElementById('enforcement-switch').title = etat.locked
    ? 'ENFORCEMENT_LOCKED=true: flipping it from the interface is forbidden'
    : 'Allow or stop writing to the routers';

  // Le bandeau de la carte dit deja si l'ecriture est active et quand la boucle
  // est passee : ne reste ici que ce qu'il ne peut pas dire.
  const notice = document.getElementById('shaping-notice');
  notice.innerHTML = etat.locked
    ? '<div class="notice"><b>Writing locked</b> ' +
      '(<code>ENFORCEMENT_LOCKED=true</code>).</div>'
    : '';
  state.enforcementReason = (etat.last_change && etat.last_change.reason) || null;
}

async function toggleEnforcement(active) {
  const toggle = document.getElementById('enforcement-toggle');
  if (active && !confirm(
      'Allow writing to the routers?\n\n' +
      'From now on, applying a plan will really change their ' +
      'configuration. Only queues marked freeqos:managed are touched.')) {
    toggle.checked = false;
    return;
  }
  const motif = active
    ? (prompt('Reason (recorded in the log, optional):') || null)
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
    host.innerHTML = '<div class="empty">No command sent.</div>';
    return;
  }
  host.innerHTML =
    '<table><thead><tr><th>When</th><th>Router</th><th>Command</th>' +
    '<th>Mode</th><th>State</th></tr></thead><tbody>' +
    rows.map((r) => '<tr>' +
      '<td class="num" style="color:var(--faint)">' + esc(clock(r.ts)) + '</td>' +
      '<td>' + esc(r.router_name) + '</td>' +
      '<td class="login" style="font-size:.74rem">' + esc(r.command) + '</td>' +
      '<td>' + (r.dry_run ? '<span class="badge">dry run</span>'
        : '<span class="badge warn">applied</span>') + '</td>' +
      '<td>' + (r.ok ? '<span class="badge ok">ok</span>'
        : '<span class="badge crit" title="' + esc(r.detail || '') + '">failed</span>') + '</td>' +
      '</tr>').join('') + '</tbody></table>';
}

async function inspectShaping() {
  const routeur = document.getElementById('shaping-router').value;
  const host = document.getElementById('shaping-state');
  host.innerHTML = '<div class="notice">Reading ' + esc(routeur) + '...</div>';
  try {
    const [etats, droits] = await Promise.all([
      api('/shaping/state?router=' + encodeURIComponent(routeur)),
      api('/shaping/capability?router=' + encodeURIComponent(routeur)).catch(() => null),
    ]);
    host.innerHTML = etats.map((e) => {
      if (!e.reachable) {
        return '<div class="notice err"><strong>' + esc(e.router) + '</strong> unreachable: ' +
          esc(e.error || '') + '</div>';
      }
      let bandeauDroits = '';
      if (droits) {
        if (droits.can_write === true) {
          bandeauDroits = '<div class="notice ok">Account <code>' +
            esc(droits.username) + '</code> can write: ' + esc(droits.detail) + '</div>';
        } else if (droits.can_write === false) {
          bandeauDroits = '<div class="notice err"><strong>Account <code>' +
            esc(droits.username) + '</code> cannot write.</strong> ' +
            esc(droits.detail) +
            '<span class="hint">On the router: <code>/user/group set ' +
            '[find name=' + esc(droits.group || '&lt;group&gt;') +
            '] policy=read,write,api,test</code></span></div>';
        } else {
          bandeauDroits = '<div class="notice warn">Rights of account <code>' +
            esc(droits.username) + '</code> not verifiable: ' + esc(droits.detail) +
            '</div>';
        }
      }
      return '<div class="card" style="margin-bottom:1rem">' +
        '<div class="node-head" style="margin-bottom:.8rem">' +
          '<div class="node-title">' + esc(e.router) + '</div>' +
          '<div class="node-metrics">' +
            '<span>' + e.counts.simple_queues + ' simple queue(s)</span>' +
            '<span style="color:var(--down)">' + e.counts.managed + ' managed by freeQoS</span>' +
            '<span style="color:var(--warn)">' + e.counts.foreign + ' third-party</span>' +
          '</div></div>' +
        (e.counts.foreign
          ? '<div class="notice warn">' + e.counts.foreign + ' queue(s) do not carry the ' +
            '<code>freeqos:managed</code> marker: written by hand or by RADIUS. ' +
            'They will never be modified nor deleted.' +
            '<span class="hint">' +
            e.foreign_queues.slice(0, 8).map((q) => esc(q.name)).join(', ') +
            (e.foreign_queues.length > 8 ? '...' : '') + '</span></div>'
          : '<div class="notice ok">No third-party queue: the controller is the only one shaping ' +
            'on this router.</div>') +
        bandeauDroits +
        '</div>';
    }).join('');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}

async function computePlan() {
  const choisi = document.getElementById('shaping-router').value;
  const host = document.getElementById('shaping-plan');

  // UN PLAN EST TOUJOURS LE PLAN D'UN ROUTEUR.
  //
  // Avec "Tous les PoP", le selecteur vaut la chaine vide : on envoyait
  // router:"" et l'API refusait, en rendant son erreur de validation brute a
  // l'ecran. Ce que l'exploitant demande dans ce cas n'a pourtant rien
  // d'ambigu -- le plan de chacun -- alors on les calcule tous.
  const routeurs = choisi ? [choisi] : [...document.getElementById('shaping-router').options]
    .map((o) => o.value).filter(Boolean);
  if (!routeurs.length) {
    host.innerHTML = '<div class="notice warn">No router in the inventory: ' +
      'declare one in the Devices tab.</div>';
    return;
  }

  host.innerHTML = '<div class="notice">Computing the plan for ' +
    esc(routeurs.join(', ')) + '...</div>';
  const morceaux = [];
  for (const routeur of routeurs) {
    try {
      const plan = await api('/shaping/plan', {
        method: 'POST', body: JSON.stringify({ router: routeur }),
      });
      morceaux.push({ routeur: routeur, plan: plan });
    } catch (err) {
      morceaux.push({ routeur: routeur, erreur: err.message });
    }
  }
  host.innerHTML = '';
  morceaux.forEach((m) => {
    const bloc = document.createElement('div');
    if (!m.erreur) {
      host.appendChild(bloc);
      try {
        renderPlan(m.plan, m.routeur, bloc);
      } catch (err) {
        // Une reponse inattendue sur UN routeur ne doit pas vider le panneau
        // des autres : on dit lequel, et on continue.
        bloc.innerHTML = '<div class="notice err"><strong>' + esc(m.routeur) +
          '</strong> : reponse inattendue (' + esc(err.message) + ')</div>';
      }
      return;
    }
    bloc.innerHTML = '<div class="notice err"><strong>' + esc(m.routeur) + '</strong> : ' +
      esc(m.erreur) + '</div>';
    host.appendChild(bloc);
  });
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
    ' subscriber(s) outside the plan.</strong> This is not an error: these are the ones ' +
    'with nothing to write.' +
    [...parMotif.entries()].map(([motif, logins]) =>
      '<span class="hint"><b>' + esc(motif) + '</b> &mdash; ' +
      esc(logins.slice(0, 12).join(', ')) +
      (logins.length > 12 ? ' and ' + (logins.length - 12) + ' more' : '') +
      '</span>').join('') +
    '</div>';
}

/** Rend un plan. ``hote`` permet d'en afficher PLUSIEURS sur la meme page
 *  (un par routeur) : les boutons sont retrouves dans leur propre bloc et non
 *  par identifiant global, qui n'aurait cable que le premier plan affiche. */
function renderPlan(plan, routeur, hote) {
  const host = hote || document.getElementById('shaping-plan');
  const c = plan.counts;
  const total = c.add + c.set + c.remove;

  let html = '<h2>Plan for ' + esc(routeur) + '</h2>';

  if (plan.conflicts.length) {
    html += '<div class="notice err"><strong>' + plan.conflicts.length +
      ' name conflict(s).</strong> These queues already exist without our marker: ' +
      'they belong to somebody else and will not be touched.' +
      '<span class="hint">' + plan.conflicts.map((x) => esc(x.name)).join(', ') +
      '</span></div>';
  }

  // Un abonne absent du plan sans explication est indiscernable d'un abonne
  // correctement shape : on dit qui est ecarte et pourquoi.
  html += renderEcartes(plan.skipped);

  if (!total) {
    html += '<div class="notice ok">Nothing to do: the router configuration ' +
      'already matches the wanted state (' + plan.unchanged + ' item(s) already correct).</div>';
    host.innerHTML = html;
    return;
  }

  html += '<div class="notice">' +
    '<strong>' + total + ' command(s)</strong>: ' +
    c.add + ' add(s), ' + c.set + ' change(s), ' + c.remove + ' removal(s). ' +
    plan.unchanged + ' item(s) already correct.' +
    '<span class="hint">Nothing is sent until you apply.</span></div>';

  html += '<div class="table-wrap"><table><thead><tr><th>Action</th><th>Reason</th>' +
    '<th>RouterOS command</th></tr></thead><tbody>' +
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
    '<button data-act="simulate">Dry run</button>' +
    '<button data-act="apply" class="primary">Apply on ' + esc(routeur) + '</button>' +
    '</div><div data-act="result"></div>';

  host.innerHTML = html;
  host.querySelector('[data-act="simulate"]')
    .addEventListener('click', () => applyPlan(routeur, true, host));
  host.querySelector('[data-act="apply"]')
    .addEventListener('click', () => applyPlan(routeur, false, host));
}

async function applyPlan(routeur, dryRun, bloc) {
  if (!dryRun && !confirm(
      'Really apply on ' + routeur + '?\n\n' +
      'Commands will be sent to the router. Only queues carrying ' +
      'the freeqos:managed marker are affected.')) {
    return;
  }
  // Le compte rendu va dans le bloc DE CE PLAN : avec plusieurs plans a
  // l'ecran, un identifiant global aurait affiche le resultat du routeur B
  // sous le plan du routeur A.
  const host = (bloc && bloc.querySelector('[data-act="result"]'))
    || document.querySelector('#shaping-plan [data-act="result"]');
  if (!host) return;
  host.innerHTML = '<div class="notice">Execution...</div>';
  try {
    const reponse = await api('/shaping/apply', {
      method: 'POST',
      body: JSON.stringify({ router: routeur, dry_run: dryRun, confirm: !dryRun }),
    });
    const r = reponse.result;
    host.innerHTML = '<div class="notice ' + (r.ok ? 'ok' : 'err') + '">' +
      '<strong>' + (r.dry_run ? 'Dry run' : 'Apply') + ': ' +
      r.applied + ' succeeded, ' + r.failed + ' failed.</strong>' +
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
  try {
    const res = await fetch('/health/ready');
    const body = await res.json();
    dot.className = 'dot' + (res.ok ? '' : ' stale');
    dot.title = body.stale_jobs && body.stale_jobs.length
      ? 'Jobs running late: ' + body.stale_jobs.join(', ') : 'Cycles on time';
  } catch (err) {
    dot.className = 'dot down';
  }
}

/* -------------------------------------------------------------- routage */


/* -------------------------------------------------------------- reglages */

const GROUPE_TITRE = {
  shaping: 'Shaping', cake: 'CAKE (AQM)', enforcement: 'Write safeguards',
  cadences: 'Collection cadences', detection: 'Static-IP client detection',
  trafic: 'Traffic (NetFlow)', services: 'Services and IP location',
};

/** Controle de saisie adapte au type du reglage. Un reglage "nullable" recoit
 *  une option VIDE explicite : pour CAKE, vide ne veut pas dire "pas de valeur"
 *  mais "ne pose pas ce champ", ce qui laisse le defaut de RouterOS. */
function settingControl(r) {
  const id = 'set-' + r.name;
  const vide = r.value === null || r.value === undefined;
  if (r.kind === 'bool') {
    const opts = (r.nullable ? [['', '— (RouterOS default)']] : [])
      .concat([['true', 'Yes'], ['false', 'No']]);
    return '<select id="' + id + '">' + opts.map(([v, t]) =>
      '<option value="' + v + '"' +
      ((vide ? '' : String(r.value)) === v ? ' selected' : '') + '>' + t + '</option>').join('') +
      '</select>';
  }
  if (r.kind === 'choix') {
    const opts = (r.nullable ? [['', '— (RouterOS default)']] : [])
      .concat(r.choices.map((c) => [c, c]));
    return '<select id="' + id + '">' + opts.map(([v, t]) =>
      '<option value="' + esc(v) + '"' +
      ((vide ? '' : String(r.value)) === v ? ' selected' : '') + '>' + esc(t) + '</option>')
      .join('') + '</select>';
  }
  const pas = r.kind === 'int' ? '1' : 'any';
  return '<input type="number" id="' + id + '" step="' + pas + '"' +
    (r.minimum !== null ? ' min="' + r.minimum + '"' : '') +
    (r.maximum !== null ? ' max="' + r.maximum + '"' : '') +
    ' value="' + (vide ? '' : esc(r.value)) + '">';
}

function settingRow(r) {
  const pose = r.source === 'db';
  const badge = pose
    ? '<span class="badge ok" title="Value set here, stored in the database">database</span>'
    : '<span class="badge" title="No value set: the default applies">default</span>';
  const defaut = r.default === null || r.default === undefined ? '—' : String(r.default);
  return '<tr>' +
    '<td><code>' + esc(r.name) + '</code>' +
      '<span class="hint">' + esc(r.help) + '</span></td>' +
    '<td style="min-width:190px">' + settingControl(r) + '</td>' +
    '<td>' + badge + '<span class="hint">default: ' + esc(defaut) + '</span></td>' +
    '<td class="sticky-actions">' +
      '<button class="sm" data-set-save="' + esc(r.name) + '">Apply</button> ' +
      (pose ? '<button class="sm" data-set-reset="' + esc(r.name) +
        '">Default</button>' : '') +
    '</td></tr>';
}

async function loadSettings() {
  const body = await api('/settings');
  const host = document.getElementById('settings-groups');
  const compte = document.getElementById('settings-count');
  if (compte) {
    compte.textContent = body.from_db.length + ' setting(s) stored in the database out of ' +
      body.settings.length;
  }

  const ordre = ['shaping', 'cake', 'enforcement', 'trafic', 'cadences'];
  const groupes = Object.keys(body.groups)
    .sort((a, b) => ordre.indexOf(a) - ordre.indexOf(b));
  host.innerHTML = groupes.map((g) =>
    '<h2>' + esc(GROUPE_TITRE[g] || g) + '</h2>' +
    '<div class="table-wrap"><table><thead><tr>' +
      '<th>Setting</th><th>Value</th><th>Source</th><th></th>' +
    '</tr></thead><tbody>' +
    body.groups[g].map(settingRow).join('') +
    '</tbody></table></div>').join('');

  host.querySelectorAll('[data-set-save]').forEach((b) => {
    b.addEventListener('click', () => saveSetting(b.dataset.setSave));
  });
  host.querySelectorAll('[data-set-reset]').forEach((b) => {
    b.addEventListener('click', () => resetSetting(b.dataset.setReset));
  });

  document.getElementById('settings-bootstrap').innerHTML =
    '<table><thead><tr><th>Variable</th><th>Why it stays in the environment</th>' +
    '</tr></thead><tbody>' + body.bootstrap_only.map((e) =>
      '<tr><td><code>' + esc(e.name) + '</code></td><td>' + esc(e.why) + '</td></tr>')
      .join('') + '</tbody></table>';

  // Le shaping n'a plus d'onglet : il vit ici, replie. On ne lit les routeurs
  // que si l'exploitant ouvre le bloc -- sinon ouvrir les Reglages
  // interrogerait tout le parc pour rien.
  const bloc = document.getElementById('settings-shaping');
  if (bloc && bloc.open) await loadShaping();
}

function settingNotice(html) {
  document.getElementById('settings-notice').innerHTML = html;
}

/** Lit le controle et renvoie la valeur au bon type. Une chaine vide sur un
 *  reglage nullable devient null : "ne pose pas ce champ". */
function readSetting(name) {
  const el = document.getElementById('set-' + name);
  const brut = (el.value || '').trim();
  if (brut === '') return null;
  if (el.tagName === 'SELECT') {
    if (brut === 'true') return true;
    if (brut === 'false') return false;
    return brut;
  }
  return Number(brut);
}

async function saveSetting(name) {
  try {
    const r = await api('/settings/' + encodeURIComponent(name), {
      method: 'PUT', body: JSON.stringify({ value: readSetting(name) }),
    });
    settingNotice('<div class="notice ok"><code>' + esc(name) + '</code> = ' +
      esc(String(r.value)) + ' — applied immediately, no restart.</div>');
    await loadSettings();
  } catch (err) {
    settingNotice('<div class="notice err">' + esc(err.message) + '</div>');
  }
}

async function resetSetting(name) {
  try {
    const r = await api('/settings/' + encodeURIComponent(name), { method: 'DELETE' });
    settingNotice('<div class="notice ok"><code>' + esc(name) +
      '</code> back to its default (' + esc(String(r.value)) + ').</div>');
    await loadSettings();
  } catch (err) {
    settingNotice('<div class="notice err">' + esc(err.message) + '</div>');
  }
}


const LOADERS = {
  dashboard: loadDashboard,
  exec: loadExec,
  traffic: loadTraffic,
  network: loadNetwork,
  subscribers: loadSubscribers,
  pops: loadRouters,
  services: loadServices,
  api: loadApi,
  settings: loadSettings,
};

async function show(view) {
  if (!LOADERS[view]) view = 'dashboard';
  state.view = view;
  document.querySelectorAll('section').forEach((s) => s.classList.remove('active'));
  document.getElementById('view-' + view).classList.add('active');
  document.querySelectorAll('nav.tabs a').forEach((a) =>
    a.classList.toggle('active', a.dataset.view === view));
  // Le grand titre reprend l'intitule de l'onglet : on sait ou l'on est sans
  // chercher l'onglet allume.
  const lien = document.querySelector('nav.tabs a[data-view="' + view + '"]');
  const titre = document.getElementById('page-title');
  if (lien && titre) titre.textContent = lien.textContent.trim();
  document.title = (lien ? lien.textContent.trim() + ' · ' : '') + 'freeQoS';
  await refresh();
}

/** Un echec de chargement doit SE VOIR.
 *
 *  Il n'etait ecrit que dans la console : l'onglet restait vide, ou fige sur
 *  ses anciennes lignes, et un ecran vide se lit comme "il n'y a rien" alors
 *  qu'il faut lire "je n'ai pas pu savoir". Les deux n'appellent pas le meme
 *  geste, et le second se diagnostique en dix secondes quand il est dit. */
function appError(message) {
  const banniere = document.getElementById('app-error');
  if (!message) { banniere.hidden = true; banniere.innerHTML = ''; return; }
  banniere.hidden = false;
  banniere.innerHTML = '<div class="notice err">' +
    '<strong>This tab could not be loaded.</strong> ' + esc(message) +
    '<span class="hint">What is shown may be stale. Check ' +
    '<a href="/health" target="_blank">/health</a> and the Settings tab; if the ' +
    'problem followed an update, reload the page (Ctrl+Shift+R).</span></div>';
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    await LOADERS[state.view]();
    appError(null);
  } catch (err) {
    console.error('Rafraichissement impossible :', err);
    appError(err && err.message ? err.message : String(err));
  } finally {
    refreshing = false;
  }
}

function route() { show((location.hash || '#/dashboard').replace('#/', '')); }

/* ------------------------------------------------------------ apparence */

/** Clair, sombre, ou comme le systeme. Le choix est retenu dans ce navigateur
 *  seulement : c'est une preference de poste, pas un reglage du controleur. */
const THEMES = ['auto', 'light', 'dark'];
const THEME_LABEL = { auto: 'Auto', light: 'Light', dark: 'Dark' };

function themeActuel() {
  try {
    const t = localStorage.getItem('freeqos-theme');
    return THEMES.includes(t) ? t : 'auto';
  } catch (err) {
    return 'auto';
  }
}

function appliquerTheme(theme) {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const bouton = document.getElementById('theme-toggle');
  if (bouton) bouton.textContent = THEME_LABEL[theme];
  try { localStorage.setItem('freeqos-theme', theme); } catch (err) { /* navigation privee */ }
}

appliquerTheme(themeActuel());
document.getElementById('theme-toggle').addEventListener('click', () => {
  const suivant = THEMES[(THEMES.indexOf(themeActuel()) + 1) % THEMES.length];
  appliquerTheme(suivant);
  // Les graphiques lisent leurs couleurs au dessin : on redessine.
  refresh();
});

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

/* ------------------------------------------------------- trafic et API */
document.getElementById('flow-range').addEventListener('change', loadTraffic);
document.getElementById('flow-vantage').addEventListener('change', loadTraffic);
document.querySelectorAll('#flow-vantage-seg button').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#flow-vantage-seg button').forEach((x) =>
      x.classList.toggle('active', x === b));
    document.getElementById('flow-vantage').value = b.dataset.vantage;
    loadTraffic();
  });
});
document.getElementById('exporter-form').addEventListener('submit', declareExporter);
document.getElementById('key-form').addEventListener('submit', createApiKey);
// Les filtres de "qui parle a qui". La recherche se declenche sur 'change'
// (validation ou perte de focus) et non sur chaque frappe : une requete par
// caractere ferait autant de lectures de base qu'il y a de lettres.
document.getElementById('flow-pairs-search').addEventListener('change', (e) => {
  FLOW.search = e.target.value.trim();
  loadFlowPairs();
});
['pop', 'category'].forEach((champ) => {
  document.getElementById('flow-pairs-' + champ).addEventListener('change', (e) => {
    FLOW[champ] = e.target.value || '';
    loadFlowPairs();
  });
});
document.getElementById('flow-pairs-reset').addEventListener('click', () => {
  FLOW.pop = '';
  FLOW.category = '';
  FLOW.search = '';
  document.getElementById('flow-pairs-search').value = '';
  loadFlowPairs();
});

// Les deux blocs replies qui ont remplace les onglets Topologie et Shaping :
// on ne charge leur contenu que lorsqu'ils s'ouvrent. C'est ce qui rend leur
// disparition des onglets gratuite -- aucune lecture de plus tant que
// personne ne les regarde.
document.getElementById('net-links-block').addEventListener('toggle', (e) => {
  if (e.target.open) loadTopology();
});
document.getElementById('settings-shaping').addEventListener('toggle', (e) => {
  if (e.target.open) loadShaping();
});
document.getElementById('shaping-router').addEventListener('change', () => {
  loadPoints();
  loadLimits();
});
document.getElementById('shaping-technique').addEventListener('toggle', (e) => {
  if (e.target.open) loadAudit();
});
document.getElementById('btn-inspect').addEventListener('click', inspectShaping);
document.getElementById('enforcement-toggle').addEventListener('change', (e) => {
  toggleEnforcement(e.target.checked);
});
document.getElementById('btn-plan').addEventListener('click', computePlan);
document.getElementById('btn-discover').addEventListener('click', async (e) => {
  e.target.disabled = true;
  const notice = document.getElementById('topo-notice');
  notice.innerHTML = '<div class="notice">Reading /ip/neighbor on every PoP...</div>';
  try {
    const r = await api('/topology/discover', { method: 'POST' });
    notice.innerHTML = '<div class="notice ok">' + r.nodes + ' device(s), ' +
      r.links + ' link(s) discovered.' +
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
document.getElementById('btn-topo-zoom-in').addEventListener('click',
  () => setTopoZoom((topo.zoom || 1) + 0.1));
document.getElementById('btn-topo-zoom-out').addEventListener('click',
  () => setTopoZoom((topo.zoom || 1) - 0.1));
document.getElementById('btn-topo-fit').addEventListener('click', topoFit);
document.getElementById('btn-topo-forget').addEventListener('click', forgetStaleNodes);
document.getElementById('btn-topo-link').addEventListener('click', (e) => {
  topo.linkMode = !topo.linkMode;
  topo.linkSource = null;
  e.target.classList.toggle('primary', topo.linkMode);
  e.target.textContent = topo.linkMode ? 'Done' : 'Create a link';
  if (topo.data) renderTopoCanvas();
  setTopoLinkNotice();
});
document.getElementById('btn-build-tree').addEventListener('click', () => buildTreeFromConfig(false));
/* ------------------------------------------------------------- services */
document.getElementById('svc-range').addEventListener('change', loadServices);
document.getElementById('svc-category').addEventListener('change', loadServices);
// La recherche se declenche sur 'change' (validation ou perte de focus) et non
// sur chaque frappe : une requete par caractere ferait autant de lectures de
// base qu'il y a de lettres dans "nflxvideo".
document.getElementById('svc-search').addEventListener('change', loadServices);
document.getElementById('svc-rule-form').addEventListener('submit', submitRule);
// Les champs qui n'ont de sens que pour un effet ou une portee donnes restent
// caches tant qu'ils ne servent pas : un formulaire qui montre tout montre
// surtout ce qu'il ne faut pas remplir.
document.getElementById('svc-rule-action').addEventListener('change', (e) => {
  document.getElementById('svc-rule-limits').hidden = e.target.value !== 'limit';
});
document.getElementById('svc-rule-scope').addEventListener('change', (e) => {
  document.getElementById('svc-rule-logins-field').hidden = e.target.value !== 'subscribers';
});
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

document.getElementById('sub-kind').addEventListener('change', (e) => {
  state.subKind = e.target.value;
  loadSubscribers();
});

document.getElementById('sc-toggle').addEventListener('click', async () => {
  const panneau = document.getElementById('sc-panel');
  panneau.hidden = !panneau.hidden;
  if (panneau.hidden) return;
  scRemplirFormulaire(null);
  // Le bouton annonce « Ajouter un client » : le curseur doit etre dans le
  // premier champ, pas quelque part au-dessus d'un panneau a parcourir.
  const reference = document.getElementById('sc-reference');
  if (reference) {
    reference.focus();
    reference.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
  await Promise.all([loadStaticClients(), loadVlanClients(), loadCandidates()]);
});
document.getElementById('sc-form').addEventListener('submit', scEnregistrer);
document.getElementById('sc-candidates-block').addEventListener('toggle', (e) => {
  if (e.target.open) loadCandidates();
});
document.getElementById('sc-diag').addEventListener('click', scDiagnostic);
document.getElementById('sc-recensement').addEventListener('click', scRecensement);
document.getElementById('sc-cancel').addEventListener('click', () => scRemplirFormulaire(null));
// La liste se relit quand on ouvre le menu : un routeur ou un PoP ajoute
// depuis un autre onglet y apparait sans recharger la page.
document.querySelectorAll('select[data-pop-select]').forEach((select) => {
  select.addEventListener('focus', () => remplirMenusPop());
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
const VUES_FIGEES = new Set(['pops', 'settings']);
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
  // Trafic : ne pas ecraser un formulaire d'exporteur en cours de saisie.
  if (state.view === 'traffic' && document.activeElement &&
      document.activeElement.tagName === 'INPUT') return;
  refresh();
  // Le tiroir d'un lien suit le meme rythme : on regarde un debit justement
  // quand il bouge.
  if (state.link) openLink(state.link.key, state.link.minutes, true);
}, 10000);
setInterval(refreshHealth, 15000);
