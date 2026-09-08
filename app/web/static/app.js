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
  view: 'dashboard', rangeMinutes: 60, subSearch: '', subPop: '',
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

/* ---------------------------------------------------------- arbre reseau */

const ICONE = {
  gateway: 'GW', core: 'CORE', pop: 'POP', radio: 'RF',
  sector: 'SECT', cpe: 'CPE', unknown: '?', subscriber: 'ABO',
};

/** Etat de repli, conserve entre deux rafraichissements pour ne pas refermer
 *  une branche que l'operateur vient d'ouvrir. */
const treeCollapsed = new Set();
let treeInitialise = false;

/** Rang d'un role dans la hierarchie. Plus petit = plus en amont. */
const RANG = { gateway: 0, core: 1, pop: 2, radio: 3, sector: 3, cpe: 4, unknown: 5 };

/**
 * Construit la hierarchie reelle a partir des noeuds, des liens et des abonnes.
 *
 * La decouverte de voisinage est SYMETRIQUE : /ip/neighbor dit "ces deux
 * equipements sont adjacents", pas lequel est en amont. Un PoP qui voit son
 * gateway produit un lien PoP -> gateway, ce qui mettrait la passerelle sous
 * le PoP. On oriente donc chaque lien par le role de ses extremites.
 */
function buildTree(nodes, links, subscribers) {
  const parNoeud = new Map(nodes.map((n) => [n.key, { ...n, children: [], link: null }]));
  const cibles = new Set();

  const oriente = links.map((l) => {
    const a = parNoeud.get(l.source_key);
    const b = parNoeud.get(l.target_key);
    if (!a || !b) return l;
    const rangA = RANG[a.kind] === undefined ? 5 : RANG[a.kind];
    const rangB = RANG[b.kind] === undefined ? 5 : RANG[b.kind];
    // La cible est plus en amont que la source : on retourne le lien.
    if (rangB < rangA) {
      return { ...l, source_key: l.target_key, target_key: l.source_key, inverted: true };
    }
    return l;
  });

  oriente.forEach((l) => {
    const parent = parNoeud.get(l.source_key);
    const enfant = parNoeud.get(l.target_key);
    if (!parent || !enfant || parent === enfant) return;
    // Un equipement vu par deux PoPs ne doit pas etre duplique : on garde le
    // premier rattachement et on note l'autre comme lien secondaire.
    if (cibles.has(l.target_key)) {
      parent.children.push({ ...enfant, key: enfant.key + '@' + parent.key,
                             children: [], link: l, alias: true });
      return;
    }
    cibles.add(l.target_key);
    enfant.link = l;
    parent.children.push(enfant);
  });

  // Les abonnes sont regroupes sous un noeud repliable : les lister a plat sous
  // le PoP noierait la structure des que le parc depasse quelques dizaines.
  const parPop = new Map();
  (subscribers || []).forEach((s) => {
    if (!s.pop_name) return;
    if (!parPop.has(s.pop_name)) parPop.set(s.pop_name, []);
    parPop.get(s.pop_name).push(s);
  });
  parNoeud.forEach((n) => {
    const abonnes = parPop.get(n.name);
    if (!abonnes || !abonnes.length) return;
    const total = abonnes.reduce(
      (acc, s) => ({ tx: acc.tx + (s.tx_bps || 0), rx: acc.rx + (s.rx_bps || 0) }),
      { tx: 0, rx: 0 },
    );
    n.children.push({
      key: 'abos:' + n.key,
      name: abonnes.length + ' abonne(s)',
      kind: 'subscriber',
      group: true,
      totals: total,
      children: abonnes.map((s) => ({
        key: 'sub:' + s.pppoe_login, name: s.pppoe_login, kind: 'subscriber',
        children: [], subscriber: s,
      })),
    });
  });

  const racines = [...parNoeud.values()].filter((n) => !cibles.has(n.key));
  return racines.length ? racines : [...parNoeud.values()];
}

function renderTreeNode(node, profondeur) {
  const estFeuille = node.children.length === 0;
  const replie = treeCollapsed.has(node.key);
  const s = node.subscriber;

  let charge = '';
  let debits = '';
  if (node.group) {
    debits =
      '<span class="d">&darr; ' + esc(bpsText(node.totals.tx)) + '</span>' +
      '<span class="u">&uarr; ' + esc(bpsText(node.totals.rx)) + '</span>';
  } else if (s) {
    const plan = (s.effective_down_mbps || s.plan_down_mbps || 0) * 1e6;
    debits =
      '<span class="d">&darr; ' + esc(bpsText(s.tx_bps)) + '</span>' +
      '<span class="u">&uarr; ' + esc(bpsText(s.rx_bps)) + '</span>';
    charge = '<div class="tree-load">' + meter(s.tx_bps, plan) + '</div>';
  } else if (node.link) {
    // Dans l'arbre, "descendant" veut dire "vers l'enfant". Le lien a peut-etre
    // ete retourne pour orienter la hierarchie : dans ce cas les compteurs du
    // routeur le sont aussi, sinon la fleche mentirait sur le sens du trafic.
    const l = node.link;
    const bas = l.inverted ? l.rx_bps : l.tx_bps;
    const haut = l.inverted ? l.tx_bps : l.rx_bps;
    const plafond = (l.port_capacity_mbps || l.capacity_mbps || 0) * 1e6;
    if (bas !== null && bas !== undefined) {
      debits =
        '<span class="d">&darr; ' + esc(bpsText(bas)) + '</span>' +
        '<span class="u">&uarr; ' + esc(bpsText(haut || 0)) + '</span>';
      charge = plafond ? '<div class="tree-load">' + meter(Math.max(bas, haut || 0), plafond) + '</div>' : '';
    } else if (l.capacity_mbps) {
      charge = '<span class="tree-meta">' + esc(mbps(l.capacity_mbps)) + '</span>';
    }
  }

  let ligne =
    '<div class="tree-row">' +
      '<button class="tree-toggle' + (estFeuille ? ' leaf' : '') + '"' +
        (estFeuille ? ' disabled' : ' data-toggle-node="' + esc(node.key) + '"') + '>' +
        (estFeuille ? '&middot;' : replie ? '+' : '\u2212') + '</button>' +
      '<span class="tree-icon" style="color:' + (KIND_COLOR[node.kind] || 'var(--faint)') + '">' +
        esc(ICONE[node.kind] || '?') + '</span>' +
      '<span class="tree-name">' + esc(node.name) + '</span>' +
      (node.alias ? '<span class="badge">lien secondaire</span>' : '') +
      (node.link && node.link.interface
        ? '<span class="tree-meta">' + esc(node.link.interface) + '</span>' : '') +
      (s && s.rtt_ms !== null && s.rtt_ms !== undefined
        ? '<span class="tree-meta">' + rtt(s.rtt_ms) + '</span>' : '') +
      (s && s.boost_expires_at ? '<span class="boost-pill">boost</span>' : '') +
      '<span class="tree-rates">' + debits + charge +
        (s ? '<span class="tree-actions">' +
              '<button class="sm" data-tree-boost="' + esc(s.pppoe_login) + '">Boost</button>' +
             '</span>'
           : node.link && !node.alias
             ? '<span class="tree-actions">' +
               '<button class="sm" data-tree-link="' + esc(node.link.key) + '">Debit</button>' +
               '</span>' : '') +
      '</span>' +
    '</div>';

  let enfants = '';
  if (!estFeuille && !replie) {
    enfants = '<div class="tree-children">' +
      node.children.map((c) => renderTreeNode(c, profondeur + 1)).join('') + '</div>';
  }
  return '<div class="tree-node">' + ligne + enfants + '</div>';
}

async function loadNetwork() {
  const [topo, abonnes] = await Promise.all([
    api('/topology'),
    api('/subscribers/latest?limit=500&order_by=login'),
  ]);
  const host = document.getElementById('network-tree');

  if (!topo.nodes.length) {
    host.innerHTML = '<div class="card"><div class="empty">' +
      'Aucun equipement decouvert.<br>Lancez la decouverte dans l\'onglet Topologie : ' +
      'elle lit /ip/neighbor sur chaque PoP pour reconstruire la hierarchie.</div></div>';
    return;
  }

  const racines = buildTree(topo.nodes, topo.links, abonnes);
  state.lastTree = racines;
  if (!treeInitialise) {
    // Au premier affichage on montre la structure reseau, pas 500 abonnes.
    const replier = (n) => {
      if (n.group) treeCollapsed.add(n.key);
      n.children.forEach(replier);
    };
    racines.forEach(replier);
    treeInitialise = true;
  }
  host.innerHTML = '<div class="card"><div class="tree">' +
    racines.map((n) => renderTreeNode(n, 0)).join('') + '</div></div>';

  host.querySelectorAll('[data-toggle-node]').forEach((b) => {
    b.addEventListener('click', () => {
      const cle = b.dataset.toggleNode;
      if (treeCollapsed.has(cle)) treeCollapsed.delete(cle);
      else treeCollapsed.add(cle);
      loadNetwork();
    });
  });
  host.querySelectorAll('[data-tree-boost]').forEach((b) => {
    const ligne = abonnes.find((s) => s.pppoe_login === b.dataset.treeBoost);
    b.addEventListener('click', () => openBoostEditor(ligne));
  });
  host.querySelectorAll('[data-tree-link]').forEach((b) => {
    b.addEventListener('click', () => openLink(b.dataset.treeLink));
  });
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
  const [rows, pops, boosts] = await Promise.all([
    api('/subscribers/latest?limit=200' + query),
    api('/pops'),
    api('/shaping/boosts').catch(() => []),
  ]);

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

  document.getElementById('sub-count').textContent =
    rows.length + ' session(s)' + (state.subPop ? ' sur ce PoP' : '');

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
    '<th class="num">Upload</th><th class="num">Latence</th><th>Boost</th>' +
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
      '</div>' +
      (data.points.some((p) => p.rtt_ms_avg !== null && p.rtt_ms_avg !== undefined)
        ? '<div class="notice">Latence sur la fenetre : moyenne ' +
          rtt(Math.max(...data.points.map((p) => p.rtt_ms_avg || 0))) +
          ', pire ' + rtt(Math.max(...data.points.map((p) => p.rtt_ms_max || 0))) +
          '<span class="hint">Sonde active depuis le PoP. Ce n\'est pas une latence ' +
          'sous charge : la correler au debit est l\'objet de la phase 3.</span></div>'
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
  (data.skipped || []).forEach((message) => {
    html += '<div class="notice err">' + esc(message) + '</div>';
  });
  notice.innerHTML = html;
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
          (r.editable
            ? '<button class="sm" data-probe="' + r.id + '">Tester</button>' +
              '<button class="sm" data-toggle="' + r.id + '">' + (r.enabled ? 'Desactiver' : 'Activer') + '</button>' +
              '<button class="sm danger" data-del="' + r.id + '">Retirer</button>'
            : '<span style="font-size:.72rem;color:var(--faint)">edite dans routers.yml</span>') +
        '</div></td></tr>';
    }).join('') + '</tbody></table>';

  host.querySelectorAll('[data-probe]').forEach((b) =>
    b.addEventListener('click', () => probeRouter(b.dataset.probe, b)));
  host.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', () => deleteRouter(b.dataset.del)));
  host.querySelectorAll('[data-toggle]').forEach((b) =>
    b.addEventListener('click', () => toggleRouter(b.dataset.toggle)));
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
      ' enregistre.</strong><span class="hint">Il est interroge des le prochain cycle, ' +
      'sans redemarrage.</span></div>');
    document.getElementById('router-form').reset();
    document.getElementById('f-username').value = 'qos-ro';
    document.getElementById('f-port').value = '8728';
    await loadRouters();
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


/* ------------------------------------------------------------- topologie */

const KIND_LABEL = {
  gateway: 'Gateway', core: 'Coeur', pop: 'PoP', radio: 'Radio',
  sector: 'Secteur', cpe: 'CPE', unknown: 'Inconnu',
};
const KIND_COLOR = {
  gateway: 'var(--accent)', core: 'var(--accent)', pop: 'var(--down)',
  radio: 'var(--up)', sector: 'var(--up)', cpe: 'var(--muted)', unknown: 'var(--faint)',
};

async function loadTopology() {
  const data = await api('/topology');
  document.getElementById('topo-count').textContent =
    data.counts.nodes + ' equipement(s), ' + data.counts.links + ' lien(s)';

  renderTopologyGraph(data);
  renderTopologyLinks(data.links);
}

/** Graphe en colonnes par role. Un vrai layout de graphe serait plus joli mais
 *  moins lisible : sur un reseau WISP, la hierarchie EST l'information. */
function renderTopologyGraph(data) {
  const host = document.getElementById('topo-graph');
  if (!data.nodes.length) {
    host.innerHTML = '<div class="card"><div class="empty">Aucun equipement decouvert.<br>' +
      'Lancez la decouverte : elle lit /ip/neighbor sur chaque PoP.</div></div>';
    return;
  }
  const ordre = ['gateway', 'core', 'pop', 'radio', 'sector', 'cpe', 'unknown'];
  const parRole = {};
  data.nodes.forEach((n) => { (parRole[n.kind] = parRole[n.kind] || []).push(n); });

  const voisins = {};
  data.links.forEach((l) => {
    (voisins[l.source_key] = voisins[l.source_key] || []).push(l);
  });

  host.innerHTML = '<div class="grid cols-2">' + ordre.filter((k) => parRole[k]).map((role) =>
    '<div class="card"><div class="label" style="color:' + KIND_COLOR[role] +
      ';font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:.6rem">' +
      esc(KIND_LABEL[role]) + ' &middot; ' + parRole[role].length + '</div>' +
    parRole[role].map((n) => {
      const sortants = voisins[n.key] || [];
      return '<div class="child">' +
        '<span class="name">' + esc(n.name) +
          (n.fresh ? '' : ' <span class="badge warn">non revu</span>') +
          (n.platform ? '<span class="host">' + esc(n.platform) + '</span>' : '') +
        '</span>' +
        '<span style="display:flex;gap:.6rem;align-items:center">' +
          (n.address ? '<span class="host">' + esc(n.address) + '</span>' : '') +
          (sortants.length ? '<span class="badge">' + sortants.length + ' lien(s)</span>' : '') +
          '<select data-node-kind="' + esc(n.key) + '" style="width:auto;font-size:.72rem;padding:.15rem .35rem">' +
            ordre.map((k) => '<option value="' + k + '"' +
              (k === n.kind ? ' selected' : '') + '>' + esc(KIND_LABEL[k]) + '</option>').join('') +
          '</select>' +
        '</span></div>';
    }).join('') + '</div>').join('') + '</div>';

  host.querySelectorAll('[data-node-kind]').forEach((select) => {
    select.addEventListener('change', async () => {
      try {
        await api('/topology/nodes/' + encodeURIComponent(select.dataset.nodeKind) +
          '?kind=' + select.value, { method: 'PATCH' });
        await loadTopology();
      } catch (err) { alert(err.message); }
    });
  });
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

function renderTopologyLinks(links) {
  const host = document.getElementById('topo-links');
  if (!links.length) {
    host.innerHTML = '<div class="empty">Aucun lien.</div>';
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
  network: loadNetwork,
  subscribers: loadSubscribers,
  topology: loadTopology,
  shaping: loadShaping,
  pops: loadRouters,
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
document.getElementById('btn-tree-expand').addEventListener('click', () => {
  treeCollapsed.clear();
  loadNetwork();
});
document.getElementById('btn-tree-collapse').addEventListener('click', () => {
  const replier = (n) => { treeCollapsed.add(n.key); n.children.forEach(replier); };
  (state.lastTree || []).forEach(replier);
  loadNetwork();
});
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
    await loadTopology();
  } catch (err) {
    notice.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  } finally {
    e.target.disabled = false;
  }
});
document.getElementById('router-form').addEventListener('submit', saveRouter);

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
  // La topologie etait figee elle aussi, a cause de ses menus de role. Elle
  // porte desormais le debit des liens : la figer entierement reviendrait a
  // afficher un debit perime. On ne suspend donc que pendant qu'un menu est
  // reellement ouvert.
  if (state.view === 'topology' && document.activeElement &&
      document.activeElement.tagName === 'SELECT') return;
  refresh();
  // Le tiroir d'un lien suit le meme rythme : on regarde un debit justement
  // quand il bouge.
  if (state.link) openLink(state.link.key, state.link.minutes, true);
}, 10000);
setInterval(refreshHealth, 15000);
