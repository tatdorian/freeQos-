/* freeQoS - portail de connexion.
 *
 * Volontairement ISOLE de app.js : ce petit script enveloppe window.fetch pour
 * detecter un 401 sur l'API et afficher un formulaire de connexion, puis ajoute
 * un bouton de deconnexion. Aucune dependance externe, comme le reste de
 * l'interface. Le cookie de session voyage tout seul (meme origine).
 */
'use strict';

(function () {
  const API = '/api/v1';
  const rawFetch = window.fetch.bind(window);
  window.__rawFetch = rawFetch;
  let overlayShown = false;

  function showLogin() {
    if (overlayShown) return;
    overlayShown = true;
    const wrap = document.createElement('div');
    wrap.id = 'login-overlay';
    wrap.innerHTML =
      '<form id="login-form" class="login-card">' +
      '<h1>freeQoS</h1>' +
      '<p class="login-sub">Connexion requise</p>' +
      '<label>Identifiant<input name="username" autocomplete="username" autofocus></label>' +
      '<label>Mot de passe<input name="password" type="password" ' +
      'autocomplete="current-password"></label>' +
      '<p class="login-error" id="login-error"></p>' +
      '<button type="submit">Se connecter</button>' +
      '</form>';
    document.body.appendChild(wrap);
    const form = document.getElementById('login-form');
    const err = document.getElementById('login-error');
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      err.textContent = '';
      const data = new FormData(form);
      try {
        const res = await rawFetch(API + '/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: data.get('username'),
            password: data.get('password'),
          }),
        });
        if (res.ok) {
          location.reload();
          return;
        }
        const body = await res.json().catch(function () { return null; });
        err.textContent = (body && body.detail) || 'Echec de connexion (' + res.status + ')';
      } catch (ex) {
        err.textContent = 'Connexion impossible';
      }
    });
  }

  // Enveloppe fetch : tout 401 sur l'API (hors la connexion elle-meme) rouvre
  // le portail plutot que de laisser l'interface afficher une erreur opaque.
  window.fetch = async function (input, init) {
    const res = await rawFetch(input, init);
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (
        res.status === 401 &&
        url.indexOf(API) !== -1 &&
        url.indexOf('/auth/login') === -1
      ) {
        showLogin();
      }
    } catch (ignored) {
      /* ne jamais casser la reponse d'origine */
    }
    return res;
  };

  // Bouton de deconnexion, ajoute discretement dans l'en-tete.
  document.addEventListener('DOMContentLoaded', function () {
    const header = document.querySelector('header.top');
    if (!header) return;
    const button = document.createElement('button');
    button.id = 'logout-btn';
    button.type = 'button';
    button.textContent = 'Deconnexion';
    button.addEventListener('click', async function () {
      try {
        await rawFetch(API + '/auth/logout', { method: 'POST' });
      } catch (ignored) {
        /* on recharge de toute facon */
      }
      location.reload();
    });
    header.appendChild(button);
  });
})();
