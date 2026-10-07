// Guide de l'API : adresse du serveur dans les exemples, boutons « Copy »,
// sommaire qui suit la lecture, et reference COMPLETE tiree de /openapi.json --
// generee par le serveur lui-meme, elle ne peut pas diverger du code installe.
(function () {
  'use strict';

  // DEMANDE EXPLICITE : jamais l'adresse reelle du serveur dans les exemples
  // (captures d'ecran, documentation transmise) -- un nom generique,
  // <your-freeqos-url>, que l'integrateur remplace.

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function ajouterCopie(pre) {
    var bouton = document.createElement('button');
    bouton.type = 'button';
    bouton.className = 'copy';
    bouton.textContent = 'Copy';
    bouton.addEventListener('click', function () {
      var texte = pre.querySelector('code').textContent;
      var fini = function () {
        bouton.textContent = 'Copied';
        setTimeout(function () { bouton.textContent = 'Copy'; }, 1500);
      };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(texte).then(fini, function () {});
      }
    });
    pre.appendChild(bouton);
  }
  document.querySelectorAll('pre').forEach(ajouterCopie);

  // Sommaire : la section lue est surlignee.
  var liens = {};
  document.querySelectorAll('.toc a[href^="#"]').forEach(function (a) {
    liens[a.getAttribute('href').slice(1)] = a;
  });
  if ('IntersectionObserver' in window) {
    var obs = new IntersectionObserver(function (entrees) {
      entrees.forEach(function (e) {
        if (!e.isIntersecting || !liens[e.target.id]) return;
        Object.keys(liens).forEach(function (k) { liens[k].classList.remove('active'); });
        liens[e.target.id].classList.add('active');
      });
    }, { rootMargin: '-10% 0px -80% 0px' });
    document.querySelectorAll('main section[id]').forEach(function (s) { obs.observe(s); });
  }

  // ------------------------------------------------------------ reference
  var ORDRE = ['get', 'post', 'put', 'patch', 'delete'];
  var hote = document.getElementById('ref');
  var filtre = document.getElementById('ref-search');
  var spec = null;

  function nomSchema(ref) { return ref ? ref.split('/').pop() : ''; }

  function typeDe(sc) {
    if (!sc) return '';
    if (sc.$ref) return nomSchema(sc.$ref);
    if (sc.anyOf) {
      return sc.anyOf.map(typeDe).filter(function (t) { return t && t !== 'null'; }).join(' | ');
    }
    if (sc.type === 'array') return 'array of ' + (typeDe(sc.items) || 'values');
    if (sc.enum) return sc.enum.join(' | ');
    return sc.type || '';
  }

  function tableChamps(lignes, entetes) {
    if (!lignes.length) return '';
    return '<table><thead><tr>' + entetes.map(function (h) { return '<th>' + h + '</th>'; }).join('') +
      '</tr></thead><tbody>' + lignes.join('') + '</tbody></table>';
  }

  function corps(op) {
    var rb = op.requestBody && op.requestBody.content && op.requestBody.content['application/json'];
    if (!rb || !rb.schema) return '';
    var sc = rb.schema.$ref ? spec.components.schemas[nomSchema(rb.schema.$ref)] : rb.schema;
    if (!sc || !sc.properties) return '<p><b>Body:</b> JSON</p>';
    var requis = sc.required || [];
    var lignes = Object.keys(sc.properties).map(function (nom) {
      var p = sc.properties[nom];
      var defaut = p['default'] !== undefined && p['default'] !== null
        ? ' <span class="muted">(default ' + esc(JSON.stringify(p['default'])) + ')</span>' : '';
      return '<tr><td><code>' + esc(nom) + '</code>' + (requis.indexOf(nom) >= 0 ? ' <b>*</b>' : '') +
        '</td><td>' + esc(typeDe(p)) + defaut + '</td></tr>';
    });
    return '<p><b>Body</b> (JSON, <b>*</b> required)</p>' + tableChamps(lignes, ['Field', 'Type']);
  }

  function parametres(op) {
    var lignes = (op.parameters || []).map(function (p) {
      var sc = p.schema || {};
      var defaut = sc['default'] !== undefined && sc['default'] !== null
        ? ' <span class="muted">(default ' + esc(JSON.stringify(sc['default'])) + ')</span>' : '';
      return '<tr><td><code>' + esc(p.name) + '</code>' + (p.required ? ' <b>*</b>' : '') +
        '</td><td>' + esc(p['in']) + '</td><td>' + esc(typeDe(sc)) + defaut + '</td></tr>';
    });
    return lignes.length
      ? '<p><b>Parameters</b></p>' + tableChamps(lignes, ['Name', 'In', 'Type'])
      : '';
  }

  function portee(methode, chemin) {
    if (/^\/api\/v1\/(api-keys|users|auth)/.test(chemin)) return 'interface only';
    return methode === 'get' ? 'read' : 'write';
  }

  function rendre() {
    if (!spec) return;
    var q = (filtre.value || '').trim().toLowerCase();
    var groupes = {};
    Object.keys(spec.paths).sort().forEach(function (chemin) {
      ORDRE.forEach(function (m) {
        var op = spec.paths[chemin][m];
        if (!op) return;
        var tag = (op.tags && op.tags[0]) || 'other';
        var texte = (m + ' ' + chemin + ' ' + (op.summary || '') + ' ' + tag).toLowerCase();
        if (q && texte.indexOf(q) < 0) return;
        (groupes[tag] = groupes[tag] || []).push({ m: m, chemin: chemin, op: op });
      });
    });
    // Ce qui sert a un integrateur d'abord ; les routes reservees a
    // l'interface (comptes, cles) en dernier.
    var PREMIERS = ['public api (model)', 'public api (usage)', 'pops', 'plans', 'shaping',
      'static clients', 'metrics', 'capacity', 'traffic (netflow)', 'operations'];
    var DERNIERS = ['accounts', 'api keys'];
    var rang = function (t) {
      var i = PREMIERS.indexOf(t);
      if (i >= 0) return i;
      return DERNIERS.indexOf(t) >= 0 ? 100 + DERNIERS.indexOf(t) : 50;
    };
    var tags = Object.keys(groupes).sort(function (a, b) {
      return rang(a) - rang(b) || (a < b ? -1 : a > b ? 1 : 0);
    });
    if (!tags.length) { hote.innerHTML = '<p class="muted">No endpoint matches.</p>'; return; }
    hote.innerHTML = tags.map(function (tag) {
      return '<div class="ref-group"><h3>' + esc(tag) + '</h3>' + groupes[tag].map(function (e) {
        return '<details class="op"><summary><span class="m ' + e.m + '">' + e.m.toUpperCase() +
          '</span><code>' + esc(e.chemin) + '</code><span class="sum">' + esc(e.op.summary || '') +
          '</span><span class="tag">' + esc(portee(e.m, e.chemin)) + '</span></summary>' +
          '<div class="body">' + (parametres(e.op) + corps(e.op) ||
            '<p class="muted">No parameter.</p>') + '</div></details>';
      }).join('') + '</div>';
    }).join('');
  }

  fetch('/openapi.json', { credentials: 'same-origin' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (s) { spec = s; rendre(); })
    .catch(function () {
      hote.innerHTML = '<p class="muted">The reference could not be loaded from this server.</p>';
    });
  filtre.addEventListener('input', rendre);
})();
