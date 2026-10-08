// Documentation technique : boutons « Copy » sur les blocs de code, et
// sommaire qui suit la lecture. Aucune dependance, aucun appel reseau.
(function () {
  'use strict';

  document.querySelectorAll('pre').forEach(function (pre) {
    var bouton = document.createElement('button');
    bouton.type = 'button';
    bouton.className = 'copy';
    bouton.textContent = 'Copy';
    bouton.addEventListener('click', function () {
      var texte = pre.querySelector('code') ? pre.querySelector('code').textContent : pre.textContent;
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(texte).then(function () {
        bouton.textContent = 'Copied';
        setTimeout(function () { bouton.textContent = 'Copy'; }, 1500);
      }, function () {});
    });
    pre.appendChild(bouton);
  });

  var liens = {};
  document.querySelectorAll('.toc a[href^="#"]').forEach(function (a) {
    liens[a.getAttribute('href').slice(1)] = a;
  });
  if (!('IntersectionObserver' in window)) return;
  var obs = new IntersectionObserver(function (entrees) {
    entrees.forEach(function (e) {
      if (!e.isIntersecting || !liens[e.target.id]) return;
      Object.keys(liens).forEach(function (k) { liens[k].classList.remove('active'); });
      var lien = liens[e.target.id];
      lien.classList.add('active');
      if (lien.scrollIntoView && lien.closest('.toc')) {
        var toc = lien.closest('.toc');
        var haut = lien.offsetTop - toc.clientHeight / 2;
        toc.scrollTo({ top: Math.max(0, haut), behavior: 'smooth' });
      }
    });
  }, { rootMargin: '-10% 0px -80% 0px' });
  document.querySelectorAll('main h2[id], main h3[id]').forEach(function (h) { obs.observe(h); });
})();
