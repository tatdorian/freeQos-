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

/* ------------------------------------------------- rond de chargement
 *
 *  UN BOUTON QUI DECLENCHE UNE REQUETE TOURNE JUSQU'A LA REPONSE. Fait ici,
 *  une seule fois, plutot que dans chacun des cent gestionnaires : le dernier
 *  bouton clique (il y a moins d'une seconde) est rattache aux requetes qu'il
 *  lance, et rendu indisponible le temps qu'elles aboutissent -- ce qui evite
 *  aussi le double clic qui pose deux fois la meme regle. */
let dernierClic = null;
document.addEventListener('click', (e) => {
  const bouton = e.target.closest && e.target.closest('button');
  if (bouton && !bouton.disabled) dernierClic = { bouton, t: Date.now() };
}, true);
const enCours = new WeakMap();

function boutonOccupe() {
  if (!dernierClic || Date.now() - dernierClic.t > 1000) return null;
  const b = dernierClic.bouton;
  return b.isConnected ? b : null;
}

function marquer(bouton, delta) {
  const n = (enCours.get(bouton) || 0) + delta;
  enCours.set(bouton, Math.max(0, n));
  const actif = n > 0;
  bouton.classList.toggle('is-busy', actif);
  bouton.setAttribute('aria-busy', actif ? 'true' : 'false');
  if (actif) {
    if (!('busyWasDisabled' in bouton.dataset)) bouton.dataset.busyWasDisabled = bouton.disabled ? '1' : '';
    bouton.disabled = true;
  } else if ('busyWasDisabled' in bouton.dataset) {
    bouton.disabled = bouton.dataset.busyWasDisabled === '1';
    delete bouton.dataset.busyWasDisabled;
  }
}

async function api(path, options) {
  const bouton = boutonOccupe();
  if (bouton) marquer(bouton, +1);
  try {
    return await apiBrut(path, options);
  } finally {
    if (bouton) marquer(bouton, -1);
  }
}

async function apiBrut(path, options) {
  const res = await fetch(API + path, {
    headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    ...options,
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => null);
  // Session expiree ou fermee ailleurs : retour a l'ecran de connexion, plutot
  // qu'une page qui se remplit d'erreurs.
  if (res.status === 401 && path.indexOf('/auth/') !== 0 && AUTH.ready) {
    AUTH.ready = false;
    showAuthGate('login', 'Your session has ended: log in again.');
  }
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : res.status + ' ' + res.statusText;
    throw new Error(typeof detail === 'string' ? detail : validationText(detail));
  }
  return body;
}

const AUTH = { user: null, ready: false, mode: 'login', started: false, passwordMin: 12 };

/* ------------------------------------------------- force du mot de passe
 *
 *  Reprend les regles du SERVEUR (longueur, mots de passe connus, repetition,
 *  email) pour prevenir AVANT l'envoi plutot qu'apres un refus. Le serveur
 *  reste le seul juge : cette jauge n'autorise rien. */
const MDP_COMMUNS = new Set(('password passw0rd motdepasse motdepass azerty azertyuiop qwerty ' +
  'qwertyuiop qwertz abcdef abcdefgh abcdefghijkl admin administrator administrateur root toor ' +
  'changeme changeit secret default freeqos mikrotik routeros preseem wisp network reseau internet ' +
  'fibre wifi iloveyou monkey dragon football soleil chocolat bonjour salut master letmein welcome ' +
  'bienvenue login connexion utilisateur user guest invite test testtest demo').split(' '));

function suiteTriviale(bas) {
  if (bas.length > 14) return false;
  for (const suite of ['0123456789', 'abcdefghijklmnopqrstuvwxyz', 'azertyuiop', 'qwertyuiop']) {
    for (const sens of [suite, suite.split('').reverse().join('')]) {
      for (let i = 0; i + 6 <= sens.length; i++) if (bas.includes(sens.slice(i, i + 6))) return true;
    }
  }
  return false;
}

function forceMdp(mdp, email) {
  const min = AUTH.passwordMin || 12;
  if (!mdp) return { score: 0, ok: false, label: 'At least ' + min + ' characters. A short sentence works best.' };
  if (mdp.length < min) {
    const manque = min - mdp.length;
    return { score: 0, ok: false, label: manque + ' more character' + (manque > 1 ? 's' : '') + ' needed' };
  }
  const bas = mdp.toLowerCase();
  if (new Set(bas).size < 6) return { score: 0, ok: false, label: 'Too repetitive: use more different characters' };
  const racine = bas.replace(/[\d\W_]+$/, '');
  if (MDP_COMMUNS.has(bas) || MDP_COMMUNS.has(racine) || suiteTriviale(bas)) {
    return { score: 0, ok: false, label: 'Too common: among the first passwords an attacker tries' };
  }
  const local = (email || '').trim().toLowerCase().split('@')[0];
  if (local.length >= 4 && bas.includes(local)) return { score: 0, ok: false, label: 'Must not contain the email address' };
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(mdp)).length;
  const score = Math.min(4, 1 + (mdp.length >= 16) + (mdp.length >= 20) + (classes >= 3));
  return { score, ok: true, label: ['', 'Acceptable', 'Good', 'Strong', 'Very strong'][score] };
}

/** Jauge en quatre barres sous un champ de mot de passe. */
function jaugeMdp(hote, mdp, email) {
  if (!hote) return;
  const f = forceMdp(mdp, email);
  hote.className = 'pwd-meter s' + f.score + (mdp && !f.ok ? ' bad' : '');
  hote.innerHTML = '<span class="pwd-bars"><i></i><i></i><i></i><i></i></span>' +
    '<span class="pwd-label">' + esc(f.label) + '</span>';
}

/** Navigateur et systeme, lus dans l'User-Agent : "Firefox on Windows". */
function appareil(ua) {
  if (!ua) return 'Unknown device';
  if (/curl|python|httpx|wget|go-http|okhttp|postman/i.test(ua)) return 'Script (' + ua.split(/[\s/]/)[0] + ')';
  const nav = /Edg\//.test(ua) ? 'Edge' : /OPR\//.test(ua) ? 'Opera' : /Firefox\//.test(ua) ? 'Firefox'
    : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser';
  const os = /Windows/.test(ua) ? 'Windows' : /iPhone|iPad/.test(ua) ? 'iOS' : /Android/.test(ua) ? 'Android'
    : /Mac OS X|Macintosh/.test(ua) ? 'macOS' : /CrOS/.test(ua) ? 'ChromeOS' : /Linux/.test(ua) ? 'Linux' : '';
  return nav + (os ? ' on ' + os : '');
}

function dateLongue(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) + ' ' +
    d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', hour12: false });
}

/* Champs de mot de passe : oeil pour afficher, alerte Verr. Maj. Delegues au
 * document : ils valent aussi pour les formulaires crees plus tard. */
document.addEventListener('click', (e) => {
  const oeil = e.target.closest('[data-eye]');
  if (!oeil) return;
  const champ = document.getElementById(oeil.dataset.eye);
  if (!champ) return;
  const montrer = champ.type === 'password';
  champ.type = montrer ? 'text' : 'password';
  oeil.classList.toggle('on', montrer);
  oeil.setAttribute('aria-label', montrer ? 'Hide password' : 'Show password');
  oeil.title = oeil.getAttribute('aria-label');
  champ.focus();
});
['keydown', 'keyup'].forEach((type) => document.addEventListener(type, (e) => {
  if (!e.target.matches || !e.target.matches('#auth-password, #auth-confirm') || !e.getModifierState) return;
  document.getElementById('auth-caps').hidden = !e.getModifierState('CapsLock');
}));

/* ----------------------------------------------------------- connexion
 *
 *  L'interface ne charge RIEN tant qu'aucune session n'est ouverte : toutes
 *  les routes d'exploitation exigent un compte. Au tout premier lancement
 *  (aucun compte en base), l'ecran de connexion sert a creer le premier,
 *  qui est en edition. Le grade est tenu par le serveur ; l'interface se
 *  contente de le dire (bandeau "lecture seule"). */
function showAuthGate(mode, message) {
  AUTH.mode = mode;
  const setup = mode === 'setup';
  document.getElementById('auth-gate').hidden = false;
  document.body.classList.add('gated');
  document.getElementById('auth-title').textContent = setup ? 'Create the first account' : 'Log in';
  document.getElementById('auth-sub').textContent = setup
    ? 'No account exists yet. This one will have edit rights and can create the others.'
    : 'freeQoS controller';
  document.getElementById('auth-confirm-row').hidden = !setup;
  document.getElementById('auth-confirm').required = setup;
  document.getElementById('auth-password').autocomplete = setup ? 'new-password' : 'current-password';
  document.getElementById('auth-password').minLength = setup ? AUTH.passwordMin : 0;
  document.getElementById('auth-submit').textContent = setup ? 'Create and log in' : 'Log in';
  const jauge = document.getElementById('auth-meter');
  jauge.hidden = !setup;
  if (setup) jaugeMdp(jauge, document.getElementById('auth-password').value, document.getElementById('auth-email').value);
  document.getElementById('auth-error').innerHTML = message
    ? '<div class="notice warn">' + esc(message) + '</div>' : '';
  setTimeout(() => document.getElementById('auth-email').focus(), 0);
}

function startApp(user) {
  AUTH.user = user;
  AUTH.ready = true;
  document.getElementById('auth-gate').hidden = true;
  document.body.classList.remove('gated');
  document.body.classList.toggle('role-read', user.role !== 'edit');
  document.getElementById('readonly-banner').hidden = user.role === 'edit';
  document.getElementById('user-chip').hidden = false;
  document.getElementById('user-email').textContent = user.email;
  document.getElementById('user-avatar').textContent = initiales(user.email, AUTH.authDisabled);
  document.getElementById('user-role').textContent = user.role === 'edit' ? 'edit' : 'read only';
  document.getElementById('user-role').className = 'badge ' + (user.role === 'edit' ? 'file' : '');
  document.getElementById('logout-btn').hidden = AUTH.authDisabled === true;
  route();
  refreshHealth();
  AUTH.started = true;
  annoncerConnexionPrecedente();
}

/** "jean.dupont@x.fr" -> "JD", "admin@x.fr" -> "AD". */
function initiales(email, sansCompte) {
  if (sansCompte || !email) return '–';
  const parts = String(email).split('@')[0].split(/[._-]+/).filter(Boolean);
  const lettres = parts.length >= 2 ? parts[0][0] + parts[1][0] : (parts[0] || '?').slice(0, 2);
  return lettres.toUpperCase();
}

/** "Derniere connexion : il y a 3 h, depuis 10.0.0.5 (Firefox on Windows)".
 *  Le moyen le plus simple de remarquer que quelqu'un d'autre s'est servi du
 *  compte. Passe par sessionStorage : la page se recharge parfois juste apres
 *  la connexion. */
function annoncerConnexionPrecedente() {
  let precedente = null;
  try {
    const brut = sessionStorage.getItem('freeqos-prev-login');
    sessionStorage.removeItem('freeqos-prev-login');
    if (brut) precedente = JSON.parse(brut);
  } catch (e) { /* stockage indisponible : rien a annoncer */ }
  if (precedente === null) return;
  const texte = precedente
    ? 'Previous login ' + depuis(precedente.at) + ' (' + dateLongue(precedente.at) + ') from ' +
      (precedente.address || 'an unknown address') + ' · ' + appareil(precedente.user_agent)
    : 'First login on this account.';
  toast('<b>Welcome back.</b> ' + esc(texte) +
    ' <a href="#/settings" data-close-toast>Not you? Review your sessions</a>', 12000);
}

/** Message discret en bas d'ecran, qui se ferme seul. */
function toast(html, duree) {
  let pile = document.getElementById('toasts');
  if (!pile) {
    pile = document.createElement('div');
    pile.id = 'toasts';
    pile.setAttribute('role', 'status');
    document.body.appendChild(pile);
  }
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = '<div>' + html + '</div><button type="button" class="toast-x" aria-label="Close">&times;</button>';
  const fermer = () => { el.classList.add('out'); setTimeout(() => el.remove(), 250); };
  el.querySelector('.toast-x').addEventListener('click', fermer);
  el.querySelectorAll('[data-close-toast]').forEach((a) => a.addEventListener('click', fermer));
  pile.appendChild(el);
  if (duree) setTimeout(fermer, duree);
}

async function boot() {
  let etat;
  try {
    etat = await api('/auth/status');
  } catch (err) {
    showAuthGate('login', err.message);
    return;
  }
  AUTH.authDisabled = etat.auth_enabled === false;
  if (etat.password_min) AUTH.passwordMin = etat.password_min;
  if (etat.user) { startApp(etat.user); return; }
  showAuthGate(etat.setup_required ? 'setup' : 'login');
}

async function submitAuth(event) {
  event.preventDefault();
  const email = document.getElementById('auth-email').value.trim();
  const password = document.getElementById('auth-password').value;
  const erreur = document.getElementById('auth-error');
  if (AUTH.mode === 'setup') {
    const f = forceMdp(password, email);
    if (!f.ok) { erreur.innerHTML = '<div class="notice err">' + esc(f.label) + '</div>'; return; }
    if (password !== document.getElementById('auth-confirm').value) {
      erreur.innerHTML = '<div class="notice err">The two passwords differ.</div>';
      return;
    }
  }
  const bouton = document.getElementById('auth-submit');
  bouton.disabled = true;
  try {
    const r = await api(AUTH.mode === 'setup' ? '/auth/setup' : '/auth/login', {
      method: 'POST', body: JSON.stringify({ email, password }),
    });
    document.getElementById('auth-password').value = '';
    document.getElementById('auth-confirm').value = '';
    erreur.innerHTML = '';
    try { sessionStorage.setItem('freeqos-prev-login', JSON.stringify(r.previous_login || false)); } catch (e) { /* rien */ }
    if (AUTH.started) { location.reload(); return; }
    startApp(r.user);
  } catch (err) {
    const bloque = /too many/i.test(err.message);
    erreur.innerHTML = '<div class="notice ' + (bloque ? 'warn' : 'err') + '">' + esc(err.message) + '</div>';
    document.getElementById('auth-password').select();
    // Un compte a ete cree entre-temps (autre navigateur) : on passe en connexion.
    if (AUTH.mode === 'setup' && /already exists/i.test(err.message)) showAuthGate('login', err.message);
  } finally {
    bouton.disabled = false;
  }
}

async function logout() {
  try { await api('/auth/logout', { method: 'POST' }); } catch (err) { /* on sort quand meme */ }
  location.reload();
}

/* ------------------------------------------------------------- comptes */

const EVENEMENTS_AUTH = {
  login_ok: ['Login', 'ok'],
  login_failed: ['Failed login', 'crit'],
  login_locked: ['Blocked: too many failures', 'crit'],
  logout: ['Logout', ''],
  setup: ['First account created', 'file'],
  password_changed: ['Password changed', 'file'],
  password_change_failed: ['Wrong current password', 'crit'],
  session_closed: ['Session closed', ''],
  sessions_closed: ['Other sessions closed', ''],
  user_created: ['Account created', 'file'],
  user_updated: ['Account changed', 'warn'],
  user_deleted: ['Account deleted', 'warn'],
};

/** Champ de mot de passe avec son oeil (et sa jauge, pour un NOUVEAU mot de passe). */
function champMdp(id, placeholder, nouveau) {
  return '<span class="pwd-field"><input type="password" id="' + id + '" placeholder="' + esc(placeholder) + '"' +
    ' autocomplete="' + (nouveau ? 'new-password' : 'current-password') + '" required maxlength="256"' +
    (nouveau ? ' minlength="' + AUTH.passwordMin + '" data-meter="' + id + '-meter"' : '') + '>' +
    '<button type="button" class="pwd-eye" data-eye="' + id + '" aria-label="Show password" title="Show password">' +
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg></button></span>' +
    (nouveau ? '<div class="pwd-meter" id="' + id + '-meter"></div>' : '');
}

/** Branche les jauges des champs "nouveau mot de passe" d'un bloc. */
function brancherJauges(hote, email) {
  hote.querySelectorAll('input[data-meter]').forEach((champ) => {
    const jauge = document.getElementById(champ.dataset.meter);
    const maj = () => jaugeMdp(jauge, champ.value, typeof email === 'function' ? email() : email);
    champ.addEventListener('input', maj);
    maj();
  });
}

/** Les comptes, dans Reglages. Un compte d'edition les gere tous (email, mot
 *  de passe, grade) et lit le journal des connexions ; tout compte change son
 *  propre mot de passe et voit (et ferme) ses propres sessions. */
async function renderAccounts() {
  const host = document.getElementById('settings-accounts');
  if (!host) return;
  const moi = AUTH.user || {};
  const monMdp =
    '<div class="acc-block"><h3>My password</h3>' +
    '<form class="acc-form" id="acc-self-form">' +
      champMdp('acc-self-current', 'Current password', false) +
      champMdp('acc-self-new', 'New password (' + AUTH.passwordMin + '+ characters)', true) +
      '<button class="sm" type="submit">Change</button><span id="acc-self-result"></span></form>' +
    '<p class="hint acc-hint">Changing it logs out your other browsers. Prefer a short sentence ' +
      '(&ldquo;the router sleeps at noon&rdquo;) to a short complicated word.</p></div>';
  const mesSessions = '<div class="acc-block"><h3>My sessions</h3><div id="acc-sessions">' +
    '<div class="hint">Loading…</div></div></div>';
  if (AUTH.authDisabled) {
    host.innerHTML = '<div class="notice warn">Authentication is off (<code>AUTH_ENABLED=false</code>): ' +
      'anyone who reaches this page has full rights.</div>';
    return;
  }
  if (moi.role !== 'edit') {
    host.innerHTML = '<div class="acc-me">Logged in as <b>' + esc(moi.email) + '</b> ' +
      '<span class="badge">read only</span></div>' + monMdp + mesSessions;
    brancherMonMdp();
    chargerSessions();
    return;
  }
  let comptes = [];
  try {
    comptes = await api('/users');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  host.innerHTML =
    '<div class="acc-block"><h3>Who can log in</h3>' +
    '<div class="table-wrap"><table><thead><tr><th>Email</th><th>Role</th><th>State</th>' +
      '<th>Last login</th><th>Created by</th><th></th></tr></thead><tbody>' +
      comptes.map((u) => '<tr data-user="' + u.id + '">' +
        '<td><b>' + esc(u.email) + '</b>' + (u.id === moi.id ? ' <span class="pct-hint">(you)</span>' : '') + '</td>' +
        '<td><select data-acc-role aria-label="Role">' +
          '<option value="read"' + (u.role === 'read' ? ' selected' : '') + '>Read only</option>' +
          '<option value="edit"' + (u.role === 'edit' ? ' selected' : '') + '>Edit</option></select></td>' +
        '<td>' + (u.disabled ? '<span class="badge warn">disabled</span>' : '<span class="badge ok">active</span>') + '</td>' +
        '<td title="' + esc(u.last_login_at ? dateLongue(u.last_login_at) : '') + '">' +
          esc(u.last_login_at ? depuis(u.last_login_at) : 'never') + '</td>' +
        '<td>' + esc(u.created_by || '-') + '</td>' +
        '<td class="nowrap acc-actions">' +
          '<button class="sm" data-acc-pwd>Set password</button>' +
          '<button class="sm" data-acc-toggle>' + (u.disabled ? 'Enable' : 'Disable') + '</button>' +
          '<button class="sm danger" data-acc-del>Delete</button></td></tr>').join('') +
    '</tbody></table></div>' +
    '<form class="acc-form" id="acc-new-form"><b>New account</b>' +
      '<input type="email" id="acc-new-email" placeholder="Email" required maxlength="254" autocomplete="off">' +
      champMdp('acc-new-pwd', 'Password (' + AUTH.passwordMin + '+ characters)', true) +
      '<select id="acc-new-role" aria-label="Role"><option value="read">Read only</option><option value="edit">Edit</option></select>' +
      '<button class="sm primary" type="submit">Create</button></form>' +
    '<div id="acc-result"></div>' +
    '<div class="exec-legend"><span><b>Read only</b>: sees everything, every change is refused by the server.</span>' +
      '<span><b>Edit</b>: can change everything, including accounts.</span></div></div>' +
    monMdp + mesSessions +
    '<div class="acc-block"><h3>Login journal</h3>' +
      '<div class="toolbar acc-journal-bar">' +
        '<select id="acc-journal-filter" aria-label="Events shown"><option value="">All events</option>' +
          '<option value="fail">Failures and blocks</option><option value="admin">Account changes</option></select>' +
        '<input type="search" id="acc-journal-email" placeholder="Filter by email" maxlength="254">' +
      '</div><div id="acc-journal"><div class="hint">Loading…</div></div></div>';

  const resultat = (html) => { document.getElementById('acc-result').innerHTML = html; };
  const faire = async (fn, ok) => {
    try { await fn(); await renderAccounts(); resultat('<div class="notice ok">' + esc(ok) + '</div>'); }
    catch (err) { resultat('<div class="notice err">' + esc(err.message) + '</div>'); }
  };
  brancherJauges(host.querySelector('#acc-new-form'), () => document.getElementById('acc-new-email').value);
  document.getElementById('acc-new-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const email = document.getElementById('acc-new-email').value.trim();
    const mdp = document.getElementById('acc-new-pwd').value;
    const f = forceMdp(mdp, email);
    if (!f.ok) { resultat('<div class="notice err">' + esc(f.label) + '</div>'); return; }
    faire(() => api('/users', { method: 'POST', body: JSON.stringify({
      email, password: mdp, role: document.getElementById('acc-new-role').value,
    }) }), 'Account ' + email + ' created.');
  });
  host.querySelectorAll('tr[data-user]').forEach((tr) => {
    const id = tr.dataset.user;
    const email = tr.querySelector('b').textContent;
    tr.querySelector('[data-acc-role]').addEventListener('change', (e) => {
      faire(() => api('/users/' + id, { method: 'PATCH', body: JSON.stringify({ role: e.target.value }) }),
        'Role of ' + email + ' changed.');
    });
    // Mot de passe d'un autre compte : une ligne qui s'ouvre sous le compte,
    // champ masque et jauge -- pas une boite prompt() qui l'affiche en clair.
    tr.querySelector('[data-acc-pwd]').addEventListener('click', () => {
      const suivante = tr.nextElementSibling;
      if (suivante && suivante.classList.contains('acc-pwd-row')) { suivante.remove(); return; }
      const ligne = document.createElement('tr');
      ligne.className = 'acc-pwd-row';
      const champ = 'acc-pwd-' + id;
      ligne.innerHTML = '<td colspan="6"><form class="acc-form acc-inline">' +
        '<span class="hint">New password for <b>' + esc(email) + '</b> — their sessions will be closed.</span>' +
        champMdp(champ, 'New password (' + AUTH.passwordMin + '+ characters)', true) +
        '<button class="sm primary" type="submit">Save</button>' +
        '<button class="sm" type="button" data-cancel>Cancel</button></form></td>';
      tr.after(ligne);
      brancherJauges(ligne, email);
      document.getElementById(champ).focus();
      ligne.querySelector('[data-cancel]').addEventListener('click', () => ligne.remove());
      ligne.querySelector('form').addEventListener('submit', (e) => {
        e.preventDefault();
        const mdp = document.getElementById(champ).value;
        const f = forceMdp(mdp, email);
        if (!f.ok) { resultat('<div class="notice err">' + esc(f.label) + '</div>'); return; }
        faire(() => api('/users/' + id, { method: 'PATCH', body: JSON.stringify({ password: mdp }) }),
          'Password of ' + email + ' changed; their sessions are closed.');
      });
    });
    tr.querySelector('[data-acc-toggle]').addEventListener('click', (e) => {
      const couper = e.target.textContent === 'Disable';
      faire(() => api('/users/' + id, { method: 'PATCH', body: JSON.stringify({ disabled: couper }) }),
        email + (couper ? ' disabled.' : ' enabled.'));
    });
    tr.querySelector('[data-acc-del]').addEventListener('click', () => {
      if (!confirm('Delete the account ' + email + '?')) return;
      faire(() => api('/users/' + id, { method: 'DELETE' }), email + ' deleted.');
    });
  });
  brancherMonMdp();
  chargerSessions();
  const relire = () => chargerJournal();
  document.getElementById('acc-journal-filter').addEventListener('change', relire);
  let minuterie = null;
  document.getElementById('acc-journal-email').addEventListener('input', () => {
    clearTimeout(minuterie);
    minuterie = setTimeout(relire, 300);
  });
  chargerJournal();
}

/** Mes sessions : chaque navigateur connecte a MON compte, d'ou, et depuis
 *  quand. Une ligne inconnue se ferme d'un clic. */
async function chargerSessions() {
  const hote = document.getElementById('acc-sessions');
  if (!hote) return;
  let sessions;
  try {
    sessions = await apiBrut('/auth/sessions');
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const autres = sessions.filter((s) => !s.current).length;
  hote.innerHTML = '<div class="table-wrap"><table><thead><tr><th>Device</th><th>Address</th>' +
    '<th>Opened</th><th>Last activity</th><th></th></tr></thead><tbody>' +
    sessions.map((s) => '<tr><td><b>' + esc(appareil(s.user_agent)) + '</b>' +
        (s.current ? ' <span class="badge ok">this browser</span>' : '') + '</td>' +
      '<td><code>' + esc(s.address || '-') + '</code></td>' +
      '<td title="' + esc(dateLongue(s.created_at)) + '">' + esc(depuis(s.created_at)) + '</td>' +
      '<td title="' + esc(dateLongue(s.last_seen)) + '">' + esc(depuis(s.last_seen)) + '</td>' +
      '<td class="nowrap">' + (s.current ? '' : '<button class="sm" data-close-session="' + esc(s.id) + '">Log out</button>') +
      '</td></tr>').join('') +
    '</tbody></table></div>' +
    '<div class="acc-sessions-foot"><span class="hint">A session ends after a day without activity, ' +
      'and after 30 days in any case.</span>' +
      (autres ? '<button class="sm danger" id="acc-close-others">Log out the ' + autres + ' other session' + (autres > 1 ? 's' : '') + '</button>' : '') +
    '</div>';
  hote.querySelectorAll('[data-close-session]').forEach((b) => b.addEventListener('click', async () => {
    try { await api('/auth/sessions/' + b.dataset.closeSession, { method: 'DELETE' }); }
    catch (err) { toast(esc(err.message), 6000); }
    chargerSessions();
  }));
  const tout = document.getElementById('acc-close-others');
  if (tout) tout.addEventListener('click', async () => {
    if (!confirm('Log out every other browser connected to your account?')) return;
    try {
      const r = await api('/auth/sessions/close-others', { method: 'POST' });
      toast(esc(r.closed + ' session' + (r.closed > 1 ? 's' : '') + ' closed.'), 5000);
    } catch (err) { toast(esc(err.message), 6000); }
    chargerSessions();
  });
}

/** Journal des connexions (comptes d'edition) : qui s'est connecte, d'ou, qui
 *  a echoue, qui a change quoi. */
async function chargerJournal() {
  const hote = document.getElementById('acc-journal');
  if (!hote) return;
  const email = (document.getElementById('acc-journal-email').value || '').trim();
  const filtre = document.getElementById('acc-journal-filter').value;
  let lignes;
  try {
    lignes = await apiBrut('/auth/events?limit=300' + (email ? '&email=' + encodeURIComponent(email) : ''));
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const echecs = new Set(['login_failed', 'login_locked', 'password_change_failed']);
  const admin = new Set(['user_created', 'user_updated', 'user_deleted', 'setup', 'password_changed']);
  if (filtre === 'fail') lignes = lignes.filter((e) => echecs.has(e.event));
  if (filtre === 'admin') lignes = lignes.filter((e) => admin.has(e.event));
  const recents = lignes.filter((e) => echecs.has(e.event) && Date.now() - new Date(e.at).getTime() < 86400e3).length;
  hote.innerHTML = (recents >= 10
    ? '<div class="notice warn">' + recents + ' failed attempts in the last 24 h: someone may be guessing a password. ' +
      'Check the addresses below; a block is applied automatically after 5 failures.</div>' : '') +
    (lignes.length ? '<div class="table-wrap acc-journal"><table><thead><tr><th>When</th><th>Event</th><th>Account</th>' +
      '<th>By</th><th>Address</th><th>Device</th><th>Detail</th></tr></thead><tbody>' +
      lignes.slice(0, 200).map((e) => {
        const [libelle, ton] = EVENEMENTS_AUTH[e.event] || [e.event, ''];
        return '<tr><td class="nowrap" title="' + esc(new Date(e.at).toLocaleString('fr-FR')) + '">' + esc(dateLongue(e.at)) + '</td>' +
          '<td><span class="badge ' + ton + '">' + esc(libelle) + '</span></td>' +
          '<td>' + esc(e.email || '-') + '</td>' +
          '<td>' + esc(e.actor && e.actor !== e.email ? e.actor : '') + '</td>' +
          '<td><code>' + esc(e.address || '-') + '</code></td>' +
          '<td>' + esc(e.user_agent ? appareil(e.user_agent) : '') + '</td>' +
          '<td class="acc-detail">' + esc(e.detail || '') + '</td></tr>';
      }).join('') + '</tbody></table></div>'
      : '<div class="empty">No event' + (filtre || email ? ' matching this filter' : ' yet') + '.</div>');
}

function brancherMonMdp() {
  const form = document.getElementById('acc-self-form');
  if (!form) return;
  brancherJauges(form, () => (AUTH.user || {}).email);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const sortie = document.getElementById('acc-self-result');
    const nouveau = document.getElementById('acc-self-new').value;
    const f = forceMdp(nouveau, (AUTH.user || {}).email);
    if (!f.ok) { sortie.innerHTML = '<span class="badge crit">' + esc(f.label) + '</span>'; return; }
    try {
      await api('/auth/password', { method: 'POST', body: JSON.stringify({
        current: document.getElementById('acc-self-current').value,
        new: nouveau,
      }) });
      form.reset();
      jaugeMdp(document.getElementById('acc-self-new-meter'), '', '');
      sortie.innerHTML = '<span class="badge ok">changed — your other sessions are closed</span>';
      chargerSessions();
    } catch (err) {
      sortie.innerHTML = '<span class="badge crit">' + esc(err.message) + '</span>';
    }
  });
}

/* ------------------------------------------------- recherche instantanee
 *
 *  Un seul champ, toujours visible : le support tape ce qu'il a sous les yeux
 *  (login, IP lue sur la box, MAC d'une radio, nom de site) et obtient la
 *  fiche en deux frappes. "/" y place le curseur depuis n'importe quel onglet. */
const GS = { timer: null, seq: 0 };

function gsItem(icone, titre, detail, action) {
  return '<button type="button" class="gs-item" data-gs="' + esc(action) + '">' +
    '<span class="gs-kind">' + esc(icone) + '</span><span class="gs-main"><b>' + esc(titre) + '</b>' +
    '<span class="hint">' + esc(detail || '') + '</span></span></button>';
}

async function globalSearch(q) {
  const hote = document.getElementById('global-search-results');
  if (q.trim().length < 2) { hote.hidden = true; hote.innerHTML = ''; return; }
  const seq = ++GS.seq;
  let r;
  try { r = await api('/search?q=' + encodeURIComponent(q.trim())); } catch (err) { return; }
  if (seq !== GS.seq) return;  // une frappe plus recente a deja repondu
  const blocs = [];
  const groupe = (titre, items) => { if (items.length) blocs.push('<div class="gs-group">' + titre + '</div>' + items.join('')); };
  groupe('Subscribers', (r.subscribers || []).map((s) => gsItem(s.kind === 'static' ? 'IP' : 'SUB', s.login,
    [s.address, s.pop_name, s.plan_down_mbps ? mbps(s.plan_down_mbps) : ''].filter(Boolean).join(' · '),
    'sub:' + s.id)));
  groupe('Routers', (r.routers || []).map((x) => gsItem('RTR', x.name, x.host + ' · ' + x.pop_name, 'router:' + x.name)));
  groupe('Sites', (r.sites || []).map((x) => gsItem('POP', x.name, x.subscribers + ' subscriber(s)', 'site:' + x.id)));
  groupe('Devices', (r.devices || []).map((d) => gsItem((ICONE[d.kind] || '?'), d.name,
    [d.address, d.mac, d.platform].filter(Boolean).join(' · '), 'node:' + d.key)));
  groupe('Internet addresses', (r.addresses || []).map((a) => gsItem('WAN', a.address,
    [a.service || a.hostname, a.org, a.city].filter(Boolean).join(' · '), 'ip:' + a.address)));
  // Une IP que personne n'a encore vue se cherche quand meme : "Find an IP".
  if (/^[0-9a-f.:]+$/i.test(q.trim()) && !(r.addresses || []).length) {
    groupe('Look up', [gsItem('WAN', q.trim(), 'who holds it and where it is', 'ip:' + q.trim())]);
  }
  hote.innerHTML = blocs.join('') || '<div class="gs-empty">Nothing matches "' + esc(q) + '".</div>';
  hote.hidden = false;
  hote.querySelectorAll('[data-gs]').forEach((b) => b.addEventListener('click', () => gsOpen(b.dataset.gs)));
}

function gsOpen(action) {
  const [type, ...reste] = action.split(':');
  const valeur = reste.join(':');
  document.getElementById('global-search-results').hidden = true;
  if (type === 'sub') { openSubscriber(Number(valeur)); return; }
  if (type === 'router') { location.hash = '#/pops'; return; }
  if (type === 'site') {
    state.subPop = valeur;
    const sel = document.getElementById('sub-pop');
    if (sel) sel.value = valeur;
    location.hash = '#/subscribers';
    if (state.view === 'subscribers') loadSubscribers();
    return;
  }
  if (type === 'node') {
    location.hash = '#/network';
    setTimeout(() => { if (typeof topo !== 'undefined') { topo.selected = valeur; renderTopoCanvas(); renderTopoPanel(); } }, 600);
    return;
  }
  if (type === 'ip') {
    location.hash = '#/traffic';
    setTimeout(() => {
      const champ = document.getElementById('svc-lookup');
      if (champ) { champ.value = valeur; lookupIp(valeur); champ.scrollIntoView({ block: 'center' }); }
    }, 400);
  }
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
  // Sous 10 Kbps, une decimale : arrondir 1.4 Kbps a "1 Kbps" faisait lire un
  // debit stable la ou il varie de 40 %.
  if (n >= 1e4) return { v: (n / 1e3).toFixed(0), u: 'Kbps' };
  if (n >= 1e3) return { v: sansZero((n / 1e3).toFixed(1)), u: 'Kbps' };
  return { v: n.toFixed(0), u: 'bps' };
}
/** "1.0" -> "1", "2.50" -> "2.5" : un zero apres la virgule n'apprend rien. */
function sansZero(texte) { return String(texte).replace(/\.?0+$/, ''); }
function bpsText(v) { const b = bps(v); return b.v + ' ' + b.u; }
/** Graduation d'axe : la valeur EXACTE de la graduation. L'echelle arrondie
 *  peut valoir 2.5 Kbps ; l'etiqueter "3 Kbps" contredisait la pointe a 2k. */
function bpsAxis(v) {
  const n = Number(v) || 0;
  const [div, u] = n >= 1e9 ? [1e9, 'Gbps'] : n >= 1e6 ? [1e6, 'Mbps'] : n >= 1e3 ? [1e3, 'Kbps'] : [1, 'bps'];
  return sansZero((n / div).toFixed(2)) + ' ' + u;
}
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
/** La derniere serie de pings est-elle ENTIEREMENT perdue ? Ce n'est pas
 *  "pas de mesure" : c'est le pire cas -- ligne saturee qui jette les pings,
 *  ou client hors ligne. L'afficher "-" en gris le cachait. */
function pingsPerdus(detail) {
  if (!detail || !(Number(detail.sent) > 0) || Number(detail.received)) return false;
  // Si TOUS les clients mesures sont muets a la fois, c'est la SONDE qui ne
  // recoit rien (source injoignable, pare-feu) : pas une perte de chaque client.
  return !sondeMuette();
}

/** Tous les clients sondes (au moins deux) sans aucune reponse : la sonde. */
function sondeMuette() {
  const series = ((typeof exec !== 'undefined' && exec.subs) || [])
    .map((x) => x.rtt_detail).filter((d) => d && Number(d.sent) > 0);
  return series.length >= 2 && series.every((d) => !Number(d.received));
}
const SANS_REPONSE = 'All pings of the last series were lost: line saturated (the queue drops ' +
  'them) or client unreachable';

/** Une latence ABSENTE dit pourquoi. "-" seul se lisait comme "rien a
 *  signaler" alors que la sonde pouvait etre coupee, en erreur, ou muette.
 *  Rend [texte, severite, explication]. */
function rttVide(detail) {
  if (detail && detail.error) {
    return ['ping error', 'warn', 'The router could not run the ping: ' + detail.error +
      '. Check Settings > latency probe and the router (ping allowed, source address).'];
  }
  if (detail && Number(detail.sent) > 0 && !Number(detail.received)) {
    return sondeMuette()
      ? ['probe silent', 'warn', 'No client answers the probe at all: the probe is at fault, not ' +
        'the clients. Executive > « Find the cause » tells why.']
      : ['no reply', 'crit', SANS_REPONSE];
  }
  return ['-', 'none', 'No latency measured in the last 5 minutes: latency probe off ' +
    '(Executive > RTT probe), or this client not probed yet (about one minute after start).'];
}
function rttVideHtml(detail) {
  const [texte, sev, titre] = rttVide(detail);
  const couleur = sev === 'crit' ? 'var(--crit)' : sev === 'warn' ? 'var(--warn)' : 'var(--faint)';
  return '<span style="color:' + couleur + '" title="' + esc(titre) + '">' + esc(texte) + '</span>';
}
function rttVideSq(detail) {
  const [texte, sev, titre] = rttVide(detail);
  return sqCell(texte, sev, titre);
}

/** LE CLIENT UTILISE TOUT SON FORFAIT (85 % ou plus, dans un sens ou l'autre).
 *  Sa latence vient alors de SA propre file : ce n'est pas le reseau qui va
 *  mal. Elle s'affiche en neutre, et ne compte ni dans la pire latence d'un
 *  noeud ni dans sa note. */
/** Pings de la sonde marques EF (prioritaires dans la file CAKE) : la latence
 *  d'un client au plafond est alors celle de la ligne, elle compte normalement. */
let sondePrioritaire = false;
function auPlafond(s) {
  if (!s || sondePrioritaire) return false;
  const bas = (Number(s.effective_down_mbps) || 0) * 1e6;
  const haut = (Number(s.effective_up_mbps) || 0) * 1e6;
  return (bas > 0 && (Number(s.tx_bps) || 0) >= 0.85 * bas) ||
    (haut > 0 && (Number(s.rx_bps) || 0) >= 0.85 * haut);
}
const AU_PLAFOND = 'The client is using its whole plan right now: this latency comes from its own ' +
  'queue, not from the network. It is not counted against the network.';

/** Latence d'un client, en tenant compte du plafond. */
function rttClient(s, avecPastille) {
  if (!auPlafond(s)) return avecPastille ? null : rtt(s.rtt_ms, s.rtt_detail);
  const valeur = s.rtt_ms != null ? Math.round(s.rtt_ms) + ' ms'
    : rttVide(s.rtt_detail)[0];
  return avecPastille
    ? sqCell(valeur + ' · at limit', 'none', AU_PLAFOND)
    : '<span style="color:var(--muted)" title="' + esc(AU_PLAFOND) + '">' + esc(valeur) +
      ' <small>at limit</small></span>';
}

function rtt(value, detail) {
  if (value === null || value === undefined) return rttVideHtml(detail);
  const ms = Number(value);
  const color = ms < 30 ? 'var(--ok)' : ms < 100 ? 'var(--warn)' : 'var(--crit)';
  const titre = detail ? ' title="' + esc(rttDetailText(detail)) + '"' : '';
  return '<span style="color:' + color + '"' + titre + '>' + ms.toFixed(ms < 10 ? 1 : 0) + ' ms' +
    (detail && detail.loss_pct ? ' <small>(' + detail.loss_pct + '% loss)</small>' : '') + '</span>';
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
  // Un client a IP fixe n'a pas de session : "-", pas "0m" qui se lirait
  // comme une session qui vient de s'ouvrir.
  if (seconds === null || seconds === undefined || seconds === '') return '-';
  const s = Number(seconds);
  if (!s && s !== 0) return '-';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
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

/** Plus petite valeur "ronde" (1, 2, 2,5, 5 x 10^n) superieure ou egale a v.
 *
 *  Une echelle graduee en 437 Mbps oblige a calculer ; graduee en 500 Mbps,
 *  elle se lit d'un coup d'oeil. */
function niceCeil(v) {
  const n = Number(v) || 0;
  if (n <= 0) return 1;
  const puissance = Math.pow(10, Math.floor(Math.log10(n)));
  for (const f of [1, 2, 2.5, 5, 10]) {
    if (f * puissance >= n) return f * puissance;
  }
  return 10 * puissance;
}

/** Libelle d'heure adapte a la fenetre : l'heure seule suffit sur 24 h, les
 *  secondes n'ont de sens que sur quelques minutes. */
function tickClock(ts, spanMs) {
  if (!ts) return '';
  const d = new Date(ts);
  if (spanMs > 2 * 86400 * 1000) {
    return d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' }) + ' ' +
      d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  return d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** Maintenant / moyenne / pic d'une serie, pour le bandeau au-dessus du graphe. */
function seriesSummary(values) {
  const vals = values.filter((v) => isFinite(v));
  if (!vals.length) return { now: 0, avg: 0, peak: 0 };
  return {
    now: vals[vals.length - 1],
    avg: vals.reduce((a, b) => a + b, 0) / vals.length,
    peak: Math.max(...vals),
  };
}

/**
 * Graphe miroir : download au-dessus de l'axe, upload en dessous.
 * Cette forme rend immediatement lisible l'asymetrie d'un reseau d'acces,
 * bien plus qu'une superposition de deux courbes.
 *
 * Au-dessus, un bandeau MAINTENANT / MOYENNE / PIC par sens : c'est la question
 * qu'on pose a un graphe de debit, et la lire sur la courbe demande de viser.
 */
function renderThroughput(container, points, options) {
  const opt = options || {};
  const legende = opt.labels || { down: 'Download', up: 'Upload', extra: 'Subscribers' };
  container.innerHTML = '';
  if (!points || points.length === 0) {
    container.innerHTML = '<div class="empty">No measurement over this period.</div>';
    return;
  }

  const down = points.map((p) => Number(p.tx_bps) || 0);
  const up = points.map((p) => Number(p.rx_bps) || 0);
  // La POINTE de chaque pas, quand le serveur la fournit : sur un pas de
  // plusieurs minutes, la moyenne noie un test de debit de vingt secondes.
  const aPointes = points.some((p) => p.tx_peak_bps != null || p.rx_peak_bps != null);
  const downPk = points.map((p, i) => Math.max(down[i], Number(p.tx_peak_bps) || 0));
  const upPk = points.map((p, i) => Math.max(up[i], Number(p.rx_peak_bps) || 0));
  const sd = seriesSummary(down);
  const su = seriesSummary(up);
  sd.peak = Math.max(sd.peak, ...downPk);
  su.peak = Math.max(su.peak, ...upPk);
  // MAINTENANT = le dernier cycle de collecte quand le serveur le donne. La
  // derniere valeur de la serie est la moyenne d'un pas dont la duree suit la
  // periode (20 s sur 1 h, 8 min sur 24 h), souvent pas termine : "now"
  // changeait avec la periode choisie.
  if (opt.now) {
    sd.now = Number(opt.now.tx_bps) || 0;
    su.now = Number(opt.now.rx_bps) || 0;
  }
  const fig = (label, v) => '<div><span>' + label + '</span><b>' + esc(bpsText(v)) + '</b></div>';
  const resume = document.createElement('div');
  resume.className = 'chart-summary';
  resume.innerHTML =
    '<div class="cs d"><span class="cs-name"><i></i>' + esc(legende.down) + '</span>' +
      fig('Now', sd.now) + fig('Average', sd.avg) + fig('Peak', sd.peak) + '</div>' +
    '<div class="cs u"><span class="cs-name"><i></i>' + esc(legende.up) + '</span>' +
      fig('Now', su.now) + fig('Average', su.avg) + fig('Peak', su.peak) + '</div>';
  container.appendChild(resume);

  const W = Math.max(320, container.clientWidth);
  const H = 260;
  const M = { top: 14, right: 14, bottom: 24, left: 70 };
  const iw = W - M.left - M.right;
  const ih = H - M.top - M.bottom;
  // Une echelle arrondie PAR SENS, et une hauteur proportionnelle a chacune.
  // Sur un reseau d'acces le montant pese souvent cinq fois moins que le
  // descendant : une echelle commune laissait la moitie basse vide et la
  // courbe montante ecrasee sur l'axe. Chaque moitie porte ses graduations.
  const pd = niceCeil(Math.max(1, ...downPk));
  const pu = niceCeil(Math.max(1, ...upPk));
  const part = Math.max(0.55, Math.min(0.7, pd / (pd + pu)));
  const hTop = ih * part;
  const hBot = ih - hTop;
  const zeroY = M.top + hTop;

  const x = (i) => M.left + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw);
  const yDown = (v) => zeroY - (v / pd) * hTop;
  const yUp = (v) => zeroY + (v / pu) * hBot;

  const svg = svgEl('svg', {
    class: 'chart', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H,
  });

  // Grille : 0, 50 % et 100 % de l'echelle arrondie, de part et d'autre.
  [0, 0.5, 1].forEach((f) => {
    [yDown(pd * f), yUp(pu * f)].forEach((yy) => {
      svg.appendChild(svgEl('line', {
        class: f === 0 ? 'zero' : 'grid-line', x1: M.left, x2: W - M.right, y1: yy, y2: yy,
      }));
    });
    if (f > 0) {
      [[yDown(pd * f), bpsAxis(pd * f)], [yUp(pu * f), bpsAxis(pu * f)]].forEach(([yy, text]) => {
        const t = svgEl('text', { class: 'axis-label', x: M.left - 8, y: yy + 3, 'text-anchor': 'end' });
        t.textContent = text;
        svg.appendChild(t);
      });
    }
  });
  // Le sens de chaque moitie, ecrit dans le graphe : plus besoin de chercher
  // la legende pour savoir si le haut est le descendant.
  // Contre la ligne du zero, de part et d'autre : les pics sont aux extremites
  // (en haut pour le descendant, en bas pour le montant), les etiquettes n'y
  // chevauchent plus le chiffre du pic ni l'axe des temps.
  [[zeroY - 5, '↓ ' + legende.down, 'var(--down)'],
    [zeroY + 13, '↑ ' + legende.up, 'var(--up)']].forEach(([yy, text, col]) => {
    const t = svgEl('text', { class: 'axis-side', x: M.left + 6, y: yy, fill: col });
    t.textContent = text;
    svg.appendChild(t);
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
  if (aPointes) {
    // Trait fin pointille : la pointe atteinte dans chaque pas.
    svg.appendChild(svgEl('path', { d: line(downPk, yDown), fill: 'none', stroke: 'var(--down)', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0.8 }));
    svg.appendChild(svgEl('path', { d: line(upPk, yUp), fill: 'none', stroke: 'var(--up)', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0.8 }));
  }
  svg.appendChild(svgEl('path', { d: line(down, yDown), fill: 'none', stroke: 'var(--down)', 'stroke-width': 1.8, 'stroke-linejoin': 'round' }));
  svg.appendChild(svgEl('path', { d: line(up, yUp), fill: 'none', stroke: 'var(--up)', 'stroke-width': 1.8, 'stroke-linejoin': 'round' }));

  // Pic de chaque sens, marque et chiffre sur la courbe.
  // Le chiffre se pose A COTE du point (a droite, ou a gauche pres du bord) :
  // dessous, celui du montant tombait sur l'axe des temps.
  [[downPk, yDown, 'var(--down)'], [upPk, yUp, 'var(--up)']].forEach(([vals, yFn, col]) => {
    const iMax = vals.indexOf(Math.max(...vals));
    if (iMax < 0 || !vals[iMax]) return;
    svg.appendChild(svgEl('circle', { cx: x(iMax), cy: yFn(vals[iMax]), r: 3, fill: col }));
    const aDroite = x(iMax) < W - 90;
    const t = svgEl('text', {
      class: 'peak-label', x: x(iMax) + (aDroite ? 7 : -7),
      y: Math.min(M.top + ih - 2, Math.max(M.top + 9, yFn(vals[iMax]) + 3)), fill: col,
      'text-anchor': aDroite ? 'start' : 'end',
    });
    t.textContent = 'peak ' + bpsShort(vals[iMax]);
    svg.appendChild(t);
  });

  // Axe des temps : un repere tous les ~110 px, avec une ligne verticale
  // discrete pour situer une pointe sans compter les pixels.
  const spanMs = new Date(points[points.length - 1].bucket) - new Date(points[0].bucket);
  const nTicks = Math.max(2, Math.min(points.length, Math.floor(iw / 110) + 1));
  const vus = new Set();
  for (let k = 0; k < nTicks; k++) {
    const i = Math.round((k / (nTicks - 1)) * (points.length - 1));
    if (vus.has(i) || !points[i]) continue;
    vus.add(i);
    if (k > 0 && k < nTicks - 1) {
      svg.appendChild(svgEl('line', { class: 'grid-line v', x1: x(i), x2: x(i), y1: M.top, y2: M.top + ih }));
    }
    const t = svgEl('text', {
      class: 'axis-label', x: x(i), y: H - 6,
      'text-anchor': k === 0 ? 'start' : k === nTicks - 1 ? 'end' : 'middle',
    });
    t.textContent = tickClock(points[i].bucket, spanMs);
    svg.appendChild(t);
  }

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
    (point.tx_peak_bps != null && Number(point.tx_peak_bps) > down
      ? '<div class="row"><span style="color:var(--down)">&nbsp;&nbsp;peak</span><span>' + esc(bpsText(point.tx_peak_bps)) + '</span></div>' : '') +
    '<div class="row"><span style="color:var(--up)">' + esc(lib.up) + '</span><span>' + esc(bpsText(up)) + '</span></div>' +
    (point.rx_peak_bps != null && Number(point.rx_peak_bps) > up
      ? '<div class="row"><span style="color:var(--up)">&nbsp;&nbsp;peak</span><span>' + esc(bpsText(point.rx_peak_bps)) + '</span></div>' : '') +
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
  const courbe = loadThroughput();
  const top = loadTopTalkers();
  const ports = loadPortsLive();
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
    // Aucun backhaul declare : "0 Mbps" se lisait comme un lien en panne.
    (overview.backhauls
      ? statCard('', 'Backhaul capacity', (overview.backhaul_capacity_mbps || 0).toFixed(0), 'Mbps',
        esc(overview.backhauls + ' link(s) measured'))
      : statCard('', 'Backhaul capacity', '-', '', 'no radio backhaul declared'));

  // En parallele : la courbe et le top partent AVANT les chiffres de tete,
  // et chaque bloc s'affiche des que SA donnee arrive.
  renderBackhaulCards(tree);
  await Promise.all([courbe, top, ports]);
}

/** PORTS EN DIRECT : tout ce que les routeurs comptent, sessions ou non.
 *
 *  La courbe au-dessus additionne les sessions d'abonnes. Un test de debit
 *  lance depuis un CPE ou entre deux routeurs n'en traverse aucune : il
 *  n'apparaissait nulle part, alors que les ports le mesuraient. Ici, chaque
 *  port de chaque routeur, trie par debit, avec l'etat des cycles de mesure --
 *  pour qu'un tableau vide ne se lise jamais "rien ne passe" quand il veut
 *  dire "la mesure est en panne". */
const PORTS = { all: false };
const CYCLE_LABEL = { collect_subscribers: 'Subscribers', collect_links: 'Ports', probe_rtt: 'Latency' };

async function loadPortsLive() {
  const host = document.getElementById('ports-live');
  if (!host) return;
  let data;
  try {
    data = await api('/ports/live');
  } catch (err) {
    host.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const ports = data.ports || [];

  // 1. L'ETAT DES MESURES, en une pastille. Detaille seulement s'il y a un
  //    probleme : trois lignes vertes n'apprennent rien a chaque visite.
  const cycles = Object.entries(data.cycles || {});
  const soucis = cycles.filter(([, c]) => !c || !c.ok || c.age_s > 60);
  const sante = !soucis.length
    ? '<span class="pl-health ok" title="' + esc(cycles.map(([job, c]) =>
        (CYCLE_LABEL[job] || job) + ': ' + Math.round(c.age_s) + ' s ago').join(' · ')) +
      '"><i class="sq ok"></i>Measurement up to date</span>'
    : soucis.map(([job, c]) => '<span class="pl-health ' + (!c || !c.ok ? 'crit' : 'warn') + '"' +
        (c && c.errors && c.errors.length ? ' title="' + esc(c.errors.join(' ; ')) + '"' : '') + '>' +
        '<i class="sq ' + (!c || !c.ok ? 'crit' : 'warn') + '"></i>' + esc(CYCLE_LABEL[job] || job) + ': ' +
        (!c ? 'not run yet' : !c.ok ? '<b>failed</b>' + (c.errors && c.errors.length
          ? ' &middot; ' + esc(c.errors[0]) : '') : 'late (' + Math.round(c.age_s) + ' s)') +
        '</span>').join('');

  // Sens UNIFIE, celui des clients : down = vers les clients, up = vers
  // Internet. Sur l'uplink c'est rx/tx ; sur un port cote clients, l'inverse.
  const sens = (p) => p.upstream
    ? { down: p.rx_bps, up: p.tx_bps } : { down: p.tx_bps, up: p.rx_bps };
  const charge = (p) => p.capacity_mbps
    ? Math.max(p.rx_bps || 0, p.tx_bps || 0) / (p.capacity_mbps * 1e6) * 100 : null;
  const paire = (m) => !m ? '<span class="na">-</span>'
    : '<span class="nowrap" style="color:var(--down)">&darr; ' + esc(bpsText(m.down_bps)) + '</span> ' +
      '<span class="nowrap" style="color:var(--up)">&uarr; ' + esc(bpsText(m.up_bps)) + '</span>';

  const bilans = new Map((data.summary || []).map((b) => [b.router, b]));
  const routeurs = Array.from(new Set([...(data.routers || []), ...ports.map((p) => p.router_name)]));
  if (!routeurs.length) {
    host.innerHTML = '<div class="pl-top">' + sante + '</div>' +
      '<div class="empty">No router collected yet. Add one in Devices.</div>';
    return;
  }

  const cartes = routeurs.map((nom) => {
    const siens = ports.filter((p) => p.router_name === nom);
    const bilan = bilans.get(nom) || {};
    const net = bilan.internet;
    const satures = siens.filter((p) => (charge(p) || 0) >= 80);
    const tombes = siens.filter((p) => p.running === false);
    const actifs = siens.filter((p) => (p.rx_bps || 0) + (p.tx_bps || 0) > 2000);
    const montres = PORTS.all ? siens : actifs;
    const horsClients = bilan.unaccounted;
    const ecart = horsClients && (horsClients.down_bps + horsClients.up_bps) > 50000;

    const alertes = satures.map((p) => '<span class="badge crit">' + esc(p.interface) + ' at ' +
        Math.round(charge(p)) + '%</span>').join('') +
      tombes.map((p) => '<span class="badge warn">' + esc(p.interface) + ' down</span>').join('');

    const chiffre = (titre, valeur, aide, extra) =>
      '<div class="pl-kpi" title="' + esc(aide) + '"><span>' + titre + '</span>' + valeur +
        (extra || '') + '</div>';

    const lignes = montres.map((p) => {
      const m = sens(p);
      const c = charge(p);
      return '<tr' + (c !== null && c >= 80 ? ' class="pl-hot"' : '') + '>' +
        '<td class="nowrap"><code>' + esc(p.interface) + '</code>' +
          (p.upstream ? ' <span class="badge file">Internet</span>' : '') +
          (p.running === false ? ' <span class="badge warn">down</span>' : '') + '</td>' +
        '<td>' + (p.link_name ? esc(p.link_name) : '<span class="na">-</span>') + '</td>' +
        '<td class="num" style="color:var(--down)">' + esc(bpsText(m.down)) + '</td>' +
        '<td class="num" style="color:var(--up)">' + esc(bpsText(m.up)) + '</td>' +
        '<td style="width:170px">' + (c !== null ? meter(Math.max(p.rx_bps || 0, p.tx_bps || 0),
          p.capacity_mbps * 1e6) + '<span class="hint">' + esc(mbps(p.capacity_mbps)) + ' port</span>'
          : '<span class="na">speed unknown</span>') + '</td></tr>';
    }).join('');

    return '<div class="pl-router">' +
      '<div class="pl-head"><b>' + esc(nom) + '</b>' +
        (net ? '<span class="hint">uplink <code>' + esc(net.interface) + '</code>' +
          (net.name ? ' &rarr; ' + esc(net.name) : '') + '</span>' : '') +
        '<span class="pl-alerts">' + alertes + '</span></div>' +
      '<div class="pl-kpis">' +
        chiffre('Internet', paire(net), 'What this router exchanges with the outside, on its uplink port',
          net && net.capacity_mbps ? meter(Math.max(net.down_bps || 0, net.up_bps || 0),
            net.capacity_mbps * 1e6) : '') +
        chiffre('Clients', paire(bilan.clients), 'Sum of the subscribers of this router (their queues)') +
        chiffre('Not from clients', paire(horsClients),
          'Internet traffic that belongs to no known client: bandwidth tests, device management, ' +
          'undeclared clients. Internet minus Clients.',
          ecart ? '<span class="hint">tests, management or undeclared clients</span>' : '') +
      '</div>' +
      (siens.length
        ? '<div class="table-wrap"><table class="pl-ports"><thead><tr><th>Port</th><th>Towards</th>' +
          '<th class="num" title="Traffic going towards the clients">&darr; To clients</th>' +
          '<th class="num" title="Traffic going towards Internet">&uarr; To Internet</th>' +
          '<th>Load</th></tr></thead><tbody>' +
          (lignes || '<tr><td colspan="5" class="hint">No port with traffic right now.</td></tr>') +
          '</tbody></table></div>' +
          (siens.length > montres.length
            ? '<div class="hint pl-more">' + (siens.length - montres.length) + ' idle port(s) hidden</div>' : '')
        : '<div class="hint">No port measured in the last two minutes.</div>') +
      '</div>';
  }).join('');

  host.innerHTML = '<div class="pl-top">' + sante +
      '<button class="sm" id="ports-toggle">' + (PORTS.all ? 'Hide idle ports' : 'Show idle ports') +
      '</button></div>' + cartes;
  document.getElementById('ports-toggle').addEventListener('click', () => {
    PORTS.all = !PORTS.all;
    loadPortsLive();
  });
}

/* Filtres du graphe de debit : par routeur, ou par client. */
const TP = { router: '', clientId: null, charge: false, clients: [], routeurs: [], sites: [] };

/** Remplit les deux filtres, une fois : la liste des routeurs et celle des
 *  clients changent rarement, les relire a chaque rafraichissement couterait
 *  une requete toutes les 10 s pour rien. */
async function remplirFiltresDebit() {
  if (TP.charge) return;
  TP.charge = true;
  const [routeurs, clients, sites] = await Promise.all([
    api('/pops/routers').catch(() => []),
    api('/subscribers/latest?limit=500&order_by=login&include_unmeasured=true').catch(() => []),
    api('/pops').catch(() => []),
  ]);
  TP.sites = Array.isArray(sites) ? sites : [];
  const liste = Array.isArray(routeurs) ? routeurs : (routeurs.routers || []);
  TP.routeurs = liste;
  document.getElementById('tp-router').innerHTML = '<option value="">All routers</option>' +
    liste.map((r) => '<option value="' + esc(r.name) + '">' + esc(r.name) +
      (r.pop_name && r.pop_name !== r.name ? ' (' + esc(r.pop_name) + ')' : '') +
      '</option>').join('');
  document.getElementById('tp-router').value = TP.router;
  TP.clients = (clients || []).filter((c) => c.login);
  remplirClientsDebit();
}

/** Les clients proposes suivent le routeur choisi (son PoP et ses VLAN). */
function remplirClientsDebit() {
  const routeur = TP.router;
  const sites = new Set();
  if (routeur) {
    sites.add(routeur);
    TP.routeurs.forEach((r) => { if (r.name === routeur && r.pop_name) sites.add(r.pop_name); });
    // Les sites VLAN que porte ce routeur.
    TP.sites.forEach((x) => { if (x.router_name === routeur) sites.add(x.name); });
  }
  document.getElementById('tp-clients').innerHTML = TP.clients
    .filter((c) => !routeur || sites.has(c.pop_name) || sites.has(c.router_name))
    .map((c) => '<option value="' + esc(c.login) + '">' + esc(c.pop_name || '') + '</option>')
    .join('');
}

async function loadThroughput() {
  const minutes = state.rangeMinutes;
  // Environ 180 points quelle que soit la fenetre : au-dela, le trace se brouille
  // et la requete grossit pour rien.
  const bucket = Math.max(10, Math.round((minutes * 60) / 180 / 10) * 10);
  // Filtres : un client l'emporte sur un routeur (il en fait partie).
  const filtre = TP.clientId ? '&subscriber_id=' + TP.clientId
    : TP.router ? '&router=' + encodeURIComponent(TP.router) : '';
  remplirFiltresDebit();
  const data = await api('/throughput?minutes=' + minutes + '&bucket_seconds=' + bucket + filtre);
  state.lastPoints = data.points;
  state.lastNow = data.now || null;
  renderThroughput(document.getElementById('throughput-chart'), data.points, { now: data.now });
}

async function loadTopTalkers() {
  const brutes = await api('/subscribers/latest?limit=12');
  // Un "gros consommateur" mesure il y a une heure n'en est pas un maintenant.
  const rows = (brutes || []).filter(mesureFraiche);
  const host = document.getElementById('top-talkers');
  if (!rows.length) {
    host.innerHTML = (brutes || []).length
      ? '<div class="notice warn"><b>No current measurement.</b> The last subscriber samples are ' +
        esc(depuis(brutes[0].ts)) + ': the Subscribers cycle is failing (see Router ports below).</div>'
      : '<div class="empty">No active session.<br>Connect a PoP in the Devices tab.</div>';
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
        '<td class="num">' + rttClient(r) + '</td>' +
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
const exec = { nodes: [], subs: [], subsById: {}, bloatById: {}, selected: null, expanded: new Set() };

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
  // LES BLOCS LENTS NE RETIENNENT PAS LES AUTRES. La heatmap, les points de
  // saturation et la latence par segment lisent des series : ils partent en
  // meme temps que le reste et s'affichent chacun a leur arrivee, au lieu de
  // faire attendre tout l'ecran derriere la requete la plus lente.
  const heures = Math.max(1, Math.ceil(minutes / 60));
  // La sante dans le temps suit la SELECTION : elle se recharge pour le site
  // choisi (cf. chargerSante). Ici, le premier chargement.
  exec.heatParams = { minutes, buckets };
  exec.heatSite = undefined;
  const pHeat = grab(chargerSante());
  const pPoints = api('/capacity/hotspots?hours=' + heures).catch(() => null);
  const pLatence = api('/latency').catch(() => null);
  api('/latency/clients?minutes=' + Math.max(5, minutes)).catch(() => null)
    .then((lc) => { exec.latencyClients = lc; renderLatencyClients(); });
  pLatence.then((latence) => {
    exec.latency = latence;
    renderLatencySegments(document.getElementById('exec-latency'), latence);
  });
  pPoints.then((points) => {
    exec.hotspots = (points && points.hotspots) || [];
    exec.hotspotData = points || null;
    // La capacite de chaque noeud (son lien montant) arrive avec les points
    // de saturation : le tableau des noeuds se redessine avec elle.
    if (exec.nodes) renderNodeTable(document.getElementById('exec-nodes'));
    renderHotspots(document.getElementById('exec-hotspots'), points);
    // Le verdict nomme les liens satures : il se redessine quand ils arrivent.
    if (exec.subs) renderExecSummary(document.getElementById('exec-summary'));
  });
  const [subsRaw, bloat, topoData, tree, rttState, sites, routeurs] = await Promise.all([
    grab(api('/subscribers/latest?limit=500&order_by=login')),
    api('/bufferbloat?minutes=' + minutes).catch(() => null),
    api('/topology').catch(() => null),
    api('/network/tree').catch(() => []),
    api('/rtt').catch(() => null),
    api('/pops').catch(() => []),
    api('/pops/routers').catch(() => null),
  ]);
  // Qui porte quoi : un site VLAN appartient a un routeur, il se range SOUS
  // le noeud de ce routeur (cf. fusionnerVlans) : une VLAN = un client.
  exec.sites = {};
  (Array.isArray(sites) ? sites : []).forEach((x) => { exec.sites[x.name] = x; });
  exec.popDuRouteur = {};
  (Array.isArray(routeurs) ? routeurs : ((routeurs && routeurs.routers) || [])).forEach((r) => {
    if (r && r.name) exec.popDuRouteur[r.name] = r.pop_name || r.name;
  });
  const subs = Array.isArray(subsRaw) ? subsRaw : [];
  sondePrioritaire = !!(rttState && rttState.probe_prioritized);
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
  exec.nodes = fusionnerVlans(aggregateNodes(subs, cc));
  // Aucun abonne mesure mais des routeurs connectes : on montre quand meme le
  // reseau reel (topologie), sinon l'onglet reste desesperement vide.
  const fromTopo = exec.nodes.length === 0;
  if (fromTopo) exec.nodes = nodesFromTopology(topoData, cc);
  // Selection par defaut : le noeud le plus charge, tant que rien n'est choisi.
  if (!selectionExists(exec.selected)) {
    exec.selected = exec.nodes.length ? { type: 'node', name: exec.nodes[0].name } : null;
  }

  exec.subs = subs;
  renderExecSummary(document.getElementById('exec-summary'));
  renderExecLoad(document.getElementById('exec-load'));
  renderQueuePanels();
  renderNodeTable(document.getElementById('exec-nodes'));
  renderExecLegend(document.getElementById('exec-legend'));
  document.getElementById('exec-count').textContent =
    exec.nodes.length + ' node(s), ' + subs.length + ' client(s)';
  renderExecNotice(rttState, firstError, {
    noNodes: exec.nodes.length === 0,
    topoOnly: fromTopo && exec.nodes.length > 0,
  });
  // Le rafraichissement suivant attend quand meme les blocs lents : sinon ils
  // s'empileraient toutes les dix secondes sur un serveur deja charge.
  await Promise.all([pHeat, pPoints, pLatence]);
}

/** Note d'experience d'un client : le score composite du serveur, ou le proxy
 *  latence a defaut. null quand on ne sait rien -- jamais "bon" par defaut. */
function clientScore(s) {
  const note = qoeOf(s.subscriber_id);
  if (note) return note.score;
  // Au plafond : sa latence du moment est celle de sa file, pas un verdict.
  if (auPlafond(s)) return null;
  return qoeScore(s.rtt_ms);
}

/** Taux d'utilisation d'un noeud : son debit descendant face a sa limite
 *  effective (somme des plafonds poses). null sans limite connue. */
function nodeUtil(n) {
  if (n.synthetic || !n.effDown) return null;
  return pct(n.tx, n.effDown);
}

/** Mediane d'une liste de nombres (les null sont ignores). */
function median(values) {
  const v = values.filter((x) => x !== null && x !== undefined && isFinite(x))
    .map(Number).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

/** Lien qui selectionne un noeud dans le tableau (et ouvre ses panneaux). */
function nodeLink(name) {
  return '<a href="#" data-exec-node="' + esc(name) + '">' + esc(name) + '</a>';
}

function brancherLiensNoeuds(host) {
  host.querySelectorAll('[data-exec-node]').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      exec.selected = { type: 'node', name: a.dataset.execNode };
      renderQueuePanels();
      renderNodeTable(document.getElementById('exec-nodes'));
      const cible = document.getElementById('lq-heading');
      if (cible) cible.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

/** LE VERDICT EN UNE LIGNE, PUIS LES QUATRE CHIFFRES QUI LE FONDENT.
 *
 *  L'ecran executif doit repondre a "est-ce que ca va ?" avant qu'on lise un
 *  tableau. Le verdict ne dit que des faits mesures : un noeud a plus de 90 %
 *  de sa limite, un client dont la note est mauvaise. Rien de mesure ne donne
 *  un verdict neutre, jamais un "tout va bien" invente. */
function renderExecSummary(host) {
  if (!host) return;
  const nodes = exec.nodes.filter((n) => !n.synthetic);
  const subs = exec.subs || [];
  const down = nodes.reduce((a, n) => a + n.tx, 0);
  const up = nodes.reduce((a, n) => a + n.rx, 0);

  // Capacite partagee (backhauls) des noeuds qui en declarent une.
  let capa = 0;
  nodes.forEach((n) => {
    const env = (exec.envByPop || {})[n.name] || {};
    capa += (env.capacity || env.nominal || 0) * 1e6;
  });
  const limite = nodes.reduce((a, n) => a + n.effDown, 0);

  const notes = { good: 0, fair: 0, poor: 0, none: 0 };
  subs.forEach((s) => {
    const sc = clientScore(s);
    if (sc === null) notes.none += 1;
    else if (sc >= 80) notes.good += 1;
    else if (sc >= 50) notes.fair += 1;
    else notes.poor += 1;
  });

  const charges = nodes.map((n) => ({ n, u: nodeUtil(n) })).filter((x) => x.u !== null)
    .sort((a, b) => b.u - a.u);
  const satures = charges.filter((x) => x.u >= 90);
  const occupes = charges.filter((x) => x.u >= 70 && x.u < 90);
  const rttMed = median(subs.map((s) => s.rtt_ms));
  const pireRtt = nodes.filter((n) => n.rttMax !== null).sort((a, b) => b.rttMax - a.rttMax)[0];

  // ---- le verdict
  const faits = [];
  const liste = (xs) => xs.slice(0, 3).map((x) => nodeLink(x.n.name)).join(', ') +
    (xs.length > 3 ? ' +' + (xs.length - 3) : '');
  if (satures.length) {
    faits.push(['crit', satures.length + ' node(s) above 90% of their limit: ' + liste(satures)]);
  }
  const chauds = (exec.hotspots || []).filter((h) => h.state === 'saturated');
  const tendus = (exec.hotspots || []).filter((h) => h.state === 'busy');
  const nomLien = (h) => '<b>' + esc(h.name) + '</b> <span class="pct-hint">(' +
    esc(HOT_SIDE[h.side] || h.side) + ')</span>';
  if (chauds.length) {
    faits.push(['crit', chauds.length + ' link(s) at 90%+ of capacity: ' +
      chauds.slice(0, 3).map(nomLien).join(', ')]);
  }
  if (tendus.length) {
    faits.push(['warn', tendus.length + ' link(s) between 70 and 90%: ' +
      tendus.slice(0, 3).map(nomLien).join(', ')]);
  }
  // Une ANTENNE qui chute : avant que ses clients ne le sentent a la pointe.
  ((exec.hotspotData && exec.hotspotData.radio_alerts) || []).slice(0, 4).forEach((r) => {
    faits.push([r.share_of_nominal < 0.5 ? 'crit' : 'warn', 'Radio <b>' + esc(r.name) + '</b>' +
      (r.pop_name ? ' <span class="pct-hint">(' + esc(r.pop_name) + ')</span>' : '') +
      ' carries ' + esc(mbps(r.capacity_mbps)) + ' of ' + esc(mbps(r.nominal_mbps)) + ' (' +
      Math.round(r.share_of_nominal * 100) + '% of nominal): rain, interference or alignment']);
  });
  if (notes.poor) faits.push(['crit', notes.poor + ' client(s) with a poor experience']);
  if (sondeMuette()) {
    faits.push(['warn', 'The latency probe gets no reply from any client: the probe is at fault, ' +
      'not the clients. <button type="button" class="sm" data-rtt-diag>Find the cause</button>']);
  }
  // "Tout va bien" ne peut pas s'afficher quand la majorite des clients notes
  // n'a qu'une experience moyenne : la tuile d'a cote disait "0 % good".
  const notesConnues = notes.good + notes.fair + notes.poor;
  if (notes.fair && notes.fair * 2 >= notesConnues) {
    faits.push(['warn', notes.fair + ' of ' + notesConnues + ' client(s) with only a fair ' +
      'experience (latency or bufferbloat)']);
  }
  if (occupes.length) {
    faits.push(['warn', occupes.length + ' node(s) between 70 and 90%: ' + liste(occupes)]);
  }
  if (pireRtt && pireRtt.rttMax >= 100) {
    faits.push(['warn', 'High latency on ' + nodeLink(pireRtt.name) + ': ' +
      Math.round(pireRtt.rttMax) + ' ms']);
  }
  let verdict;
  if (!nodes.length) {
    verdict = '<div class="verdict none"><span class="v-dot"></span><div>' +
      '<b>No traffic measured yet.</b></div></div>';
  } else if (!faits.length) {
    verdict = '<div class="verdict ok"><span class="v-dot"></span><div>' +
      '<b>All good.</b> No saturated node, no client with a poor experience.</div></div>';
  } else {
    const pire = faits.some((f) => f[0] === 'crit') ? 'crit' : 'warn';
    verdict = '<div class="verdict ' + pire + '"><span class="v-dot"></span><div>' +
      '<b>' + (pire === 'crit' ? 'Needs attention.' : 'Worth watching.') + '</b>' +
      '<ul>' + faits.map((f) => '<li><span class="sq ' + f[0] + '"></span>' + f[1] + '</li>')
        .join('') + '</ul></div></div>';
  }

  // ---- les quatre chiffres
  const b = bps(down);
  const refCapa = capa || limite;
  const partCapa = refCapa ? pct(down, refCapa) : null;
  const tuile1 = '<div class="card stat down"><div class="label">Traffic now</div>' +
    '<div class="value">' + esc(b.v) + '<span class="unit">' + esc(b.u) + '</span></div>' +
    '<div class="sub"><span style="color:var(--up)">&uarr; ' + esc(bpsText(up)) + '</span>' +
    (partCapa === null ? '' : ' &middot; ' + partCapa.toFixed(0) + '% of ' +
      (capa ? 'backhaul capacity' : 'the sum of limits')) + '</div>' +
    (partCapa === null ? '' : meter(down, refCapa)) + '</div>';

  // La part "bonne" se calcule sur les clients NOTES : un client sans mesure
  // n'est ni bon ni mauvais, il est inconnu, et il est compte a part.
  const connues = notes.good + notes.fair + notes.poor;
  const barre = (k, n) => n ? '<span class="qbar-seg ' + k + '" style="flex:' + n + '" title="' +
    n + ' ' + k + '"></span>' : '';
  const tuile2 = '<div class="card stat"><div class="label">Client experience</div>' +
    (connues
      ? '<div class="value">' + Math.round((notes.good / connues) * 100) +
        '<span class="unit">% good</span></div>'
      : '<div class="value">-</div>') +
    '<div class="qbar">' + barre('ok', notes.good) + barre('warn', notes.fair) +
      barre('crit', notes.poor) + barre('none', notes.none) + '</div>' +
    '<div class="sub qlegend"><span><i class="sq ok"></i>' + notes.good + ' good</span>' +
      '<span><i class="sq warn"></i>' + notes.fair + ' fair</span>' +
      '<span><i class="sq crit"></i>' + notes.poor + ' poor</span>' +
      (notes.none ? '<span><i class="sq none"></i>' + notes.none + ' unknown</span>' : '') +
    '</div></div>';

  const top = charges[0];
  const tuile3 = '<div class="card stat' + (top && top.u >= 90 ? ' crit' : top && top.u >= 70 ? ' warn' : '') +
    '"><div class="label">Busiest node</div>' +
    (top
      ? '<div class="value">' + top.u.toFixed(0) + '<span class="unit">%</span></div>' +
        '<div class="sub">' + nodeLink(top.n.name) + ' &middot; ' + esc(bpsText(top.n.tx)) +
          ' of ' + esc(mbps(top.n.effDown / 1e6)) + '</div>' + meter(top.n.tx, top.n.effDown)
      : '<div class="value">-</div><div class="sub">no limit known</div>') + '</div>';

  const tuile4 = '<div class="card stat"><div class="label">Latency (median)</div>' +
    (rttMed === null
      ? '<div class="value">-</div><div class="sub">RTT probe off or no reply</div>'
      : '<div class="value" style="color:' + (rttMed < 30 ? 'var(--ok)' : rttMed < 100 ? 'var(--warn)' : 'var(--crit)') +
        '">' + Math.round(rttMed) + '<span class="unit">ms</span></div>' +
        '<div class="sub">' + (pireRtt ? 'worst: ' + nodeLink(pireRtt.name) + ' ' +
          Math.round(pireRtt.rttMax) + ' ms' : '') + '</div>') + '</div>';

  host.innerHTML = verdict + '<div class="grid stats">' + tuile1 + tuile2 + tuile3 + tuile4 + '</div>';
  brancherLiensNoeuds(host);
}

/** CHARGE PAR NOEUD : une barre par noeud, a l'echelle de sa propre limite.
 *
 *  Remplace le Sankey, qui n'avait qu'une source et ne disait donc qu'une
 *  proportion, sans la limite. Ici chaque barre dit ce qui passe, ce qui est
 *  permis, et la part que ca represente -- trois chiffres qu'on lisait avant
 *  dans trois colonnes differentes. */
function renderExecLoad(host) {
  if (!host) return;
  const nodes = exec.nodes.filter((n) => !n.synthetic);
  if (!nodes.length) {
    host.innerHTML = '<div class="empty">No node with measured traffic.</div>';
    return;
  }
  const total = nodes.reduce((a, n) => a + n.tx, 0) || 1;
  host.innerHTML = '<div class="loadbars">' + nodes.slice(0, 20).map((n) => {
    const u = nodeUtil(n);
    const sev = u === null ? 'none' : severity(u);
    const larg = u === null ? pct(n.tx, total) || 0 : Math.min(100, u);
    const env = (exec.envByPop || {})[n.name] || {};
    const capa = (env.capacity || env.nominal || 0) * 1e6;
    const repere = capa && n.effDown ? Math.min(100, (capa / n.effDown) * 100) : null;
    return '<div class="lb-row" data-exec-row="' + esc(n.name) + '">' +
      '<div class="lb-name">' + nodeLink(n.name) +
        '<span class="hint">' + n.circuits + ' client(s)</span></div>' +
      '<div class="lb-track" title="' + esc(bpsText(n.tx) + ' of ' + mbps(n.effDown / 1e6)) + '">' +
        '<div class="lb-fill ' + sev + '" style="width:' + larg.toFixed(1) + '%"></div>' +
        (repere !== null && repere < 100
          ? '<div class="lb-cap" style="left:' + repere.toFixed(1) + '%" title="Backhaul capacity ' +
            esc(mbps(capa / 1e6)) + '"></div>' : '') +
      '</div>' +
      '<div class="lb-val"><b>' + esc(bpsText(n.tx)) + '</b>' +
        '<span class="hint">' + (u === null ? 'no limit' : u.toFixed(0) + '% of ' +
          esc(mbps(n.effDown / 1e6))) + '</span></div>' +
      '<div class="lb-up"><span style="color:var(--up)">&uarr; ' + esc(bpsText(n.rx)) + '</span></div>' +
    '</div>';
  }).join('') + '</div>' +
  (nodes.length > 20 ? '<div class="hint" style="margin-top:.5rem">' + (nodes.length - 20) +
    ' more node(s) in the table below.</div>' : '') +
  '<div class="exec-legend"><span><i class="sq ok"></i>under 70% of limit</span>' +
    '<span><i class="sq warn"></i>70-90%</span><span><i class="sq crit"></i>over 90%</span>' +
    '<span><i class="lb-cap-key"></i>backhaul capacity</span></div>';
  brancherLiensNoeuds(host);
}

const HOT_SIDE = {
  internet: 'internet uplink',
  upstream: 'PoP to core',
  pop: 'PoP side',
};

/** POINTS DE SATURATION : chaque lien, ce qui y passe, ce qu'il peut porter.
 *
 *  Deux familles, parce qu'elles ne se traitent pas de la meme facon : le cote
 *  INTERNET (la sortie de la passerelle, les liens des PoPs vers le coeur) fait
 *  tomber tout le monde d'un coup ; le cote POP (vers les abonnes, un VLAN, un
 *  relais) ne fait tomber que ce qui pend dessous. La barre montre l'instant,
 *  le repere la pointe de la periode, et le chiffre la marge qui reste. */
function renderHotspots(host, data) {
  if (!host) return;
  const lignes = (data && data.hotspots) || [];
  if (!data) {
    host.innerHTML = '<div class="empty">Link measurements unavailable.</div>';
    return;
  }
  if (!lignes.length) {
    host.innerHTML = '<div class="empty">No link measured yet.</div>';
    return;
  }
  const connus = lignes.filter((h) => h.capacity_mbps);
  const inconnus = lignes.filter((h) => !h.capacity_mbps);
  const groupe = (titre, sous, xs) => {
    if (!xs.length) return '';
    return '<div class="hot-group"><div class="hot-title">' + titre +
      '<span class="pct-hint">' + sous + '</span></div>' + xs.slice(0, 12).map((h) => {
        const now = h.now_share === null ? null : Math.min(1.2, h.now_share);
        const pic = h.peak_share === null ? null : Math.min(1.2, h.peak_share);
        const sev = h.state === 'saturated' ? 'crit' : h.state === 'busy' ? 'warn' : 'ok';
        const nowMbps = Math.max(h.now_down_mbps || 0, h.now_up_mbps || 0);
        const pkMbps = Math.max(h.peak_down_mbps || 0, h.peak_up_mbps || 0);
        const pkQuand = (h.peak_down_mbps || 0) >= (h.peak_up_mbps || 0) ? h.peak_down_at : h.peak_up_at;
        const pkSens = (h.peak_down_mbps || 0) >= (h.peak_up_mbps || 0) ? 'down' : 'up';
        return '<div class="hot-row">' +
          '<div class="hot-name"><b>' + esc(h.name) + '</b> ' + badgeMilieu(h) +
            '<span class="hint">' + esc(h.router) + ' &middot; ' + esc(h.interface) +
            ' <a href="#" class="medium-edit" data-medium-router="' + esc(h.router) +
            '" data-medium-iface="' + esc(h.interface) + '">wired / radio…</a></span></div>' +
          '<div class="hot-bar" title="Capacity ' + esc(mbps(h.capacity_mbps)) + ' (' +
            esc(h.capacity_source || '') + ')">' +
            '<div class="lb-track"><div class="lb-fill ' + sev + '" style="width:' +
              Math.min(100, (now || 0) * 100).toFixed(1) + '%"></div>' +
              (pic !== null ? '<div class="hot-peak" style="left:' + Math.min(100, pic * 100).toFixed(1) +
                '%" title="Peak ' + esc(mbps(pkMbps)) + '"></div>' : '') +
            '</div>' +
          '</div>' +
          '<div class="hot-val"><b>' + (h.now_share === null ? '-' : Math.round(h.now_share * 100) + '%') +
            '</b> <span class="pct-hint">now</span>' +
            '<span class="hint">' + esc(mbps(nowMbps)) + ' of ' + esc(mbps(h.capacity_mbps)) + '</span></div>' +
          '<div class="hot-val"><b class="sev-' + sev + '">' +
            (h.peak_share === null ? '-' : Math.round(h.peak_share * 100) + '%') +
            '</b> <span class="pct-hint">peak ' + pkSens + '</span>' +
            '<span class="hint">' + (pkQuand ? esc(clock(pkQuand)) : '') + '</span></div>' +
          '<div class="hot-val"><b>' + esc(mbps(Math.max(0, h.headroom_mbps))) + '</b>' +
            '<span class="hint">headroom</span></div>' +
        '</div>';
      }).join('') + '</div>';
  };
  const amont = connus.filter((h) => h.side !== 'pop');
  const pop = connus.filter((h) => h.side === 'pop');
  host.innerHTML =
    groupe('Internet side', 'gateway uplink and PoP-to-core links: a saturation here hits everyone', amont) +
    groupe('PoP side', 'towards subscribers, VLANs and relays', pop) +
    (!amont.length && (data.upstream_known || []).length === 0
      ? '<div class="notice">Upstream links are identified from each router\'s default route at ' +
        'discovery: run <a href="#/network">discovery</a> once to split internet side from PoP side.</div>'
      : '') +
    (inconnus.length
      ? '<details class="hot-unknown"><summary>' + inconnus.length + ' measured link(s) without a known ' +
        'capacity</summary><div class="hint" style="margin:.4rem 0">Set their capacity with the ' +
        '<b>Bandwidth</b> button on the link in the <a href="#/network">network tree</a> to track their ' +
        'saturation.</div>' + inconnus.slice(0, 20).map((h) => '<div class="hot-mini"><b>' + esc(h.name) +
        '</b> <span class="pct-hint">' + esc(h.router) + ' &middot; ' + esc(h.interface) + ' &middot; ' +
        esc(HOT_SIDE[h.side] || h.side) + ' <a href="#" class="medium-edit" data-medium-router="' +
        esc(h.router) + '" data-medium-iface="' + esc(h.interface) + '">wired / radio…</a>' +
        '</span><span>' +
        esc(mbps(Math.max(h.now_down_mbps || 0, h.now_up_mbps || 0))) + ' now, peak ' +
        esc(mbps(Math.max(h.peak_down_mbps || 0, h.peak_up_mbps || 0))) + '</span></div>').join('') +
        '</details>'
      : '') +
    '<div class="exec-legend"><span><i class="sq ok"></i>under 70%</span><span><i class="sq warn"></i>70-90%</span>' +
      '<span><i class="sq crit"></i>90%+</span><span><i class="hot-peak-key"></i>peak over the period</span>' +
      '<span>Capacity: <b>wired</b> = fixed (declared, or port speed) · <b>radio</b> = read live on ' +
      'its antenna · <b>(auto)</b> = detected by itself</span></div>';
  host.querySelectorAll('.medium-edit').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    const ligne = lignes.find((h) => h.router === a.dataset.mediumRouter && h.interface === a.dataset.mediumIface);
    ouvrirMilieu(ligne || { router: a.dataset.mediumRouter, interface: a.dataset.mediumIface });
  }));
}

/** Declarer un lien filaire (capacite fixe) ou radio (capacite de l'antenne,
 *  en direct). Fenetre a part : le rafraichissement de la page ne l'efface pas. */
async function ouvrirMilieu(h) {
  let infos = { media: [], antennas: [] };
  try { infos = await api('/capacity/media'); } catch (err) { /* liste vide */ }
  const actuel = (infos.media || []).find((m) => m.router_name === h.router && m.interface === h.interface) || {};
  const dlg = document.createElement('dialog');
  dlg.className = 'medium-dialog';
  const antennes = (infos.antennas || []).filter(Boolean);
  dlg.innerHTML = '<form method="dialog" class="medium-form">' +
    '<h3>' + esc(h.name || h.interface) + ' <span class="pct-hint">' + esc(h.router) + ' · ' + esc(h.interface) + '</span></h3>' +
    '<p class="hint">Where does the capacity of this link come from?</p>' +
    '<label class="medium-opt"><input type="radio" name="medium" value=""' + (!actuel.medium ? ' checked' : '') + '>' +
      '<span><b>Automatic</b> (recommended)<span class="hint">Detected by itself: a link towards a radio ' +
      '(or a radio port) is radio and follows the antenna recognised at its end; the rest is wired.</span></span></label>' +
    '<label class="medium-opt"><input type="radio" name="medium" value="wired"' + (actuel.medium === 'wired' ? ' checked' : '') + '>' +
      '<span><b>Wired</b> (fibre, copper) <span class="hint">Fixed capacity, nothing to poll.</span>' +
      '<span class="medium-sub">Capacity <input type="number" name="capacity" min="1" step="any" ' +
        'placeholder="port speed' + (h.port_speed_mbps ? ' (' + esc(mbps(h.port_speed_mbps)) + ')' : '') + '" value="' +
        esc(actuel.capacity_mbps || '') + '"> Mbps</span></span></label>' +
    '<label class="medium-opt"><input type="radio" name="medium" value="radio"' + (actuel.medium === 'radio' ? ' checked' : '') + '>' +
      '<span><b>Radio</b> (antenna) <span class="hint">Capacity read live on the antenna: it follows ' +
      'rain fade, interference and alignment, and an alert fires when it drops.</span>' +
      '<span class="medium-sub">Antenna <select name="antenna">' +
        (antennes.length ? antennes.map((r) => '<option value="' + esc(r.name) + '"' +
          (actuel.backhaul_name === r.name ? ' selected' : '') + '>' + esc(r.name) +
          (r.pop_name ? ' — ' + esc(r.pop_name) : '') +
          (r.capacity_mbps ? ' (' + esc(mbps(r.capacity_mbps)) + ' now)' : ' (silent)') + '</option>').join('')
          : '<option value="">No antenna polled yet — add it in Devices</option>') +
      '</select></span></span></label>' +
    '<div class="medium-error"></div>' +
    '<div class="actions"><button type="button" class="sm" data-cancel>Cancel</button>' +
      '<button type="submit" class="sm primary">Save</button></div></form>';
  document.body.appendChild(dlg);
  const fermer = () => { dlg.close(); dlg.remove(); };
  dlg.querySelector('[data-cancel]').addEventListener('click', fermer);
  dlg.addEventListener('cancel', fermer);
  dlg.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const milieu = f.medium.value;
    const erreur = dlg.querySelector('.medium-error');
    try {
      if (!milieu) {
        await api('/capacity/media/' + encodeURIComponent(h.router) + '/' + encodeURIComponent(h.interface),
          { method: 'DELETE' });
      } else {
        await api('/capacity/media', { method: 'PUT', body: JSON.stringify({
          router: h.router, interface: h.interface, medium: milieu,
          capacity_mbps: milieu === 'wired' && f.capacity.value ? Number(f.capacity.value) : null,
          backhaul_name: milieu === 'radio' ? (f.antenna.value || null) : null,
        }) });
      }
      fermer();
      toast(esc((h.name || h.interface) + ': ' + (milieu || 'auto') + '.'), 4000);
      refresh();
    } catch (err) {
      erreur.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    }
  });
  dlg.showModal();
}

/** Une mesure de latence lisible : la mediane, et tout le reste en infobulle. */
function latCell(st, seuils) {
  if (!st || st.median_ms === null || st.median_ms === undefined) {
    const perdu = st && st.sent && !st.received;
    return '<td class="num">' + (perdu ? sqCell('no reply', 'crit') : '<span class="na">-</span>') + '</td>';
  }
  const s = seuils || [30, 100];
  const m = Number(st.median_ms);
  let sev = m < s[0] ? 'ok' : m < s[1] ? 'warn' : 'crit';
  if (st.loss_pct >= 20) sev = 'crit';
  else if (st.loss_pct > 0 && sev === 'ok') sev = 'warn';
  const detail = 'median ' + m.toFixed(1) + ' ms' +
    (st.min_ms != null ? ' · min ' + Number(st.min_ms).toFixed(1) : '') +
    (st.max_ms != null ? ' · max ' + Number(st.max_ms).toFixed(1) : '') +
    (st.jitter_ms != null ? ' · jitter ' + Number(st.jitter_ms).toFixed(1) : '') +
    (st.p90_ms != null ? ' · p90 ' + Number(st.p90_ms).toFixed(1) : '') +
    ' ms · loss ' + (st.loss_pct == null ? '-' : st.loss_pct + '%') +
    (st.sent ? ' · ' + st.received + '/' + st.sent + ' replies' : '') +
    (st.subscribers ? ' · ' + st.subscribers + ' subscriber(s)' : '') +
    (st.age_s != null ? ' · ' + Math.round(st.age_s) + ' s ago' : '');
  return '<td class="num" title="' + esc(detail) + '">' + sqCell(m.toFixed(m < 10 ? 1 : 0) + ' ms', sev) +
    '<span class="lat-sub">' + (st.jitter_ms != null ? '±' + Number(st.jitter_ms).toFixed(1) + ' ' : '') +
    (st.loss_pct ? '<span class="sev-crit">' + st.loss_pct + '% loss</span>' : '') + '</span></td>';
}

/** LATENCE PAR SEGMENT : ou se perd le temps, routeur par routeur.
 *
 *  Le meme routeur mesure trois choses par la meme methode : ses abonnes
 *  (l'acces), sa passerelle (le lien vers le coeur), et internet. Comparer les
 *  trois dit ou chercher : internet lent mais passerelle rapide, c'est au-dessus
 *  du coeur ; passerelle deja lente, c'est entre le PoP et le coeur. */
/** Latence par CLIENT : chaque abonne, son ressenti, le pire en tete.
 *
 *  La latence par segment dit ou se perd le temps ; celle-ci dit QUI le
 *  subit. La mediane est l'habitude, le p95 les moments penibles, la latence
 *  sous charge ce qui fait hacher un appel quand quelqu'un telecharge. */
function renderLatencyClients() {
  const host = document.getElementById('exec-latency-clients');
  if (!host) return;
  const data = exec.latencyClients;
  const stats = document.getElementById('exec-lat-stats');
  const compte = document.getElementById('exec-lat-count');
  if (!data) { host.innerHTML = '<div class="empty">Latency unavailable.</div>'; return; }
  if (!data.enabled) {
    host.innerHTML = '<div class="notice"><b>RTT probe off.</b> Tick <b>RTT probe</b> above to ' +
      'measure each client\'s latency.</div>';
    if (stats) stats.innerHTML = '';
    return;
  }
  const s = data.summary || {};
  if (stats) {
    stats.innerHTML =
      statCard(s.poor ? 'crit' : '', 'Poor experience', String(s.poor || 0), '',
        'high latency, loss or bufferbloat') +
      statCard(s.fair ? 'warn' : '', 'Fair', String(s.fair || 0), '', 'noticeable but usable') +
      statCard('ok', 'Good', String(s.good || 0), '', 'under 30 ms, stable') +
      statCard('', 'Clients measured', String(s.measured || 0), '',
        'over ' + esc(data.minutes) + ' min');
  }
  const q = (document.getElementById('exec-lat-search').value || '').trim().toLowerCase();
  const filtre = document.getElementById('exec-lat-filter').value;
  const lignes = (data.clients || []).filter((c) =>
    (!filtre || c.experience === filtre) &&
    (!q || String(c.login || '').toLowerCase().includes(q) ||
      String(c.pop_name || '').toLowerCase().includes(q)));
  if (compte) compte.textContent = lignes.length + ' client(s)';
  if (!lignes.length) {
    host.innerHTML = '<div class="empty">' + ((data.clients || []).length
      ? 'No client matches this filter.'
      : 'No client measured yet: the probe pings ' + esc(data.method.count) + ' times every ' +
        esc(data.method.every_s) + ' s, a few clients per router at a time.') + '</div>';
    return;
  }
  const ms = (v, seuils) => v == null ? '<span class="na">-</span>'
    : sqCell(Math.round(v) + ' ms', v < seuils[0] ? 'ok' : v < seuils[1] ? 'warn' : 'crit');
  const ressenti = { good: ['ok', 'Good'], fair: ['warn', 'Fair'], poor: ['crit', 'Poor'],
    limit: ['none', 'At plan limit'] };
  host.innerHTML = '<table><thead><tr><th>Client</th><th>Site</th>' +
    '<th>Experience</th>' +
    '<th class="num" title="Usual latency: median over the period">Latency</th>' +
    '<th class="num" title="The bad moments: 95th percentile">p95</th>' +
    '<th class="num" title="Variation between pings of the last series">Jitter</th>' +
    '<th class="num">Loss</th>' +
    '<th class="num" title="Latency while the line is busy (bufferbloat)">Under load</th>' +
    '<th class="num" title="Composite experience score 0-100">Score</th>' +
    '<th>Why</th></tr></thead><tbody>' +
    lignes.slice(0, 300).map((c) => {
      const r = ressenti[c.experience];
      return '<tr><td><a href="#" data-lat-sub="' + esc(c.subscriber_id) + '"><b>' +
          esc(c.login) + '</b></a></td>' +
        '<td>' + esc(c.pop_name || '-') + '</td>' +
        '<td>' + (r ? sqCell(r[1], r[0]) : '<span class="na">-</span>') + '</td>' +
        '<td class="num">' + ms(c.median_ms, [30, 100]) + '</td>' +
        '<td class="num">' + ms(c.p95_ms, [60, 150]) + '</td>' +
        '<td class="num">' + (c.jitter_ms == null ? '<span class="na">-</span>'
          : esc(Math.round(c.jitter_ms)) + ' ms') + '</td>' +
        '<td class="num">' + (c.loss_pct == null ? '<span class="na">-</span>'
          : sqCell(Math.round(c.loss_pct) + '%', c.loss_pct === 0 ? 'ok' : c.loss_pct < 2 ? 'warn' : 'crit')) +
        '</td>' +
        '<td class="num">' + (c.loaded_ms == null ? '<span class="na" title="not enough load yet">-</span>'
          : ms(c.loaded_ms, [60, 150]) + (c.bloat_ms ? '<span class="pct-hint">+' +
            esc(Math.round(c.bloat_ms)) + '</span>' : '')) + '</td>' +
        '<td class="num">' + (c.qoe_score == null ? '<span class="na">-</span>'
          : sqCell(String(Math.round(c.qoe_score)), qoeSev(c.qoe_score))) + '</td>' +
        '<td class="hint" style="display:table-cell">' + esc((c.reasons || []).join(' · ')) + '</td></tr>';
    }).join('') + '</tbody></table>';
  host.querySelectorAll('[data-lat-sub]').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    openSubscriber(Number(a.dataset.latSub));
  }));
}

function renderLatencySegments(host, data) {
  if (!host) return;
  if (!data) { host.innerHTML = '<div class="empty">Latency unavailable.</div>'; return; }
  const m = data.method || {};
  const methode = '<div class="lat-method">Measured from each router: ' + esc(m.count) + ' pings ' +
    esc(m.interval_ms) + ' ms apart every ' + esc(m.every_s) + ' s; <b>median</b> shown, ' +
    '± = jitter. Hover a value for min / max / loss.</div>';
  if (!data.enabled) {
    host.innerHTML = '<div class="notice"><b>RTT probe off.</b> Tick <b>RTT probe</b> above to measure ' +
      'latency by segment.</div>';
    return;
  }
  const cibles = m.internet_targets || [];
  const diag = (r) => {
    const gw = r.gateway && r.gateway.median_ms;
    const net = (r.internet || []).map((x) => x.median_ms).filter((v) => v != null);
    const inet = net.length ? Math.min(...net) : null;
    const acc = r.access && r.access.median_ms;
    const pertes = [r.gateway, ...(r.internet || [])].filter(Boolean).some((x) => x.loss_pct > 0);
    if (inet != null && gw != null && inet - gw > 60) {
      return '<span class="sq warn"></span>delay above the core (transit / internet)';
    }
    if (gw != null && gw > 30) {
      // Le saut mesure est la passerelle par defaut : pour un PoP c'est le
      // coeur, pour le coeur la passerelle, pour la passerelle le transitaire.
      const vers = r.role === 'gateway' ? 'the transit provider'
        : r.role === 'core' ? 'the gateway' : 'the core';
      return '<span class="sq warn"></span>delay towards ' + vers;
    }
    if (acc != null && r.access.p90_ms > 100) return '<span class="sq warn"></span>delay on the access side';
    if (pertes) return '<span class="sq warn"></span>packet loss upstream';
    if (gw == null && inet == null && acc == null) return '<span class="na">not measured yet</span>';
    return '<span class="sq ok"></span>no abnormal segment';
  };
  host.innerHTML = methode + '<table><thead><tr><th>Router</th>' +
    '<th class="num" title="PoP to its subscribers: median of their medians (p90 in the tooltip)">Access</th>' +
    '<th class="num" title="Router to its default gateway: the next hop up (core for a PoP, ' +
      'gateway for the core, transit provider for the gateway)">Next hop up</th>' +
    cibles.map((c) => '<th class="num">To ' + esc(c) + '</th>').join('') +
    '<th>Where</th></tr></thead><tbody>' +
    (data.routers || []).map((r) => {
      const parCible = {};
      (r.internet || []).forEach((x) => { parCible[x.target] = x; });
      return '<tr><td><b>' + esc(r.pop_name || r.router) + '</b><span class="hint">' + esc(r.router) +
          (r.upstream_gateway ? ' &rarr; ' + esc(r.upstream_gateway) +
            (r.upstream_interface ? ' via ' + esc(r.upstream_interface) : '') : '') + '</span></td>' +
        latCell(r.access, [30, 100]) + latCell(r.gateway, [10, 30]) +
        cibles.map((c) => latCell(parCible[c], [40, 120])).join('') +
        '<td class="nowrap">' + diag(r) + '</td></tr>';
    }).join('') + '</tbody></table>';
}

/** Ce que veulent dire les pastilles du tableau, en une ligne. */
function renderExecLegend(host) {
  if (!host) return;
  host.innerHTML =
    '<span><b>Throughput</b> vs capacity (node) or limit (client): <i class="sq ok"></i>&lt;70% <i class="sq warn"></i>70-90% ' +
      '<i class="sq crit"></i>&gt;90%</span>' +
    '<span><b>Latency</b>: <i class="sq ok"></i>&lt;30 ms <i class="sq warn"></i>30-100 ms ' +
      '<i class="sq crit"></i>&gt;100 ms</span>' +
    '<span><b>Experience</b> (0-100): <i class="sq ok"></i>&ge;80 <i class="sq warn"></i>50-79 ' +
      '<i class="sq crit"></i>&lt;50</span>';
}

/** Bandeau d'etat de l'onglet : erreur de chargement, sonde coupee, ou reseau
 *  vide. Il y a TOUJOURS quelque chose a l'ecran, jamais un blanc silencieux. */
function renderExecNotice(rttState, error, st) {
  const notice = document.getElementById('exec-notice');
  if (!notice) return;
  const state = st || {};
  let html = '';
  if (error) {
    html += '<div class="notice err"><b>Partly loaded.</b> ' +
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

/** Ce que chaque ligne de la heatmap mesure, dit simplement. La cle vient du
 *  serveur ; un libelle inconnu est affiche tel quel. */
const HEAT_LABEL = {
  qoe: 'Experience score',
  rtt: 'Latency (p90)',
  utilisation: 'Load vs limit',
};

function renderHeatmap(host, heat) {
  if (!heat || !Array.isArray(heat.rows)) {
    host.innerHTML = '<div class="empty">Heatmap unavailable for now.</div>';
    return;
  }
  const lignes = heat.rows.filter((r) => !r.unavailable);
  // Les lignes qu'un controleur hors-bande ne PEUT PAS mesurer ne meritent
  // pas une ligne grise chacune : on les nomme une fois, en bas.
  const absentes = heat.rows.filter((r) => r.unavailable);
  const cellsOf = lignes.length ? lignes[0].cells : [];

  const rows = lignes.map((row) => {
    const last = [...row.cells].reverse().find((c) => c.value !== null && c.value !== undefined);
    const cells = row.cells.map((c) => {
      let t = c.value !== null && c.value !== undefined
        ? new Date(c.ts).toLocaleTimeString('fr-FR', { hour12: false, hour: '2-digit', minute: '2-digit' }) +
          ' : ' + c.value + (row.unit ? ' ' + row.unit : '')
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
      '<span class="heat-label">' + esc(HEAT_LABEL[row.key] || row.label) +
        (row.unit ? ' <span class="u">(' + esc(row.unit) + ')</span>' : '') + '</span>' +
      '<span class="heat-cells">' + cells + '</span>' +
      '<span class="heat-now" title="Last value">' + esc(now) + '</span></div>';
  }).join('');

  // Axe des temps sous les cases : debut, milieu, maintenant.
  let axe = '';
  if (cellsOf.length) {
    const hh = (c) => c && c.ts
      ? new Date(c.ts).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
    axe = '<div class="heat-row heat-axis"><span class="heat-label"></span>' +
      '<span class="heat-ticks"><span>' + esc(hh(cellsOf[0])) + '</span>' +
      '<span>' + esc(hh(cellsOf[Math.floor(cellsOf.length / 2)])) + '</span>' +
      '<span>now</span></span><span class="heat-now"></span></div>';
  }
  host.innerHTML = (rows || '<div class="empty">No measurement over this period.</div>') + axe +
    '<div class="exec-legend"><span><i class="sq ok"></i>good</span>' +
      '<span><i class="sq warn"></i>degraded</span><span><i class="sq crit"></i>bad</span>' +
      '<span><i class="sq none"></i>no measurement</span>' +
      (absentes.length ? '<span class="hint">Not measurable out-of-band: ' +
        esc(absentes.map((r) => r.label).join(', ')) + '</span>' : '') +
    '</div>';
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
    // Seul le debit ACTUEL s'additionne (cf. mesureFraiche).
    if (mesureFraiche(s)) {
      n.tx += Number(s.tx_bps) || 0;
      n.rx += Number(s.rx_bps) || 0;
    }
    n.effDown += (Number(s.effective_down_mbps) || 0) * 1e6;
    n.effUp += (Number(s.effective_up_mbps) || 0) * 1e6;
    n.confDown += (Number(s.plan_down_mbps) || 0) * 1e6;
    n.confUp += (Number(s.plan_up_mbps) || 0) * 1e6;
    // Un client au plafond de son forfait ne fait pas la pire latence du noeud.
    if (s.rtt_ms !== null && s.rtt_ms !== undefined && !auPlafond(s)) {
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

/** Infobulle d'une latence d'abonne : la serie qui a donne le chiffre. */
function rttDetailText(d) {
  if (!d) return 'median of the last ping series from the PoP router';
  return 'median ' + (d.median_ms ?? '-') + ' ms · min ' + (d.min_ms ?? '-') + ' · max ' +
    (d.max_ms ?? '-') + ' · jitter ' + (d.jitter_ms ?? '-') + ' ms · loss ' +
    (d.loss_pct ?? '-') + '% (' + (d.received ?? 0) + '/' + (d.sent ?? 0) + ') · ' +
    (d.method || '') + ' · ' + Math.round(d.age_s || 0) + ' s ago';
}

/** UN CLIENT PAR VLAN : LA VLAN EST UN CLIENT DU ROUTEUR QUI LA PORTE.
 *
 *  Un site VLAN n'est pas un PoP : il s'affichait au meme niveau que les
 *  routeurs, et le noeud NAS-FRANCOPHONIE ne montrait pas son client nestle,
 *  pourtant porte par lui. Chaque site VLAN est fusionne dans le noeud de son
 *  routeur : ses clients y figurent (marques de leur VLAN), ses debits et
 *  limites s'y ajoutent -- une seule fois, puisque le noeud VLAN disparait.
 *  Un routeur qui n'a de clients que dans ses VLAN recoit son noeud. */
function fusionnerVlans(nodes) {
  const parNom = new Map(nodes.map((n) => [n.name, n]));
  const garde = [];
  const nouveaux = [];
  nodes.forEach((n) => {
    const site = (exec.sites || {})[n.name];
    if (!site || site.kind !== 'vlan' || !site.router_name) { garde.push(n); return; }
    const routeur = site.router_name;
    const candidats = [routeur, (exec.popDuRouteur || {})[routeur]].filter(Boolean);
    let parent = candidats.map((c) => parNom.get(c)).find((x) => x && x !== n);
    if (!parent) {
      const nom = (exec.popDuRouteur || {})[routeur] || routeur;
      parent = { name: nom, circuits: 0, tx: 0, rx: 0, effDown: 0, effUp: 0, confDown: 0,
        confUp: 0, rttMax: null, qoe: null, subs: [], nodesCount: null };
      parNom.set(nom, parent);
      nouveaux.push(parent);
    }
    ['circuits', 'tx', 'rx', 'effDown', 'effUp', 'confDown', 'confUp'].forEach((k) => {
      parent[k] += n[k] || 0;
    });
    if (n.rttMax !== null) parent.rttMax = parent.rttMax === null ? n.rttMax : Math.max(parent.rttMax, n.rttMax);
    if (n.qoe && (!parent.qoe || n.qoe.score < parent.qoe.score)) parent.qoe = n.qoe;
    n.subs.forEach((x) => { x.vlanSite = site; parent.subs.push(x); });
    parent.vlans = (parent.vlans || []).concat(n.name);
  });
  return garde.concat(nouveaux).sort((a, b) => (b.tx + b.rx) - (a.tx + a.rx));
}

/** Badge VLAN d'un client porte par une VLAN du routeur. */
function vlanBadge(c) {
  const v = c && c.vlanSite;
  if (!v) return '';
  return ' <span class="badge" title="Client on its own VLAN (' + esc(v.name) + ') of this router">VLAN' +
    (v.vlan_id != null ? ' ' + esc(v.vlan_id) : '') + '</span>';
}

/** Le lien montant d'un noeud (vers le coeur ou le transit), tel que le
 *  donnent les points de saturation : capacite, nature (filaire / radio). */
function nodeUplink(n) {
  const montants = (exec.hotspotData && exec.hotspotData.uplinks) || {};
  return montants[n.name] || montants[n.router] || null;
}

/** "300 Mbps · radio" / "1 Gbps · wired" : ce que le noeud peut porter. */
function capaciteNoeud(lien, n) {
  if ((!lien || !lien.capacity_mbps) && !(n.effDown > 0) && !(n.effUp > 0)) {
    // Ni capacite connue ni client limite : "0.0 Mbps / 0.0 Mbps" se lisait
    // comme un noeud coupe. On dit ce qui manque.
    return '<span class="na" title="Uplink capacity unknown, and no client of this node has a ' +
      'limit yet. Declare the uplink wired or radio in Saturation risks, or push plans by API.">' +
      'unknown</span>';
  }
  if (!lien || !lien.capacity_mbps) {
    return '<span class="na" title="Uplink capacity unknown: declare the uplink wired or radio in ' +
      'Saturation risks. Shown here: the sum of the clients\' limits.">' +
      esc(mbps(n.effDown / 1e6) + ' / ' + mbps(n.effUp / 1e6)) + ' <span class="pct-hint">limits</span></span>';
  }
  return esc(mbps(lien.capacity_mbps)) + ' ' + badgeMilieu(lien);
}

/** Pastille de la nature d'un lien et, pour une radio, de son etat. */
function badgeMilieu(lien) {
  const r = lien.radio;
  // Deduit tout seul (rien de declare) : le dire, sans en faire une alerte.
  const auto = lien.medium_declared === false ? ' (auto)' : '';
  if (lien.medium === 'radio' && !r && auto) {
    return '<span class="badge medium-radio" title="Radio link detected automatically (radio neighbour ' +
      'or radio port). No polled antenna recognised at its end: its capacity is the port speed until ' +
      'the antenna is added in Devices.">radio' + auto + '</span>';
  }
  if (lien.medium === 'radio') {
    const etat = !r || r.state === 'missing' ? ['crit', 'antenna not found']
      : r.state === 'silent' ? ['crit', 'antenna silent']
      : r.state === 'degraded' ? ['warn', Math.round(r.share_of_nominal * 100) + '% of nominal']
      : ['ok', 'live'];
    return '<span class="badge medium-radio ' + etat[0] + '" title="Radio link: capacity read live on ' +
      esc((r && r.name) || '?') + (r && r.nominal_mbps ? ' (nominal ' + esc(mbps(r.nominal_mbps)) + ')' : '') +
      ' — ' + esc(etat[1]) + (auto ? ' — detected automatically' : '') + '">radio · ' + esc(etat[1]) + auto + '</span>';
  }
  if (lien.medium === 'wired') {
    return '<span class="badge medium-wired" title="Wired link: fixed capacity (' +
      esc(lien.capacity_source || '') + ')' + (auto ? ' — detected automatically' : '') + '">wired' + auto + '</span>';
  }
  return '<span class="badge" title="Not declared: the smallest known capacity (' +
    esc(lien.capacity_source || 'none') + '). Declare it wired or radio in Saturation risks.">auto</span>';
}

function renderNodeTable(host) {
  const nodes = exec.nodes;
  if (!nodes.length) {
    host.innerHTML = '<div class="empty">No active circuit.</div>';
    return;
  }
  const rttSq = (ms, detail) => (ms === null || ms === undefined)
    ? rttVideSq(detail)
    : sqCell(Math.round(ms) + ' ms', rttSevJs(ms), rttDetailText(detail));
  const naSq = '<span class="na">-</span>';
  // Le debit ET sa part de la limite, dans la meme cellule : c'est la part qui
  // dit s'il faut s'inquieter, le debit seul ne le dit pas.
  const usage = (v, lim) => {
    const p = pct(v, lim);
    return sqCell(bpsText(v), severity(p)) +
      (p === null ? '' : ' <span class="pct-hint">' + p.toFixed(0) + '%</span>');
  };
  // Les colonnes "hors-bande" (retransmissions, marks, drops) ont disparu :
  // toujours vides pour ce controleur, elles ne faisaient que brouiller.
  const head =
    '<table class="exec-table"><thead><tr><th></th><th>Node</th><th class="num">Clients</th>' +
    '<th class="num">Download now</th><th class="num">Upload now</th>' +
    '<th class="num" title="Node: what its uplink can carry (wired, or radio read live). ' +
      'Client: the cap applied (plan, override or boost)">Capacity / limit</th>' +
    '<th class="num" title="Node: plans sold against the uplink capacity. Client: its plan">' +
      'Sold / plan</th>' +
    '<th class="num">Latency</th><th class="num" title="0-100, from bufferbloat and latency">Experience</th>' +
    '</tr></thead><tbody>';

  const body = nodes.map((n) => {
    const open = exec.expanded.has(n.name);
    const sel = exec.selected && exec.selected.type === 'node' && exec.selected.name === n.name;
    // Noeud synthetique (issu de la topologie, sans abonne mesure) : debit /
    // effectif / RTT / QoO en n/d, jamais des zeros inventes.
    // UN NOEUD N'A PAS DE PLAN : ses clients en ont. Ce qui compte pour lui,
    // c'est ce que son lien montant PEUT PORTER (filaire, ou radio lue en
    // direct) et la part qu'il en utilise. A defaut de capacite connue, la
    // somme des limites de ses clients sert de reference, et le dit.
    const lienMontant = nodeUplink(n);
    const capa = lienMontant && lienMontant.capacity_mbps ? lienMontant.capacity_mbps * 1e6 : null;
    const effCell = n.synthetic ? '<td class="num na">-</td>'
      : '<td class="num">' + capaciteNoeud(lienMontant, n) + '</td>';
    const ratio = capa && n.confDown ? n.confDown / capa : null;
    const confCell = n.synthetic ? '<td class="num na">-</td>'
      : ratio !== null
        ? '<td class="num" title="Plans sold ' + esc(mbps(n.confDown / 1e6)) + ' on an uplink of ' +
          esc(mbps(capa / 1e6)) + '">' + sqCell(ratio.toFixed(1) + '× sold',
            ratio <= 1 ? 'ok' : ratio <= 3 ? 'warn' : 'crit') + '</td>'
        : !n.confDown && !n.confUp
          ? '<td class="num na" title="No plan: limits set by hand (forced) or by default">no plan</td>'
          : '<td class="num na">' + esc(mbps(n.confDown / 1e6) + ' / ' + mbps(n.confUp / 1e6)) + '</td>';
    const txCell = n.synthetic ? '<td class="num">' + naSq + '</td>'
      : '<td class="num">' + usage(n.tx, capa || n.effDown) + '</td>';
    const rxCell = n.synthetic ? '<td class="num">' + naSq + '</td>'
      : '<td class="num">' + usage(n.rx, capa || n.effUp) + '</td>';
    const vlans = n.vlans || [];
    const nodeRow =
      '<tr class="node-row' + (sel ? ' selected' : '') + '" data-node="' + esc(n.name) + '">' +
      '<td>' + (n.synthetic ? ''
        : '<span class="expand" data-expand="' + esc(n.name) + '" title="Show its clients">' +
          (open ? '−' : '+') + '</span>') + '</td>' +
      '<td><strong>' + esc(n.name) + '</strong>' +
        (vlans.length ? ' <span class="pct-hint" title="' + esc(vlans.join(', ')) + '">incl. ' +
          vlans.length + ' VLAN client(s)</span>' : '') +
        (n.synthetic && n.kind ? ' <span class="badge">' + esc(KIND_LABEL[n.kind] || n.kind) +
          '</span>' : '') + '</td>' +
      '<td class="num">' + n.circuits + '</td>' +
      txCell + rxCell + effCell + confCell +
      '<td class="num">' + ((n.subs || []).some((x) => pingsPerdus(x.rtt_detail) && !auPlafond(x))
        ? sqCell('no reply', 'crit', SANS_REPONSE) : rttSq(n.rttMax)) + '</td>' +
      '<td class="num">' + qooCell(n.qoe, n.rttMax) + '</td></tr>';

    const subRows = !open ? '' : n.subs.map((s) => {
      const eff = (Number(s.effective_down_mbps) || 0) * 1e6;
      const effU = (Number(s.effective_up_mbps) || 0) * 1e6;
      const csel = exec.selected && exec.selected.type === 'client' && exec.selected.id === s.subscriber_id;
      return '<tr class="sub-row' + (csel ? ' selected' : '') + '" data-client="' + s.subscriber_id + '">' +
        '<td></td><td class="login">' + esc(s.login) + vlanBadge(s) + '</td>' +
        '<td class="num"></td>' +
        '<td class="num">' + usage(Number(s.tx_bps) || 0, eff) + '</td>' +
        '<td class="num">' + usage(Number(s.rx_bps) || 0, effU) + '</td>' +
        '<td class="num">' + esc(mbps(s.effective_down_mbps || 0) + ' / ' + mbps(s.effective_up_mbps || 0)) + '</td>' +
        '<td class="num na">' + (!s.plan_down_mbps && !s.plan_up_mbps ? 'no plan'
          : esc(mbps(s.plan_down_mbps || 0) + ' / ' + mbps(s.plan_up_mbps || 0))) + '</td>' +
        '<td class="num">' + (rttClient(s, true) || rttSq(s.rtt_ms, s.rtt_detail)) + '</td>' +
        '<td class="num">' + qooCell(qoeOf(s.subscriber_id), s.rtt_ms) + '</td></tr>';
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
    '<svg width="46" height="128" viewBox="0 0 46 128"><text class="lbl" fill="var(--muted)" x="23" y="10" ' +
      'text-anchor="middle">Score</text>' +
    '<rect x="14" y="14" width="18" height="120" rx="3" fill="var(--surface-2)"></rect>' +
    '<rect x="14" y="' + (14 + 120 - qh) + '" width="18" height="' + qh + '" rx="3" fill="' +
      qCol + '"></rect>' +
    '<text x="23" y="' + (10 + 120) + '" text-anchor="middle" font-size="11" font-weight="700" ' +
      'fill="' + qCol + '">' + (qoe === null ? '-' : qoe) + '</text></svg></div>';
}

/** Les trois panneaux (Live Queue State | Node Snapshot | Node Details) pour le
 *  noeud ou le client selectionne dans le tableau. Reproduit l'ecran LibreQoS :
 *  un noeud est un agregat (lecture seule), un client peut recevoir un override. */
/** Le site dont on montre la sante : celui du noeud choisi, ou celui du
 *  client choisi. null = tout le reseau. */
function siteSelectionne() {
  const sel = exec.selected;
  if (!sel) return null;
  if (sel.type === 'node') return sel.name;
  const c = (exec.subsById || {})[sel.id];
  return c && c.pop_name ? c.pop_name : null;
}

/** LA SANTE DANS LE TEMPS DU SITE SELECTIONNE. Elle portait sur tout le
 *  reseau tout en s'affichant sous "Node VLAN 2060" : 0 % de charge pour un
 *  site a 96 % de sa limite. Rechargee seulement quand le site change. */
async function chargerSante() {
  const p = exec.heatParams;
  if (!p) return null;
  const site = siteSelectionne();
  if (site === exec.heatSite) return null;
  exec.heatSite = site;
  const titre = document.getElementById('exec-heat-title');
  if (titre) titre.textContent = 'Health over time' + (site ? ' — ' + site : ' — whole network');
  // Un routeur, c'est aussi les VLAN qu'il porte (cf. fusionnerVlans).
  const noeud = site ? (exec.nodes || []).find((n) => n.name === site) : null;
  const sites = site ? [site].concat((noeud && noeud.vlans) || []) : [];
  const heat = await api('/heatmap?minutes=' + p.minutes + '&buckets=' + p.buckets +
    sites.map((x) => '&pop=' + encodeURIComponent(x)).join(''));
  // Une reponse arrivee apres un autre clic ne doit pas ecraser la bonne.
  if (site === exec.heatSite) renderHeatmap(document.getElementById('exec-heatmap'), heat);
  return heat;
}

function renderQueuePanels() {
  chargerSante().catch(() => {});
  const live = document.getElementById('lq-live');
  const snap = document.getElementById('lq-snapshot');
  const det = document.getElementById('lq-details');
  if (!live || !snap || !det) return;

  const sel = exec.selected;
  const titre = document.getElementById('lq-heading');
  if (!selectionExists(sel)) {
    const vide = '<div class="empty">Select a node or a client in the table.</div>';
    live.innerHTML = snap.innerHTML = det.innerHTML = vide;
    if (titre) titre.textContent = 'Selection';
    return;
  }

  const isClient = sel.type === 'client';
  const client = isClient ? exec.subsById[sel.id] : null;
  const node = isClient ? null : exec.nodes.find((n) => n.name === sel.name);
  const title = isClient ? client.login : node.name;
  if (titre) {
    titre.innerHTML = (isClient ? 'Client ' : 'Node ') + '<span class="sel-name">' + esc(title) +
      '</span>' + (isClient && client.pop_name ? ' <span class="pct-hint">on ' + esc(client.pop_name) +
      '</span>' : '');
  }
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
  const rttSq = (ms, detail) => (ms === null || ms === undefined)
    ? rttVideSq(detail)
    : sqCell(Math.round(ms) + 'ms', rttSevJs(ms));
  // Un noeud est "sans reponse" des qu'UN de ses clients l'est : c'est son pire.
  const clientPlafond = isClient && auPlafond(client);
  const perduIci = isClient ? (clientPlafond ? null : client.rtt_detail)
    : ((node.subs || []).filter((x) => !auPlafond(x)).map((x) => x.rtt_detail).find(pingsPerdus) || null);
  // Au plafond sans note mesuree hors plafond : pas de verdict tire de la
  // latence de sa propre file.
  // Pings tous perdus SANS etre au plafond : un vrai signal, la note le dit
  // (elle restait verte a 95 a cote d'un « no reply »).
  const qooSq = clientPlafond && !note ? sqCell('-', 'none', AU_PLAFOND)
    : isClient && pingsPerdus(client.rtt_detail)
      ? sqCell((note ? Math.round(note.score) + ' · ' : '') + 'no reply', 'crit', SANS_REPONSE)
      : qooCell(note, rttMs);
  const naSq = sqCell('n/d', 'none');
  const naCell = '<td class="num na">' + naSq + '</td>';

  // ---- Right now
  // Un NOEUD n'a ni plan ni limite : son debit se lit contre la capacite de son
  // lien quand elle est connue, sinon sans couleur. Sa latence et son score sont
  // ceux de son PIRE client : c'est celui-la qu'on appellera.
  const envNoeud = isClient ? {} : ((exec.envByPop || {})[node.name] || {});
  const capaNoeud = isClient ? 0 : (envNoeud.capacity || envNoeud.nominal || 0) * 1e6;
  const refDown = isClient ? effDown : capaNoeud;
  const refUp = isClient ? effUp : 0;
  const sevDebit = (v, ref) => (ref > 0 ? severity(pct(v, ref)) : 'none');
  const dwn = (t, sv) => '<td class="num">' + sqCell(t, sv) + '</td>';
  live.innerHTML =
    '<h3>Right now</h3>' +
    '<table class="lq-table"><thead><tr><th></th><th>&darr; Down</th><th>&uarr; Up</th></tr></thead><tbody>' +
    '<tr><td>Throughput</td>' + (synth ? naCell + naCell
      : dwn(bpsText(down), sevDebit(down, refDown)) + dwn(bpsText(up), sevDebit(up, refUp))) + '</tr>' +
    (isClient
      // Sans plan, la LIMITE appliquee : "Plan 0.0 Mbps" se lisait comme un
      // client bride a zero, alors qu'il est plafonne a la main.
      ? (confDown || confUp
        ? '<tr><td>Plan</td>' + dwn(mbps(confDown / 1e6), 'none') + dwn(mbps(confUp / 1e6), 'none') + '</tr>'
        : '<tr><td>Limit <span class="pct-hint">no plan</span></td>' +
          dwn(mbps(effDown / 1e6), 'none') + dwn(mbps(effUp / 1e6), 'none') + '</tr>')
      : '') +
    '<tr><td>' + (isClient ? 'Latency' : 'Worst latency') + '</td><td class="num" colspan="2">' +
      // Un client SANS REPONSE est le pire de tous : il l'emporte sur la
      // latence des autres (« 12 ms » s'affichait a cote d'un « no reply »).
      (clientPlafond ? rttClient(client, true)
        : pingsPerdus(perduIci) ? sqCell('no reply', 'crit', SANS_REPONSE)
          : rttSq(rttMs, isClient ? client.rtt_detail : null)) +
      '</td></tr>' +
    '<tr><td>' + (isClient ? 'Score' : 'Worst score') + '</td><td class="num" colspan="2">' +
      qooSq + '</td></tr>' +
    '</tbody></table>';

  if (isClient) {
    // ---- Usage du plan + ce qui fait le score
    const composantes = note
      ? [['Baseline', note.rtt_ms != null ? Math.round(note.rtt_ms) + ' ms' : '-'],
        ['Under load', note.bloat_ms != null ? '+' + Math.round(note.bloat_ms) + ' ms' : '-'],
        ['Grade', note.grade || '-']]
      : [['Baseline', rttMs != null ? Math.round(rttMs) + ' ms' : '-'], ['Under load', '-']];
    snap.innerHTML = '<h3>' + (confDown || confUp ? 'Plan usage' : 'Limit usage') + '</h3>' +
      (effDown > 0 ? gaugeSvg(down, up, effDown, qoe)
        : '<div class="empty">No limit on this client.</div>') +
      '<div class="lq-chips" title="Score = the lower of: baseline latency, latency added under load">' +
      composantes.map((c) => '<span><i>' + esc(c[0]) + '</i> <b>' + esc(c[1]) + '</b></span>').join('') +
      '</div>';

    const src = String(client.plan_source || '');
    const deQui = src.startsWith('api') ? 'API' : src.startsWith('ui') ? 'Set by hand'
      : src.startsWith('default') ? 'Default plan' : src.startsWith('static') ? 'Client record' : (src || '-');
    det.innerHTML =
      '<h3>Plan</h3>' +
      '<div class="lq-kv">' +
        '<span class="k">Plan</span><span class="v">' +
          (confDown || confUp ? esc(mbps(confDown / 1e6) + ' / ' + mbps(confUp / 1e6))
            : '<span class="na" title="Limited by hand (forced) or by default">no plan</span>') +
          '</span>' +
        '<span class="k">Source</span><span class="v">' + esc(deQui) + '</span>' +
        (client.limit_source && client.limit_source !== 'plan'
          ? '<span class="k">Limit applied</span><span class="v">' +
            esc(mbps(effDown / 1e6) + ' / ' + mbps(effUp / 1e6)) + ' (' + esc(client.limit_source) + ')</span>'
          : '') +
        '<span class="k">Link</span><span class="v">' + esc(client.pop_name || '-') + '</span>' +
      '</div>' +
      '<div class="actions" style="margin-top:.8rem">' +
        '<button class="sm primary" id="lq-plan">Change plan</button></div>';
  } else {
    // ---- Les clients de ce lien, le plus mal servi en tete
    const clients = (node.subs || []).map((c) => {
      const n = qoeOf(c.subscriber_id);
      const plan = (Number(c.effective_down_mbps) || Number(c.plan_down_mbps) || 0) * 1e6;
      const debit = mesureFraiche(c) ? (Number(c.tx_bps) || 0) : 0;
      return { c, score: n ? n.score : qoeScore(c.rtt_ms), usage: plan > 0 ? debit / plan : null, debit };
    }).sort((x, y) => (x.score ?? 101) - (y.score ?? 101) || (y.usage ?? 0) - (x.usage ?? 0));
    const barre = (u) => {
      if (u == null) return '<span class="na">no limit</span>';
      const v = Math.min(100, Math.round(u * 100));
      const sv = severity(v);
      return '<span class="mini-bar ' + sv + '"><span style="width:' + v + '%"></span></span>' +
        '<span class="pct-hint">' + v + '%</span>';
    };
    snap.innerHTML = '<h3>Clients on this link</h3>' +
      (clients.length
        ? '<table class="lq-table lq-clients"><thead><tr><th>Client</th>' +
          '<th title="Download now vs its plan">Plan used</th><th class="num">Latency</th>' +
          '<th class="num" title="Lower of: latency at rest, latency added under load">Score</th>' +
          '</tr></thead><tbody>' +
          clients.slice(0, 8).map((x) => '<tr data-lq-client="' + esc(x.c.subscriber_id) + '">' +
            '<td><a href="#">' + esc(x.c.login) + '</a>' + vlanBadge(x.c) + '</td>' +
            '<td>' + barre(x.usage) + '</td>' +
            '<td class="num">' + rttSq(x.c.rtt_ms, x.c.rtt_detail) + '</td>' +
            '<td class="num">' + (x.score == null ? sqCell('-', 'none') : sqCell(String(Math.round(x.score)), qoeSev(x.score))) +
            '</td></tr>').join('') +
          '</tbody></table>' +
          (clients.length > 8 ? '<div class="pct-hint">+' + (clients.length - 8) + ' more</div>' : '')
        : '<div class="empty">No client measured on this link yet.</div>');

    const env = (exec.envByPop || {})[node.name] || {};
    // Le lien MONTANT du noeud, declare filaire ou radio, fait foi ; a defaut,
    // les backhauls rattaches au PoP.
    const montant = nodeUplink(node);
    const capa = (montant && montant.capacity_mbps) || env.capacity || env.nominal || null;
    const vendu = node.confDown / 1e6;
    const charge = capa ? Math.round((node.tx / 1e6 / capa) * 100) : null;
    const ratio = capa ? vendu / capa : null;
    det.innerHTML =
      '<h3>Link</h3>' +
      '<div class="lq-kv">' +
        '<span class="k">Capacity</span><span class="v">' +
          (capa ? esc(mbps(capa)) + (montant && montant.capacity_mbps ? ' ' + badgeMilieu(montant) : '')
            : sqCell('unknown', 'none')) + '</span>' +
        '<span class="k">Load now</span><span class="v">' +
          (charge == null ? esc(bpsText(down)) : sqCell(charge + '%', severity(charge))) + '</span>' +
        '<span class="k">Clients</span><span class="v">' + esc(node.circuits) + '</span>' +
        // Les limites APPLIQUEES (plan, limite forcee ou boost) a cote des plans
        // vendus : un client plafonne a la main sans plan donnait "Sold 0.0
        // Mbps", qu'on lisait comme "rien n'est limite".
        '<span class="k">Limits applied (&Sigma;)</span><span class="v">' +
          (node.effDown ? esc(mbps(node.effDown / 1e6)) : sqCell('none', 'none')) + '</span>' +
        '<span class="k">Sold (&Sigma; plans)</span><span class="v">' +
          (vendu ? esc(mbps(vendu)) : '<span class="na" title="No plan: the limits are set by ' +
            'hand (forced) or by default">no plan</span>') + '</span>' +
        (ratio != null
          ? '<span class="k">Oversubscription</span><span class="v">' +
            sqCell(ratio.toFixed(1) + '×', ratio <= 1 ? 'ok' : ratio <= 3 ? 'warn' : 'crit') + '</span>'
          : '') +
      '</div>' +
      '<div class="actions" style="margin-top:.8rem">' +
        '<button class="sm primary" id="lq-site-plans">Plans of these clients</button></div>';
  }

  const plan = document.getElementById('lq-plan');
  if (plan) plan.addEventListener('click', () => openClientPlan(client.login));
  const planSite = document.getElementById('lq-site-plans');
  if (planSite) planSite.addEventListener('click', () => openClientPlan(node.name));
  snap.querySelectorAll('[data-lq-client]').forEach((tr) => tr.addEventListener('click', (e) => {
    e.preventDefault();
    exec.selected = { type: 'client', id: Number(tr.dataset.lqClient) };
    renderQueuePanels();
  }));
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
    'file-a-poser': 'Saved, queue computed (simulation)',
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
  edge: 'at the internet exit',
  pop: 'on the PoP routers',
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
  const [etat, top, exporteurs, points] = await Promise.all([
    api('/netflow/status'),
    api('/netflow/top' + suffixe + '&limit=25').catch(() => null),
    api('/netflow/exporters').catch(() => []),
    api('/netflow/vantages?minutes=' + FLOW.minutes).catch(() => null),
  ]);

  const exportEtat = await api('/netflow/export').catch(() => null);
  // Un exporteur NON DECLARE qui envoie : ses flux sont comptes, mais sans
  // point de mesure ; les deux cartes du dessus restaient "Silent" a 0 B alors
  // que des gigaoctets s'affichaient juste dessous.
  const muets = (Array.isArray(exporteurs) ? exporteurs : [])
    .filter((x) => x.vantage === 'unknown' && x.packets_seen);
  // Les routeurs de l'inventaire se declarent seuls (au cycle NetFlow) : ne
  // reste a signaler que ce que freeQoS n'a PAS su reconnaitre.
  const avisExport = muets.length
    ? '<div class="notice"><b>' + muets.length + ' exporter(s) not recognised as one of your ' +
      'routers: ' + muets.slice(0, 3).map((x) => '<code>' + esc(x.address) + '</code>').join(', ') +
      '.</b> If it is a router of yours, it is declared on its own within a minute; otherwise set ' +
      'it in <button type="button" class="sm" data-open-exporters>Advanced: NetFlow exporters</button></div>'
    : '';
  flowNotice(flowDiagnostic(etat, exportEtat) + avisExport);
  renderVantages(points, top, exporteurs);
  renderFlowStats(etat, top);
  renderFlowTop(top);
  renderFlowExporters(exporteurs);
  await Promise.all([loadFlowPairs(), loadServices()]);

  const compte = document.getElementById('flow-count');
  if (compte) {
    compte.textContent = etat.listening
      ? etat.bind + ' · ' + etat.packets_received + ' datagram(s)'
      : 'collector stopped';
  }
}

function renderVantages(data, top, exporteurs) {
  // UNE PHRASE, pas deux cartes : « ou le trafic est mesure ». Le detail des
  // deux points de mesure (et le choix) vit dans « Advanced » : un client qui
  // decouvre l'outil n'a pas a comprendre « edge » contre « PoP ».
  const hote = document.getElementById('flow-vantages');
  const detail = document.getElementById('flow-vantage-detail');
  if (!hote) return;
  if (!data || !data.points) { hote.innerHTML = ''; return; }
  const compte = (top && top.vantage) || data.accounting;
  const point = (v) => data.points.find((p) => p.vantage === v) || {};
  const noms = (v) => (Array.isArray(exporteurs) ? exporteurs : [])
    .filter((e) => e.vantage === v && e.packets_seen)
    .map((e) => e.name || e.address);
  const actif = point(compte).active;
  const ou = compte === 'edge' ? 'at the internet exit' : 'on the PoP routers';
  const qui = noms(compte);
  hote.innerHTML = actif
    ? '<div class="notice ok"><b>Traffic measured ' + ou +
      (qui.length ? ' (' + esc(qui.slice(0, 3).join(', ')) + ')' : '') + '.</b> ' +
      'Each client is counted once, whatever the number of routers its traffic crosses.</div>'
    : '<div class="notice warn"><b>No router sends traffic flows yet.</b> The export is set up ' +
      'automatically on the routers added in Devices; it shows here within a few minutes.</div>';
  if (detail) {
    const etat = (v, nom) => {
      const p = point(v);
      return nom + ': ' + (p.active ? 'receiving'
        : p.exporters ? p.exporters + ' router(s) declared, no flow received recently'
          : 'no exporter') + (v === compte ? ' (counted)' : '');
    };
    detail.textContent = etat('edge', 'Internet exit') + ' · ' + etat('pop', 'PoP routers') +
      '. One point is enough: the same traffic crosses both.';
  }
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
    statCard('', 'Clients seen', String(totaux.subscribers || 0), '',
      'with traffic over ' + FLOW.minutes + ' min') +
    statCard(part < 50 ? 'warn' : '', 'Traffic identified', String(part), '%',
      'tied to a known client');
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
  // Une restriction en cours de saisie ne doit pas etre effacee par le
  // rafraichissement automatique.
  const actif = document.activeElement;
  if (FLOW.restricting && actif && actif.closest && actif.closest('[data-restrict-form]')) return;
  const parametres = '?minutes=' + FLOW.minutes + '&limit=500' +
    (FLOW.pop ? '&pop=' + encodeURIComponent(FLOW.pop) : '') +
    (FLOW.category ? '&category=' + encodeURIComponent(FLOW.category) : '') +
    (FLOW.search ? '&q=' + encodeURIComponent(FLOW.search) : '');

  let data;
  try {
    const [paires, regles] = await Promise.all([
      api('/netflow/pairs' + parametres),
      api('/traffic-rules').catch(() => ({ rules: [] })),
    ]);
    data = paires;
    FLOW.rules = regles.rules || [];
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const facettes = data.facets || {};
  remplirFacette('flow-pairs-pop', 'All PoPs', facettes.pops, FLOW.pop);
  remplirFacette('flow-pairs-category', 'All categories', facettes.categories, FLOW.category);
  const direct = new Set((data.live || []).map((c) => c[0] + '|' + c[1]));
  // DEBITS EN COURS, sens par sens, calcules sur la duree reelle de chaque flux.
  const debitsDirect = data.live_rates || {};
  const lignes = data.pairs || [];
  FLOW.lastPairs = lignes;
  // ↓ recu par le client / ↑ envoye par le client, en bit/s.
  const deuxSens = (bas, haut) => '<span class="nowrap">&darr; ' + bpsText(bas) +
    '</span><br><span class="nowrap">&uarr; ' + bpsText(haut) + '</span>';
  // Debit VECU : volume / temps ou la conversation a reellement echange sur la
  // periode -- et non volume / periode entiere, qui diluait un test de deux
  // minutes dans une heure.
  const debitActif = (octets, actif) => (actif > 0 ? (Number(octets || 0) * 8) / actif : 0);

  // UN BLOC PAR CLIENT. La ligne de tete donne sa consommation totale ; on
  // deplie pour voir avec quelles adresses elle se fait, et a quel debit.
  const clients = new Map();
  lignes.forEach((r) => {
    const cle = String(r.client);
    if (!clients.has(cle)) {
      clients.set(cle, {
        tete: r, lignes: [], down: 0, up: 0, vivants: 0, directBas: 0, directHaut: 0,
      });
    }
    const c = clients.get(cle);
    c.lignes.push(r);
    c.down += Number(r.down_bytes || 0);
    c.up += Number(r.up_bytes || 0);
    const paire = r.client + '|' + r.address;
    if (debitsDirect[paire]) {
      c.vivants += 1;
      c.directBas += Number(debitsDirect[paire].down_bps || 0);
      c.directHaut += Number(debitsDirect[paire].up_bps || 0);
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
        // Le nom DEMANDE par le client (cache DNS du routeur) passe avant tout :
        // c'est "syit.fr", pas "cluster100.hosting.ovh.net".
        (r.domain || domaine(r.hostname)
          ? '<br><b style="font-size:.75rem"' + (r.domain ? ' title="Name the client asked for (router DNS cache)"' : '') +
            '>' + esc(r.domain || domaine(r.hostname)) + '</b>' : '') +
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
      '<td class="num">' + (Number(r.active_s) > 0
        ? deuxSens(debitActif(r.down_bytes, r.active_s), debitActif(r.up_bytes, r.active_s))
        : '<span class="hint">-</span>') + '</td>' +
      '<td class="num">' + (debitsDirect[paire]
        ? '<b>' + deuxSens(debitsDirect[paire].down_bps, debitsDirect[paire].up_bps) + '</b>'
        : '<span class="hint">-</span>') + '</td>' +
      '<td class="nowrap">' + actionsRestriction(r) + '</td>' +
      '</tr>' + (FLOW.restricting === paire ? formulaireRestriction(r) : '');
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
          '<span title="Received by the client over the period">&darr; ' +
            bytesText(c.down) + '</span>' +
          '<span title="Sent by the client over the period">&uarr; ' + bytesText(c.up) + '</span>' +
          '<span>' + (c.vivants
            ? 'live <b>&darr; ' + bpsText(c.directBas) + ' &uarr; ' + bpsText(c.directHaut) + '</b>'
            : '<span class="hint">idle</span>') + '</span>' +
          '<span class="hint">' + c.lignes.length + ' address(es)</span>' +
        '</span>' +
      '</summary>' +
      '<div class="table-wrap"><table><thead><tr><th>Destination</th><th>Service</th>' +
        '<th>Category</th><th class="num">Port</th><th>Proto</th>' +
        '<th class="num" title="Volume received by the client over the selected period">' +
          '&darr; Received</th>' +
        '<th class="num" title="Volume sent by the client over the selected period">' +
          '&uarr; Sent</th>' +
        '<th class="num" title="Rate while the conversation was actually active: volume / active time">' +
          'Rate when active</th>' +
        '<th class="num" title="Current rate, from the real duration of each flow (NetFlow)">' +
          'Live</th><th></th>' +
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
  brancherRestrictions(hote);
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

/* ---------------------------------------------- bloquer / limiter une IP */

/** Les restrictions qui visent DEJA cette adresse (pour tous, ou pour ce client). */
function restrictionsSur(r) {
  const nu = (x) => String(x || '').replace(/\/32$/, '');
  return (FLOW.rules || []).filter((g) => (g.prefixes || []).some((x) => nu(x) === r.address) &&
    (g.scope === 'all' || (r.login && (g.logins || []).includes(r.login))));
}

function actionsRestriction(r) {
  const deja = restrictionsSur(r);
  if (deja.length) {
    return deja.map((g) => '<span class="badge ' + (g.action === 'block' ? 'crit' : 'warn') + '" title="' +
        esc(g.name) + '">' + (g.action === 'block' ? 'blocked' : 'limited ' +
        esc(mbps(g.limit_down_mbps || 0))) + (g.scope === 'all' ? ' · all' : '') + '</span>' +
      ' <button class="sm" data-rule-lift="' + esc(g.id) + '">Lift</button>').join(' ');
  }
  const cle = esc(r.client + '|' + r.address);
  return '<button class="sm danger" data-restrict="' + cle + '" data-action="block">Block</button> ' +
    '<button class="sm" data-restrict="' + cle + '" data-action="limit">Limit</button>';
}

function formulaireRestriction(r) {
  const limite = FLOW.restrictAction === 'limit';
  const qui = r.login ? esc(r.login) : esc(r.client);
  return '<tr class="plan-edit"><td colspan="10"><form class="lq-rate" data-restrict-form="' +
      esc(r.client + '|' + r.address) + '" style="margin:0">' +
    '<b>' + (limite ? 'Limit' : 'Block') + ' ' + esc(r.address) + '</b> for ' +
    '<select name="scope" style="width:auto">' +
      (r.login ? '<option value="client">' + qui + ' only</option>' : '') +
      '<option value="all">every client</option></select> ' +
    (limite
      ? '&darr; <input name="down" type="number" min="0.01" step="any" value="1" style="width:5.5rem"> ' +
        '&uarr; <input name="up" type="number" min="0.01" step="any" value="1" style="width:5.5rem"> Mbps '
      : '') +
    '<button class="sm ' + (limite ? 'primary' : 'danger') + '" type="submit">Apply now</button> ' +
    '<button class="sm" type="button" data-restrict-cancel>Cancel</button>' +
    '</form></td></tr>';
}

function brancherRestrictions(hote) {
  hote.querySelectorAll('[data-restrict]').forEach((b) => b.addEventListener('click', (e) => {
    e.preventDefault();
    FLOW.restricting = b.dataset.restrict;
    FLOW.restrictAction = b.dataset.action;
    loadFlowPairs();
  }));
  hote.querySelectorAll('[data-restrict-cancel]').forEach((b) => b.addEventListener('click', () => {
    FLOW.restricting = null;
    loadFlowPairs();
  }));
  hote.querySelectorAll('[data-rule-lift]').forEach((b) => b.addEventListener('click', async (e) => {
    e.preventDefault();
    b.disabled = true;
    try {
      await api('/traffic-rules/' + b.dataset.ruleLift, { method: 'DELETE' });
    } catch (err) { alert(err.message); }
    loadFlowPairs();
  }));
  hote.querySelectorAll('[data-restrict-form]').forEach((f) => f.addEventListener('submit', async (e) => {
    e.preventDefault();
    const [client, adresse] = f.dataset.restrictForm.split('|');
    const ligne = ((FLOW.lastPairs || []).find((x) => x.client === client && x.address === adresse)) || {};
    const pourClient = f.scope.value === 'client' && ligne.login;
    const limite = FLOW.restrictAction === 'limit';
    const corps = {
      name: (limite ? 'Limit ' : 'Block ') + adresse + (pourClient ? ' for ' + ligne.login : ' for all'),
      action: limite ? 'limit' : 'block',
      prefixes: [adresse],
      scope: pourClient ? 'subscribers' : 'all',
      logins: pourClient ? [ligne.login] : [],
    };
    if (limite) {
      corps.limit_down_mbps = Number(f.down.value) || null;
      corps.limit_up_mbps = Number(f.up.value) || null;
    }
    const bouton = f.querySelector('button[type=submit]');
    bouton.disabled = true;
    try {
      const regle = await api('/traffic-rules', { method: 'POST', body: JSON.stringify(corps) });
      const etat = (regle.apply && regle.apply.state) || regle.last_state;
      if (etat && etat !== 'posee') {
        alert('Saved, but the routers answered: ' + etat +
          ((regle.apply && regle.apply.reason) ? ' (' + regle.apply.reason + ')' : ''));
      }
    } catch (err) {
      alert(err.message);
    }
    FLOW.restricting = null;
    loadFlowPairs();
  }));
}

/** La fiche complete d'une adresse, depuis l'onglet Trafic. */
async function openPairAddress(address) {
  const hote = document.getElementById('flow-pair-detail');
  hote.innerHTML = '<div class="ip-card">Reading <code>' + esc(address) + '</code>...</div>';
  try {
    const fiche = await api('/netflow/destinations/' + encodeURIComponent(address) +
      '?minutes=' + Math.max(FLOW.minutes, 1440));
    hote.innerHTML = ipCard(fiche, Math.max(FLOW.minutes, 1440) * 60, false);
    hydrateMiniMaps(hote);
    brancherLiensServices(hote);
    hote.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
}


// Ouvre le bloc avance replie, puis y descend.
document.addEventListener('click', (e) => {
  if (!(e.target.closest && e.target.closest('[data-open-exporters]'))) return;
  const bloc = document.getElementById('flow-exporters-block');
  if (bloc) { bloc.open = true; bloc.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
});

/** « Probe silent » : le controleur rejoue la sonde sur un client muet, pingue
 *  la passerelle du routeur en controle, lit son pare-feu, et rend la cause. */
document.addEventListener('click', async (e) => {
  const bouton = e.target.closest && e.target.closest('[data-rtt-diag]');
  if (!bouton) return;
  bouton.disabled = true;
  bouton.textContent = 'Testing…';
  try {
    const d = await api('/rtt/diagnose');
    const regles = (d.firewall_suspects || []).map((r) => '#' + r.position + ' ' + r.chain + ' ' +
      r.action + ' ' + r.protocol + (r.comment ? ' (' + r.comment + ')' : ''));
    toast('<b>Latency probe: ' + esc(d.router) + ' → ' + esc(d.address) + '</b><br>' +
      esc(d.verdict.message) +
      (d.control ? '<br><small>Control ping to gateway ' + esc(d.control.address) + ': ' +
        (d.control.stats && d.control.stats.received ? 'answers' : 'no reply') + '</small>' : '') +
      (regles.length ? '<br><small>Rules: ' + esc(regles.join(' · ')) + '</small>' : ''), 60000);
  } catch (err) {
    toast('<b>Diagnosis failed.</b> ' + esc(err.message || String(err)), 15000);
  } finally {
    bouton.disabled = false;
    bouton.textContent = 'Find the cause';
  }
});

/** Menu du point de vue d'un exporteur. "Automatic" montre ce qu'il a deduit. */
function choixVantage(e) {
  const auto = e.vantage === 'unknown' || String(e.note || '').startsWith('declared automatically');
  const deduit = auto && e.vantage !== 'unknown' ? ' · ' + (VANTAGE_LABEL[e.vantage] || e.vantage) : '';
  const opt = (v, txt) => '<option value="' + v + '"' +
    ((auto ? 'auto' : e.vantage) === v ? ' selected' : '') + '>' + esc(txt) + '</option>';
  return '<select class="exp-mode' + (e.vantage === 'unknown' ? ' warn' : '') + '" data-exp-mode="' + esc(e.id) +
    '" aria-label="Vantage point" title="Automatic: deduced from the router\'s role (core or gateway = ' +
    'internet edge, PoP = at the PoP). A choice made here is kept.">' +
    opt('auto', 'Automatic' + (deduit || (e.vantage === 'unknown' ? ' · not recognised' : ''))) +
    opt('edge', 'Internet exit') +
    opt('pop', 'At the PoP') + '</select>';
}

function renderFlowExporters(rows) {
  const host = document.getElementById('flow-exporters');
  const resume = document.getElementById('flow-exporters-sum');
  if (resume) {
    const liste = rows || [];
    const auto = liste.filter((e) => String(e.note || '').startsWith('declared automatically')).length;
    resume.textContent = liste.length ? '· ' + liste.length + ' exporter(s), ' + auto + ' recognised automatically' : '';
  }
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
      return '<tr><td><code>' + esc(e.address) + '</code></td>' +
        '<td>' + esc(e.name || '-') + '</td>' +
        '<td>' + choixVantage(e) + '</td>' +
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

  // Le point de vue se change sur place : automatique (suit le role du
  // routeur), sortie internet, ou PoP. Un choix a la main n'est plus recalcule.
  host.querySelectorAll('select[data-exp-mode]').forEach((sel) => {
    sel.addEventListener('change', async () => {
      try {
        await api('/netflow/exporters/' + sel.dataset.expMode, { method: 'PATCH',
          body: JSON.stringify({ vantage: sel.value === 'auto' ? 'unknown' : sel.value }) });
        toast(esc('Vantage saved' + (sel.value === 'auto' ? ': follows the router\'s role.' : '.')), 4000);
      } catch (err) { toast(esc(err.message), 6000); }
      await loadTraffic();
    });
  });
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
 *  Les exemples montrent une adresse GENERIQUE (BASE_API) : jamais l'IP du
 *  serveur, qui n'a rien a faire dans une capture d'ecran ou une doc partagee.
 *  (Le contrat reste compatible : un integrateur qui parlait deja a Preseem
 *  change l'URL de base et la cle, rien d'autre. */
// Pas de schema ecrit en dur : l'interface ne reference aucune adresse externe
// (cf. test_interface_sans_dependance_externe).
const BASE_API = '<your-freeqos-url>';

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
  // L'API de PILOTAGE, avec la meme cle : tout ce que fait l'interface.
  ['POST', '/api/v1/pops/routers', 'Add a router (set up by itself: CAKE, queues, NetFlow)'],
  ['GET', '/api/v1/pops/routers', 'List the routers'],
  ['PATCH', '/api/v1/pops/routers/{id}', 'Edit a router'],
  ['GET', '/api/v1/pops/provisioning/{name}', 'Progress of a new router\'s setup'],
  ['PUT', '/api/v1/plans/{login}', 'Set the plan of a client (written to the router at once)'],
  ['DELETE', '/api/v1/plans/{login}', 'Remove the plan: the client is no longer throttled'],
  ['PUT', '/api/v1/shaping/policies', 'Force a limit (client or link), above its plan'],
  ['POST', '/api/v1/shaping/boosts', 'Temporary boost'],
  ['POST', '/api/v1/plans/refresh', 'Re-apply every plan now'],
];

async function loadApi() {
  document.getElementById('api-base').textContent = 'Base URL: ' + BASE_API;
  document.getElementById('api-endpoints').innerHTML =
    '<table><thead><tr><th>Method</th><th>Path</th><th>Object</th></tr></thead><tbody>' +
    API_ENDPOINTS.map(([verbe, chemin, objet]) =>
      '<tr><td><b>' + esc(verbe) + '</b></td>' +
      '<td class="login">' + esc(chemin) + '</td>' +
      '<td>' + esc(objet) + '</td></tr>').join('') +
    '</tbody></table>';
  const B = BASE_API;
  document.getElementById('api-sample').textContent =
    '# Replace ' + B + ' with the address of your freeQoS server,\n' +
    '# and <key> with a key created above.\n\n' +
    '# Declare a client (static IP), with its rate in kbit/s:\n' +
    'curl -u <key>: -X PUT ' + B + '/model/v1/services/abo-42 \\\n' +
    "  -H 'content-type: application/json' \\\n" +
    '  -d \'{"id":"abo-42","account":"cli-7","package":"fibre-100",' +
    '"parent_device_id":"sect-n1","down_speed":100000,"up_speed":20000,' +
    '"attachments":[{"cpe_mac":"00:10:0b:6e:4c:ff","network_prefixes":["10.20.0.10"]}]}\'\n\n' +
    '# List the clients:\n' +
    'curl -u <key>: \'' + B + '/model/v1/services?page=1&limit=500\'   # -> {"data": [...]}\n\n' +
    '# Add a router (set up by itself):\n' +
    'curl -H \'Authorization: Bearer <key>\' -X POST ' + B + '/api/v1/pops/routers \\\n' +
    "  -H 'content-type: application/json' \\\n" +
    '  -d \'{"name":"nas-north","host":"<router-address>","username":"qos","password":"…","role":"pop","pop_name":"North"}\'\n\n' +
    '# Set the plan of a PPPoE client (Mbit/s):\n' +
    'curl -H \'Authorization: Bearer <key>\' -X PUT ' + B + '/api/v1/plans/<login> \\\n' +
    "  -H 'content-type: application/json' -d '{\"down_mbps\":100,\"up_mbps\":20}'";
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
      '<code>curl -u ' + esc(cle.secret) + ': ' + esc(BASE_API) +
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
  static: 'FIXE', candidate: '?IP', vlan: 'VLAN',
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

/** Le routeur porte le bon plafond, mais le debit MESURE le depasse nettement
 *  dans un sens : ce trafic ne passe pas par la file. "held" serait faux.
 *  Cause la plus frequente : un test adresse AU ROUTEUR lui-meme (bandwidth-test
 *  ou ping vers son adresse), que ses files simples ne tiennent que dans un sens ;
 *  sinon le fasttrack, ou un trafic qui ne traverse pas ce routeur. */
function depassement(r) {
  if (typeof mesureFraiche === 'function' && !mesureFraiche(r)) return '';
  const sens = [];
  const trop = (mesure, limite) => limite > 0 && mesure > limite * 1.3 + 50e3;
  if (trop(Number(r.tx_bps) || 0, (Number(r.effective_down_mbps) || 0) * 1e6)) sens.push('&darr;');
  if (trop(Number(r.rx_bps) || 0, (Number(r.effective_up_mbps) || 0) * 1e6)) sens.push('&uarr;');
  if (!sens.length) return '';
  return '<span class="badge crit" style="margin-left:.35rem" title="The router carries this cap, ' +
    'but the measured rate is well above it: this traffic does not go through the queue. ' +
    'Most often a test addressed to the router itself (bandwidth-test or ping to one of its ' +
    'addresses): test towards a host beyond the router. Otherwise: fasttrack, or traffic that ' +
    'does not cross this router.">exceeded ' + sens.join(' ') + '</span>';
}

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
  const sceau = depassement(r) || limitProof(etat);
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

  // SEULS LES PROBLEMES SUR LESQUELS ON PEUT AGIR. La verification tourne en
  // tache de fond : un routeur pas encore lu, ou qui a manque une lecture,
  // n'est PAS un probleme -- huit avertissements "non verifie" repetes a
  // chaque rafraichissement ne disaient rien d'utile. Un routeur n'est cite
  // que s'il ne se laisse plus verifier depuis longtemps, en une seule ligne.
  const morceaux = [];
  const muets = [];
  (plafonds.routers || []).forEach((rt) => {
    const ft = rt.fasttrack || {};
    if (ft.active === true) {
      morceaux.push('<div class="notice err"><strong>' + esc(rt.router) +
        ': fasttrack bypasses the queues.</strong>' +
        '<span class="hint">' + esc(ft.detail || '') + '</span>' +
        (ft.remedy ? '<span class="hint">Run on the router: <code>' +
          esc(ft.remedy) + '</code></span>' : '') + '</div>');
    }
    if (rt.unverified_since) muets.push(rt);
  });
  if (muets.length) {
    morceaux.push('<div class="notice"><span class="hint" style="margin:0">Caps not checked for ' +
      esc(depuis(muets[0].unverified_since).replace(' ago', '')) + ' on ' +
      muets.map((rt) => '<b>' + esc(rt.router) + '</b>').join(', ') +
      ' (' + esc(muets[0].unverified_reason || 'router too slow') + '). Measurement and shaping ' +
      'are not affected; the check retries every 3 minutes.</span></div>');
  }

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

/** CONTROLE EN DIRECT : ce que le routeur compte pour cet abonne, face a ce
 *  que freeQoS affiche. Chaque source sur une ligne, et un verdict qui dit OU
 *  le debit se perd -- plutot que de deviner. */
async function liveCheck(id) {
  const hote = document.getElementById('sub-live');
  const bouton = document.getElementById('sub-live-btn');
  hote.innerHTML = '<div class="hint">Reading the router twice, 2 s apart...</div>';
  bouton.disabled = true;
  let r;
  try {
    r = await api('/subscribers/' + id + '/live');
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    bouton.disabled = false;
    return;
  }
  bouton.disabled = false;
  const l = r.live || {};
  const ligne = (source, down, up, detail) => '<tr><td>' + source + '</td>' +
    '<td class="num" style="color:var(--down)">' + (down == null ? '<span class="na">-</span>' : esc(bpsText(down))) + '</td>' +
    '<td class="num" style="color:var(--up)">' + (up == null ? '<span class="na">-</span>' : esc(bpsText(up))) + '</td>' +
    '<td class="hint" style="display:table-cell">' + detail + '</td></tr>';
  const sess = l.session;
  const lignes = [
    // Interface PPPoE : tx = ce que le routeur envoie a l'abonne (download).
    ligne('<b>Router, PPPoE interface</b>', l.interface_tx_bps, l.interface_rx_bps,
      l.interface ? '<code>' + esc(l.interface) + '</code> on ' + esc(l.router) + ', measured over ' +
        esc(l.window_s) + ' s' : (sess ? 'interface not found' : 'no open session')),
  ];
  (l.queues || []).forEach((q) => lignes.push(ligne('Router, queue', q.rate_down_bps, q.rate_up_bps,
    '<code>' + esc(q.name) + '</code> max ' + maxLimitText(q.max_limit) + (q.disabled ? ' (disabled)' : ''))));
  lignes.push(ligne('NetFlow (last window)', r.netflow ? r.netflow.tx_bps : null,
    r.netflow ? r.netflow.rx_bps : null, r.netflow ? '' : 'collector off'));
  lignes.push(ligne('Stored by freeQoS', r.stored ? r.stored.tx_bps : null, r.stored ? r.stored.rx_bps : null,
    r.stored ? 'sample ' + esc(depuis(r.stored.ts)) : 'no sample'));
  const v = r.verdict || {};
  hote.innerHTML = '<div class="notice ' + (v.level === 'ok' ? 'ok' : v.level === 'warn' ? 'warn' : 'err') + '">' +
      esc(v.text || '') + '</div>' +
    (sess ? '<div class="hint">Session: ' + esc(sess.address || '-') + ' · up ' + esc(sess.uptime || '-') +
      (sess.caller_id ? ' · ' + esc(sess.caller_id) : '') + '</div>' : '') +
    '<div class="table-wrap" style="margin-top:.5rem"><table><thead><tr><th>Source</th>' +
      '<th class="num">Download</th><th class="num">Upload</th><th></th></tr></thead><tbody>' +
      lignes.join('') + '</tbody></table></div>' +
    ((r.errors || []).length ? '<div class="notice warn">' + esc(r.errors.join(' ; ')) + '</div>' : '');
}

/** L'etat du cycle de mesure des abonnes, en tete de leur liste : un debit
 *  absent doit se lire "rien ne passe" OU "la mesure est en panne". */
async function renderSubscriberCycles() {
  const hote = document.getElementById('sub-cycles');
  if (!hote) return;
  let data;
  try { data = await api('/collection/cycles'); } catch (err) { hote.innerHTML = ''; return; }
  const c = (data.cycles || {}).collect_subscribers;
  if (!c) { hote.innerHTML = '<div class="hint">Measurement cycle not run yet.</div>'; return; }
  const sev = !c.ok ? 'crit' : c.age_s > 60 ? 'warn' : 'ok';
  hote.innerHTML = '<div class="cycles"><span class="cycle"><i class="sq ' + sev + '"></i>Measurement: ' +
    (c.ok ? 'ok' : '<b>failed</b>') + ' · ' + Math.round(c.age_s) + ' s ago · ' + c.duration_s + ' s · ' +
    c.items + ' subscriber(s) written' +
    (!c.ok && (c.errors || []).length ? ' · <span class="sev-crit">' + esc(c.errors.join(' ; ')) + '</span>' : '') +
    '</span><span class="pct-hint">Click a subscriber, then "Measure on the router now" to compare with the router.</span></div>';
}


async function loadSubscribers() {
  renderSubscriberCycles();
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
  // Retenu pour le panneau Boost : "Remove the running boost" n'a de sens que
  // s'il y en a un.
  state.boostsParLogin = parLogin;

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
    // "all good" seulement si tout est note A ou mieux : avec des B et des C a
    // l'ecran (+30 a +90 ms sous charge), le resume se contredisait.
    const bons = (dist['A+'] || 0) + (dist.A || 0);
    const moyens = bloat.summary.measured - bons - mauvais;
    compte += ' · bufferbloat: ' + bloat.summary.measured + ' measured' +
      (mauvais ? ', ' + mauvais + ' degraded' : '') +
      (moyens > 0 ? ', ' + moyens + ' fair (B/C)' : '') +
      (!mauvais && moyens <= 0 ? ', all good' : '') +
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
      const perime = mesure && !mesureFraiche(r);
      const trou = '<span style="color:var(--faint)">-</span>';
      return '<tr class="clickable' + (perime ? ' stale-row' : '') + '" data-sub="' + r.subscriber_id + '">' +
        '<td class="login">' + esc(r.login) +
          (mesure ? '' : '<span class="hint" style="display:block" title="No sample: ' +
            'this subscriber was never measured, or its PoP is no longer collected">never measured</span>') +
          (perime ? '<span class="badge warn" style="display:table;margin-top:.2rem" title="The last ' +
            'measurement is ' + esc(depuis(r.ts)) + ': these figures are NOT current. Check the ' +
            'Subscribers cycle on the dashboard.">stale · ' + esc(depuis(r.ts)) + '</span>' : '') +
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
        '<td class="num">' + (mesure ? rttClient(r) : trou) + '</td>' +
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
          '<button class="sm danger" data-del-sub="' + r.subscriber_id + '" title="Delete this ' +
            'subscriber and its history">Delete</button>' +
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
  host.querySelectorAll('[data-del-sub]').forEach((b) => {
    const ligne = rows.find((r) => String(r.subscriber_id) === b.dataset.delSub);
    b.addEventListener('click', (e) => { e.stopPropagation(); deleteSubscriber(ligne); });
  });

  // La verification des plafonds part MAINTENANT, sans etre attendue : la
  // liste est deja a l'ecran, elle se decorera quand les routeurs auront
  // repondu. Une page vide n'est pas une reponse plus honnete qu'une page
  // incomplete -- c'est l'absence de reponse.
  annoterLesPlafonds(rows);
}

/** Supprime un abonne, son historique, et ce qui le ferait revenir.
 *
 *  Le message de confirmation dit ce qui part ; la reponse dit ce qui a ete
 *  fait -- et, pour un abonne PPPoE encore connecte, qu'il reviendra tant que
 *  sa session existe sur le routeur (ce controleur ne fait que la lire). */
async function deleteSubscriber(r) {
  if (!r) return;
  const statique = r.kind === 'static';
  const ok = confirm('Delete ' + r.login + '?\n\n' +
    'Its measurement history, override and boost are deleted' +
    (statique ? ', and it is removed from the static-client inventory (its queue is removed from the router).'
      : '.\nA PPPoE subscriber that is still connected reappears at the next cycle: close its session ' +
        'or remove its PPPoE account to make it disappear for good.') +
    '\n\nThis cannot be undone.');
  if (!ok) return;
  const notice = document.getElementById('sub-notice') || document.getElementById('app-error');
  try {
    const rep = await api('/subscribers/' + r.subscriber_id + '?confirm=true', { method: 'DELETE' });
    const msg = r.login + ' deleted (' + (rep.samples_deleted || 0) + ' sample(s) removed).' +
      (rep.will_reappear ? ' Its PPPoE session is still open: it will reappear until the session ' +
        'is closed on the router.' : '');
    if (notice) {
      notice.hidden = false;
      notice.innerHTML = '<div class="notice ' + (rep.will_reappear ? 'warn' : 'ok') + '">' + esc(msg) + '</div>';
    }
    await loadSubscribers();
  } catch (err) {
    alert(err.message);
  }
}

/** Va lire sur les routeurs si les plafonds tiennent, puis decore la liste.
 *
 *  Detache a dessein : cette lecture touche chaque routeur (calcul du plan,
 *  files, pare-feu). Elle peut prendre plusieurs secondes sur un parc etendu,
 *  et un PoP injoignable ne doit pas faire disparaitre la liste des abonnes. */
/** Derniere verification des plafonds, et son heure : elle touche CHAQUE
 *  routeur (plan, files, pare-feu). La relancer a chaque rafraichissement de
 *  dix secondes chargeait les routeurs en permanence, et la connexion qu'elle
 *  occupe est celle de la collecte des debits. Une fois par minute suffit. */
const PLAFONDS = { data: null, at: 0, pending: null };
const PLAFONDS_TTL_MS = 60000;

async function annoterLesPlafonds(rows) {
  let data;
  try {
    if (!PLAFONDS.data || Date.now() - PLAFONDS.at > PLAFONDS_TTL_MS) {
      PLAFONDS.pending = PLAFONDS.pending || api('/shaping/limits')
        .then((d) => { PLAFONDS.data = d; PLAFONDS.at = Date.now(); return d; })
        .finally(() => { PLAFONDS.pending = null; });
      data = await PLAFONDS.pending;
    } else {
      data = PLAFONDS.data;
    }
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
  return '<span class="badge ok" title="PPPoE session found on the router">PPPoE</span>';
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
    await Promise.all([loadStaticClients(), loadVlanClients()]);
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
    await Promise.all([loadStaticClients(), loadVlanClients()]);
    await loadSubscribers();
  } catch (err) {
    scNotice('<span class="badge crit">' + esc(err.message) + '</span>');
  }
}

async function openSubscriber(id) {
  const root = document.getElementById('drawer-root');
  root.innerHTML = '<div class="drawer-backdrop"></div><div class="drawer">' +
    '<div class="empty">Loading...</div></div>';
  root.querySelector('.drawer-backdrop').addEventListener('click', closeDrawer);

  try {
    const data = await api('/subscribers/' + id + '/metrics?minutes=60&bucket_seconds=30');
    const s = data.subscriber;
    root.querySelector('.drawer').innerHTML =
      '<div class="drawer-head"><h3>' + esc(s.login) + '</h3>' +
      '<button class="sm" id="drawer-close">Close</button></div>' +
      '<div class="grid stats" style="margin-bottom:1rem">' +
        statCard('', 'PoP', esc(s.pop_name || '-'), '', '') +
        statCard('', 'Applied limit',
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
      '<h2>Live check</h2><div class="card"><div class="actions">' +
        '<button class="sm primary" id="sub-live-btn">Measure on the router now (2 s)</button></div>' +
        '<div id="sub-live"></div></div>' +
      '<h2>Last hour</h2><div class="card"><div id="sub-chart"></div></div>';
    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    document.getElementById('sub-live-btn').addEventListener('click', () => liveCheck(id));
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
  const orphelins = pops.filter((p) => p.declared === false);
  host.innerHTML =
    '<table><thead><tr><th>Site</th><th>Router</th><th class="num">Subscribers</th>' +
    '<th class="num">Backhauls</th><th></th></tr></thead><tbody>' +
    pops.map((p) => '<tr>' +
      '<td><strong>' + esc(p.name) + '</strong>' + (p.declared === false
        ? ' <span class="badge warn" title="No router, antenna or client declares this site any more">' +
          'no device</span>' : '') + '</td>' +
      '<td class="login">' + esc(p.router_host || '-') + '</td>' +
      '<td class="num">' + p.subscriber_count + '</td>' +
      '<td class="num">' + p.backhaul_count + '</td>' +
      '<td><div class="actions" style="justify-content:flex-end">' +
        '<button class="sm danger" data-del-pop="' + p.id + '">Delete</button>' +
      '</div></td></tr>').join('') + '</tbody></table>' +
    (orphelins.length
      ? '<div class="actions" style="padding:.6rem .9rem"><span class="hint">' + orphelins.length +
        ' site(s) no device declares: ' + esc(orphelins.map((p) => p.name).join(', ')) + '</span>' +
        '<button class="sm danger" id="del-orphans">Delete sites without a device</button></div>'
      : '');

  const tousOrphelins = document.getElementById('del-orphans');
  if (tousOrphelins) tousOrphelins.addEventListener('click', async () => {
    if (!confirm('Permanently delete ' + orphelins.length + ' site(s) that no device declares?\n\n' +
        orphelins.map((p) => p.name + ' (' + p.subscriber_count + ' subscriber(s))').join('\n') +
        '\n\nTheir subscribers and measurement history are erased too.')) return;
    try {
      await api('/pops/orphans/delete?confirm=true', { method: 'POST' });
      await loadRouters();
    } catch (err) { alert(err.message); }
  });

  host.querySelectorAll('[data-del-pop]').forEach((b) => {
    const pop = pops.find((p) => String(p.id) === b.dataset.delPop);
    b.addEventListener('click', async () => {
      if (!confirm('Permanently delete "' + pop.name + '"?\n\n' +
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
/** Modele et version d'un routeur tels que la lecture de sante les a vus. */
function modeleConnu(nom) {
  const h = (state.routerHealth || {})[nom];
  if (!h || !h.board_name) return '-';
  return h.board_name + (h.version ? ' · ' + h.version : '');
}

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
  state.routerHealth = {};
  routeurs.forEach((r) => { if (r.reachable) state.routerHealth[r.router] = r; });
  document.querySelectorAll('[data-model-for]').forEach((td) => {
    td.textContent = modeleConnu(td.dataset.modelFor);
  });
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
              ? '<span class="hint">' + esc(bytesText(r.free_memory)) + ' free</span>'
              : '<span class="hint">-</span>')
            : badge(ram, 80, 90) +
              (r.free_memory ? '<span class="hint" style="display:block">' +
                esc(bytesText(r.free_memory)) + ' free</span>' : '')) + '</td>' +
        '<td class="num">' + esc(uptime(r.uptime_s)) + '</td>' +
        '<td style="color:var(--faint)">' + esc(r.version || '-') + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
}

/** Octets en unite lisible, dans la langue de l'interface (anglais) : "0 o"
 *  et "3 Kio" y detonnaient. */
function bytesText(octets) {
  const n = Number(octets) || 0;
  if (n >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + ' TiB';
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + ' GiB';
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(0) + ' MiB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KiB';
  return n + ' B';
}

/** SANTE RADIO : chaque AP (signal, bruit, SNR, CCQ, airtime, frequence) et
 *  chacun de ses CPE, avec en clair ce qui ne va pas. Lu au dernier cycle. */
async function loadRadioHealth() {
  const hote = document.getElementById('radio-health');
  if (!hote) return;
  let data;
  try { data = await api('/radios'); } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  const antennes = data.antennas || [];
  if (!antennes.length) {
    hote.innerHTML = '<div class="empty">No antenna declared: add one below to read its radio and its CPEs.</div>';
    return;
  }
  const v = (x, u, d) => x == null ? '<span class="na">-</span>' : esc(Number(x).toFixed(d || 0)) + (u ? ' ' + u : '');
  hote.innerHTML = antennes.map((a) => {
    const r = a.radio || {};
    const stations = a.stations || [];
    const entete = '<div class="radio-head"><b>' + esc(a.name) + '</b>' +
      '<span class="pct-hint">' + esc([a.pop_name, r.model, r.firmware, r.mode].filter(Boolean).join(' · ')) + '</span>' +
      '<span class="spacer"></span>' +
      (a.error ? '<span class="badge crit" title="' + esc(a.error) + '">unreachable</span>'
        : a.read_at ? '<span class="pct-hint">read ' + esc(depuis(a.read_at)) + '</span>' : '<span class="badge">not read yet</span>') +
      '</div>';
    const faits = a.radio ? '<div class="radio-facts">' +
      '<div><span>Frequency</span>' + v(r.frequency_mhz, 'MHz') + (r.channel_width_mhz ? ' / ' + v(r.channel_width_mhz, 'MHz') : '') + '</div>' +
      '<div><span>Signal</span>' + v(r.signal_dbm, 'dBm') + '</div>' +
      '<div><span>Noise floor</span>' + v(r.noise_dbm, 'dBm') + '</div>' +
      '<div><span>SNR</span>' + v(r.snr_db, 'dB') + '</div>' +
      '<div><span>CCQ</span>' + v(r.ccq_pct, '%') + '</div>' +
      '<div><span>Airtime</span>' + v(r.airtime_pct, '%') + '</div>' +
      '<div><span>Capacity ↓/↑</span>' + v(r.capacity_down_mbps, '') + ' / ' + v(r.capacity_up_mbps, 'Mbps') + '</div>' +
      '<div><span>Rate tx/rx</span>' + v(r.tx_rate_mbps, '') + ' / ' + v(r.rx_rate_mbps, 'Mbps') + '</div>' +
      '<div><span>Distance</span>' + (r.distance_m != null ? v(r.distance_m / 1000, 'km', 1) : '<span class="na">-</span>') + '</div>' +
      '<div><span>CPEs</span>' + (stations.length || v(r.stations)) + '</div>' +
      '</div>' : '';
    const problemes = (a.issues || []).length
      ? '<div class="notice warn">' + a.issues.map(esc).join(' · ') + '</div>' : '';
    const cpe = stations.length
      ? '<details' + (a.stations_with_issues ? ' open' : '') + '><summary>' + stations.length + ' CPE(s)' +
        (a.stations_with_issues ? ', <b class="sev-warn">' + a.stations_with_issues + ' with an issue</b>' : '') +
        '</summary><div class="table-wrap"><table><thead><tr><th>CPE</th><th>IP</th><th class="num">Signal</th>' +
        '<th class="num">Remote</th><th class="num">Noise</th><th class="num">SNR</th><th class="num">CCQ</th>' +
        '<th class="num">Rate tx/rx</th><th class="num">Distance</th><th>Issue</th></tr></thead><tbody>' +
        stations.map((c) => '<tr><td><b>' + esc(c.name || c.mac || '?') + '</b><span class="hint">' + esc(c.mac || '') + '</span></td>' +
          '<td>' + (c.ip ? '<code>' + esc(c.ip) + '</code>' : '<span class="na">-</span>') + '</td>' +
          '<td class="num">' + (c.signal_dbm == null ? '-' : sqCell(Math.round(c.signal_dbm) + ' dBm',
            c.signal_dbm < -75 ? 'crit' : c.signal_dbm < -68 ? 'warn' : 'ok')) + '</td>' +
          '<td class="num">' + v(c.remote_signal_dbm, 'dBm') + '</td>' +
          '<td class="num">' + v(c.noise_dbm, 'dBm') + '</td>' +
          '<td class="num">' + v(c.snr_db, 'dB') + '</td>' +
          '<td class="num">' + v(c.ccq_pct, '%') + '</td>' +
          '<td class="num">' + v(c.tx_rate_mbps) + ' / ' + v(c.rx_rate_mbps, 'Mbps') + '</td>' +
          '<td class="num">' + (c.distance_m != null ? v(c.distance_m / 1000, 'km', 1) : '-') + '</td>' +
          '<td>' + esc((c.issues || []).join(' · ')) + '</td></tr>').join('') +
        '</tbody></table></div></details>' : '';
    return '<div class="card radio-card">' + entete + faits + problemes + cpe + '</div>';
  }).join('');
}

async function loadRouters() {
  await loadPops();
  await loadAntennas();
  loadRadioHealth();
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
  notice.innerHTML = html;
  notice.querySelectorAll('[data-hide-file]').forEach((b) =>
    b.addEventListener('click', () => hideFileRouter(b.dataset.hideFile)));
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
        // Un routeur du fichier n'a pas de fiche en base : son modele vient de
        // la lecture de sante, qui le connait deja ("-" alors qu'on sait).
        (r.board_name
          ? '<td style="font-size:.76rem;color:var(--muted)">' + esc(r.board_name) +
            (r.routeros_version ? ' &middot; ' + esc(r.routeros_version) : '') + '</td>'
          : '<td style="font-size:.76rem;color:var(--muted)" data-model-for="' + esc(r.name) + '">' +
            esc(modeleConnu(r.name)) + '</td>') +
        '<td><div class="actions" style="justify-content:flex-end">' +
          '<button class="sm primary" data-provision="' + esc(r.name) +
            '" title="Create the CAKE types and queues, set up the NetFlow export, rebuild the tree">' +
            'Set up</button>' +
          '<button class="sm" data-config="' + esc(r.name) +
            '" title="See the full config (/export) the controller reads">Config</button>' +
          '<button class="sm danger" data-clean-queues="' + esc(r.name) +
            '" title="Delete every freeQoS queue of this router, then lay them down again">' +
            'Reset queues</button>' +
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
  host.querySelectorAll('[data-provision]').forEach((b) =>
    b.addEventListener('click', async () => {
      const sortie = document.getElementById('router-export');
      try {
        await api('/pops/provisioning/' + encodeURIComponent(b.dataset.provision), { method: 'POST' });
      } catch (err) { sortie.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>'; return; }
      sortie.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      followProvisioning(b.dataset.provision, sortie);
    }));
  host.querySelectorAll('[data-clean-queues]').forEach((b) =>
    b.addEventListener('click', () => cleanRouterQueues(b.dataset.cleanQueues, b)));
  host.querySelectorAll('[data-probe]').forEach((b) =>
    b.addEventListener('click', () => probeRouter(b.dataset.probe, b)));
  host.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', () => deleteRouter(b.dataset.del)));
  host.querySelectorAll('[data-toggle]').forEach((b) =>
    b.addEventListener('click', () => toggleRouter(b.dataset.toggle)));
  host.querySelectorAll('[data-hide-file]').forEach((b) =>
    b.addEventListener('click', () => hideFileRouter(b.dataset.hideFile)));
}

/** Supprime toutes les files freeQoS d'un routeur puis les repose : repartir
 *  sur de bonnes bases. Les files posees a la main ne sont jamais touchees. */
async function cleanRouterQueues(name, bouton) {
  if (!confirm('Delete ALL freeQoS queues on ' + name + ' and rebuild them from scratch?\n\n' +
    'Every client of this router goes back to its plan: limits forced in Subscribers (Rate) are lifted.\n' +
    'Queues you created by hand are kept. Clients are briefly unshaped while the queues are rebuilt.')) return;
  const sortie = document.getElementById('router-export');
  bouton.disabled = true;
  sortie.innerHTML = '<div class="muted">Cleaning the queues of ' + esc(name) + '…</div>';
  try {
    const r = await api('/shaping/routers/' + encodeURIComponent(name) + '/clean-queues', { method: 'POST' });
    const errs = r.errors || [];
    sortie.innerHTML = '<div class="notice ' + (errs.length ? 'err' : 'ok') + '">' +
      esc(name) + ': ' + r.removed + ' queue(s) deleted, ' + r.recreated + ' recreated' +
      (r.kept_foreign ? ', ' + r.kept_foreign + ' other queue(s) left untouched' : '') + '.' +
      ((r.limits_reset || []).length ? ' Back to their plan: ' + r.limits_reset.map(esc).join(', ') + '.' : '') +
      (errs.length ? '<br>' + errs.map(esc).join('<br>') : '') + '</div>';
  } catch (err) {
    sortie.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  } finally {
    bouton.disabled = false;
    sortie.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
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
    'The router, its site and its history are deleted for good.')) return;
  try {
    await api('/pops/routers/file/' + encodeURIComponent(name), { method: 'DELETE' });
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
      showFormResult('<div class="notice ok"><strong>Connected.</strong> ' +
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
      ' saved.</b> Setting it up now (CAKE, queues, NetFlow export, tree)...</div>' +
      '<div id="provision-out"></div>');
    document.getElementById('router-form').reset();
    document.getElementById('f-username').value = 'qos-ro';
    document.getElementById('f-port').value = '8728';
    await loadRouters();
    // Le serveur met le routeur en service tout seul : on suit l'avancement.
    followProvisioning(String(created.name), document.getElementById('provision-out'));
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

/** MISE EN SERVICE AUTOMATIQUE : le serveur deroule tout seul, a l'ajout
 *  d'un equipement, ce que les cycles feraient en plusieurs minutes (types
 *  CAKE, files des abonnes, export NetFlow, arbre). On en montre les etapes
 *  au fur et a mesure, puis ce qui est REELLEMENT pose sur le routeur. */
async function followProvisioning(nom, hote) {
  if (!hote) return;
  for (let essai = 0; essai < 150; essai++) {
    let etat;
    try { etat = await api('/pops/provisioning/' + encodeURIComponent(nom)); } catch (err) {
      hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
      return;
    }
    hote.innerHTML = renderProvisioning(etat);
    if (etat.state !== 'running') {
      if (etat.state === 'done') await loadRouters();
      return;
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

function renderProvisioning(etat) {
  if (!etat || etat.state === 'none') return '<div class="hint">No automatic setup has run yet.</div>';
  const icone = (ok) => ok === true ? '<i class="sq ok"></i>' : ok === false ? '<i class="sq crit"></i>'
    : '<i class="sq none"></i>';
  const titre = {
    running: 'Setting up ' + esc(etat.router) + '...',
    done: esc(etat.router) + ' is set up.',
    blocked: esc(etat.router) + ': setup blocked.',
    failed: esc(etat.router) + ': setup finished with errors.',
  }[etat.state] || esc(etat.state);
  const r = etat.result || {};
  const constat = etat.state === 'running' ? '' : r.error
    ? '<div class="hint" style="display:block">Could not read the router back: ' + esc(r.error) + '</div>'
    : '<div class="prov-result">' +
      '<span><b>' + esc((r.cake_types || []).length) + '</b> CAKE type(s)' +
        ((r.cake_types || []).length ? ' <code>' + esc(r.cake_types.join(', ')) + '</code>' : '') + '</span>' +
      '<span><b>' + esc(r.managed_queues ?? 0) + '</b> queue(s) managed by freeQoS</span>' +
      (r.netflow_export ? '<span>NetFlow export: <b>' + (r.netflow_export.enabled ? 'on' : 'off') + '</b>' +
        (r.netflow_export.collector ? ' → ' + esc(r.netflow_export.collector) : '') + '</span>' : '') +
      '</div>' +
      (r.warnings || []).map((w) => '<div class="notice warn" style="margin-top:.5rem">' + esc(w) +
        '</div>').join('');
  const niveau = { done: 'ok', running: '', blocked: 'warn', failed: 'err' }[etat.state] || '';
  return '<div class="notice ' + niveau + ' prov"><b>' + titre + '</b>' +
    (etat.blocker ? '<div style="margin-top:.35rem">' + esc(etat.blocker) + '</div>' : '') +
    '<ul class="prov-steps">' + (etat.steps || []).map((x) =>
      '<li>' + icone(x.ok) + esc(x.step) + (x.detail ? ' <span class="hint">' + esc(x.detail) + '</span>' : '') +
      '</li>').join('') + (etat.state === 'running' ? '<li class="hint">...</li>' : '') + '</ul>' +
    constat + '</div>';
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
  locations: null,
  // Contour du monde, lu une fois a la demande (/static/world-110m.json).
  world: null,
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
  // Pas de fiche : on dit OU elle est (routeur · interface), lu dans la table
  // d'adresses des routeurs -- bien plus utile que "non declaree".
  const ou = ligne.where;
  return '<code>' + esc(ligne.client || '?') + '</code>' +
    ' <span class="hint">' + (ou ? 'on ' + esc(ou.router) + (ou.interface ? ' · ' + esc(ou.interface) : '')
      : 'undeclared') + '</span>';
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
  // Une seule periode pour toute la page Trafic : celle du haut.
  SVC.minutes = Number(document.getElementById('flow-range').value) || 60;
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

  // Chaque bloc s'affiche a l'arrivee de SA donnee. Les connexions en direct
  // ne sont plus lues ici : "Qui parle a qui" les montre deja, client par client.
  const pRegles = api('/traffic-rules').catch(() => ({ rules: [] }))
    .then((regles) => renderRules(regles));
  const pLieux = api('/netflow/locations' + suffixe).catch(() => null)
    .then(async (lieux) => {
      SVC.locations = lieux;
      await renderTrafficMap(document.getElementById('svc-map'), lieux);
      renderCountries(document.getElementById('svc-countries'), lieux);
    });
  const [etat, intel, dest] = await Promise.all([
    api('/netflow/status'),
    api('/netflow/intel').catch(() => null),
    api('/netflow/destinations' + suffixe + '&limit=120')
      .catch(() => ({ destinations: [], services: [] })),
  ]);

  renderServiceNotice(etat, intel);
  renderServiceTable(dest.services || []);
  renderDestinations(dest.destinations || []);

  document.getElementById('svc-count').textContent = etat.listening
    ? (dest.destinations || []).length + ' address(es) over ' + SVC.minutes + ' min'
    : 'collector stopped';
  await Promise.all([pRegles, pLieux]);
}

/** Ce qui empeche cette page de repondre, dit en toutes lettres.
 *
 *  Un tableau vide se lit "il n'y a rien". Or il veut souvent dire "je ne peux
 *  pas savoir" : collecteur coupe, suivi des destinations desactive,
 *  enrichissement a l'arret. Les trois appellent des gestes differents. */
function renderServiceNotice(etat, intel) {
  // Collecteur coupe, muet ou sans datagramme : deja dit par l'avis NetFlow
  // juste au-dessus. Ici, uniquement ce qui concerne les destinations.
  const hote = document.getElementById('svc-notice');
  if (!hote) return;
  const messages = [];
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
          ? '<button class="sm" data-svc-restrict="' + esc(r.service) + '">Restrict</button>'
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
    '<th>Location</th>' +
    '<th>Service</th><th>Category</th><th class="num">Clients</th>' +
    '<th class="num">Down</th><th class="num">Up</th><th>Seen</th>' +
    '</tr></thead><tbody>' +
    lignes.map((d) =>
      '<tr><td><a href="#" data-svc-ip="' + esc(d.address) + '"><code>' +
        esc(d.address) + '</code></a></td>' +
      '<td class="login">' + (d.domain ? '<b>' + esc(d.domain) + '</b><br>' : '') + (d.hostname
        ? '<span class="hint">' + esc(d.hostname) + '</span>' : (d.domain ? '' : '<span class="hint">-</span>')) + '</td>' +
      '<td class="nowrap">' + (lieu(d) || '<span class="na">-</span>') + '</td>' +
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

/** Fermer la fiche d'une adresse : par sa croix, ou par Echap. Delegue au
 *  document, parce que la fiche est rendue a trois endroits (Services, Trafic,
 *  recherche) et que chacun la remplace a chaque ouverture. */
function fermerFicheIp(carte) {
  const hote = carte && carte.parentElement;
  if (!hote) return;
  hote.innerHTML = '';
  if (hote.id === 'svc-detail' && typeof SVC !== 'undefined') SVC.detail = null;
}
document.addEventListener('click', (e) => {
  const bouton = e.target.closest && e.target.closest('[data-ip-close]');
  if (bouton) fermerFicheIp(bouton.closest('.ip-card'));
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const ouvertes = document.querySelectorAll('.ip-card [data-ip-close]');
  if (ouvertes.length) fermerFicheIp(ouvertes[ouvertes.length - 1].closest('.ip-card'));
});

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
    '<h3><code>' + esc(fiche.address) + '</code> ' + svcBadge(famille) +
      (lieu(intel) ? ' ' + lieu(intel) : '') +
      '<button class="sm ip-close" data-ip-close title="Close (Esc)" aria-label="Close">&times;</button></h3>' +
    (position ? miniMapHtml(intel.latitude, intel.longitude, intel.city || intel.country) : '') +
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
  hydrateMiniMaps(detail);
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

/* ------------------------------------------------ carte et recherche d'IP
 *
 *  OU SONT LES ADRESSES QUE LES CLIENTS JOIGNENT.
 *
 *  La carte est dessinee sans dependance ni tuile telechargee : un contour du
 *  monde (Natural Earth 1:110m, domaine public) est servi par le controleur
 *  lui-meme, et projete en equirectangulaire -- x = longitude, y = latitude.
 *  C'est la seule projection ou un point se place sans bibliotheque, et elle
 *  suffit a dire "Paris" ou "Virginie". Le lien OpenStreetMap de chaque lieu
 *  donne la precision de la rue a qui en a besoin (et un acces internet).
 */

const MAP_W = 3600;
const MAP_H = 1500;

async function loadWorld() {
  if (SVC.world) return SVC.world;
  try {
    const r = await fetch('/static/world-110m.json');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    SVC.world = await r.json();
  } catch (err) {
    SVC.world = { d: '', error: err.message };
  }
  return SVC.world;
}

function mapX(lon) { return (Number(lon) + 180) * 10; }
function mapY(lat) { return (90 - Number(lat)) * 10; }

/** Nom du pays dans la langue du navigateur, sans table embarquee. */
function countryName(code) {
  const c = String(code || '').toUpperCase();
  if (!/^[A-Z]{2}$/.test(c)) return code || '';
  try {
    return new Intl.DisplayNames([navigator.language || 'en'], { type: 'region' }).of(c) || c;
  } catch (err) {
    return c;
  }
}

/** Le fond de carte : les pays, et une graticule tous les 30 degres. */
function mapBackground(world) {
  let grille = '';
  for (let lon = -150; lon <= 150; lon += 30) {
    grille += '<line class="grat" x1="' + mapX(lon) + '" x2="' + mapX(lon) + '" y1="0" y2="' + MAP_H + '"/>';
  }
  for (let lat = -30; lat <= 60; lat += 30) {
    grille += '<line class="grat' + (lat === 0 ? ' eq' : '') + '" x1="0" x2="' + MAP_W +
      '" y1="' + mapY(lat) + '" y2="' + mapY(lat) + '"/>';
  }
  return '<rect class="sea" x="0" y="0" width="' + MAP_W + '" height="' + MAP_H + '"/>' +
    grille + '<path class="land" d="' + (world.d || '') + '"/>';
}

/** Carte du trafic : un cercle par lieu, surface proportionnelle au volume.
 *
 *  Zoom a la molette (ou aux boutons), deplacement a la souris. Les cercles
 *  gardent leur taille a l'ecran quand on zoome : sinon un zoom sur l'Europe
 *  transformerait Francfort en tache qui couvre l'Allemagne. */
async function renderTrafficMap(host, data) {
  if (!host) return;
  // Le rafraichissement periodique ne redessine pas sous la souris : ce
  // serait lacher la carte en plein deplacement.
  if (SVC.mapDragging) return;
  const place = document.getElementById('svc-map-place');
  if (!data) {
    host.innerHTML = '<div class="empty">Locations unavailable.</div>';
    if (place) place.innerHTML = '';
    SVC.mapPlace = null;
    return;
  }
  const points = data.points || [];
  const world = await loadWorld();
  const hors = data.unlocated || {};

  let entete = '';
  if (!data.geoip_enabled) {
    entete = '<div class="notice warn"><b>Location is off.</b> Turn on ' +
      '<i>Locate the destinations reached</i> in <a href="#/settings">Settings</a> to place ' +
      'addresses on the map.</div>';
  } else if (!points.length) {
    entete = '<div class="notice">No located destination over this period yet.</div>';
  }

  const max = Math.max(1, ...points.map((p) => Number(p.down_bytes || 0) + Number(p.up_bytes || 0)));
  const total = points.reduce((a, p) => a + Number(p.down_bytes || 0) + Number(p.up_bytes || 0), 0);
  // Du plus gros au plus petit : les petits cercles restent cliquables au-dessus.
  const tries = points.slice().sort((a, b) =>
    (Number(b.down_bytes || 0) + Number(b.up_bytes || 0)) - (Number(a.down_bytes || 0) + Number(a.up_bytes || 0)));

  host.innerHTML = entete +
    '<div class="map-wrap">' +
      '<svg class="worldmap" viewBox="0 0 ' + MAP_W + ' ' + MAP_H + '" preserveAspectRatio="xMidYMid meet">' +
        mapBackground(world) +
        '<g class="pts">' + tries.map((p, i) => {
          const v = Number(p.down_bytes || 0) + Number(p.up_bytes || 0);
          return '<circle class="pt" data-i="' + points.indexOf(p) + '" data-r="' +
            (5 + Math.sqrt(v / max) * 22).toFixed(1) + '" cx="' + mapX(p.longitude).toFixed(1) +
            '" cy="' + mapY(p.latitude).toFixed(1) + '"' + (i < 3 ? ' data-top="1"' : '') + '></circle>';
        }).join('') + '</g>' +
        '<g class="lbls">' + tries.slice(0, 15).map((p) =>
          '<text class="pt-label" data-x="' + mapX(p.longitude).toFixed(1) + '" data-y="' +
            mapY(p.latitude).toFixed(1) + '">' + esc(p.city || countryName(p.country)) + '</text>').join('') +
        '</g>' +
      '</svg>' +
      '<div class="map-zoom"><button class="sm" data-z="in" title="Zoom in">+</button>' +
        '<button class="sm" data-z="out" title="Zoom out">&minus;</button>' +
        '<button class="sm" data-z="reset" title="Whole world">&#8634;</button></div>' +
    '</div>' +
    '<div class="map-foot"><span>' + esc(points.length) + ' place(s) &middot; ' +
      bytesText(total) + ' located</span>' +
      (Number(hors.bytes) ? '<span>' + bytesText(hors.bytes) + ' not located (' +
        esc(hors.addresses || 0) + ' address(es))</span>' : '') +
      '<span class="hint" style="display:inline;margin:0">Circle area = volume &middot; ' +
      'scroll to zoom, drag to move, click a circle for details</span></div>';

  const svg = host.querySelector('svg.worldmap');
  // Le cadrage survit au rafraichissement : zoomer sur l'Europe puis la voir
  // revenir au monde entier toutes les dix secondes rendrait la carte inutile.
  let vb = SVC.mapView || { x: 0, y: 0, w: MAP_W, h: MAP_H };
  const applique = () => {
    SVC.mapView = vb;
    svg.setAttribute('viewBox', vb.x + ' ' + vb.y + ' ' + vb.w + ' ' + vb.h);
    // Taille constante a l'ecran : le rayon suit l'echelle du cadre.
    const k = vb.w / MAP_W;
    svg.querySelectorAll('circle.pt').forEach((c) => c.setAttribute('r', (Number(c.dataset.r) * 3 * k).toFixed(2)));
    // Les noms ne se chevauchent pas : Paris, Londres, Amsterdam et Francfort
    // tiennent dans un pouce au zoom monde. Le plus gros lieu garde son nom,
    // les suivants ne l'affichent que s'il reste de la place.
    const poses = [];
    svg.querySelectorAll('text.pt-label').forEach((t) => {
      const fs = 13 * 3 * k;
      const bx = Number(t.dataset.x) + 14 * 3 * k;
      const by = Number(t.dataset.y) + 5 * 3 * k;
      const boite = { x0: bx, x1: bx + t.textContent.length * fs * 0.6, y0: by - fs, y1: by + fs * 0.25 };
      const libre = !poses.some((o) => boite.x0 < o.x1 && boite.x1 > o.x0 && boite.y0 < o.y1 && boite.y1 > o.y0);
      t.setAttribute('x', bx);
      t.setAttribute('y', by);
      t.setAttribute('font-size', fs.toFixed(2));
      t.style.display = libre ? '' : 'none';
      if (libre) poses.push(boite);
    });
    svg.style.setProperty('--sw', (1.2 * 3 * k).toFixed(2));
  };
  const borne = () => {
    vb.w = Math.max(MAP_W / 40, Math.min(MAP_W, vb.w));
    vb.h = vb.w * MAP_H / MAP_W;
    vb.x = Math.max(0, Math.min(MAP_W - vb.w, vb.x));
    vb.y = Math.max(0, Math.min(MAP_H - vb.h, vb.y));
  };
  const zoom = (f, cx, cy) => {
    const px = cx === undefined ? vb.x + vb.w / 2 : cx;
    const py = cy === undefined ? vb.y + vb.h / 2 : cy;
    vb.x = px - (px - vb.x) * f;
    vb.y = py - (py - vb.y) * f;
    vb.w *= f;
    borne();
    applique();
  };
  const versCarte = (ev) => {
    const r = svg.getBoundingClientRect();
    // preserveAspectRatio meet : le dessin peut ne pas remplir la boite.
    const echelle = Math.min(r.width / vb.w, r.height / vb.h);
    const ox = (r.width - vb.w * echelle) / 2;
    const oy = (r.height - vb.h * echelle) / 2;
    return { x: vb.x + (ev.clientX - r.left - ox) / echelle, y: vb.y + (ev.clientY - r.top - oy) / echelle, echelle };
  };
  applique();

  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const m = versCarte(ev);
    zoom(ev.deltaY < 0 ? 0.8 : 1.25, m.x, m.y);
  }, { passive: false });
  let glisse = null;
  svg.addEventListener('mousedown', (ev) => {
    if (ev.target.closest('circle.pt')) return;
    glisse = { x: ev.clientX, y: ev.clientY, vx: vb.x, vy: vb.y, e: versCarte(ev).echelle };
    SVC.mapDragging = true;
    svg.classList.add('dragging');
  });
  const lache = () => { glisse = null; SVC.mapDragging = false; svg.classList.remove('dragging'); };
  svg.addEventListener('mouseup', lache);
  svg.addEventListener('mouseleave', lache);
  svg.addEventListener('mousemove', (ev) => {
    if (!glisse) return;
    vb.x = glisse.vx - (ev.clientX - glisse.x) / glisse.e;
    vb.y = glisse.vy - (ev.clientY - glisse.y) / glisse.e;
    borne();
    applique();
  });
  host.querySelectorAll('[data-z]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.z === 'reset') { vb = { x: 0, y: 0, w: MAP_W, h: MAP_H }; applique(); return; }
    zoom(b.dataset.z === 'in' ? 0.6 : 1 / 0.6);
  }));

  const cle = (p) => p.latitude + ',' + p.longitude;
  svg.querySelectorAll('circle.pt').forEach((c) => {
    const p = points[Number(c.dataset.i)];
    if (SVC.mapPlace && SVC.mapPlace === cle(p)) c.classList.add('on');
    c.addEventListener('mousemove', (ev) => showMapTip(ev, p));
    c.addEventListener('mouseleave', hideTooltip);
    c.addEventListener('click', () => {
      svg.querySelectorAll('circle.pt.on').forEach((o) => o.classList.remove('on'));
      c.classList.add('on');
      SVC.mapPlace = cle(p);
      renderMapPlace(place, p);
    });
  });
}

function showMapTip(event, p) {
  if (!tooltipEl) {
    tooltipEl = document.createElement('div');
    tooltipEl.className = 'tooltip';
    document.body.appendChild(tooltipEl);
  }
  tooltipEl.innerHTML =
    '<div class="t">' + drapeau(p.country) + esc([p.city, p.region, countryName(p.country)]
      .filter(Boolean).join(', ')) + '</div>' +
    '<div class="row"><span style="color:var(--down)">Down</span><span>' + bytesText(p.down_bytes) + '</span></div>' +
    '<div class="row"><span style="color:var(--up)">Up</span><span>' + bytesText(p.up_bytes) + '</span></div>' +
    '<div class="row"><span>Addresses</span><span>' + esc(p.addresses) + '</span></div>' +
    '<div class="row"><span>Clients</span><span>' + esc(p.clients) + '</span></div>' +
    ((p.services || []).length ? '<div class="row"><span>Services</span><span>' +
      esc(p.services.slice(0, 3).join(', ')) + '</span></div>' : '');
  tooltipEl.style.display = 'block';
  const pad = 14;
  tooltipEl.style.left = Math.min(event.clientX + pad, window.innerWidth - tooltipEl.offsetWidth - 8) + 'px';
  tooltipEl.style.top = Math.min(event.clientY + pad, window.innerHeight - tooltipEl.offsetHeight - 8) + 'px';
}

/** Le lieu clique : ou exactement, qui y heberge quoi, et quelles adresses. */
function renderMapPlace(host, p) {
  if (!host || !p) return;
  host.innerHTML = '<div class="map-place">' +
    '<div class="mp-head"><b>' + drapeau(p.country) + esc([p.city, p.region].filter(Boolean).join(', ') ||
      countryName(p.country)) + '</b> <span class="pct-hint">' + esc(countryName(p.country)) + '</span>' +
      '<span class="spacer"></span><code>' + esc(Number(p.latitude).toFixed(2) + ', ' +
      Number(p.longitude).toFixed(2)) + '</code> ' + lienCarte(p.latitude, p.longitude) +
      '<button class="sm" id="svc-map-place-close" title="Close">&times;</button></div>' +
    '<div class="ip-facts">' +
      '<div><span>Down</span>' + bytesText(p.down_bytes) + '</div>' +
      '<div><span>Up</span>' + bytesText(p.up_bytes) + '</div>' +
      '<div><span>Addresses</span>' + esc(p.addresses) + '</div>' +
      '<div><span>Clients</span>' + esc(p.clients) + '</div>' +
      '<div><span>Services</span>' + (esc((p.services || []).join(', ')) || '<span class="hint">-</span>') + '</div>' +
      '<div><span>Organisations</span>' + (esc((p.orgs || []).join(', ')) || '<span class="hint">-</span>') + '</div>' +
    '</div>' +
    '<div class="mp-addr"><span class="pct-hint">Top addresses:</span> ' +
      (p.top_addresses || []).map((a) => '<a href="#" data-svc-ip="' + esc(a) + '"><code>' +
        esc(a) + '</code></a>').join(' ') + '</div>' +
  '</div>';
  brancherLiensServices(host);
  document.getElementById('svc-map-place-close').addEventListener('click', () => {
    host.innerHTML = '';
    SVC.mapPlace = null;
    document.querySelectorAll('#svc-map circle.pt.on').forEach((o) => o.classList.remove('on'));
  });
}

/** Volume par pays, avec ce qui n'a pas pu etre localise en derniere ligne. */
function renderCountries(host, data) {
  if (!host) return;
  const pays = (data && data.countries) || [];
  const localises = pays.filter((c) => c.country);
  if (!localises.length) { host.innerHTML = ''; return; }
  const total = pays.reduce((a, c) => a + Number(c.down_bytes || 0) + Number(c.up_bytes || 0), 0) || 1;
  const inconnu = pays.find((c) => !c.country);
  host.innerHTML = '<table><thead><tr><th>Country</th><th class="num">Cities</th>' +
    '<th class="num">Addresses</th><th class="num">Clients</th>' +
    '<th class="num">Down</th><th class="num">Up</th><th>Share</th></tr></thead><tbody>' +
    localises.slice(0, 30).map((c) => {
      const v = Number(c.down_bytes || 0) + Number(c.up_bytes || 0);
      return '<tr><td>' + drapeau(c.country) + '<b>' + esc(countryName(c.country)) + '</b> ' +
        '<span class="pct-hint">' + esc(c.country) + '</span></td>' +
        '<td class="num">' + esc(c.cities || 0) + '</td>' +
        '<td class="num">' + esc(c.addresses) + '</td>' +
        '<td class="num">' + esc(c.clients) + '</td>' +
        '<td class="num">' + bytesText(c.down_bytes) + '</td>' +
        '<td class="num">' + bytesText(c.up_bytes) + '</td>' +
        '<td style="min-width:140px">' + meter(v, total, 'down') + '</td></tr>';
    }).join('') +
    (inconnu ? '<tr class="muted-row"><td><span class="na">Not located</span></td><td></td>' +
      '<td class="num">' + esc(inconnu.addresses) + '</td><td class="num">' + esc(inconnu.clients) + '</td>' +
      '<td class="num">' + bytesText(inconnu.down_bytes) + '</td>' +
      '<td class="num">' + bytesText(inconnu.up_bytes) + '</td>' +
      '<td style="min-width:140px">' + meter(Number(inconnu.down_bytes || 0) + Number(inconnu.up_bytes || 0), total, 'muted') +
      '</td></tr>' : '') +
    '</tbody></table>';
}

/** Emplacement d'une mini-carte, rempli par hydrateMiniMaps() une fois le
 *  contour du monde charge. Separe en deux temps parce que ipCard() rend une
 *  chaine synchrone et que le contour se lit en asynchrone. */
function miniMapHtml(lat, lon, nom) {
  return '<div class="minimap" data-lat="' + esc(lat) + '" data-lon="' + esc(lon) + '" data-name="' +
    esc(nom || '') + '"></div>';
}

/** Une mini-carte centree sur un point, sur une fenetre de 60 x 25 degres :
 *  assez pour reconnaitre le pays et ses voisins. */
async function hydrateMiniMaps(root) {
  const cibles = (root || document).querySelectorAll('.minimap[data-lat]');
  if (!cibles.length) return;
  const world = await loadWorld();
  cibles.forEach((el) => {
    const lat = Number(el.dataset.lat);
    const lon = Number(el.dataset.lon);
    if (!isFinite(lat) || !isFinite(lon)) return;
    const w = 600;
    const h = 250;
    const x = Math.max(0, Math.min(MAP_W - w, mapX(lon) - w / 2));
    const y = Math.max(0, Math.min(MAP_H - h, mapY(lat) - h / 2));
    el.innerHTML = '<svg class="worldmap mini" viewBox="' + x + ' ' + y + ' ' + w + ' ' + h +
      '" preserveAspectRatio="xMidYMid slice" style="--sw:.6">' + mapBackground(world) +
      '<circle class="pin-halo" cx="' + mapX(lon) + '" cy="' + mapY(lat) + '" r="14"></circle>' +
      '<circle class="pin" cx="' + mapX(lon) + '" cy="' + mapY(lat) + '" r="5"></circle>' +
      (el.dataset.name ? '<text class="pt-label" x="' + (mapX(lon) + 9) + '" y="' + (mapY(lat) + 4) +
        '" font-size="13">' + esc(el.dataset.name) + '</text>' : '') +
      '</svg>';
  });
}

/** TROUVER UNE IP : n'importe quelle adresse (ou nom), vue ou non sur le reseau.
 *
 *  La reponse fusionne ce que la base sait deja et ce qu'une analyse fraiche
 *  vient d'apprendre, avec les SEULES sources que l'exploitant autorise. Ce
 *  qu'une source coupee aurait pu dire est signale, pas invente. */
async function lookupIp(query) {
  const hote = document.getElementById('svc-lookup-result');
  const q = String(query || '').trim();
  if (!q) { hote.innerHTML = ''; return; }
  hote.innerHTML = '<div class="notice">Looking up <code>' + esc(q) + '</code>...</div>';
  let r;
  try {
    r = await api('/netflow/lookup/' + encodeURIComponent(q) + '?minutes=' + Math.max(SVC.minutes, 1440));
  } catch (err) {
    hote.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
    return;
  }
  if (r.internal) {
    hote.innerHTML = '<div class="notice ok"><b><code>' + esc(r.address) + '</code> belongs to your router ' +
      esc(r.router) + '</b> (' + esc(String(r.hostname || '').split(' · ').pop()) + '). ' +
      'Traffic to it stays on your network: no location, no internet service.</div>';
    return;
  }
  const frais = r.live || {};
  const connu = r.stored || {};
  // Le frais l'emporte champ par champ, la base comble ce qu'il n'a pas dit.
  const intel = {};
  ['hostname', 'service', 'category', 'source', 'org', 'asn', 'country', 'city', 'region',
    'latitude', 'longitude', 'network', 'resolved_at', 'attempts'].forEach((k) => {
    const v = frais[k] !== undefined && frais[k] !== null ? frais[k] : connu[k];
    if (v !== undefined && v !== null) intel[k] = v;
  });
  if (intel.category === 'unknown') delete intel.category;
  const fiche = {
    address: r.address,
    intel: intel,
    catalogue: r.catalogue || {},
    totals: (r.seen && r.seen.totals) || {},
    clients: (r.seen && r.seen.clients) || [],
  };
  const src = r.sources || {};
  const manque = [];
  if (!src.geoip) manque.push('location');
  if (!src.rdap) manque.push('registry (organisation, AS)');
  if (!src.rdns) manque.push('reverse name');
  hote.innerHTML =
    (r.resolved_from ? '<div class="notice"><code>' + esc(r.resolved_from) + '</code> resolves to <code>' +
      esc(r.address) + '</code>' + ((r.other_addresses || []).length ? ' (also ' +
      r.other_addresses.map((a) => '<a href="#" data-lookup="' + esc(a) + '"><code>' + esc(a) +
      '</code></a>').join(', ') + ')' : '') + '</div>' : '') +
    (!r.routable ? '<div class="notice warn"><b>Private or reserved address.</b> It is inside a ' +
      'network, not on the internet: it has no public location or owner.</div>' : '') +
    (r.routable && !src.enabled ? '<div class="notice warn"><b>Identification is off</b> ' +
      '(<a href="#/settings">Settings</a>): only the built-in catalogue answered.</div>' : '') +
    (r.routable && src.enabled && manque.length ? '<div class="notice">Sources turned off in ' +
      '<a href="#/settings">Settings</a>: ' + esc(manque.join(', ')) + '.</div>' : '') +
    ipCard(fiche, Math.max(SVC.minutes, 1440) * 60, false);
  brancherLiensServices(hote);
  hydrateMiniMaps(hote);
  hote.querySelectorAll('[data-lookup]').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    document.getElementById('svc-lookup').value = a.dataset.lookup;
    lookupIp(a.dataset.lookup);
  }));
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
          ? esc((r.logins || []).join(', ') || '0 subscriber')
          : r.scope === 'pops' ? 'site: ' + esc((r.pops || []).join(', ')) : 'everyone') + '</td>' +
        '<td>' + (r.enabled
          ? '<span class="badge ok">active</span>' : '<span class="badge">suspended</span>') +
          '</td>' +
        '<td>' + (r.last_applied_at
          ? esc(RULE_STATE[r.last_state] || r.last_state || '') + ' <span class="hint">' +
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
/** Etats d'une restriction : codes stockes en base (inchanges), libelles lus. */
const RULE_STATE = {
  'posee': 'applied', 'a poser': 'to apply', 'erreur': 'error', 'aucun routeur': 'no router',
  'aucune adresse': 'no address yet', 'levee': 'lifted', 'indisponible': 'unavailable',
};

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
          ? esc(vue.client_count) + ' prefix(es)'
          : vue.rule.scope === 'pops' ? 'site: ' + esc((vue.rule.pops || []).join(', ')) : 'everyone') +
          '</div>' +
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

/* ------------------------------------------- cibles d'une regle, en cascade
 *
 *  1. un ou plusieurs SITES (PoP ou VLAN) -- on peut s'arreter la : la regle
 *     vise tous leurs clients ;
 *  2. ou continuer et cocher un ou plusieurs CLIENTS de ces sites. */
const CIBLES = { sites: [], clients: [] };

function coches(id) {
  return Array.from(document.querySelectorAll('#' + id + ' input:checked')).map((c) => c.value);
}

function caseACocher(valeur, libelle, detail, coche) {
  return '<label class="pick-item"><input type="checkbox" value="' + esc(valeur) + '"' +
    (coche ? ' checked' : '') + '> <span>' + esc(libelle) + '</span>' +
    (detail ? '<span class="hint">' + esc(detail) + '</span>' : '') + '</label>';
}

async function chargerCiblesRegle() {
  const [sites, abonnes] = await Promise.all([
    api('/pops').catch(() => []),
    api('/subscribers/latest?limit=500&order_by=login&include_unmeasured=true').catch(() => []),
  ]);
  CIBLES.sites = Array.isArray(sites) ? sites : (sites.pops || []);
  CIBLES.clients = (abonnes || []).filter((r) => r.login);
  const deja = new Set(coches('svc-rule-pops'));
  document.getElementById('svc-rule-pops').innerHTML = CIBLES.sites.length
    ? CIBLES.sites.map((x) => caseACocher(x.name, x.name,
        x.kind === 'vlan' ? 'VLAN' + (x.vlan_id ? ' ' + x.vlan_id : '') : 'PoP',
        deja.has(x.name))).join('')
    : '<div class="hint">No site known.</div>';
  remplirClientsRegle();
  majResumes();
}

/** Les clients proposes suivent les sites coches (tous, si aucun). */
function remplirClientsRegle() {
  const sites = new Set(coches('svc-rule-pops'));
  const deja = new Set(coches('svc-rule-logins'));
  const proposes = CIBLES.clients.filter((r) => !sites.size || sites.has(r.pop_name));
  document.getElementById('svc-rule-logins').innerHTML = proposes.length
    ? proposes.map((r) => caseACocher(r.login, r.login,
        (r.pop_name || '') + (r.kind === 'static' ? ' · static IP' : ''),
        deja.has(r.login))).join('')
    : '<div class="hint">No client on these sites.</div>';
}

function majResumes() {
  const sites = coches('svc-rule-pops');
  const clients = coches('svc-rule-logins');
  const resume = (liste, vide) => !liste.length ? vide
    : liste.length <= 3 ? liste.join(', ') : liste.slice(0, 2).join(', ') + ' +' + (liste.length - 2);
  document.getElementById('svc-rule-pops-sum').textContent =
    resume(sites, 'Choose one or more sites');
  document.getElementById('svc-rule-logins-sum').textContent =
    resume(clients, sites.length ? 'All clients of the chosen sites' : 'Choose clients');
}

async function submitRule(event) {
  event.preventDefault();
  const action = document.getElementById('svc-rule-action').value;
  const choix = document.getElementById('svc-rule-scope').value;
  const sites = coches('svc-rule-pops');
  const clients = coches('svc-rule-logins');
  if (choix === 'pick' && !sites.length && !clients.length) {
    ruleNotice('<div class="notice err">Choose at least one site, or switch to "Every client".</div>');
    return;
  }
  // Des clients coches l'emportent : la regle ne vise qu'eux. Sinon, les sites.
  const scope = choix !== 'pick' ? 'all' : clients.length ? 'subscribers' : 'pops';
  const corps = {
    name: document.getElementById('svc-rule-name').value.trim(),
    action,
    services: selectedValues('svc-rule-services'),
    categories: selectedValues('svc-rule-categories'),
    prefixes: lignesNonVides('svc-rule-prefixes'),
    scope,
    logins: scope === 'subscribers' ? clients : [],
    pops: scope === 'pops' ? sites : [],
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
    document.getElementById('svc-rule-target').hidden = true;
    majResumes();
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
  // Un VLAN qui porte des clients declares : un site derriere son routeur.
  vlan: 'VLAN',
  // Nature a part entiere : ce noeud est DECLARE, pas decouvert.
  static: 'Static-IP client',
  // Ni infrastructure, ni abonne : une adresse vue, rien de plus.
  candidate: 'Detected, undeclared',
};
const KIND_COLOR = {
  gateway: 'var(--accent)', core: 'var(--accent)', pop: 'var(--down)',
  radio: 'var(--up)', sector: 'var(--up)', cpe: 'var(--muted)', client: '#a78bfa',
  unknown: 'var(--faint)', subscriber: '#a78bfa', static: '#f0abfc', vlan: 'var(--purple)',
  candidate: 'var(--warn)',
};

/* ------------------------------------------------- editeur d'arbre reseau */

const KIND_ORDER = [
  'gateway', 'core', 'pop', 'radio', 'sector', 'vlan', 'cpe', 'static', 'candidate', 'client', 'unknown',
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
  gateway: 0, core: 1, pop: 2, radio: 3, sector: 3, vlan: 3,
  cpe: 4, static: 4, candidate: 4, client: 4, unknown: 5,
};

/** Une mesure d'abonne est-elle ACTUELLE ? Un cycle toutes les 10 s : au-dela
 *  de 90 s, le chiffre est celui d'un autre moment. L'afficher comme le debit
 *  present cachait une collecte en panne -- des abonnes figes sur leurs
 *  keepalives pendant qu'un test de debit passait. */
function mesureFraiche(r) {
  if (!r || !r.ts) return false;
  return (Date.now() - new Date(r.ts).getTime()) < 90000;
}


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
  // Lien routeur -> VLAN : l'interface VLAN est lue dans la configuration du
  // routeur, c'est une preuve au meme titre qu'un /30 -- meme si d'autres
  // voisins se montrent sur ce VLAN.
  if (a && a.vlan) return true;
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
 *  vrai lien vers le coeur. Resultat : une CHAINE (coeur > PoP Altair > PoP Vega)
 *  la ou il fallait un arbre (coeur > PoP Altair, PoP Vega).
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
  // Un client a IP fixe a DEJA sa propre case dans le graphe : le compter
  // aussi dans l'agregat de son site le montrait deux fois.
  const fiches = new Set();
  nodes.forEach((n) => {
    if (n.kind !== 'static') return;
    let a = n.attributes;
    if (typeof a === 'string') { try { a = JSON.parse(a); } catch (e) { a = null; } }
    fiches.add((a && a.reference) || n.name);
  });
  (topo.subs || []).forEach((s) => {
    if (!s.pop_name) return;
    if (fiches.has(s.login)) return;
    if (!parPop.has(s.pop_name)) parPop.set(s.pop_name, []);
    parPop.get(s.pop_name).push(s);
  });
  if (parPop.size) {
    [...nodes.values()].forEach((n) => {
      const abonnes = parPop.get(n.name);
      if (!abonnes || !abonnes.length) return;
      // Seules les mesures ACTUELLES s'additionnent : une valeur vieille d'une
      // heure n'est pas le debit de ce PoP maintenant.
      const frais = abonnes.filter(mesureFraiche);
      const tx = frais.reduce((a, s) => a + (Number(s.tx_bps) || 0), 0);
      const rx = frais.reduce((a, s) => a + (Number(s.rx_bps) || 0), 0);
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
          synthRates: mesureFraiche(s) && (s.tx_bps || s.rx_bps)
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

/** Le debit d'un client a IP fixe, porte par son trait dans l'arbre.
 *
 *  Son rattachement est declare : aucun port ne le compte. Mais la collecte
 *  le mesure (file, interface VLAN s'il y est seul, ou NetFlow) : c'est ce
 *  debit-la que le trait affiche, plutot qu'un trait muet. */
function topoStaticRates(n) {
  if (!n || n.kind !== 'static') return null;
  let a = n.attributes;
  if (typeof a === 'string') { try { a = JSON.parse(a); } catch (e) { a = null; } }
  const ref = (a && a.reference) || n.name;
  const s = (topo.subs || []).find((x) => x.login === ref);
  if (!s || !mesureFraiche(s) || (s.tx_bps == null && s.rx_bps == null)) return null;
  return { down: Number(s.tx_bps) || 0, up: Number(s.rx_bps) || 0, cap: 0 };
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
    const rates = n.synthRates || topoEdgeRates(n.edge) || topoStaticRates(n);
    // Un client a IP fixe est une DECLARATION : son trait se dessine toujours,
    // comme celui d'un abonne -- sinon sa case flotte sous son VLAN sans rien
    // qui dise de qui elle depend.
    const declared = n.kind === 'static';
    // Un lien FORCE (parent pose a la main) ou MANUEL est toujours dessine :
    // sinon un lien qu'on vient de creer disparaitrait sous "debit seulement".
    const linkKey = (n.edge && n.edge.link && n.edge.link.key) || null;
    const manual = !!linkKey && String(linkKey).indexOf('manual:') === 0;
    const forced = n.parent_override && n.parent_override === p.key;
    // Le trait d'un abonne est un RATTACHEMENT, pas un cable : "liens a debit
    // seulement" filtre les adjacences decouvertes sans compteur, pas
    // l'appartenance d'un abonne a son PoP. Le masquer laissait sa case flotter
    // a cote de l'arbre, sans rien pour dire de qui elle depend.
    if (!rates && topo.rateOnly && !forced && !manual && !n.synthetic && !declared) return;

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
      '<div class="empty">Loading...</div></div>';
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
          ? '<div class="notice">' + (cible.plan_down_mbps
              ? 'Current plan: <strong>' + esc(mbps(cible.plan_down_mbps)) + ' / ' +
                esc(mbps(cible.plan_up_mbps || 0)) + '</strong>. '
              : '') + 'The queue will target <code>' + esc(cible.last_ip) +
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
            esc(dep.value) + '" placeholder="auto" title="auto = the plan (subscriber) or the measured capacity (link)">' +
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
        ((state.boostsParLogin || {})[abonne.login]
          ? '<button type="button" id="boost-clear" class="danger">Remove the running boost</button>'
          : '') +
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

  const effacer = document.getElementById('boost-clear');
  if (effacer) effacer.addEventListener('click', async () => {
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
  const mot = (n) => (n > 0 ? bpsText(n) : 'unlimited');
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
    if (rt.pending) {
      return '<div class="notice"><strong>' + esc(rt.router) + '</strong>' +
        '<span class="hint">First check running in the background: refresh in a minute.</span></div>';
    }
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
    } else if (ft.active === null && ft.detail) {
      bandeau = '<div class="notice"><span class="hint" style="margin:0">Fasttrack state unknown: ' +
        esc(ft.detail) + '</span></div>';
    }
    if (rt.checked_at) {
      bandeau += '<div class="hint" style="margin:.2rem 0 .5rem">' + esc(rt.router) + ' checked ' +
        esc(depuis(rt.checked_at)) + (rt.unverified_since ? ' · not checkable since ' +
          esc(depuis(rt.unverified_since)) + ' (' + esc(rt.unverified_reason || '') + ')' : '') + '</div>';
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
    : (!etat.enabled && etat.env_default)
      ? '<div class="notice warn"><b>Writing paused until the next restart.</b> ' +
        'Enforcement is on by default and comes back on at startup ' +
        '(<code>ENFORCEMENT_ENABLED=false</code> for a permanently read-only controller).</div>'
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
  renderAccounts();
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


/* ------------------------------------------------------------- insights
 *
 *  QUI RISQUE DE PARTIR, QUI EST PRET A MONTER EN GAMME, et combien d'abonnes
 *  chaque site peut encore prendre. Tout vient de ce qui est deja mesure :
 *  debit, plan, latence sous charge. */
/* ------------------------------------------------------------------ plans */

/** LE PLAN APPARTIENT AU CLIENT. Un PoP n'est qu'un point de connexion : il a
 *  une capacite, pas un plan. Le plan d'un client est pousse par la facturation
 *  (API Preseem) ou saisi ici ; la derniere ecriture gagne. Sans plan, le
 *  client recoit le plan par defaut. */
const PLANS = { data: null, editing: null, focus: null };

async function loadPlans() {
  // Un plan en cours de saisie ne doit pas etre efface par le rafraichissement.
  if (PLANS.editing) return;
  PLANS.data = await api('/plans');
  const d = PLANS.data;
  const bas = document.getElementById('plans-default-down');
  const haut = document.getElementById('plans-default-up');
  if (bas && document.activeElement !== bas && document.activeElement !== haut) {
    bas.value = d.default.down_mbps == null ? 0 : d.default.down_mbps;
    haut.value = d.default.up_mbps == null ? 0 : d.default.up_mbps;
  }
  const s = d.summary || {};
  document.getElementById('plans-stats').innerHTML =
    statCard('', 'Clients', String(s.clients || 0), '', 'every client known to the network') +
    statCard('', 'Pushed by the API', String(s.api || 0), '', 'billing is the source') +
    statCard('', 'Set here', String(s.manual || 0), '', 'until the next API push') +
    statCard('', 'Default plan', String(s.default || 0), '',
      d.default.down_mbps ? mbps(d.default.down_mbps) + ' / ' + mbps(d.default.up_mbps || 0)
        : 'no limit');
  renderPlanPackages();
  renderPlanClients();
}

const ORIGINE_PLAN = {
  api: ['ok', 'API'],
  ui: ['warn', 'Set here'],
  default: ['none', 'Default'],
};

function renderPlanClients() {
  const host = document.getElementById('plans-clients');
  const d = PLANS.data;
  if (!host || !d) return;
  const q = (document.getElementById('plans-search').value || '').trim().toLowerCase();
  const origine = document.getElementById('plans-origin').value;
  const lignes = (d.clients || []).filter((c) =>
    (!origine || c.origin === origine) &&
    (!q || [c.login, c.pop_name, c.address, c.service_id].some((v) =>
      String(v || '').toLowerCase().includes(q))));
  document.getElementById('plans-count').textContent = lignes.length + ' client(s)';
  if (!lignes.length) {
    host.innerHTML = '<div class="empty">' + ((d.clients || []).length
      ? 'No client matches this filter.' : 'No client yet: they appear as soon as they connect.') +
      '</div>';
    return;
  }
  const debit = (v) => (v == null ? '<span class="na">no limit</span>' : esc(mbps(v)));
  const forfaits = d.packages || [];
  host.innerHTML = '<table><thead><tr><th>Client</th><th>Site</th><th>Address</th>' +
    '<th class="num">&darr; Download</th><th class="num">&uarr; Upload</th>' +
    '<th>Source</th><th>Last change</th><th></th></tr></thead><tbody>' +
    lignes.slice(0, 500).map((c) => {
      const o = ORIGINE_PLAN[c.origin] || ['none', c.origin];
      const detail = c.origin === 'api'
        ? (c.service_id ? 'service ' + c.service_id : '') + (c.package_id ? ' · package ' + c.package_id : '')
        : c.origin === 'ui' ? (c.updated_by ? 'by ' + c.updated_by : '')
          : '';
      const edition = PLANS.editing === c.login;
      const force = c.forced_down_mbps != null || c.forced_up_mbps != null;
      const ligne = '<tr' + (PLANS.focus === c.login ? ' class="row-focus"' : '') + '>' +
        '<td><b>' + esc(c.login) + '</b>' + (c.kind === 'static' ? ' <span class="badge">static IP</span>' : '') +
          '</td>' +
        '<td>' + esc(c.pop_name || '-') + '</td>' +
        '<td><code>' + esc(c.address || '-') + '</code></td>' +
        // Une limite FORCEE (Subscribers > Rate) prime sur le plan : c'est elle
        // qu'on affiche, le plan en dessous. Sinon la page disait 100/20 pour un
        // client bride a 300k/750k.
        (force
          ? '<td class="num">' + debit(c.forced_down_mbps) +
              '<span class="hint">plan ' + debit(c.down_mbps) + '</span></td>' +
            '<td class="num">' + debit(c.forced_up_mbps) +
              '<span class="hint">plan ' + debit(c.up_mbps) + '</span></td>' +
            '<td>' + sqCell('Forced', 'warn', 'Limit set by hand in Subscribers (Rate): it overrides ' +
              'the plan until you change the plan here or reset the queues') +
              '<span class="hint">plan: ' + esc(o[1]) + '</span></td>'
          : '<td class="num">' + debit(c.down_mbps) + '</td>' +
            '<td class="num">' + debit(c.up_mbps) + '</td>' +
            '<td>' + sqCell(o[1], o[0]) + (detail ? '<span class="hint">' + esc(detail) + '</span>' : '') +
              '</td>') +
        '<td>' + (c.updated_at ? esc(depuis(c.updated_at)) : '<span class="na">-</span>') + '</td>' +
        '<td class="nowrap"><button class="sm" data-plan-edit="' + esc(c.login) + '">Change</button>' +
          (c.origin !== 'default' || force
            ? ' <button class="sm" data-plan-reset="' + esc(c.login) + '"' +
              (force ? ' title="Lift the forced limit and remove the plan: the client is observed, not throttled"' : '') +
              '>Default</button>' : '') +
        '</td></tr>';
      if (!edition) return ligne;
      return ligne + '<tr class="plan-edit"><td colspan="8"><form class="lq-rate" data-plan-form="' +
          esc(c.login) + '" style="margin:0">' +
        (forfaits.length
          ? 'Package <select name="package" style="width:auto"><option value="">— custom rate —</option>' +
            forfaits.map((f) => '<option value="' + esc(f.id) + '"' +
              (f.id === c.package_id ? ' selected' : '') + '>' + esc(f.name || f.id) + ' (' +
              esc(mbps(f.down_mbps || 0)) + ' / ' + esc(mbps(f.up_mbps || 0)) + ')</option>').join('') +
            '</select> or ' : '') +
        '&darr; <input name="down" type="number" min="0" step="any" style="width:6rem" value="' +
          esc(c.down_mbps == null ? '' : c.down_mbps) + '"> Mbps ' +
        '&uarr; <input name="up" type="number" min="0" step="any" style="width:6rem" value="' +
          esc(c.up_mbps == null ? '' : c.up_mbps) + '"> Mbps ' +
        '<button class="sm primary" type="submit">Save and apply</button> ' +
        '<button class="sm" type="button" data-plan-cancel>Cancel</button>' +
        '<span class="hint" style="display:inline"> Applied on the router right away. ' +
          (force ? '<b>Lifts the forced limit set in Subscribers.</b> ' : '') +
          'The next API push for this client replaces it.</span>' +
        '</form></td></tr>';
    }).join('') + '</tbody></table>';

  host.querySelectorAll('[data-plan-edit]').forEach((b) => b.addEventListener('click', () => {
    PLANS.editing = b.dataset.planEdit;
    renderPlanClients();
  }));
  host.querySelectorAll('[data-plan-cancel]').forEach((b) => b.addEventListener('click', () => {
    PLANS.editing = null;
    renderPlanClients();
  }));
  host.querySelectorAll('[data-plan-reset]').forEach((b) => b.addEventListener('click', () =>
    savePlan(b.dataset.planReset, null)));
  host.querySelectorAll('[data-plan-form]').forEach((f) => f.addEventListener('submit', (e) => {
    e.preventDefault();
    const forfait = f.package ? f.package.value : '';
    const corps = forfait ? { package_id: forfait } : {
      down_mbps: Number(f.down.value) > 0 ? Number(f.down.value) : null,
      up_mbps: Number(f.up.value) > 0 ? Number(f.up.value) : null,
    };
    if (!forfait && corps.down_mbps == null && corps.up_mbps == null) {
      alert('Enter a rate, or choose a package.');
      return;
    }
    savePlan(f.dataset.planForm, corps);
  }));
}

async function savePlan(login, corps) {
  const res = document.getElementById('plans-result');
  try {
    const r = corps
      ? await api('/plans/' + encodeURIComponent(login), { method: 'PUT', body: JSON.stringify(corps) })
      : await api('/plans/' + encodeURIComponent(login), { method: 'DELETE' });
    const c = r.client || {};
    res.innerHTML = '<div class="notice ok"><b>' + esc(login) + '</b>: ' +
      esc(c.down_mbps == null ? 'no limit' : mbps(c.down_mbps)) + ' / ' +
      esc(c.up_mbps == null ? 'no limit' : mbps(c.up_mbps)) +
      (corps ? '' : ' (default plan)') +
      (r.forced_limit_lifted ? ' — forced limit lifted' : '') + '</div>' + poseText(r.enforcement);
  } catch (err) {
    res.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
  PLANS.editing = null;
  PLANS.focus = login;
  await loadPlans();
}

function renderPlanPackages() {
  const host = document.getElementById('plans-packages');
  const forfaits = (PLANS.data && PLANS.data.packages) || [];
  if (!host) return;
  host.innerHTML = forfaits.length
    ? '<table><thead><tr><th>Package</th><th>Id</th><th class="num">&darr; Download</th>' +
      '<th class="num">&uarr; Upload</th></tr></thead><tbody>' +
      forfaits.map((f) => '<tr><td><b>' + esc(f.name || f.id) + '</b></td><td><code>' + esc(f.id) +
        '</code></td><td class="num">' + esc(f.down_mbps ? mbps(f.down_mbps) : '-') +
        '</td><td class="num">' + esc(f.up_mbps ? mbps(f.up_mbps) : '-') + '</td></tr>').join('') +
      '</tbody></table>'
    : '<div class="empty">No package pushed yet (<code>PUT /model/v1/packages/&lt;id&gt;</code>). ' +
      'Packages are optional: a service can carry its own rates.</div>';
}

/** Ouvre la page Plans sur un client (depuis l'onglet Executive). */
function openClientPlan(login) {
  PLANS.focus = login;
  location.hash = '#/plans';
  setTimeout(() => {
    const champ = document.getElementById('plans-search');
    if (champ) { champ.value = login; renderPlanClients(); }
  }, 300);
}

/** Pourquoi les listes sont vides, quand c'est faute de donnees.
 *
 *  "Nobody shows a sign of leaving" sur une installation d'hier ne veut pas
 *  dire "tout va bien" mais "pas encore assez d'historique" : on le dit. */
function renderInsightsNotice(r, jours) {
  const hote = document.getElementById('ins-notice');
  if (!hote) return;
  if (!r) { hote.innerHTML = ''; return; }
  const manques = [];
  if (!r.subscribers) {
    manques.push('no subscriber collected yet: add a router in Devices');
  } else {
    if (r.history_days != null && r.history_days < r.needed_days) {
      manques.push(esc(r.history_days) + ' day(s) of measurements out of the ' +
        esc(r.needed_days) + ' needed to compare ' + esc(jours) + ' days with the ' +
        esc(jours) + ' before (usage drop and silent lines appear after that)');
    }
    if (r.with_plan < r.subscribers) {
      manques.push((r.subscribers - r.with_plan) + ' of ' + r.subscribers +
        ' subscriber(s) without a plan: "bigger plan" cannot be judged for them ' +
        '(set the speed in the PPP profile or the API)');
    }
    if (!r.with_traffic) manques.push('no traffic measured over the period');
    if (!r.with_qoe) {
      manques.push('no latency-under-load measurement yet: the experience score stays empty');
    }
  }
  hote.innerHTML = manques.length
    ? '<div class="notice warn"><b>Not enough data yet for reliable lists.</b><ul>' +
      manques.map((m) => '<li>' + m + '</li>').join('') + '</ul></div>'
    : '';
}

async function loadInsights() {
  const jours = Number(document.getElementById('ins-days').value) || 7;
  const [abos, sites] = await Promise.all([
    api('/insights/subscribers?days=' + jours),
    api('/insights/capacity?hours=' + Math.min(720, jours * 24)).catch(() => ({ sites: [] })),
  ]);
  const lignes = abos.subscribers || [];
  const sm = abos.summary || {};
  document.getElementById('ins-count').textContent = lignes.length + ' subscriber(s)';
  renderInsightsNotice(abos.readiness, jours);
  document.getElementById('ins-stats').innerHTML =
    statCard(sm.at_risk ? 'crit' : '', 'At risk of leaving', String(sm.at_risk || 0), '',
      'poor experience, usage collapsing, or silent') +
    statCard(sm.upgrade ? 'down' : '', 'Ready for a bigger plan', String(sm.upgrade || 0), '',
      'living at their plan ceiling, good experience') +
    statCard('', 'Healthy', String(sm.healthy || 0), '', 'nothing to act on');

  const lien = (r) => '<a href="#" data-ins-sub="' + r.subscriber_id + '">' + esc(r.login) + '</a>';
  const usage = (r) => esc(bpsText(r.avg_down_bps)) +
    (r.prev_avg_down_bps ? ' <span class="pct-hint">was ' + esc(bpsText(r.prev_avg_down_bps)) + '</span>' : '');
  const qoe = (r) => r.qoe_score == null ? '<span class="na">-</span>'
    : sqCell(Math.round(r.qoe_score) + (r.qoe_grade ? ' · ' + r.qoe_grade : ''), qoeSev(r.qoe_score));
  const plan = (r) => r.plan_down_mbps ? esc(mbps(r.plan_down_mbps) + ' / ' + mbps(r.plan_up_mbps || 0))
    : '<span class="na">no plan</span>';
  const table = (xs, vide) => !xs.length ? '<div class="empty">' + vide + '</div>'
    : '<table><thead><tr><th>Subscriber</th><th>Site</th><th class="num">Plan</th>' +
      '<th class="num">Avg download</th><th class="num">At ceiling</th><th class="num">Experience</th>' +
      '<th>Why</th></tr></thead><tbody>' + xs.map((r) => '<tr><td>' + lien(r) + '</td>' +
        '<td>' + esc(r.pop_name || '-') + '</td><td class="num">' + plan(r) + '</td>' +
        '<td class="num">' + usage(r) + '</td>' +
        '<td class="num">' + (r.ceiling_share ? Math.round(r.ceiling_share * 100) + '%' : '-') + '</td>' +
        '<td class="num">' + qoe(r) + '</td><td>' + esc((r.reasons || []).join(' · ')) + '</td></tr>').join('') +
      '</tbody></table>';
  document.getElementById('ins-risk').innerHTML =
    table(lignes.filter((r) => r.status === 'at_risk'), 'Nobody shows a sign of leaving.');
  document.getElementById('ins-upgrade').innerHTML =
    table(lignes.filter((r) => r.status === 'upgrade'), 'Nobody lives at their plan ceiling.');
  document.getElementById('ins-all').innerHTML = table(lignes, 'No subscriber.');

  const lesSites = sites.sites || [];
  document.getElementById('ins-capacity').innerHTML = !lesSites.length
    ? '<div class="empty">No site.</div>'
    : '<table><thead><tr><th>Site</th><th class="num">Subscribers</th><th class="num">Capacity</th>' +
      '<th class="num">Busy-hour peak</th><th class="num">Poor experience</th>' +
      '<th class="num">Room for</th><th>Basis</th></tr></thead><tbody>' +
      lesSites.map((x) => '<tr><td><b>' + esc(x.pop_name) + '</b></td>' +
        '<td class="num">' + esc(x.subscribers) + '</td>' +
        '<td class="num">' + (x.capacity_mbps ? esc(mbps(x.capacity_mbps)) : '<span class="na">-</span>') + '</td>' +
        '<td class="num">' + (x.peak_mbps != null ? esc(mbps(x.peak_mbps)) : '<span class="na">-</span>') + '</td>' +
        '<td class="num">' + (x.poor_share != null ? sqCell(Math.round(x.poor_share * 100) + '%',
          x.poor_share >= 0.2 ? 'crit' : x.poor_share > 0 ? 'warn' : 'ok') : '<span class="na">-</span>') + '</td>' +
        '<td class="num">' + (x.room == null ? '<span class="na">?</span>'
          : sqCell(x.room + ' more', x.room === 0 ? 'crit' : x.room < 5 ? 'warn' : 'ok')) + '</td>' +
        '<td class="hint" style="display:table-cell">' + esc(x.reason || '') + '</td></tr>').join('') +
      '</tbody></table>';

  document.querySelectorAll('[data-ins-sub]').forEach((a) => a.addEventListener('click', (e) => {
    e.preventDefault();
    openSubscriber(Number(a.dataset.insSub));
  }));
}

const LOADERS = {
  dashboard: loadDashboard,
  exec: loadExec,
  traffic: loadTraffic,
  network: loadNetwork,
  subscribers: loadSubscribers,
  plans: loadPlans,
  insights: loadInsights,
  pops: loadRouters,
  api: loadApi,
  settings: loadSettings,
};

async function show(view) {
  if (view === 'services') view = 'traffic'; // ancien onglet, fusionne dans Trafic
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
  document.getElementById('pb-title').textContent = lien ? lien.textContent.trim() : '';
  DIRECT.ok = null;
  DIRECT.echec = null;
  majDirect();
  // La page se charge DES SON OUVERTURE, et le dit : un rond a cote du titre
  // tant que ses donnees arrivent. Une page vide sans signe se lit "il n'y a
  // rien", alors qu'elle veut dire "ca arrive".
  if (titre) titre.classList.add('page-loading');
  try {
    await refresh();
  } finally {
    if (titre && state.view === view) titre.classList.remove('page-loading');
  }
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

/** Derniere mise a jour reussie et dernier echec de l'onglet affiche. */
const DIRECT = { ok: null, echec: null };

let refreshing = false;
async function refresh() {
  // UN CHARGEMENT EN COURS NE BLOQUE QUE LE MEME ONGLET.
  //
  // Le verrou etait global : cliquer sur un onglet pendant qu'un autre
  // chargeait encore ne chargeait RIEN -- la page restait vide jusqu'au
  // rafraichissement automatique suivant, dix secondes plus tard. C'est ce
  // qui rendait toutes les pages "lentes". Seul un second chargement du MEME
  // onglet est inutile ; un changement d'onglet part tout de suite.
  const vue = state.view;
  if (refreshing === vue) return;
  refreshing = vue;
  try {
    await LOADERS[vue]();
    if (state.view === vue) {
      appError(null);
      DIRECT.ok = Date.now();
      DIRECT.echec = null;
    }
  } catch (err) {
    console.error('Rafraichissement impossible :', err);
    if (state.view === vue) {
      appError(err && err.message ? err.message : String(err));
      DIRECT.echec = Date.now();
    }
  } finally {
    if (refreshing === vue) refreshing = false;
    if (state.view === vue) { majDirect(); construireSommaire(); }
  }
}

function route() { if (!AUTH.ready) return; show((location.hash || '#/dashboard').replace('#/', '')); }

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
    renderThroughput(document.getElementById('throughput-chart'), state.lastPoints,
      { now: state.lastNow });
  }
});

document.getElementById('range-select').addEventListener('change', (e) => {
  state.rangeMinutes = Number(e.target.value);
  loadThroughput();
});
document.getElementById('btn-test').addEventListener('click', testConnection);

/* ------------------------------------------------------- trafic et API */
document.getElementById('flow-range').addEventListener('change', loadTraffic);
document.getElementById('exec-lat-search').addEventListener('input', renderLatencyClients);
document.getElementById('plans-search').addEventListener('input', renderPlanClients);
document.getElementById('plans-origin').addEventListener('change', renderPlanClients);
document.getElementById('plans-default-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const res = document.getElementById('plans-default-result');
  try {
    for (const [nom, id] of [['default_plan_down_mbps', 'plans-default-down'],
      ['default_plan_up_mbps', 'plans-default-up']]) {
      await api('/settings/' + nom, {
        method: 'PUT',
        body: JSON.stringify({ value: Number(document.getElementById(id).value) || 0 }),
      });
    }
    const r = await api('/plans/refresh', { method: 'POST' }).catch(() => ({ ok: false }));
    res.innerHTML = r.ok
      ? '<div class="notice ok">Default plan saved and applied to the clients pushed without a rate.</div>'
      : '<div class="notice warn">Default plan saved. It reaches the routers at the next cycle ' +
        '(a few minutes).</div>';
  } catch (err) {
    res.innerHTML = '<div class="notice err">' + esc(err.message) + '</div>';
  }
  await loadPlans();
});
document.getElementById('exec-lat-filter').addEventListener('change', renderLatencyClients);
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
document.getElementById('svc-category').addEventListener('change', loadServices);
// La recherche se declenche sur 'change' (validation ou perte de focus) et non
// sur chaque frappe : une requete par caractere ferait autant de lectures de
// base qu'il y a de lettres dans "nflxvideo".
document.getElementById('svc-search').addEventListener('change', loadServices);
document.getElementById('svc-lookup-form').addEventListener('submit', (e) => {
  e.preventDefault();
  lookupIp(document.getElementById('svc-lookup').value);
});
document.getElementById('svc-rule-form').addEventListener('submit', submitRule);
// Les champs qui n'ont de sens que pour un effet ou une portee donnes restent
// caches tant qu'ils ne servent pas : un formulaire qui montre tout montre
// surtout ce qu'il ne faut pas remplir.
document.getElementById('svc-rule-action').addEventListener('change', (e) => {
  document.getElementById('svc-rule-limits').hidden = e.target.value !== 'limit';
});
document.getElementById('svc-rule-scope').addEventListener('change', async (e) => {
  const choisir = e.target.value === 'pick';
  document.getElementById('svc-rule-target').hidden = !choisir;
  if (choisir) await chargerCiblesRegle();
});
document.getElementById('svc-rule-pops').addEventListener('change', () => {
  remplirClientsRegle();
  majResumes();
});
document.getElementById('svc-rule-logins').addEventListener('change', majResumes);
document.querySelectorAll('.pick-filter').forEach((champ) =>
  champ.addEventListener('input', () => {
    const q = champ.value.trim().toLowerCase();
    document.getElementById(champ.dataset.filter).querySelectorAll('label').forEach((l) => {
      l.hidden = !!q && !l.textContent.toLowerCase().includes(q);
    });
  }));
// Une liste ouverte se referme quand on clique ailleurs.
document.addEventListener('click', (e) => {
  document.querySelectorAll('details.pick-dd[open]').forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  });
});
document.getElementById('tp-router').addEventListener('change', (e) => {
  TP.router = e.target.value;
  // Un client d'un autre routeur n'a plus de sens : on repart de tous.
  TP.clientId = null;
  document.getElementById('tp-client').value = '';
  remplirClientsDebit();
  loadThroughput();
});
document.getElementById('tp-client').addEventListener('change', (e) => {
  const saisi = e.target.value.trim();
  const trouve = TP.clients.find((c) => c.login === saisi);
  if (saisi && !trouve) {
    e.target.setCustomValidity('Unknown client');
    e.target.reportValidity();
    return;
  }
  e.target.setCustomValidity('');
  TP.clientId = trouve ? trouve.subscriber_id : null;
  loadThroughput();
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
  await Promise.all([loadStaticClients(), loadVlanClients()]);
});
document.getElementById('sc-form').addEventListener('submit', scEnregistrer);
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

document.getElementById('auth-form').addEventListener('submit', submitAuth);
['auth-password', 'auth-email'].forEach((id) => document.getElementById(id).addEventListener('input', () => {
  if (AUTH.mode !== 'setup') return;
  jaugeMdp(document.getElementById('auth-meter'), document.getElementById('auth-password').value,
    document.getElementById('auth-email').value);
}));
document.getElementById('ins-days').addEventListener('change', loadInsights);
document.getElementById('global-search').addEventListener('input', (e) => {
  clearTimeout(GS.timer);
  GS.timer = setTimeout(() => globalSearch(e.target.value), 180);
});
document.getElementById('global-search').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.target.value = ''; globalSearch(''); e.target.blur(); }
  if (e.key === 'Enter') {
    const premier = document.querySelector('#global-search-results [data-gs]');
    if (premier) premier.click();
  }
});
document.addEventListener('keydown', (e) => {
  const cible = e.target && e.target.tagName;
  if (e.key === '/' && cible !== 'INPUT' && cible !== 'TEXTAREA' && cible !== 'SELECT') {
    e.preventDefault();
    document.getElementById('global-search').focus();
  }
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.global-search')) document.getElementById('global-search-results').hidden = true;
});
document.getElementById('logout-btn').addEventListener('click', logout);
boot();
// Les PoPs ne changent pas tout seuls : inutile de recharger ce formulaire
// pendant qu'un administrateur le remplit.
// Les vues d'edition ne se rafraichissent pas toutes seules : ce serait effacer
// un formulaire en cours de saisie, ou un plan qu'on est en train de lire.
const VUES_FIGEES = new Set(['pops', 'settings']);
/** Pourquoi le direct est suspendu en ce moment, ou null s'il tourne. */
function pauseDirect() {
  // L'arbre porte le debit des liens : le laisser vivre pour ne pas afficher un
  // debit perime. Mais on ne rafraichit PAS pendant qu'on deplace une case,
  // qu'une case est selectionnee (panneau ouvert), ou qu'un menu est ouvert :
  // ce serait annuler le geste en cours.
  if (state.view === 'network' && (topo.dragging || topo.selected || topo.linkMode ||
      (document.activeElement && document.activeElement.tagName === 'SELECT'))) {
    return 'Paused while you edit the tree';
  }
  // Vue Files live : ne pas ecraser un champ de debit en cours de saisie.
  if (state.view === 'exec' && document.activeElement &&
      document.activeElement.tagName === 'INPUT') return 'Paused while you type';
  // Trafic : ne pas ecraser un FORMULAIRE en cours de saisie (regle, exporteur).
  // Un champ de recherche ou de filtre, lui, n'arrete plus le direct : un clic
  // dans la recherche figeait la page pour toujours.
  if (state.view === 'traffic' && document.activeElement &&
      ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) &&
      document.activeElement.closest('form')) return 'Paused while you fill the form';
  return null;
}

setInterval(() => {
  // Rien ne se rafraichit derriere l'ecran de connexion.
  if (!AUTH.ready) return;
  if (VUES_FIGEES.has(state.view)) return;
  if (pauseDirect()) return;
  refresh();
  // Le tiroir d'un lien suit le meme rythme : on regarde un debit justement
  // quand il bouge.
  if (state.link) openLink(state.link.key, state.link.minutes, true);
}, 10000);
setInterval(() => { if (AUTH.ready) refreshHealth(); }, 15000);

/* ------------------------------------------------------- barre de page
 *
 *  L'ETAT DU DIRECT SE VOIT. Les pages se rafraichissent toutes les 10 s, mais
 *  rien ne le disait : un chiffre fige (rafraichissement en pause pendant une
 *  saisie, API en panne) ne se distinguait pas d'un chiffre frais. La barre dit
 *  "Live · updated 4 s ago", "Paused while you type", ou "Update failed". */
function depuisCourt(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return s + ' s ago';
  const m = Math.round(s / 60);
  return m < 60 ? m + ' min ago' : Math.round(m / 60) + ' h ago';
}

function majDirect() {
  const pastille = document.getElementById('live-state');
  const texte = document.getElementById('live-text');
  if (!pastille || !texte) return;
  let classe = 'live';
  let libelle;
  const pause = state.view ? pauseDirect() : null;
  if (DIRECT.echec && (!DIRECT.ok || DIRECT.echec > DIRECT.ok)) {
    classe = 'err';
    libelle = 'Update failed' + (DIRECT.ok ? ' · data from ' + depuisCourt(DIRECT.ok) : '');
  } else if (!DIRECT.ok) {
    classe = 'wait';
    libelle = 'Loading…';
  } else if (VUES_FIGEES.has(state.view)) {
    classe = 'still';
    libelle = 'Loaded ' + depuisCourt(DIRECT.ok) + ' · not auto-refreshed';
  } else if (pause) {
    classe = 'pause';
    libelle = pause;
  } else {
    libelle = 'Live · updated ' + depuisCourt(DIRECT.ok);
  }
  pastille.className = 'live ' + classe;
  if (texte.textContent !== libelle) { texte.textContent = libelle; pastille.title = libelle; }
}
setInterval(majDirect, 1000);
document.getElementById('refresh-btn').addEventListener('click', () => { if (AUTH.ready) refresh(); });

/** Le sommaire de la page : une pastille par section visible, pour y sauter.
 *  Reconstruit apres chaque chargement (une section peut apparaitre avec ses
 *  donnees) ; rien n'est touche si la liste n'a pas change. */
function construireSommaire() {
  const nav = document.getElementById('page-toc');
  const vue = document.getElementById('view-' + state.view);
  if (!nav || !vue) return;
  const titres = [...vue.querySelectorAll('h2')].filter((h) => h.offsetParent !== null &&
    !h.closest('details:not([open]) > :not(summary)') && libelleAide(h, true));
  const signature = state.view + '|' + titres.map((h) => libelleAide(h, true)).join('|');
  if (nav.dataset.sig === signature) return;
  nav.dataset.sig = signature;
  if (titres.length < 3) { nav.innerHTML = ''; return; }
  nav.innerHTML = titres.map((h, i) => {
    if (!h.id) h.id = 'sec-' + state.view + '-' + i;
    return '<a href="#" data-toc="' + h.id + '">' + esc(libelleAide(h, true).split(' — ')[0]) + '</a>';
  }).join('');
  suivreSommaire();
}

document.getElementById('page-toc').addEventListener('click', (e) => {
  const lien = e.target.closest('[data-toc]');
  if (!lien) return;
  e.preventDefault();
  const cible = document.getElementById(lien.dataset.toc);
  if (cible) cible.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

/** La section en cours de lecture est allumee dans le sommaire. */
function suivreSommaire() {
  const nav = document.getElementById('page-toc');
  const liens = nav ? [...nav.querySelectorAll('[data-toc]')] : [];
  if (!liens.length) return;
  const barre = document.getElementById('page-bar').getBoundingClientRect().bottom;
  let actif = liens[0];
  liens.forEach((a) => {
    const h = document.getElementById(a.dataset.toc);
    if (h && h.getBoundingClientRect().top <= barre + 40) actif = a;
  });
  // En bas de page, la derniere section est forcement celle qu'on lit.
  if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) actif = liens[liens.length - 1];
  liens.forEach((a) => a.classList.toggle('active', a === actif));
  if (actif && nav.scrollWidth > nav.clientWidth) {
    const g = actif.offsetLeft - nav.offsetLeft;
    if (g < nav.scrollLeft || g + actif.offsetWidth > nav.scrollLeft + nav.clientWidth) {
      nav.scrollTo({ left: g - 24, behavior: 'smooth' });
    }
  }
}
let sommairePrevu = false;
window.addEventListener('scroll', () => {
  if (sommairePrevu) return;
  sommairePrevu = true;
  requestAnimationFrame(() => { sommairePrevu = false; suivreSommaire(); });
}, { passive: true });

/* La barre se "colle" : une sentinelle juste au-dessus d'elle sort de l'ecran,
 * la barre prend un fond et montre le nom de la page. Sur petit ecran, elle se
 * colle sous la barre d'onglets, dont la hauteur varie. */
function hauteurBandeau() {
  const tete = document.querySelector('header.top');
  const h = tete && getComputedStyle(tete).position === 'sticky' ? tete.offsetHeight : 0;
  document.documentElement.style.setProperty('--topbar-h', h + 'px');
  return h;
}
let observateurBarre = null;
function surveillerBarre() {
  if (observateurBarre) observateurBarre.disconnect();
  observateurBarre = new IntersectionObserver(([e]) => {
    document.getElementById('page-bar').classList.toggle('stuck', !e.isIntersecting);
  }, { rootMargin: '-' + hauteurBandeau() + 'px 0px 0px 0px' });
  observateurBarre.observe(document.getElementById('page-bar-sentinel'));
}
surveillerBarre();
// La barre d'onglets change de hauteur (compte affiche apres la connexion,
// rotation de l'ecran) : la barre de page se recale dessous.
const recaler = () => { clearTimeout(surveillerBarre.t); surveillerBarre.t = setTimeout(surveillerBarre, 100); };
window.addEventListener('resize', recaler);
if (window.ResizeObserver) new ResizeObserver(recaler).observe(document.querySelector('header.top'));

// Boutons "aller a" : un lien #ancre casserait le routage par #/onglet.
document.addEventListener('click', (e) => {
  const cible = e.target.closest && e.target.closest('[data-scroll-to]');
  if (!cible) return;
  const el = document.getElementById(cible.dataset.scrollTo);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

/* ============================================================ info-bulles
 *
 *  UNE EXPLICATION POUR CHAQUE SECTION ET CHAQUE VALEUR, au meme endroit.
 *  Plutot que de semer des title="" dans cent gabarits, un dictionnaire unique
 *  indexe par le LIBELLE affiche ; apres chaque rendu, une petite icone (i) est
 *  posee a cote de tout libelle connu (titres, colonnes, tuiles, lignes de
 *  panneau). Un meme mot peut vouloir dire deux choses selon l'endroit : la cle
 *  "contexte|libelle" l'emporte alors sur "libelle" (contexte du gabarit, ou
 *  data-aide-ctx pose sur un bloc entier).
 *
 *  Chaque entree repond, quand c'est utile, a quatre questions :
 *    t  : ce que c'est ;
 *    m  : comment c'est mesure (d'ou vient le chiffre, a quel rythme) ;
 *    s  : l'echelle de lecture, en couleurs ([ton, texte]) ;
 *    r  : comment le lire (pieges, cas particuliers) ;
 *    a  : quoi faire quand c'est mauvais.
 *  Une simple chaine reste permise pour une valeur qui se passe d'echelle. */
const OK = 'ok', WARN = 'warn', CRIT = 'crit', NA = 'none';
const ECHELLE_LATENCE = [[OK, 'under 30 ms: good — calls, games and browsing feel instant'],
  [WARN, '30 to 100 ms: noticeable — fine for streaming, sluggish for games'],
  [CRIT, 'over 100 ms: poor — video calls stutter, pages hesitate']];
const ECHELLE_SCORE = [[OK, '80 to 100: good'], [WARN, '50 to 79: fair — noticeable at busy times'],
  [CRIT, 'under 50: poor — the client feels it'], [NA, 'grey / “-”: not measured yet']];
const ECHELLE_CHARGE = [[OK, 'under 70%: comfortable'], [WARN, '70 to 90%: watch it — peaks start to queue'],
  [CRIT, 'over 90%: saturated — latency rises for everyone behind it']];
const ECHELLE_BLOAT = [[OK, 'A+ / A: up to +30 ms — imperceptible'], [WARN, 'B / C: +30 to +100 ms — calls degrade while someone downloads'],
  [CRIT, 'D / F: over +100 ms — real-time use breaks under load']];

const AIDE = {
  // ---------------------------------------------------------- sections
  'network throughput': {
    t: 'Total traffic of all your subscribers (PPPoE and static clients) over time. Download is drawn above the axis, upload below.',
    m: 'Read every 10 s from each subscriber’s queue on its router, then summed. Solid line = average of each time step; dotted line = the highest 10-second moment inside the step, so a short burst is not flattened away.',
    r: 'Traffic that belongs to no subscriber — a bandwidth test between routers, device management, an undeclared client — is not in this curve: it shows in Routers — live traffic as “not from clients”. Filter by router or client with the menus.',
    a: 'A flat line at 0 while clients are online usually means the collection stopped: check the health dot at the top left and Devices › Polled routers.',
  },
  'routers': {
    t: 'Each router, live: what it exchanges with the internet on its uplink, what its own clients consume, and the gap between the two.',
    m: 'Uplink port counters and subscriber queues, read every 10 s.',
    r: 'A large gap (“not from clients”) is traffic of no known client: tests, management, or clients not yet declared.',
  },
  'routers — live traffic': {
    t: 'Each router, live: what it exchanges with the internet on its uplink, what its own clients consume, and the gap between the two.',
    m: 'Uplink port counters and subscriber queues, read every 10 s. Router CPU and port load come from the same poll.',
    s: ECHELLE_CHARGE,
    r: 'The coloured state line at the top says whether each collection (subscribers, ports, latency) is running. A large “not from clients” share is traffic of no known client.',
    a: 'Red collection state: open Devices › Polled routers and test the connection of that router.',
  },
  'top consumers': {
    t: 'The subscribers using the most bandwidth right now.',
    m: 'Last 10-second measurement of each subscriber’s queue, sorted by download.',
    r: 'A client at the top of this list is not a problem in itself — it is using what it pays for. It matters when its row is red (at its limit) and the node it belongs to is saturated too.',
  },
  'radio backhauls': {
    t: 'The radio links declared in the inventory (UISP / airOS), with what they can carry now and how loaded they are.',
    m: 'Current capacity is read from the radio (it drops when the signal fades); nominal capacity is what the link is rated for. Load = traffic ÷ current capacity.',
    s: ECHELLE_CHARGE,
    a: 'Current capacity far below nominal: alignment, interference or rain fade — check signal and SNR on the antenna page.',
  },
  'saturation risks': {
    t: 'Every link compared with what it can carry, worst first.',
    m: 'Traffic of the link ÷ its capacity (the lowest of: rate set by hand, measured radio capacity, port speed). Bar = now; marker = the peak of the selected period.',
    s: ECHELLE_CHARGE,
    r: 'Internet side (gateway uplink, PoP-to-core): a saturation hits every client. PoP side (towards subscribers, VLANs, relays): only the clients behind that link. A link with “capacity unknown” cannot be judged — set its rate in the Network tree.',
    a: 'Peak regularly over 90%: upgrade the link or lower the plans sold behind it. Only the peak is high: usually fine, links are meant to be full at the busiest minute.',
  },
  'latency by client': {
    t: 'The experience each subscriber really gets, with a verdict (good / fair / poor) and the reason in plain words.',
    m: 'The PoP router pings each client 5 times every 30 s (200 ms apart, from its loopback). Median, p95, jitter and loss come from those pings; “under load” compares latency when the client’s line is busy with latency at rest.',
    s: [[OK, 'good: median under 30 ms, no loss, little bufferbloat'], [WARN, 'fair: 30–100 ms, spikes, some loss, or +30 to +150 ms under load'],
      [CRIT, 'poor: over 100 ms, 2%+ loss, +150 ms under load, or score under 50'], [NA, 'at plan limit: judged apart (see below)']],
    r: 'Samples taken while the client uses 85%+ of its plan are left out: a full line makes its own queue — that is not a network fault. “no reply” on every client of a router means the probe itself is failing, not the clients.',
    a: 'Many poor clients behind the same node: look at that node’s load and backhaul. A single poor client: its CPE, signal or home Wi-Fi.',
  },
  'load by node': {
    t: 'Traffic of each node (site) compared with the sum of its clients’ limits.',
    m: 'Sum of the clients’ queues every 10 s, divided by the sum of their limits.',
    r: 'This is a share of what was sold, not of the link: 100% means every client is at its cap at once, which practically never happens.',
  },
  'queues by node': {
    t: 'Each node (PoP or router) with its clients: traffic now vs limit, limits applied, plans sold, worst latency and experience.',
    m: 'Queue counters every 10 s; latency from the probe every 30 s. A VLAN client is listed under the router that carries it.',
    r: 'Click a row to open its detail below. The “Rate” field lets you force a client’s limit by hand — it then overrides its plan until you clear it.',
  },
  'selection': 'Details of the node or client selected in the table above: link, plans, limits and the clients behind it.',
  'sec|node': {
    t: 'Detail of the selected node: its link (capacity and load), the limits and plans of its clients, and how each client is doing.',
    r: 'Oversubscription = plans sold ÷ link capacity. Some is normal (clients are not all active at once); far above 3× the link will be full at busy hours.',
  },
  'sec|client': {
    t: 'Detail of the selected client: its plan and where it comes from, the limit really applied, its usage and its latency at rest and under load.',
  },
  'health over time': {
    t: 'One colour per time step for three indicators: experience score, latency and load vs limit. Read it left to right to see when things went wrong.',
    m: 'Steps of the selected period (e.g. 5 min over 24 h). Steps where a client was at its plan limit are left out of latency and score.',
    s: [[OK, 'green: good'], [WARN, 'orange: fair / watch'], [CRIT, 'red: poor / saturated'], [NA, 'grey: no measurement in that step']],
    r: 'For a node: the worst client of the step. For the whole network: all clients together. A red band at the same hour every evening is a capacity problem; a red band at random times is more often radio.',
  },
  'who consumes': {
    t: 'Volume per subscriber over the period.',
    m: 'From NetFlow at the counting point, each client counted once (the best exporter for that client is used, never two).',
    r: 'This is volume (GB), not speed: a client streaming all evening outweighs one who ran a short speed test.',
  },
  'where the traffic goes': {
    t: 'Map of the destinations your clients reach: each country and city sized by the volume exchanged with it.',
    m: 'From NetFlow: each remote address is located from its address block (geolocation database). Hover a point for its volume and clients; click it to open the detail of that place.',
    r: 'A location is that of the server’s block, not of the company: a CDN often answers from a nearby city even for a foreign service.',
  },
  'which services the traffic comes from': {
    t: 'Traffic grouped by recognised service (YouTube, Netflix, Steam, Microsoft updates…).',
    m: 'The remote address of each flow is matched with a catalogue of published address blocks, then with its reverse name and owner. Content is never inspected — it stays encrypted.',
    r: '“unknown” is traffic to addresses that match no catalogue entry yet; it shrinks as names are resolved.',
  },
  'who talks to whom, client by client': {
    t: 'Each client and the internet addresses it exchanges with: volume, rate while active and live rate.',
    m: 'NetFlow records of the period, grouped by client and remote address.',
    r: '“Rate when active” divides by the time the conversation was really active, so a 10-second download at 100 Mbps shows 100 Mbps — not the 1 Mbps a whole-hour average would give.',
  },
  'destinations reached': {
    t: 'Internet addresses reached by your clients, with their owner, location and volume.',
    m: 'From NetFlow; names come from the catalogue, reverse DNS and (if enabled) the registry.',
  },
  'traffic restrictions': {
    t: 'Rules that block or cap traffic towards a service, a category or an address range — for everyone, some sites, or some clients.',
    m: 'Written on the routers as address lists plus firewall (block) or mangle + queue tree (cap). The address list is refreshed automatically from the catalogue and from what NetFlow discovers.',
    a: 'A rule that seems to have no effect: check “Last applied” and that the router is reachable; new addresses join the list at the next refresh.',
  },
  'find an ip': {
    t: 'Look up any IP address or domain: owner, location, recognised service, and which of your clients reach it.',
    r: 'Useful when a client complains about one site: see whether others reach it too and how much traffic it carries.',
  },
  'every subscriber plan, usage, experience': {
    t: 'All subscribers with their limit, current usage, latency and whether the router really holds the cap.',
    r: 'Click a client to see its plan, its queue on the router and its history. The search box accepts a login, an IP, a MAC or a site.',
  },
  'are the caps actually held': {
    t: 'Compares the limit freeQoS intends for each client with what is really written on its router.',
    r: 'A mismatch means the router was changed by hand, a queue failed to write, or another queue matches first (RouterOS applies the first matching queue).',
    a: 'Use Devices › Reset queues on that router to rebuild its freeQoS queues from scratch.',
  },
  'default plan': {
    t: 'The limit given to a client pushed by the API (or entered) without a rate. A client only DETECTED on a router, with no plan pushed, is observed and never throttled.',
    r: '0 = no limit. To also cap detected clients, turn on default_plan_for_detected_clients in Settings. Changing it re-writes the queues of the clients that use it.',
  },
  'clients': {
    t: 'Each client and the plan applied to it, with where that plan comes from.',
    r: 'Source: pushed by the API (billing / CRM), set by hand here, or the default plan. A limit forced in Subscribers (Rate) overrides all three — it is shown here so you know why the plan is not what is applied.',
  },
  'packages pushed by the api': 'Offers received from your billing system or CRM through the public API (/model/v1/packages). Clients pushed with a package get its rates automatically.',
  'at risk of leaving': {
    t: 'Clients showing the signs that come before a cancellation.',
    m: 'Any of: experience score under 50 over the period; usage down more than 70% versus the previous period; no traffic at all for 3 days or more (for a client that used to consume).',
    a: 'Call before they do: a CPE swap, a realignment or a plan change is cheaper than a lost client.',
  },
  'ready for a bigger plan': {
    t: 'Clients that live at their plan ceiling while their experience stays good — the plan limits them, not the network.',
    m: 'At 90%+ of their plan in 15% or more of the samples of the period, with a score of 50 or more.',
    r: 'A client at its ceiling with poor latency is not here on purpose: selling it more would not fix what is a network problem.',
  },
  'sites and access points room for more subscribers': {
    t: 'How many more subscribers each site or access point can take at today’s usage.',
    m: 'Busy-hour peak per subscriber, against a target of 80% of the capacity (above that, queues fill and latency rises before the link is full).',
    r: 'A site where 20% or more of the clients already have a poor experience shows no room, whatever the margin on paper.',
  },
  'router health': {
    t: 'CPU, memory and uptime of each polled router.',
    s: [[OK, 'CPU under 70%: fine'], [WARN, '70–90%: busy — queues and NetFlow may lag'], [CRIT, 'over 90%: overloaded — packets and measurements suffer']],
    a: 'A sustained high CPU on a PoP: too many simple queues for the hardware, or a firewall rule doing heavy work.',
  },
  'polled routers': {
    t: 'The routers freeQoS reads (and writes queues to).',
    r: 'Reset queues deletes every freeQoS queue of that router and rewrites them cleanly — use it when caps behave strangely. Queues not created by freeQoS are never touched.',
  },
  'connect a router': {
    t: 'Add a MikroTik router: its address, API port and an account with read rights (and write rights if freeQoS should shape).',
    r: 'The API-SSL port (8729) is preferred. A dedicated account with the “read, write, api, test” policies is enough — “test” is needed for the latency probe.',
  },
  'ubiquiti antennas': 'airOS access points and CPEs polled for their radio state: signal, noise, CCQ, capacity. Their capacity feeds the saturation and room-for-more calculations.',
  'radio health access points and their cpes': {
    t: 'Each access point and the CPEs connected to it, with the radio quality of each link.',
    s: [[OK, 'signal above −68 dBm, SNR above 25 dB, CCQ above 90%'], [WARN, 'signal −68 to −75 dBm, SNR 20–25 dB'], [CRIT, 'signal below −75 dBm, SNR under 20 dB or CCQ under 75%']],
    a: 'Weak signal on one CPE: alignment or obstacle. Weak on all CPEs of an AP: interference — check the noise floor and change channel.',
  },
  'add an antenna': 'Declare an airOS device by its address and credentials. Credentials are encrypted at rest with the controller key.',
  'inventory': 'Every device freeQoS knows, with where it was discovered.',
  'known sites': 'The PoPs and VLAN sites known to freeQoS. A site pushed by the API takes the place of an automatically discovered one.',
  'shaping and writing to the routers': {
    t: 'Whether freeQoS writes queues by itself.',
    r: 'On: a reconciliation every 2 min writes any missing or wrong queue. Off: it writes only when you act (Rate, plan change, Reset queues). Either way, only queues marked freeqos:managed are ever modified.',
  },
  'log of commands sent': 'Every command freeQoS sent to a router: when, by whom (account or automatic loop), what, and the result. The first place to look when a cap changed unexpectedly.',
  'create a key': {
    t: 'Create a key for an external system (billing, CRM) to call the public API.',
    r: 'The key is shown once — copy it then. “read” gives GET; “write” adds PUT and DELETE. Give each system its own key so you can revoke one without breaking the others.',
  },
  'endpoints': {
    t: 'The routes an external application (billing, CRM, provisioning) calls with a key.',
    r: '/model/v1 and /usage/v1: clients, packages, sites, sectors and usage. /api/v1: everything the interface does — routers, plans, forced limits, boosts.',
  },
  'example': 'A ready-to-run call with curl. The key goes in Basic authentication as the username, with an empty password.',
  'accounts': 'Who can log in to this interface, your own password and sessions, and (for edit accounts) the journal of logins.',
  'operational settings': {
    t: 'Settings stored in the database and applied without restart.',
    r: '“default” = the value from the environment / built-in default. Apply writes it; the source column then says “database”.',
  },
  'sec|services and ip location': {
    t: 'What NetFlow keeps about the destinations your clients reach, and how addresses are named (catalogue, reverse DNS, registry, geolocation).',
    r: 'Volumes per client are measured whatever these say; they only decide what is kept per destination and how much naming work (and outbound queries) the controller does.',
  },
  'sec|shaping': {
    t: 'How queues are computed and written: safety factor on measured capacity, floor rate, default plan, adoption of queues freeQoS did not create, and how a queue targets the client.',
    r: 'A change here is applied at the next reconciliation (or right away with Reset queues in Devices).',
  },
  'sec|cake': {
    t: 'Parameters of the CAKE queues — the queue discipline that keeps latency low while a line is full.',
    r: 'Keep the defaults unless you know why: overhead and MPU must match the encapsulation (PPPoE, VLAN) for the rate to be exact; rtt sets how fast CAKE reacts.',
  },
  'sec|write safeguards': 'Limits that stop an automatic loop from changing too much at once (circuit breaker), and whether a separate write account is required on the routers.',
  'sec|traffic': 'Where the volume of each client is counted when both the internet edge and the PoPs export NetFlow — so a byte is never counted twice.',
  'sec|collection cadences': {
    t: 'How often each collection runs.',
    r: 'Shorter = fresher figures, but more load on the routers and the database. A 10 s subscriber poll and a 30 s latency probe suit most networks.',
  },
  'what stays out of reach of the interface': 'Settings that can only change in the environment (.env) — the database address, the encryption key… — because the interface itself depends on them.',
  // ---------------------------------------------------------- comptes
  'who can log in': {
    t: 'The accounts of this interface.',
    s: [[NA, 'Read only: sees everything; every change is refused by the server'], [OK, 'Edit: can change everything, including accounts']],
    r: 'There is always at least one active edit account: the last one cannot be deleted, demoted or disabled. Changing someone’s role or password logs them out everywhere.',
  },
  'my password': {
    t: 'Change your own password. Your other browsers are logged out; this one stays connected.',
    r: 'At least 12 characters. Passwords from attacker lists (Password2024!, azerty123…), repetitive ones or ones containing your email are refused. A short sentence is both stronger and easier to remember than a short complicated word.',
  },
  'my sessions': {
    t: 'Every browser currently logged in to your account: device, address, when it logged in and when it was last active.',
    m: 'A session ends after 24 h without activity, and after 30 days in any case. At most 10 per account — the oldest is closed beyond that.',
    a: 'A device or address you do not recognise: log it out, then change your password.',
  },
  'login journal': {
    t: 'Every login, failed attempt, block, logout and account change, with address and device. Kept 6 months.',
    s: [[OK, 'Login'], [CRIT, 'Failed login / Blocked: someone typed a wrong password'], [WARN, 'Account changed or deleted']],
    r: 'After 5 failures the email and the address are blocked 5 min, then 10, 20… up to 1 h if it continues. Many failures from one unknown address = someone guessing.',
    a: 'Repeated failures on a real account from an unknown address: change that password and, if exposed to the internet, restrict access to the interface.',
  },
  'acc|role': 'Read only: sees everything, changes nothing (refused by the server, not just hidden). Edit: can change everything, including accounts.',
  'acc|state': 'A disabled account can no longer log in, and its open sessions are closed at once.',
  'acc|last login': 'Last successful login of the account.',
  'acc|created by': 'The account that created this one (“setup” = the very first account).',
  'acc|device': 'Browser and system, read from the browser’s User-Agent.',
  'acc|address': 'IP address the session was opened from (behind a reverse proxy, set FORWARDED_ALLOW_IPS so this is the real client).',
  'acc|opened': 'When this session logged in.',
  'acc|last activity': 'Last request made with this session.',
  'acc|when': 'Date and time of the event (hover for the full date).',
  'acc|event': 'What happened: login, failed login, block, logout, account change…',
  'acc|account': 'The account concerned (for a failed login, the email that was typed — it may not exist).',
  'acc|by': 'Who made the change, when it is not the account itself (e.g. an administrator resetting a password).',
  'acc|detail': 'Extra information: new role, why it failed, how long the block lasts…',
  // ---------------------------------------------------------- panneaux
  'right now': 'Last 10-second measurement.',
  'clients on this link': {
    t: 'The clients of this node with how much of their limit they use, their latency and score.',
    r: 'Several clients red on latency at the same time as the link is loaded = the link is the bottleneck. Only one client red = look at that client.',
  },
  'link': {
    t: 'What freeQoS knows about this node’s link: capacity, load, limits applied and plans sold behind it.',
    r: 'Capacity unknown: set the rate of the link in the Network tree so load and oversubscription can be judged.',
  },
  'plan': {
    t: 'The plan of this client, where it comes from, and the limit really applied.',
    r: 'When “limit applied” differs from the plan, the limit was forced by hand (Rate) or a temporary boost is running.',
  },
  'plan usage': {
    t: 'How much of its limit the client uses right now, and its latency at rest and under load.',
    r: 'At 85%+ of its plan the client is “at plan limit”: its latency then reflects its own full queue and is not held against the network.',
  },
  'limit usage': 'How much of its forced limit the client uses right now, and its latency at rest and under load.',
  'who reaches this address': 'Your clients that exchanged traffic with this address over the period, with the volume of each.',
  // ---------------------------------------------------------- tuiles
  'traffic now': {
    t: 'Download of all subscribers at the last measurement; upload below.',
    m: 'Sum of the subscriber queues, read every 10 s.',
    r: 'Compared with the backhaul capacity, or with the sum of limits when no capacity is declared.',
  },
  'client experience': {
    t: 'Share of measured clients with a good experience.',
    s: ECHELLE_SCORE,
    r: 'Clients at their plan limit and clients not measured are counted apart, so neither makes the network look worse than it is.',
  },
  'busiest node': {
    t: 'The node using the largest share of its limit right now.',
    s: ECHELLE_CHARGE,
  },
  'latency': {
    t: 'Round-trip time between the PoP router and the client — how long a packet takes to go and come back.',
    m: 'The router pings the client 5 times every 30 s (200 ms apart, from its loopback). The median of the series is shown, so one slow ping does not colour the client.',
    s: ECHELLE_LATENCE,
    r: '“no reply” = the whole last series was lost. If every client of a router shows it at once, the probe is failing (firewall, source address, account without the “test” right) — not the clients.',
    a: 'High latency on one client only: its radio link or CPE. On every client of a node: the node is saturated or its backhaul is weak.',
  },
  'download': {
    t: 'What your clients receive, from the internet to them.',
    m: 'Sum of the subscriber queues (tx side of the router), read every 10 s.',
  },
  'upload': {
    t: 'What your clients send, from them to the internet.',
    m: 'Sum of the subscriber queues (rx side of the router), read every 10 s.',
  },
  'subscribers online': 'Subscribers with a measurement in the last 2 minutes (PPPoE session up, or static client with traffic).',
  'sold throughput': {
    t: 'Sum of the download plans of all subscribers, and how much of it is used right now.',
    r: 'A few percent is normal: clients are not all active at the same time. That is what makes oversubscription possible.',
  },
  'backhaul capacity': 'Sum of the measured capacity of the radio backhauls declared in the inventory. “-” = no radio backhaul declared.',
  'downstream': 'Total received by your subscribers over the period, at the counting point (each byte counted once).',
  'upstream': 'Total sent by your subscribers over the period, at the counting point (each byte counted once).',
  'clients seen': 'Clients with traffic seen over the period. Each client is counted once, ' +
    'even if its traffic crosses several routers.',
  'traffic identified': {
    t: 'Share of the traffic flows tied to a known client (' +
      'the figure is since the controller started).',
    r: 'The rest is network management, routers talking to each other, or addresses of clients not declared yet — those appear in the entry-aid list.',
  },
  'poor experience': {
    t: 'Clients the network is failing right now.',
    m: 'Median over 100 ms, 2%+ loss, +150 ms or more under load, or an experience score under 50.',
    a: 'Open Latency by client and sort by verdict: the “why” column says which of these it is.',
  },
  'fair': 'Noticeable but usable: latency 30–100 ms, spikes, a little loss, or +30 to +150 ms under load.',
  'good': 'Latency under 30 ms, stable, no loss, little or no bufferbloat.',
  'at plan limit': {
    t: 'Clients that used their whole plan the whole period.',
    r: 'Their latency was that of their own full queue — a client downloading at its cap delays its own pings. That is the plan doing its job, not a network fault, so they are not counted as poor.',
  },
  'clients measured': 'Clients that answered the latency probe over the period.',
  // ---------------------------------------------------------- colonnes
  'client': 'The subscriber: PPPoE login, or the reference of a static-IP client.',
  'subscriber': 'The subscriber: PPPoE login, or the reference of a static-IP client.',
  'site': 'PoP or VLAN site the client belongs to.',
  'pop': 'Point of presence the client is attached to.',
  'node': 'A site (PoP or router) and its clients. A VLAN client is counted with the router that carries it.',
  'address': 'IP address (or block) of the client: its queue targets this address.',
  'limit': {
    t: 'The cap really applied to the client.',
    r: 'In order of priority: the limit forced by hand in Subscribers (Rate), otherwise its plan, otherwise the default plan.',
  },
  'plans': 'Sum of the plans sold. “no plan” = the clients are limited by hand or by the default plan.',
  'download now': 'Current download, with its share of the limit.',
  'upload now': 'Current upload, with its share of the limit.',
  'vs limit': { t: 'Current download as a share of the limit.', s: ECHELLE_CHARGE },
  'plan used': { t: 'Current traffic as a share of the client’s limit.', r: 'At 85% and above the client is “at plan limit”: its latency is then left out of its verdict.' },
  'experience': {
    t: 'Score from 0 to 100 summing up what the client feels.',
    m: 'The weaker of two parts: latency at rest (no penalty up to 10 ms, then −0.6 point per ms) and the latency added under load (bufferbloat: +5 ms ≈ 90, +30 ms ≈ 80, +100 ms = 50, +200 ms = 25).',
    s: ECHELLE_SCORE,
    r: 'The weakest part decides: low latency does not make up for heavy bufferbloat, and the reverse.',
  },
  'score': {
    t: 'Experience score from 0 to 100: the weaker of latency at rest and latency added under load (bufferbloat).',
    s: ECHELLE_SCORE,
  },
  'p95': {
    t: 'The bad moments: 95% of the pings were faster than this.',
    r: 'A p95 far above the median (3× or more, and over 100 ms) means spikes: the line is fine most of the time but stalls now and then — typical of radio interference or a queue filling up.',
  },
  'jitter': {
    t: 'How much latency varies from one ping to the next in the last series.',
    s: [[OK, 'under 10 ms: smooth'], [WARN, '10 to 30 ms: calls may crackle'], [CRIT, 'over 30 ms: choppy voice and video']],
  },
  'loss': {
    t: 'Share of pings lost in the last series of 5.',
    s: [[OK, '0%'], [WARN, 'under 2%: occasional'], [CRIT, '2% and more: calls cut, downloads slow down']],
    r: 'A loss while the client is at its plan limit is not counted against the network: its own traffic filled the queue.',
  },
  'under load': {
    t: 'Latency while the client’s line is busy, and how much the load adds over rest (bufferbloat).',
    m: 'Pings taken while the client was transferring are compared with pings taken at rest.',
    s: ECHELLE_BLOAT,
    a: 'Strong bufferbloat with the client under its plan: the queue upstream (backhaul, radio) is too deep. CAKE queues on the bottleneck fix most of it.',
  },
  'why': 'What lowered the verdict, in plain words: high latency, spikes, loss, bufferbloat — or “at plan limit”.',
  'bufferbloat': { t: 'Grade of the latency added when the line is loaded.', s: ECHELLE_BLOAT },
  'source': 'Where the plan comes from: pushed by the API, set by hand, or the default plan — or a limit forced in Subscribers, which overrides all three.',
  'last change': 'When the plan was last changed, and by what (API, an account, the default).',
  'destination': 'Internet address the client exchanged traffic with.',
  'service': 'Recognised service behind the address: from the catalogue of published blocks, the domain name or the owner of the address.',
  'category': 'Family of the service: streaming, gaming, voice/video, CDN, cloud, updates…',
  'port': 'Service port on the remote side (443 = HTTPS, 53 = DNS…). “-” for ICMP (ping).',
  'proto': 'Transport protocol: tcp, udp, icmp…',
  'received': 'Volume received by the client from this address over the period.',
  'sent': 'Volume sent by the client to this address over the period.',
  'rate when active': {
    t: 'Volume divided by the time the conversation was really active.',
    r: 'Not divided by the whole period: a 10-second download at 100 Mbps reads 100 Mbps, which is what the client experienced.',
  },
  'live': 'Current rate of the conversation, from the real duration of its last NetFlow records. Empty = not active right now.',
  'down': 'Volume received by the clients.',
  'up': 'Volume sent by the clients.',
  'flows': 'Number of NetFlow records counted.',
  'seen': 'Last time it was seen.',
  'kind': 'PPPoE subscriber (discovered from the router sessions) or static-IP client (declared by hand).',
  'router': 'The router concerned.',
  'port speed': 'Negotiated speed of the port (for a VLAN or bridge: the speed of the physical port carrying it).',
  'load': { t: 'Busiest direction compared with the port speed.', s: ECHELLE_CHARGE },
  'towards': 'Equipment on the other side of the port, when known (from discovery or the network tree).',
  'to clients': 'Traffic going towards the clients on this port.',
  'to internet': 'Traffic going towards the internet on this port.',
  'capacity': {
    t: 'What the link can carry.',
    m: 'The lowest of: the rate set by hand on the link, the capacity measured on the radio, and the port speed.',
    r: '“unknown” = none of the three is known: load and saturation cannot be judged for this link.',
  },
  'headroom': { t: 'Capacity left at the peak of the period.', r: 'Under 10% at the peak: the next growth in usage will show as latency.' },
  'vantage': {
    t: 'Where this exporter measures: internet edge (above the core) or at a PoP.',
    r: 'Set automatically for your own routers. Only one vantage is used for counting, so a byte seen at both is counted once.',
  },
  'sampling': '“all” = every flow is exported; 1:N = one in N, volumes are multiplied back by N.',
  'datagrams': 'NetFlow packets received from this exporter. Not increasing = the router stopped exporting, or a firewall drops UDP 2055.',
  'version': 'NetFlow format received: v5, v9 or IPFIX.',
  'access': 'PoP to its subscribers: median of their medians (p90 when hovering). The part of the latency your access network adds.',
  'next hop up': 'Router to its default gateway — the next hop up: the core for a PoP, the gateway for the core, the transit provider for the gateway.',
  'where': 'Which segment adds the delay: access side, towards the core / gateway, or above it (transit, internet).',
  'oversubscription': {
    t: 'Plans sold ÷ capacity of the link.',
    s: [[OK, 'up to 1×: everything sold fits at once'], [WARN, '1 to 3×: usual for residential access'], [CRIT, 'over 3×: the link will be full at busy hours']],
    r: 'Some oversubscription is normal — clients are not all active at the same time. Watch the evening peak rather than the ratio alone.',
  },
  'addresses': 'Number of distinct internet addresses reached.',
  'country': 'Country of the address, from the geolocation of its block.',
  'cities': 'Number of distinct cities among the addresses reached.',
  'location': 'Approximate location of the address (city, country) from its block — an indication, not a proof.',
  'from': 'Start of the link (the upstream equipment).',
  'to': 'End of the link (the downstream equipment).',
  'type': 'How the link was found: discovered on the router, observed in traffic, or set by hand.',
  'method': 'HTTP method: GET reads, PUT creates or replaces, DELETE removes.',
  'path': 'Route of the call, after the base URL.',
  'object': 'The collection concerned: accounts, packages, sites, access points, services.',
  // ---------------------------------------------------------- lignes de panneau
  'throughput': 'Current download and upload.',
  'worst latency': 'Latency of the worst client of the node; “no reply” if one of them lost all its pings.',
  'worst score': { t: 'Lowest experience score among the clients of the node.', s: ECHELLE_SCORE },
  'load now': 'Current traffic of the node.',
  'limits applied': 'Sum of the limits really applied to the clients (forced, plan or default).',
  'sold': 'Sum of the plans sold to these clients. “no plan” = limited by hand or by default.',
  'limit applied': 'The cap written on the router, when it differs from the plan (forced by hand, or a boost).',
  // ---------------------------------------------------------- contextes
  'col|clients': 'Number of clients of this node.',
  'boost': 'Temporary extra rate given to the client; it ends on its own at the time shown.',
  'session': 'How long the PPPoE session has been up (static clients have none). A session that keeps restarting points to a CPE or radio problem.',
  'sample': 'Age of the last measurement. “stale” = the figures are not current: the collection of this router is late or stopped.',
  'at ceiling': { t: 'Share of the time the client was at 90%+ of its limit over the period.', r: '15% or more with a good experience: candidate for a bigger plan.' },
  'avg download': 'Average download over the period.',
  'avg rate': 'Average rate over the period (volume ÷ period).',
  'busy-hour peak': 'Highest load reached during the busiest hour of the period — what the capacity must hold.',
  'ccq': {
    t: 'Client Connection Quality: share of radio frames that went through without retransmission.',
    s: [[OK, '90 to 100%'], [WARN, '75 to 90%'], [CRIT, 'under 75%: flagged as an issue — retransmissions eat throughput and add latency']],
  },
  'signal': {
    t: 'Strength of the radio signal received, in dBm (closer to 0 is stronger).',
    s: [[OK, 'above −68 dBm'], [WARN, '−68 to −75 dBm'], [CRIT, 'below −75 dBm: weak — alignment or obstacle']],
  },
  'noise': 'Radio noise floor, in dBm (lower is better, e.g. −95 is quieter than −85). A high noise floor means interference on the channel.',
  'snr': {
    t: 'Signal-to-noise ratio, in dB: signal minus noise.',
    s: [[OK, 'above 25 dB: clean'], [WARN, '20 to 25 dB: usable, lower modulation'], [CRIT, 'under 20 dB: flagged as an issue — unstable link']],
  },
  'distance': 'Radio distance between the access point and the CPE.',
  'cpu': { t: 'Processor load of the router.', s: [[OK, 'under 70%'], [WARN, '70 to 90%'], [CRIT, 'over 90%']] },
  'memory': 'Memory used on the router.',
  'uptime': 'Time since the last restart. A recent restart nobody planned is worth a look (power, crash).',
  'measured rate': 'Rate measured on the router for this client.',
  'forced rate': 'Limit set by hand in Subscribers (Rate): it overrides the plan until you clear it.',
  'on the router': 'What is really written on the router for this client.',
  'wanted': 'What freeQoS intends to write.',
  'where the cap comes from': 'Origin of the limit: plan, forced by hand, boost or default plan.',
  'why it does not throttle': 'Reason why the cap is not held on the router (no queue, another queue matching first, a parent queue blocking…).',
  'room for': { t: 'How many more subscribers this site can take at the current usage.', m: 'Busy-hour peak per subscriber against 80% of the capacity.' },
  'share': 'Share of the total traffic.',
  'usage': 'Detected application of the conversation.',
  'capacity read': 'Capacity read from the radio.',
  'rate tx/rx': 'Radio link rates (transmit / receive).',
  'sold (down)': 'Sum of the download plans sold on this site.',
  'sold (up)': 'Sum of the upload plans sold on this site.',
  'subscribers': 'Number of subscribers.',
  'prefix': 'Address block.',
  'published prefixes': 'Address blocks announced by this owner on the internet.',
  'reverse name': 'Name returned by reverse DNS for this address.',
  'last applied': 'When the rule was last written on the routers.',
  'effect': 'Block the traffic, or cap it at a rate.',
  'for whom': 'Every client, some sites, or some clients.',
  'state': 'Current state.',
  'interface': 'Router interface concerned.',
  'queue': 'freeQoS queue on the router for this client.',
  'target': 'Address or block the queue applies to.',
  'role': 'Role of the router: PoP (serves clients), core, or gateway (internet exit).',
  'origin': 'Where the item comes from.',
  'package': 'Offer pushed by the API.',
  'col|plan': 'The plan sold to the client (download / upload).',
  'col|capacity / limit': {
    t: 'On a node row: what its uplink (towards the core or the transit) can carry. On a client row: the cap applied to the client.',
    m: 'Wired link: the capacity declared, or the port speed. Radio link: the capacity its antenna announces live (airOS / UISP). Auto: the lowest known.',
    r: 'A node has no plan — its clients do. Its download / upload % is taken against this capacity. Grey “limits” = uplink capacity unknown: the sum of the clients’ limits is shown instead.',
    a: 'Declare each uplink wired or radio with “wired / radio…” in Saturation risks.',
  },
  'col|sold / plan': {
    t: 'On a node row: the plans sold behind it divided by its uplink capacity. On a client row: its plan.',
    s: [[OK, 'up to 1×: everything sold fits at once'], [WARN, '1 to 3×: usual for residential access'], [CRIT, 'over 3×: the uplink will be full at busy hours']],
  },
  'radio': {
    t: 'Radio link: its capacity is read live on its antenna and follows rain fade, interference and alignment.',
    s: [[OK, 'live: at or near its nominal capacity'], [WARN, 'under 70% of nominal: degraded'], [CRIT, 'silent: no recent reading — capacity unknown']],
  },
  'heat|experience score': { t: 'Experience score of the step (the worst client of the node).', s: ECHELLE_SCORE },
  'heat|latency': { t: '90th percentile of the latency of the step: 9 pings out of 10 were faster.', s: ECHELLE_LATENCE },
  'heat|load vs limit': {
    t: 'For a node: its most loaded client against its own limit. For the whole network: total traffic against the sum of limits.',
    s: ECHELLE_CHARGE,
  },
  'kpi|internet': 'What this router exchanges with the outside on its uplink port, with the port load.',
  'kpi|clients': 'Sum of the traffic of this router’s subscribers (their queues).',
  'kpi|not from clients': {
    t: 'Internet minus clients: traffic of no known client.',
    r: 'Bandwidth tests, device management, routers talking to each other, or clients not declared yet. A large, steady share is worth investigating.',
  },
  'vantage|internet edge': 'NetFlow measured at the internet exit (above the core). Used for counting whenever it sends flows: each client once, internet traffic only.',
  'vantage|pops': 'NetFlow measured on each PoP router, next to the clients. Used for counting only when the internet exit sends nothing.',
  'hot|internet side': 'Gateway uplink and PoP-to-core links: a saturation here hits every client.',
  'hot|pop side': 'Links towards subscribers, VLANs and relays: a saturation only hits what hangs below.',
};

/** Libelles variables (une adresse, un nom) : reconnus par leur forme. */
const AIDE_MOTIFS = [
  [/^to \d{1,3}(\.\d{1,3}){3}$/, {
    t: 'Router to this public address (a well-known resolver, reachable everywhere): latency through your gateway and the transit above it.',
    r: 'Compare with “Next hop up”: if this is much higher, the delay is outside your network (transit provider, internet), not in it.',
  }],
];

/** Le libelle d'un element, sans ses badges, indices ni icones. */
function libelleAide(el, garderCasse) {
  const copie = el.cloneNode(true);
  copie.querySelectorAll('.info, .hint, .pct-hint, .badge, .u, .pl-alerts, .sq, .sel-name, button, select, input')
    .forEach((x) => x.remove());
  const texte = copie.textContent.replace(/[↓↑↕]/g, ' ').replace(/\(.*?\)/g, ' ')
    .replace(/[?·Σ]/g, ' ').replace(/:/g, ' ').replace(/\s+/g, ' ').replace(/\s*\/\s*$/, '').trim();
  return garderCasse ? texte : texte.toLowerCase();
}

const CIBLES_AIDE = [
  ['h2', 'sec'], ['h3', 'sec'], ['th', 'col'], ['.stat > .label', ''], ['.lq-kv > .k', ''],
  ['.lq-table tbody td:first-child', ''], ['.pl-kpi > span:first-child', 'kpi'],
  ['.hot-title', 'hot'], ['.heat-label', 'heat'], ['.vantage-title', 'vantage'],
];

function poserAides(racine) {
  CIBLES_AIDE.forEach(([sel, ctx]) => {
    (racine || document).querySelectorAll(sel).forEach((el) => {
      // Le texte a deja ete examine tel quel : rien a refaire. S'il a change
      // (un titre qui prend le nom du noeud choisi), on recommence.
      if (el.dataset.aide === el.textContent) return;
      const ancienne = el.querySelector(':scope > .info');
      if (ancienne) ancienne.remove();
      const brut = libelleAide(el);
      if (!brut) { el.dataset.aide = el.textContent; return; }
      // Un bloc entier peut changer le sens des mots (data-aide-ctx) : "Role"
      // d'un compte n'est pas "Role" d'un routeur.
      const zone = el.closest('[data-aide-ctx]');
      const contextes = [zone && zone.dataset.aideCtx, ctx].filter(Boolean);
      // "Health over time — NAS-FRANCOPHONIE" : la partie avant le tiret.
      const essais = [brut, brut.split(' — ')[0], brut.split(' - ')[0]];
      let entree = null;
      for (const e of essais) {
        for (const c of contextes) { entree = AIDE[c + '|' + e]; if (entree) break; }
        entree = entree || AIDE[e];
        if (entree) break;
      }
      if (!entree) {
        const motif = AIDE_MOTIFS.find(([re]) => re.test(brut));
        if (motif) entree = motif[1];
      }
      // A defaut d'entree, l'infobulle native d'un en-tete devient une vraie bulle.
      if (!entree && el.title) { entree = el.title; el.removeAttribute('title'); }
      if (!entree) { el.dataset.aide = el.textContent; return; }
      const icone = document.createElement('span');
      icone.className = 'info';
      icone.tabIndex = 0;
      icone.setAttribute('role', 'button');
      icone.setAttribute('aria-label', texteAide(entree));
      icone.dataset.titre = libelleAide(el, true).split(' — ')[0];
      bullesAide.set(icone, entree);
      icone.textContent = 'i';
      // Le titre d'une liste depliable reste a lui seul le declencheur.
      icone.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); montrerAide(icone, true); });
      el.appendChild(icone);
      el.dataset.aide = el.textContent;
    });
  });
}

/** L'entree associee a chaque icone (une chaine ou une fiche structuree). */
const bullesAide = new WeakMap();

function ficheAide(entree) {
  return typeof entree === 'string' ? { t: entree } : entree;
}

/** Version texte, pour les lecteurs d'ecran. */
function texteAide(entree) {
  const f = ficheAide(entree);
  return [f.t, f.m && 'Measured: ' + f.m, (f.s || []).map((x) => x[1]).join('; '), f.r, f.a && 'What to do: ' + f.a]
    .filter(Boolean).join(' ');
}

/** La bulle : ce que c'est, comment c'est mesure, l'echelle en couleurs,
 *  comment le lire, quoi faire. */
function rendreAide(titre, entree) {
  const f = ficheAide(entree);
  const bloc = (libelle, texte) => texte
    ? '<div class="ib-sec"><span class="ib-k">' + libelle + '</span>' + esc(texte) + '</div>' : '';
  return (titre ? '<div class="ib-title">' + esc(titre) + '</div>' : '') +
    (f.t ? '<p class="ib-what">' + esc(f.t) + '</p>' : '') +
    bloc('How it is measured', f.m) +
    (f.s ? '<ul class="ib-scale">' + f.s.map(([ton, txt]) =>
      '<li><span class="sq ' + ton + '"></span>' + esc(txt) + '</li>').join('') + '</ul>' : '') +
    bloc('How to read it', f.r) +
    bloc('What to do', f.a);
}

/* La bulle elle-meme : un seul element, pose sur le body, pour ne jamais etre
   coupe par un tableau qui defile. */
let bulleAide = null;
let bulleEpinglee = null;
function montrerAide(icone, epingler) {
  if (!bulleAide) {
    bulleAide = document.createElement('div');
    bulleAide.className = 'info-bulle';
    bulleAide.setAttribute('role', 'tooltip');
    document.body.appendChild(bulleAide);
  }
  // Un clic epingle la bulle (lecture tranquille, ou ecran tactile) ; un
  // second clic, Echap ou un clic ailleurs la ferme.
  if (epingler) {
    if (bulleEpinglee === icone) { bulleEpinglee = null; cacherAide(true); return; }
    bulleEpinglee = icone;
  } else if (bulleEpinglee && bulleEpinglee !== icone) {
    return;
  }
  bulleAide.innerHTML = rendreAide(icone.dataset.titre, bullesAide.get(icone) || '');
  bulleAide.classList.toggle('pinned', bulleEpinglee === icone);
  bulleAide.style.display = 'block';
  const r = icone.getBoundingClientRect();
  const largeur = bulleAide.offsetWidth;
  const hauteur = bulleAide.offsetHeight;
  const x = Math.max(8, Math.min(r.left + r.width / 2 - largeur / 2, window.innerWidth - largeur - 8));
  let y = r.bottom + 8;
  if (y + hauteur > window.innerHeight - 8 && r.top - hauteur - 8 >= 8) y = r.top - hauteur - 8;
  bulleAide.style.left = x + 'px';
  bulleAide.style.top = Math.max(8, Math.min(y, window.innerHeight - hauteur - 8)) + 'px';
}
function cacherAide(forcer) {
  if (!bulleAide) return;
  if (bulleEpinglee && forcer !== true) return;
  if (forcer === true) bulleEpinglee = null;
  bulleAide.style.display = 'none';
}
document.addEventListener('mouseover', (e) => {
  const i = e.target.closest && e.target.closest('.info');
  if (i) montrerAide(i);
});
document.addEventListener('mouseout', (e) => {
  if (e.target.closest && e.target.closest('.info')) cacherAide();
});
document.addEventListener('focusin', (e) => { if (e.target.classList && e.target.classList.contains('info')) montrerAide(e.target); });
document.addEventListener('focusout', (e) => { if (e.target.classList && e.target.classList.contains('info')) cacherAide(); });
document.addEventListener('click', (e) => {
  if (bulleEpinglee && !(e.target.closest && (e.target.closest('.info') || e.target.closest('.info-bulle')))) cacherAide(true);
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') cacherAide(true); });
window.addEventListener('scroll', () => cacherAide(true), true);

// Apres chaque rendu : les ecrans se redessinent sans cesse (rafraichissement
// toutes les 10 s), l'observateur pose les icones sur ce qui est nouveau.
let aidePrevue = false;
new MutationObserver(() => {
  if (aidePrevue) return;
  aidePrevue = true;
  requestAnimationFrame(() => { aidePrevue = false; poserAides(document); });
}).observe(document.body, { childList: true, subtree: true });
poserAides(document);
