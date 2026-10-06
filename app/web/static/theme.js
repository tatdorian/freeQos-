// Le theme choisi s'applique AVANT le premier affichage : sinon la page
// s'ouvre en clair puis bascule en sombre, ce qui clignote. Fichier a part (et
// non script en ligne) : la politique de securite du contenu n'autorise que les
// scripts servis par le controleur lui-meme.
try {
  var t = localStorage.getItem('freeqos-theme');
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
} catch (e) {}
