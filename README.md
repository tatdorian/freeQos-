# freeQoS

Contrôleur QoS/QoE souverain et auto-hébergé pour WISP (MikroTik RouterOS 7 + backhaul
radio Ubiquiti). Alternative à Preseem, **hors-bande**.

> **Hors-bande, strictement.** Cette application n'est **jamais** dans le chemin des
> paquets. Elle tourne sur une VM Linux de management, lit la télémétrie des équipements
> via leurs API et — à partir de la phase 2 — poussera du shaping sur les routeurs.
> Ce n'est **pas** le modèle LibreQoS (qui est inline).

## Modèle à deux boucles

| | Boucle **centrale lente** | Boucle **locale rapide** |
|---|---|---|
| Où | Cette application (VM de management) | Sur le PoP |
| Période | 10 s et plus | sous-seconde |
| Rôle | Collecte, référentiel, plans, score QoE, **fixe les baselines** de débit | Réagit aux fades radio à la latence, **dans** les baselines |
| Statut | Implémentée ici | **Hors périmètre.** Volontairement non implémentée |

L'abstraction est prévue (les baselines sont des données de premier ordre), mais rien de
la boucle locale ne vit dans ce dépôt.

## Où cette application se place dans le réseau

Elle n'est **jamais** dans le chemin des paquets. Elle se branche **en amont du cœur**,
juste derrière la sortie internet, et **au niveau du PoP** — aux deux extrémités du
réseau, jamais au milieu.

```
   Internet
      │
      ▼
 ┌──────────┐   NetFlow (vantage 'edge')
 │ Sortie   │ ─────────────────────────────┐
 │ internet │                              │
 └──────────┘                              │
      │                                    ▼
      ▼                            ┌────────────────┐
 ┌──────────┐   rien à exporter,   │   freeQoS      │
 │  CŒUR    │   rien à interroger  │  (hors-bande)  │
 └──────────┘                      └────────────────┘
      │                                    ▲
      ▼                                    │
 ┌──────────┐   NetFlow (vantage 'pop')    │
 │   PoP    │ ─────────────────────────────┘
 └──────────┘   + API RouterOS (lecture, puis files CAKE)
      │
      ▼
   Abonnés
```

**Pourquoi ces deux points-là.** Ils voient tout ce qui compte, et rien d'autre :

- **En amont du cœur** (`vantage='edge'`) : tout ce qui vient d'internet et tout ce qui y
  va passe par là, **une seule fois**. C'est la mesure de référence de la consommation
  d'un abonné.
- **Au PoP** (`vantage='pop'`) : le même trafic, mais là où le dernier kilomètre commence
  — donc avec l'étiquette VLAN et le secteur, qui n'existent plus ailleurs.

**Pourquoi pas au milieu.** Le cœur n'exporte rien, n'est pas interrogé, et aucune sonde
ne transite par lui. **La mesure ne lui ajoute aucune charge, ni à l'aller ni au retour.**
C'est exactement ce qu'un miroir de port ne permet pas : il recopie chaque octet sur le
lien de collecte, donc sur une sortie à 10 Gbit/s, c'est 10 Gbit/s de plus à transporter,
dans les deux sens, à travers le cœur qu'on cherchait justement à épargner. Un export
NetFlow tient dans quelques dizaines de kbit/s : un datagramme UDP résume des milliers de
conversations.

**Corollaire assumé : le même octet est vu deux fois.** Un flux qui traverse le PoP puis
la sortie internet est exporté par les deux. Les additionner donnerait le double du trafic
réel. Le **point de mesure** est donc enregistré *avec* la mesure — il fait partie de la
clé primaire de `flow_metrics` — et la consommation se lit depuis **un seul**
(`NETFLOW_ACCOUNTING_VANTAGE`, `edge` par défaut).

---

## Les trois goulots à shaper

1. **Dernier km par abonné**, sur le PoP — file CAKE par session PPPoE.
2. **Backhaul radio** — débit parent = capacité **réelle** de la parabole, lue chez UISP.
3. **Egress internet** (optionnel) — sur le gateway.

En phase 1 les trois sont **observés**, aucun n'est encore piloté.

## État : phase 1 (collecteur)

| Phase | Contenu | Statut |
|---|---|---|
| **1** | Collecte `/ppp active` multi-routeurs, capacité backhaul, plans, TimescaleDB, boucle périodique, API de lecture, `/health` | **fait** |
| **1.5** | Interface d'administration, connexion d'un PoP depuis l'UI, inventaire à chaud, sonde de latence | **fait** |
| **2** | Topologie (arbre éditable), analyse de l'existant, files CAKE par abonné et parent backhaul | **fait** |
| **3** | Latence **sous charge** (bufferbloat) : corrélation RTT ↔ débit, note A+…F, **score QoE composite** | **fait** ; **exige la sonde RTT**, coupée par défaut |
| **4** | Boucle fermée : ajustement du partage d'un secteur selon la QoE + capacité radio | **fait** ; désactivée tant que `ENFORCEMENT_ENABLED` est faux |

L'enforcement existe désormais, mais reste **désactivé par défaut** : `ENFORCEMENT_ENABLED`
doit être passé à `true` explicitement, et chaque plan demande une application distincte.

**Deux interrupteurs, pas un.** Les phases 3 et 4 ont besoin de la **sonde RTT**, qui est
coupée par défaut parce qu'elle coûte du CPU aux routeurs. Sans elle, `rtt_ms` reste vide,
le bufferbloat ne peut pas se calculer (il faut corréler latence et débit), le score de QoE
n'existe pas, et la boucle fermée n'a rien à évaluer. Activez-la dans **Exécutif › Sonde
RTT** — l'interface le dit désormais explicitement plutôt que d'afficher des colonnes vides
qu'on pourrait prendre pour un réseau sain.

**Ce qui rattache un abonné à son secteur.** La phase 4 agit sur l'enveloppe d'un *secteur*,
et la file d'un abonné pend sous celle du lien qu'il traverse : les deux ont besoin du
rattachement abonné → secteur. Il se construit à la découverte, par la jointure `caller-id`
↔ station UISP (cf. [Comprendre la topologie](#comprendre-la-topologie--quel-lien-va-où)),
ou par le champ *secteur* de la fiche pour un client à IP fixe. Sans UISP, un abonné dont le
CPE n'est reconnu nulle part reste sans secteur : sa file est posée à la racine, et
`POST /api/v1/shaping/qoe/run` le nomme dans `unattached` plutôt que de se taire.

---

## Démarrage rapide (lab)

```bash
cp .env.example .env                       # adapter POSTGRES_PASSWORD au minimum
cp config/routers.example.yml config/routers.yml
export MT_POP_1_PASSWORD='...'             # jamais dans un fichier versionné

docker compose up -d --build
open http://localhost:8000/                # tableau de bord
open http://localhost:8000/docs            # API
```

Sans routeur sous la main, les providers `mock` suffisent à faire tourner toute la chaîne :
`BACKHAUL_PROVIDER=mock` et `PLAN_PROVIDER=mock` (valeurs par défaut).

### Mettre à jour, nettoyer, repartir de zéro

Quatre gestes, du plus doux au plus radical. Prenez le premier qui suffit.

```bash
make update      # récupère le code et redémarre l'app — AUCUNE donnée perdue
```

L'interface se recharge toute seule après une mise à jour : `app.js` et `app.css` sont
servis avec une **empreinte de leur contenu**, donc le navigateur ne peut pas servir
l'ancienne version. Le `Ctrl+Maj+R` d'autrefois n'est plus nécessaire — et il l'était
d'autant plus qu'un script périmé face à une API à jour produit un onglet vide, un symptôme
qui n'oriente vers rien.

Et si un onglet ne se charge pas, il le **dit** : un bandeau nomme l'erreur au lieu de
laisser un écran vide, qui se lirait comme « il n'y a rien » alors qu'il faut lire « je
n'ai pas pu savoir ».

**Nettoyer l'arbre sans rien perdre d'autre.** Le graphe n'efface jamais rien tout seul —
c'est voulu, pour qu'un équipement momentanément invisible (fade radio, redémarrage, lecture
en échec) ne disparaisse pas. Le revers : une adresse de gestion changée, un lien de test
démonté ou un voisin croisé pendant une migration y restent. *Arbre réseau ›* **Oublier les
équipements disparus** les retire, en demandant depuis combien de temps ils doivent avoir
disparu. Vos routeurs déclarés ne sont **jamais** concernés, même injoignables depuis des
jours : leur case est déclarée, pas découverte. Les liens et fusions posés à la main non plus.

```bash
# Le même geste en ligne de commande (ici : rien vu depuis 24 h)
curl -X POST 'http://localhost:8000/api/v1/topology/forget-stale?confirm=true&older_than_minutes=1440'
```

**Tout effacer.** Mesures, routeurs déclarés, antennes, topologie, réglages et clients
statiques :

```bash
make reset-db    # demande confirmation, puis recrée une base vide
```

> **La clé de chiffrement part avec.** Elle vit dans le volume `qosdata` et protège les mots
> de passe des routeurs enregistrés depuis l'interface. `make reset-db` la régénère : il
> faudra redéclarer vos routeurs. C'est sans conséquence ici puisque la base part aussi —
> mais ne supprimez **jamais** ce volume seul, sinon les fiches survivent avec des mots de
> passe devenus illisibles.

### Connecter un PoP depuis l'interface

Onglet **PoPs** → formulaire *Connecter un PoP*. **Tester la connexion** ouvre une
session API en lecture seule et renvoie l'identité du routeur, sa version RouterOS et
le nombre de sessions PPPoE — dont celles dont les compteurs sont effectivement
corrélés, qui est le seul chiffre qui garantit qu'un débit sera calculable.
**Enregistrer** ajoute le PoP à l'inventaire : il est interrogé **au cycle suivant,
sans redémarrage**.

Aucun prérequis : la clé de chiffrement est générée au premier démarrage (voir plus haut).
Si elle manque malgré tout — chemin non inscriptible, volume Docker absent — l'API
**refuse** d'enregistrer un PoP et le dit dans l'interface : elle n'écrira jamais un mot de
passe de routeur en clair dans PostgreSQL.

Pour la générer à la main : `python -m app.services.crypto`.

Les deux inventaires coexistent :

| Source | Secrets | Modifiable dans l'UI |
|---|---|---|
| `config/routers.yml` (fichier) | variables d'environnement | non — signalé « édité dans routers.yml » |
| Interface | chiffrés en base (Fernet) | oui — tester / désactiver / retirer |

En cas d'homonymie **le fichier gagne** : une déclaration versionnée et revue prime sur
une saisie au clavier.

### Interface d'administration

Cinq vues, thème sombre, à `http://localhost:8000/` :

- **Tableau de bord** — débit global (download/upload en miroir, graphe live), abonnés en
  ligne, débit vendu et son taux d'utilisation, capacité backhaul, top consommateurs avec
  barre d'usage vs plan, cartes backhaul (capacité vs nominal, charge vs capacité).
- **Exécutif** — écran *Files live* façon LibreQoS : trois panneaux (Live Queue State,
  Node Snapshot avec jauge + QoO, Node Details) pilotés par la ligne sélectionnée, puis un
  tableau des files par nœud (Circuits, Nodes, Effective, Configured, ↓/↑, RTT, QoO) avec
  cellules colorées et lignes dépliables vers les clients, plus un *heatmap* et un *Sankey*.
  Sélectionner un **client** ouvre son override éditable (Save / Clear). Le panneau d'un
  **nœud** montre la **capacité partagée** du parent (backhaul), le **vendu** (Σ plans) et
  la **sur-souscription** : c'est sous cette enveloppe que les circuits se disputent la
  bande passante (le planner pose la file parent et CAKE arbitre par circuit — *topology-aware
  shaping*). Les colonnes RETR / MARKS / DROPS et la ligne *Retransmissions TCP* sont
  marquées **« n/d »** : ces compteurs de qdisc n'existent que dans le chemin des paquets,
  hors de portée du hors-bande.
- **Arbre réseau** — un **vrai arbre éditable** au glisser-déposer : chaque équipement est
  une case qu'on déplace, qu'on dépose sur une autre pour la rattacher. On peut **corriger
  son rôle** (dont *Client*), **créer un lien** manquant (*Créer un lien* → clic parent puis
  enfant) et **retirer un lien** erroné (clic sur l'arête). Chaque lien porte son **débit
  mesuré** (couleur de charge), et chaque PoP ses abonnés. Leur case affiche le compte et le
  débit total ; **un clic la déplie** et montre chaque abonné avec son adresse et son débit,
  un second la referme. Le repli est le défaut, et la liste dépliée est bornée : un PoP
  d'opérateur porte des centaines d'abonnés, et aucun arbre ne se lit avec des centaines de
  cases — le détail complet vit dans l'onglet *Abonnés*, qui est fait pour ça.
  **Les abonnés de l'arbre viennent de la liste des abonnés, et d'elle seule.** Les deux
  natures y sont traitées pareil : un abonné PPPoE porte le badge *CPE*, un client à IP fixe
  le badge *FIXE*. Ni les adresses repérées en ARP (elles restent des *candidats* à examiner
  dans l'onglet des clients à IP fixe, cf. [Clients à IP fixe](#clients-à-ip-fixe-non-pppoe)),
  ni une seconde case pour un client déjà compté : une seule source, donc une seule case.
  Les liens **routeur↔routeur** sont découverts de plusieurs façons complémentaires :
  `/ip/neighbor` (MNDP/LLDP/CDP), **et surtout la configuration complète** lue par API. La
  découverte analyse chaque `/export` : deux PoP portant chacun une adresse sur le **même /30**
  (point-à-point) sont directement reliés, et un **tunnel** (EoIP/GRE/IPIP/VPLS) dont le
  `remote-address` appartient à un autre PoP relie les deux — même quand MNDP ne voit rien
  (lien routé, overlay, switch muet). Ces liens déduits de la config sont fiables et ne
  doublent jamais un lien déjà trouvé. Le bouton **Config** (onglet Équipements) montre le
  `/export` brut d'un routeur et ce que le contrôleur en tire (adresses, tunnels, commentaires).
  **Les routeurs sont uniquement ceux ajoutés par API.** Quand un PoP en voit un autre en
  voisin, on ne crée pas une seconde case : le routeur géré est reconnu par son IP, sa MAC ou
  son identité, et le lien pointe vers sa case API. Ce qui ne correspond à aucun routeur géré
  reste une feuille — ce sont les **clients** (PPPoE, VLAN), qui ne sont pas sous API.
  L'ossature de l'arbre est bâtie en deux temps pour rester juste sans perdre de nœud :
  d'abord les **adjacences sûres** (lien point-à-point — un seul voisin sur le port —, lien
  UISP/radio déclaré, ou lien posé à la main) ; puis, pour un nœud encore sans parent, son
  **meilleur lien probable** vu sur un segment partagé (switch, VLAN de gestion, où MNDP/LLDP
  montre tout le monde), tracé en **pointillé** et marqué *incertain* — jamais un maillage
  (un nœud n'a qu'un parent), et jamais un routeur détaché à tort. Le panneau signale un
  rattachement incertain et offre de le **confirmer** ou de le corriger d'un clic ; un forçage
  manuel et les liens manuels priment toujours.
  L'option *Liens à débit seulement* ne garde que les liens réellement mesurés. Les doublons
  sont **réconciliés** : un même routeur vu plusieurs fois (PoP géré *et* voisin du cœur,
  casses ou IPv4/IPv6 différentes, ou joignable sous plusieurs adresses de gestion) devient
  une seule case, ses adresses rassemblées. La réconciliation reconnaît un routeur géré par
  son **numéro de série** (`/system/routerboard`, l'identifiant qui ne bouge jamais), son
  **identité RouterOS** et *toutes* ses **MAC d'interface** — c'est ce qui empêche qu'il se
  dédouble, que ce soit sous une autre IP ou vu en voisin par un autre PoP (le voisinage ne
  révèle que la MAC de l'interface en face). **Un équipement est reconnu par son numéro de
  série** (`/system/routerboard`, ou le `system-id` de `/system/license` pour une CHR, qui
  n'a pas de RouterBOARD) : c'est le seul identifiant qui ne dépende ni du nom, ni de
  l'adresse, ni du matériel par lequel on le regarde. **Deux routeurs déclarés ne fusionnent
  jamais entre eux**, et une **MAC revendiquée par plusieurs** d'entre eux cesse de les
  identifier — des machines virtuelles déployées depuis la même image partagent les MAC de
  leurs interfaces, et s'y fier repliait des routeurs bien distincts en une seule case, qui
  absorbait leurs liens pendant que les autres disparaissaient de l'arbre. Le nom, lui, ne
  fusionne plus rien : il n'est unique que par convention, et une convention ne se vérifie
  pas. Un PoP **injoignable** reste affiché (badge
  *injoignable*) plutôt que de disparaître silencieusement. Quand l'automatique ne peut pas
  *prouver* l'identité (nom générique « MikroTik », pas de MAC commune), l'opérateur tranche
  à la main : *Même équipement que…* replie une case sur une autre, *Séparer* défait la
  fusion. L'arbre **signale aussi les doublons probables** — deux cases aux mêmes mots-clés
  dans un ordre différent (« CCR DS » / « DS-CCR ») — avec un bouton *Fusionner* en un clic ;
  il ne les fusionne pas d'office, car deux bouts d'un même lien peuvent être deux vrais
  routeurs. Rôles, position, rattachements, liens et fusions manuels sont enregistrés, mais ne
  changent que l'arbre **affiché** — aucun équipement n'est reconfiguré. Une case qui regroupe
  plusieurs observations **dit lesquelles** : le compte seul ne permet pas de juger, puisque
  « 4 vues » est parfaitement normal pour un équipement vu par quatre ports et parfaitement
  faux pour quatre équipements confondus. La liste des observations repliées laisse trancher,
  et un loopback distinct déclaré à chacun défait une fusion abusive à la racine.
- **Abonnés** — **tout l'effectif** de chaque PoP, filtrable par PoP, par nature et par
  login : débit vs plan, latence, **note de bufferbloat** (latence sous charge), boost en
  cours et son décompte. Les abonnés **déclarés mais jamais mesurés** — jamais connectés,
  ou PoP qui n'est plus collecté — y figurent aussi, marqués comme tels et avec des
  **trous plutôt que des zéros** : un zéro se lirait comme une absence de trafic, alors
  qu'il s'agit d'une absence d'information, et un abonné facturé qui n'apparaît nulle part
  est indiscernable d'un abonné qui n'existe pas. Un clic ouvre la série de l'abonné ; les
  boutons *Débit* et *Boost* agissent directement.
- **Topologie** — le tableau technique des liens : **débit mesuré**, charge vs capacité du
  port, capacité négociée et débit imposé. Le bouton *Débit* ouvre l'historique d'un lien
  et permet une mesure instantanée ; *Bande passante* enregistre une intention de shaping.
  L'onglet **nomme les routeurs interrogés** et signale ceux qui ne produisent rien, avec
  leur erreur : seul un routeur *lu par API* apporte des liens, des abonnés et des files.
  Comme un câble entre deux routeurs interrogés ne compte qu'**une** ligne — portée par
  l'un des deux bouts — le badge *interrogé* de la colonne *Vers* signale l'autre bout.
  Sans lui, un réseau **en étoile** faisait disparaître tous les PoPs derrière le cœur :
  chacun n'a qu'un câble, celui qui monte, donc chacun se retrouvait du côté replié.
- **Équipements** — **santé des routeurs** lue en direct (charge CPU, mémoire, uptime,
  version) : un routeur à 95 % de CPU n'appliquera pas les files qu'on lui envoie, et un
  routeur qui vient de redémarrer a perdu les siennes — deux causes de « mon abonné n'est
  pas bridé » qui n'ont rien à voir avec le contrôleur. Puis l'ajout d'un routeur ou d'une
  antenne **via leur API** ; chaque ajout **analyse la configuration et (re)construit
  l'arbre tout seul**. Inventaire des sites et routeurs en bas de page.
- **API** — la surface d'intégration : créer une clé, la liste des points d'entrée
  (`PUT /model/v1/services/{id}` et le reste du contrat Preseem) et un appel prêt à
  copier. C'est ce qu'un système tiers vient chercher pour **pousser ses données dans
  cette application** ; l'enterrer dans les Réglages obligeait à savoir où regarder.
- **Services** — **qui se connecte à quoi**. Les connexions clients *en cours* (lues dans
  la mémoire du collecteur : la seule vue réellement en direct), les services d'où vient
  le trafic — Netflix, YouTube, Twitch, les CDN — et la fiche complète d'une adresse
  atteinte : nom inverse, service **et à quel titre**, organisation, AS, pays, et la liste
  nominative des abonnés qui la joignent. C'est aussi d'ici que se posent les
  **restrictions de trafic**, parce que décider de brider un trafic se fait en le
  regardant. Cf. [Qui se connecte à quoi](#qui-se-connecte-à-quoi-ipfinder).

> L'onglet **Capacité** a été retiré de la navigation : la lecture qu'il servait reste
> disponible sur `GET /api/v1/capacity`, pour un système tiers qui la consommait. Retirer
> un onglet n'est pas supprimer une capacité — c'est retirer la place qu'il prenait dans
> la navigation de tous les jours.

**L'interface ne fait pas la leçon.** Les paragraphes d'explication et les blurbs sous
les champs ont été retirés : ce qui reste nomme un fait — un état, une erreur, une
valeur — et s'arrête là. Le *pourquoi* vit dans ce README et dans `/docs`, pas au
milieu de l'écran où l'on travaille.

Aucune dépendance externe : ni framework, ni CDN, ni chaîne de build. Les graphes sont du
SVG généré à la main, pour que le contrôleur reste utilisable sur une VM de management
coupée d'internet.

### Comprendre la topologie : quel lien va où

C'est la question qui conditionne tout le reste — sans elle, impossible de savoir quel
backhaul un abonné traverse, donc quelle file doit être son parent.

#### La topologie se découvre toute seule

L'onglet **Arbre réseau** lit le graphe en base — l'arbre éditable, et le tableau
des liens replié juste dessous. Ce graphe
n'est écrit que par `discover()` — et il est désormais appelé par un **job
périodique** (`discover_topology`, cadence `topology_refresh_interval_s`), pas
seulement par le bouton « Relancer la découverte ».

Le planificateur exécute chaque job une première fois **immédiatement** : sur une
installation neuve, l'arbre est peuplé dès le démarrage, sans geste de
l'exploitant.

Le job et le bouton empruntent **la même fonction** (`discover_with_devices`) :
un arbre qui différerait selon qu'il a été construit par le planificateur ou par
un clic serait impossible à diagnostiquer.

`/topology` renvoie aussi les **remarques de la dernière analyse** et sa date,
quelle que soit son origine (job ou bouton) : un arbre de cases isolées sans
explication n'aide personne, alors que « aucun loopback trouvé, déclarez-le »
est actionnable. La date distingue par ailleurs les **deux causes opposées** d'un
arbre vide — rien à découvrir, ou rien n'a encore été découvert.

> Toute cadence déclarée dans le registre des réglages doit piloter un job qui
> existe. Un réglage orphelin s'affiche, se modifie, et ne change rien — c'est
> arrivé deux fois. Un test monte le conteneur réel et le vérifie pour les onze.

#### Un PoP connu n'est jamais simplement absent

`registry.collectors` alimente **tout** : la découverte de topologie, la collecte
d'abonnés, la détection ARP des clients à IP fixe. Un routeur qui n'y entre pas
disparaît donc de partout à la fois — et le repli « injoignable » de la
découverte ne peut pas le sauver, puisqu'il est *dans* la boucle sur les
collecteurs.

Trois raisons peuvent l'en écarter, et toutes sont désormais **visibles depuis
l'interface**, pas seulement dans le journal du serveur :

| Situation | Ce que montre l'onglet Équipements |
|---|---|
| secret indéchiffrable (clé changée) | badge **écarté** + avertissement nommant le motif |
| fiche invalide (rôle inconnu, loopback mal saisi) | idem, et **les autres routeurs survivent** |
| inventaire en base illisible | avertissement global : « les routeurs déclarés en base sont absents de la collecte » |
| masqué à la main | listé dans « retiré de l'inventaire », avec **Restaurer** |
| présent mais non collecté, quelle qu'en soit la cause | badge **hors collecte** |

Le routeur garde par ailleurs **sa case dans l'arbre**, marquée « écarté » : rien
ne distingue visuellement un PoP effacé d'un PoP qui n'a jamais existé, alors
qu'un PoP écarté est une situation à corriger.

Un routeur masqué à la main, lui, ne réapparaît pas — « Retirer » doit retirer.

#### L'arbre vient de la configuration, pas d'une heuristique

`/ip/neighbor` répond à une question faible : *qui se voit ?* — ce qui est vrai
aussi de deux équipements branchés sur le même switch. L'arbre devait donc être
**déduit** : racine choisie au rang, parents calculés au plus court chemin. Ces
heuristiques tombent souvent juste, mais elles ne *savent* rien.

La configuration répond aux questions fortes, parce que c'est elle qui fait le
réseau :

| Lecture | Ce qu'elle établit |
|---|---|
| `/ip/route` | **qui est au-dessus**. La route par défaut dit où part ce que le routeur ne sait pas router : c'est la relation hiérarchique elle-même |
| `/routing/ospf/neighbor` · `/routing/bgp/session` | une adjacence **prouvée** — deux routeurs qui échangent des routes, pas deux qui se voient |
| `/interface/vlan` · `/interface/bridge/port` · `/interface/bonding` | par quel **port physique** sort un trafic donné, donc à quel lien rattacher un client |

**Le cas où l'arbre deviné se trompe.** Deux PoPs reliés au cœur *et* entre eux
(anneau) : les deux chemins ont la même longueur, rien dans le graphe ne dit
lequel est le bon, et le calcul finit par pendre un PoP sous son frère. Les
tables de routage, elles, sont formelles — les deux sortent par le cœur. C'est
verrouillé des deux côtés : `test_un_anneau_ne_pend_pas_un_pop_sous_son_frere`
côté Python, `test_le_parent_de_la_config_bat_le_plus_court_chemin` côté
interface, qui exécute la vraie fonction d'arbre avec Node.

**L'ordre de priorité** des rattachements, du plus fort au plus faible :

1. le parent **posé à la main** dans l'éditeur d'arbre — l'opérateur garde le
   dernier mot, ici comme partout ;
2. le parent **prouvé par la table de routage** (`config_parent`) ;
3. le **plus court chemin** dans le graphe, qui ne sert plus que là où la
   configuration ne dit rien : équipements non gérés, voisins découverts.

L'arbre marque chaque case — « à la main », « route » ou « déduit » — pour que
vous sachiez ce qui est établi et ce qui est supposé.

Un routeur **multi-homé** (deux routes par défaut à égalité) n'a pas *un*
parent : le contrôleur refuse d'en désigner un, le dit, et laisse le calcul
faire. Un routeur dont `/ip/route` est illisible retombe simplement sur l'arbre
déduit, sans faire échouer la découverte.

**Les clients suivent leur VLAN.** Un client à IP fixe sans secteur déclaré
pendait à la racine et échappait au partage du lien qu'il sature pourtant.
L'empilement `vlan120 → bridge-accès → ether3` résolu depuis la configuration
le rattache au lien réellement emprunté.

#### L'identité d'un routeur est son loopback

Un routeur géré est identifié par son adresse de **loopback**, et par elle seule
quand elle est connue. C'est le seul identifiant qui tienne dans un réseau réel :

| Candidat | Pourquoi il ne suffit pas |
|---|---|
| le nom | change, et n'est unique que par convention |
| la MAC | dépend du port par lequel on regarde l'équipement, et suit le matériel |
| une adresse d'interface | **un `/30` de liaison appartient aux deux bouts** — et les configurations modèles donnent souvent le même `/30` à tous les sites |
| le loopback | unique par construction, indépendant de toute interface |

Le dernier point n'est pas théorique. Avec des configurations modèles, deux PoPs
portent le même `10.0.0.1/30` vers leur accès. Sans loopback, les deux liens du
cœur aboutissaient **sur le même PoP** et l'autre restait orphelin : un arbre qui
montre un réseau qui n'existe pas. C'est verrouillé par
`test_deux_sites_au_meme_30_ne_se_confondent_plus`.

Le loopback se déclare dans la fiche du PoP. Laissé vide, il est **cherché dans la
configuration**, dans cet ordre :

| Source | Ce qu'on lit |
|---|---|
| interface de loopback | adresse d'hôte sur `lo`, `lo0`, `loopback*`, `dummy0`, `bridge-loopback`, `lo-bridge`… |
| `router-id` | `/routing/id`, instances OSPF et BGP — **lus par l'API structurée**, puis à défaut dans le texte de `/export` |
| commentaire d'adresse | un `/32` posé sur un `bridge1` quelconque mais annoté « loopback » ou « router-id » |
| `/32` isolé | faute de mieux, et l'arbre le dit |

Le `router-id` mérite son rang : dans un réseau d'opérateur, il *est* le loopback.
Il n'était lu que dans le texte de `/export`, dont l'API RouterOS refuse
l'exécution selon la version — cette source disparaissait alors sans bruit. Elle
passe désormais par les chemins structurés, qui répondent toujours.

L'arbre affiche toujours **d'où vient** le loopback, et marque « déduit » ce qui
n'a pas été déclaré.

L'unicité est vérifiée, pas supposée : la base refuse deux routeurs au même
loopback, et si la découverte en trouve deux malgré tout, l'adresse est écartée
de l'index avec un avertissement — les fusionner silencieusement donnerait un
arbre faux plutôt qu'incomplet.

#### La nature d'un routeur est son rôle déclaré

`gateway` / `core` / `pop`, saisis dans l'inventaire, donnent leur hiérarchie aux
nœuds (la passerelle en haut, puis le cœur, puis les PoPs). Auparavant tout
routeur géré était posé en « PoP » : l'arbre s'aplatissait et sa racine devenait
arbitraire.

#### L'arbre suit l'inventaire tout seul

Ajouter, retirer, désactiver un routeur ou changer son rôle **relance la
découverte dans la minute**, sans redémarrage et sans qu'il faille cliquer
« Relancer la découverte ». Le déclencheur est côté serveur : il compare
l'inventaire courant à celui de la dernière découverte, quel que soit le chemin
par lequel l'inventaire a changé — l'interface, un appel direct à l'API, ou une
modification du fichier YAML.

Un routeur **retiré** de l'inventaire sort aussi de l'arbre. Seule sa case de
routeur géré est effacée : si l'équipement existe encore physiquement et qu'un
voisin le voit toujours, il réapparaît comme équipement **non géré**, ce qu'il est
devenu. Un inventaire vide, lui, n'efface rien — ce serait le comportement d'une
base momentanément illisible, pas d'une suppression.

**Neuf sources, réconciliées** :

| Source | Ce qu'elle apporte |
|---|---|
| `/ip/neighbor` | **source maîtresse** : MNDP, LLDP et CDP. Pour chaque interface locale, l'équipement d'en face (identité, plateforme, MAC, IP) |
| `/interface/ethernet` | débit négocié = plafond physique du lien |
| `/ip/address` | segment L3 auquel appartient le lien |
| UISP `/devices` | liens radio PtP/PtMP, capacité du moment, rattachement station → AP |
| `/ppp/active` → `caller-id` | **la jointure clé** : la MAC du CPE de l'abonné |
| `/interface` `rx-byte`/`tx-byte` | **le débit réellement mesuré** sur le port qui porte le lien |
| `/ip/address` (`lo`) · `router-id` | **le loopback** : l'identité unique du routeur, quand elle n'est pas déclarée |
| `/ip/route` · OSPF · BGP | **la hiérarchie réelle** : qui est au-dessus, et quelles adjacences sont prouvées |
| `/ip/arp` · baux DHCP · table de ponts · routes · files (recensement) | présence d'une adresse **non identifiée** : confirme un client déclaré, ou propose un candidat. Les seules sources qui ne disent pas *qui* est en face |

Ce dernier point mérite d'être souligné. Le champ `caller-id` de `/ppp/active` contient la
**MAC du CPE**. UISP sait sur quel secteur radio chaque CPE est accroché, et connaît sa
MAC. **Le rapprochement `caller-id` ↔ MAC de station UISP est le seul moyen de savoir par
quelle antenne passe un abonné.** Sans lui, on sait qu'il est sur un PoP, pas quelle est sa
vraie chaîne de goulots.

La réconciliation se fait sur la MAC normalisée : RouterOS écrit `AA:BB:CC:DD:EE:FF`, UISP
parfois `aa-bb-cc-dd-ee-ff`. Sans normalisation, la jointure échoue en silence.

Le graphe obtenu — `Gateway → Cœur → PoP → Backhaul → Secteur → Abonné` — **est** l'arbre
de shaping : le parent d'une file abonné est le lien qu'il traverse.

Quand le rattachement est inconnu, la file abonné est créée **sans parent** plutôt qu'avec
un parent deviné : le dernier km est correctement shapé, la contention backhaul ne l'est
pas, et le plan le dit explicitement (`unparented_subscribers`). Rattacher un abonné au
mauvais backhaul serait pire que de ne rien faire.

La classification automatique des rôles est une heuristique (d'après la plateforme
annoncée) : elle est corrigeable d'un menu déroulant dans l'interface, et la correction
prime sur la détection. Elle ne s'autorise que ce que la plateforme **dit vraiment** :
une marque de radio (Ubiquiti, Cambium, un PowerBeam) désigne une radio, rien d'autre
ne s'appelle ainsi. « MikroTik » ne désigne rien — c'est aussi bien un PoP qu'un CPE
d'abonné ou un switch de local technique — et en déduire un PoP peuplait l'arbre de
sites qui n'existent pas. Un équipement découvert mais non déclaré est donc *inconnu*,
et il pend en feuille sous le routeur qui le voit.

#### Le CPE d'un abonné n'est pas un équipement de plus

Le routeur d'un abonné arrive par deux chemins : une session `/ppp/active` — c'est
l'abonné, avec son login et ses files — et un voisin `/ip/neighbor` au bout du port du
PoP — c'est un équipement découvert. Rien ne disait que c'était le **même boîtier** :
l'arbre affichait les deux, et l'exploitant y comptait plus de clients qu'il n'en a.

La jointure est une **égalité**, jamais une ressemblance :

| Nature de l'abonné | Ce qui prouve que c'est le même boîtier |
|---|---|
| PPPoE | `caller-id` de la session = MAC annoncée par le voisin. Quand le voisin n'annonce qu'une adresse de lien-local `fe80::`, sa MAC s'y **relit** : l'identifiant d'interface en est dérivé (EUI-64, RFC 4291) |
| VLAN routée | pas de session, donc pas de `caller-id` : l'adresse déclarée du client = adresse annoncée par le voisin |

Un routeur de l'inventaire n'est **jamais** reclassé en CPE, même s'il ouvre lui-même
une session PPPoE vers son transit : il disparaîtrait de l'arbre avec tout ce qui pend
dessous. La case du CPE reste dans le graphe — son lien porte le rattachement de
l'abonné — elle n'est simplement pas servie en double : l'arbre lit les abonnés dans la
liste des abonnés, comme pour les clients à IP fixe.

### Voir le débit d'un lien

Savoir *quel lien va où* ne dit pas *combien y passe*. Le débit d'un lien vient des
compteurs du **port** qui le porte : `/interface` expose `rx-byte` / `tx-byte`, deux
lectures successives donnent des bits/s. Même dérivation que pour les abonnés — mêmes
garde-fous (compteur qui recule après un redémarrage, débit invraisemblable rejeté), et
une valeur nulle plutôt qu'un chiffre faux.

**RouterOS compte par interface, pas par adjacence.** C'est la nuance qui décide de tout le
reste. Quand un switch se trouve entre le routeur et plusieurs équipements, `/ip/neighbor`
voit plusieurs voisins sur le même port : attribuer le compteur à chacun tripleraient le
total. Les mesures sont donc stockées par `(routeur, interface)`, et un lien hérite du
débit de son port — avec `interface_links` qui dit combien d'adjacences le partagent.
L'interface affiche alors un badge *partagé* plutôt que de faire passer un débit de port
pour un débit de lien. Une adjacence déclarée par UISP, sans port local, l'annonce aussi :
elle n'a pas de compteur.

**Le sens.** `rx` et `tx` restent ceux du routeur : `→` ce qu'il émet vers l'équipement
d'en face, `←` ce qu'il en reçoit. Selon que le voisin soit en amont (passerelle) ou en
aval (secteur), le même `tx` est du montant ou du descendant. Le tableau des liens ne
devine rien et montre les deux sens. L'arbre réseau, lui, applique au débit la même
orientation qu'aux nœuds : `↓` y veut dire « vers l'enfant », donc descendant.

**Deux échelles de temps.** L'historique vient de la collecte (`LINK_INTERVAL_S`, 10 s par
défaut). Pour la question « combien passe *maintenant* », le bouton *Mesurer maintenant*
interroge `/interface/monitor-traffic` — une commande de **lecture**, qui ne modifie aucune
configuration et rend la mesure que le routeur tient déjà. Si elle échoue (version, droits,
port virtuel), la dernière valeur collectée est renvoyée avec la raison, plutôt qu'une
erreur : l'exploitant voulait un chiffre.

### Piloter les files

**Le principe.** Preseem et LibreQoS reposent sur la même idée : pour qu'une gestion de
file serve à quelque chose, il faut que **le goulot soit chez nous**. On shape donc
légèrement **sous** la capacité réelle du lien (`SHAPING_SAFETY_FACTOR`, 90 % par défaut),
pour que la file se forme dans CAKE — où on la contrôle — plutôt que dans le buffer de la
radio, où on ne peut rien.

**Autoriser l'écriture.** L'interrupteur de *Réglages › Shaping et écriture sur les
routeurs* bascule
`ENFORCEMENT_ENABLED` **sans redémarrage** : la variable d'environnement ne sert plus
qu'à l'amorçage, ensuite c'est la base qui fait foi et la bascule survit au redémarrage.
Activer demande une confirmation et un motif, tracé dans le journal ; couper est immédiat
et sans cérémonie. `ENFORCEMENT_LOCKED=true` interdit la bascule depuis l'interface, pour
qui préfère garder la friction du redémarrage.

**Les commandes partent seules.** La boucle de réconciliation
(`SHAPING_RECONCILE_INTERVAL_S`, 120 s par défaut) relit l'état désiré, le compare aux
routeurs et écrit l'écart — sans que personne n'ait rien à cliquer. C'est elle qui fait
qu'un débit saisi dans l'interface **plafonne vraiment**, et qu'une reconnexion PPPoE ne
laisse pas une file posée sur l'adresse d'hier. Déclarer un client à IP fixe applique en
plus **immédiatement**, sans attendre son passage.

**Ce panneau montre donc la CARTE, pas les commandes.** La question de l'exploitant
n'est pas « quelles commandes as-tu envoyées » — c'est **où ça bride, et à combien**. La
page rend l'arbre que RouterOS applique réellement : chaque lien parent porte les abonnés
qui passent par lui, avec son plafond et **d'où vient ce plafond** (capacité mesurée,
surcharge saisie, plan souscrit, boost, resserrage QoE).

| État d'un point | Ce qu'il veut dire |
|---|---|
| **bridé** | la file est en place sur le routeur, conforme à ce qui est prévu |
| **à poser** | elle sera écrite au prochain passage de la boucle (ou dès l'activation de l'enforcement) |
| **pas de file** | le planificateur l'a écartée, avec son motif : aucun débit à appliquer, adresse revendiquée deux fois, lien désactivé à la main… |
| **conflit** | une file tierce occupe déjà cette cible ; RouterOS n'appliquerait que la première |
| **file manuelle** | une file posée par l'exploitant, sans `freeqos:managed` : montrée parce qu'elle bride, **jamais** modifiée |

Les points **sans file y figurent au même titre que les autres**. Une carte qui ne
montrerait que ce qui marche laisserait chercher le reste dans le journal des commandes,
c'est-à-dire nulle part. `GET /api/v1/shaping/points` rend cet arbre, en lecture seule —
même enforcement actif, cette page regarde, la boucle écrit.

**Le détail technique reste accessible**, replié sous la carte : analyse brute de
l'existant, plan calculé à la demande, et le journal des commandes envoyées. Toute
commande doit rester vérifiable ; ce n'est simplement pas la question de tous les jours.

**Le parcours manuel, quand on veut voir avant d'écrire :**

```
GET  /api/v1/shaping/points   OÙ le réseau est bridé, et à combien
GET  /api/v1/shaping/state    ce qui est DÉJÀ configuré sur le routeur
POST /api/v1/shaping/plan     ce qu'il faudrait changer, commandes exactes
POST /api/v1/shaping/apply    exécution — dry_run:true par défaut
```

Le plan affiche chaque commande RouterOS telle qu'elle sera envoyée, avec sa
raison et ce qui change :

```
/queue/type/add name=freeqos-cake-down kind=cake cake-overhead=22 cake-rtt=50ms
/queue/simple/add name=freeqos-parent-BH-Nord target=ether2 max-limit=300000000/300000000 …
/queue/simple/add name=freeqos-dupont target=10.20.0.12/32 max-limit=20000000/100000000 …
```

**Sur quoi la file d'un abonné est accrochée : son adresse.** Le détail décide de tout le
reste, et le mauvais choix échoue en silence.

`target=<pppoe-dupont>` semble le plus direct — c'est bien l'interface de l'abonné. Trois
choses le rendent inutilisable :

1. l'interface dynamique est **recréée à chaque reconnexion** ; la file reste accrochée à
   un objet disparu et devient inactive, sans rien signaler ;
2. RouterOS **inverse alors le sens** des deux limites — il raisonne du point de vue de
   l'interface — donc un plan 100 down / 20 up est appliqué à l'envers ;
3. les chevrons du nom dynamique ne passent pas l'API sur RouterOS 7.

`target=10.20.0.12/32` n'a aucun de ces défauts : le sens est celui du client
(`max-limit=montant/descendant`, le montant étant ce qui **vient** de la cible), et
l'adresse est ce que le routeur relit. Elle est écrite sous sa forme **canonique** avec son
préfixe : RouterOS réécrit toujours `10.20.0.12` en `10.20.0.12/32`, et envoyer l'adresse
nue produirait un écart à chaque cycle, donc un `set` perpétuel.

**L'adresse est relue sur le routeur au moment du plan**, jamais prise en base. Une adresse
stockée peut avoir un cycle de retard ; si l'abonné s'est reconnecté entre-temps, le pool a
pu réattribuer son IP à un voisin, et la file briderait le mauvais client. `/ppp/active`
est la seule source qui dise ce qui est vrai à l'instant où l'on écrit.

Il en découle trois comportements, tous visibles dans le plan :

| Situation | Ce qui se passe |
|---|---|
| Abonné **hors ligne** | aucune file. Écrire sur sa dernière adresse connue briderait celui qui l'a récupérée |
| Abonné **reconnecté** sur une autre IP | `set target=…` sur la file existante. Le nom de file ne dépend pas de l'adresse, donc pas de suppression/recréation |
| **Deux abonnés** sur la même adresse | aucune des deux files. L'un des deux est périmé, on ne sait pas lequel, et RouterOS n'appliquerait que la première — en silence |

Chaque abonné écarté est listé avec son motif : un abonné absent du plan sans explication
est indiscernable d'un abonné correctement shapé.

`SUBSCRIBER_QUEUE_TARGET=interface` rétablit l'ancien comportement pour un parc qui en
dépend déjà. Ce n'est pas conseillé, pour les trois raisons ci-dessus.

**Les écritures automatiques ne suppriment jamais.** L'expiration d'un boost et
l'application immédiate d'une bride écrivent sans relecture humaine : elles posent des
files, jamais n'en retirent. Sans cette règle, une coupure momentanée de `/ppp/active`
ferait passer tout le monde pour hors ligne et effacerait les files de tout un PoP. Seul un
plan relu dans l'interface peut supprimer.

**Pour changer une bande passante** : cliquer *Bande passante* sur un lien (onglet
Topologie) ou *Débit* sur un abonné. Enregistrer **n'écrit rien sur le routeur** — cela
enregistre l'intention. Le plan montre ensuite ce qui en découle.

**Unités.** Chaque champ de débit a son sélecteur **kbps / Mbps / Gbps** : un abonné bridé
à 512 kbps ou un lien de secours ne se saisissent pas en `0.512 Mbps`. L'affichage suit la
même logique — un plan à 512 kbps s'écrit « 512 kbps », pas « 0.5 Mbps ». En interne tout
est ramené au **Mbps, unité unique** : mélanger les unités en base serait une fabrique à
bugs, la conversion se fait donc une seule fois, à l'entrée.

L'API accepte les trois : `max_down_mbps`, `max_down_kbps` ou `max_down_gbps` (idem en
upload, et `down_*` / `up_*` pour un boost). Deux unités pour le même sens sont refusées —
ambigu, mieux vaut ne pas deviner. Les deux sens peuvent en revanche utiliser des unités
différentes.

```bash
# Brider un abonné à 512/128 kbps
curl -X PUT localhost:8000/api/v1/shaping/policies -H 'Content-Type: application/json' \
  -d '{"scope":"subscriber","target_key":"dupont","max_down_kbps":512,"max_up_kbps":128}'
# → /queue/simple/… max-limit=128000/512000, poussé tout de suite sur le routeur.
#   La réponse porte "enforcement" : ce qui a réellement été écrit, ou ce qui l'a empêché.
```

**Le plafond part au moment de la saisie.** Il ne suffisait pas de l'enregistrer : tant
qu'il fallait un second geste — demander un plan, puis l'appliquer — ou attendre la
réconciliation, l'interface affichait « 100 kbps imposé » sur un abonné qui passait dix
fois plus. Elle présentait une **intention** comme un **fait**. Fixer un débit écrit
maintenant la file correspondante (et elle seule : plan complet du routeur, puis
restriction à la chaîne de cette file), et la réponse dit ce qui a été écrit, sur quel
routeur, ou ce qui l'en a empêché.

### Un VLAN qui porte des clients est un site

Chez un opérateur radio, un VLAN ne découpe pas un réseau au hasard : il porte un village,
un relais, une zone. Le routeur n'en est que la tête. Tant que le contrôleur ne connaissait
que le site du **routeur**, tous les clients de tous les VLAN d'un même NAS tombaient dans
un seul sac : impossible de filtrer « les abonnés de Francophonie », de dire lequel des
sites sature, ou de lire l'arbre. Le découpage existait sur le terrain et dans la
configuration ; il manquait seulement dans le référentiel.

**Ce qui devient un site.** Uniquement un VLAN sur lequel au moins un client est déclaré ou
vu. Un VLAN de gestion, de transit ou de supervision ne porte pas d'abonné : en faire un
site remplirait la liste des PoP de lignes vides.

**Le nom vient de l'interface**, c'est-à-dire de ce que vous avez vous-même écrit sur le
routeur — donc de ce que vous reconnaîtrez :

| Interface | Site |
| --- | --- |
| `vlan-francophonie` | Francophonie |
| `vlan-zone-nord` | Zone Nord |
| `vlan101`, `ether1.101` | VLAN 101 |

Une fiche de client ne porte qu'un numéro de VLAN ; c'est l'observation ARP qui connaît le
nom de l'interface. Les deux sont rapprochés, pour qu'un même VLAN ne donne pas deux sites.

**Le piège, nommé et verrouillé.** Ranger un abonné dans un site de VLAN change son
`pop_name`, or le rapprochement abonné → routeur se faisait par **égalité** de ce nom avec
le PoP du routeur. Tel quel, reconnaître les VLAN aurait fait sortir ces abonnés de l'état
désiré : plus de file, aucune erreur, et une interface qui continue d'afficher leur
plafond. Le référentiel porte donc, pour chaque site, le **routeur qui le dessert**
(`pops.router_name`), et c'est par lui que le shaping retrouve ses abonnés. Un site sans
routeur serait un site dont les abonnés ne sont jamais bridés.

### Les étiquettes des clients se déplacent dans l'arbre

Les cases d'abonnés étaient les seules de l'arbre qu'on ne pouvait pas bouger : elles sont
calculées à l'affichage depuis la liste des abonnés, donc aucune ligne de topologie
n'attendait leur position. Elle vit maintenant dans `topology_layout`, à part — plutôt que
dans de faux équipements qui apparaîtraient ensuite dans tous les comptages.

Elles se **rangent**, mais ne se **rattachent** pas : un abonné pend à son PoP, et c'est la
collecte qui le dit, pas un glisser-déposer. « Réinitialiser la disposition » les oublie
comme les autres.

### Un plafond posé est-il réellement tenu ?

Trois questions différentes, souvent confondues : ce que le contrôleur **veut** poser (le
plan), ce qu'il a **écrit** (le journal), et ce que le réseau **applique**. Seule la
troisième se ressent chez l'abonné, et sur RouterOS une file peut exister, porter le bon
débit, se lire sans erreur — et ne rien brider du tout :

| Cause | Ce qu'on voit | Ce qui se passe |
| --- | --- | --- |
| **FastTrack** | file normale, compteur qui n'avance pas | `action=fasttrack-connection` fait sauter aux connexions établies le reste du chemin, **files simples comprises**. Active par défaut dans le pare-feu d'usine |
| **File masquée** | deux files, chacune avec son débit | RouterOS n'applique que la **première** file qui vise une cible ; celles qui suivent sont décoratives |
| **File désactivée** | débit parfaitement lisible | `disabled=yes` ne bride rien |
| **Écart de débit** | interface et routeur ne disent pas la même chose | plafond changé en base, jamais repoussé |

`GET /api/v1/shaping/limits` va chercher ces quatre causes **sur le routeur**, file par
file. Le résultat alimente l'onglet *Shaping* (« Les plafonds sont-ils réellement
tenus ? ») et la colonne *Limite* de l'onglet *Abonnés*, où un plafond que le réseau ne
tient pas s'affiche **NON TENU** avec sa cause — impossible à confondre avec le badge
« imposé », qui ne dit que l'intention.

Le contrôleur **ne touche pas au pare-feu** : le fasttrack porte une décision de
performance qui n'est pas la sienne. Il le nomme, dit ce qu'il coûte, et donne la ligne à
coller (`/ip firewall filter disable [find action=fasttrack-connection]`). En revanche il
corrige ce qui lui appartient : une file désactivée à la main est **réactivée** par la
réconciliation, et une file masquée par une autre est signalée comme conflit plutôt que
maquillée en plafond conforme.

**Coup de boost temporaire.** Bouton *Boost* sur un abonné (onglet Abonnés ou arbre
réseau) : une durée, un facteur (×2, ×3, ×5) ou un débit explicite — en kbps, Mbps ou Gbps —, un motif. Le boost est
appliqué immédiatement si l'écriture est autorisée, et **expire tout seul** — un job
vérifie l'échéance toutes les 30 s et ramène la file au débit normal. La file RouterOS ne
sait rien de la durée : c'est le contrôleur qui la fait respecter.

Trois niveaux de débit, du plus fort au plus faible : **boost** (tant qu'il court),
**surcharge permanente**, **plan RADIUS**. Le boost n'écrase pas la surcharge, il passe
par-dessus puis s'efface. Un boost sans échéance est refusé : ce serait une surcharge
déguisée qui ne s'effacerait jamais.

**Cinq garde-fous, dans cet ordre :**

1. **Marquage de propriété.** Seules les files portant `comment=freeqos:managed` sont
   modifiées ou supprimées. Une file posée à la main ou par RADIUS n'est **jamais** touchée ;
   si son nom entre en collision avec un nom voulu, le plan signale un conflit et s'abstient.
2. **Plan avant exécution.** Le planificateur est une fonction pure : il produit des
   commandes comme données. Rien ne part tant qu'on n'a pas appliqué.
3. **`dry_run` par défaut**, et l'application réelle exige `confirm: true`.
4. **`ENFORCEMENT_ENABLED`**, le drapeau global : à `false`, aucune écriture ne part,
   quelle que soit la confirmation.
5. **Droits réels du compte.** Si un compte `rw_*` distinct est déclaré, il est utilisé.
   Sinon le compte configuré sert à l'écriture — **ses droits réels décident, pas la
   déclaration** : le contrôleur lit `/user` et `/user/group` sur le routeur pour savoir
   si le compte possède bien `write` et `api`. Beaucoup d'exploitants se connectent déjà
   avec un compte complet ; refuser sur la seule absence de `rw_username` reviendrait à
   ignorer la réalité. `REQUIRE_SEPARATE_WRITE_ACCOUNT=true` rétablit l'exigence stricte.

Plus un **coupe-circuit** : au-delà de `ENFORCEMENT_MAX_ACTIONS` (500), le plan est refusé —
un plan anormalement gros signale presque toujours un état désiré mal calculé.

Toute commande envoyée, y compris simulée, est journalisée dans `enforcement_audit` et
visible dans l'interface.

**Droits nécessaires sur RouterOS.** L'enforcement exige les politiques `write` et `api`.
*Réglages › Shaping et écriture › Analyser l'existant* affiche le verdict lu sur le
routeur : le compte
peut écrire, ne peut pas (avec la politique manquante), ou c'est indéterminable.

Si votre compte les a déjà, il n'y a **rien à faire**. Sinon :

```
# soit ajouter les droits au groupe existant
/user/group set [find name=<groupe>] policy=read,write,api,test

# soit créer un compte d'écriture distinct, et le déclarer via rw_username
/user group add name=qos-rw policy=read,write,api,test
/user add name=qos-rw group=qos-rw password=…
```

Trois verdicts possibles, et le troisième compte : quand `/user` n'est pas lisible — compte
authentifié par RADIUS, par exemple — le contrôleur **ne bloque pas**. Il tente la commande
et rapporte ce que RouterOS répond réellement, en traduisant
`not enough permissions` en la correction à faire.

### Clients à IP fixe (non PPPoE)

Tous les WISP n'ont pas que du PPPoE. Les clients professionnels, les collectivités et
les liaisons dédiées sont souvent en **IP fixe sur un VLAN**, sans session à observer.
Ils sont pris en charge au même titre que les abonnés PPPoE, avec une différence
importante à comprendre.

**C'est une déclaration, pas une découverte — et c'est assumé.** Un abonné PPPoE
s'annonce tout seul : il ouvre une session, `/ppp/active` le nomme, RADIUS donne son
plan. Un client à IP fixe ne fait rien de tout cela. Rien sur le réseau ne dit qu'une
adresse lui appartient, ni quel débit il a souscrit. L'inventaire saisi depuis
l'interface (onglet **Abonnés → Inventaire des clients à IP fixe**) est donc la
**seule source possible**, et le contrôleur la traite comme une vérité — exactement
comme l'inventaire de routeurs.

**Une seule notion d'abonné, un discriminant explicite.** Les deux types vivent dans
la même table `subscribers`, distingués par `kind` (`pppoe` / `static`). Deux raisons :

- RouterOS n'a **qu'un seul espace de noms de files** (`freeqos-<référence>`). Un seul
  `UNIQUE (login)` rend impossible que deux abonnés produisent la même file et se
  battent à chaque cycle de réconciliation.
- une fois la ligne posée, plan, surcharges, boosts, planification et journal d'audit
  suivent **rigoureusement le même chemin**. Il n'y a pas de second pipeline à maintenir.

**La référence ne doit pas encoder l'adresse.** La clé de réconciliation de la file en
dérive, et elle doit survivre à un déménagement : `mairie-vitre`, pas `static:120:10.0.0.5`.
Une référence qui contient l'IP ferait détruire puis recréer la file au moindre
changement d'adresse, en perdant au passage surcharges et historique.

**Un sous-réseau reste un sous-réseau.** Un client à qui on a vendu un `/29` voit son
bloc entier plafonné. C'est la seule différence de traitement à l'écriture : une
session PPPoE est toujours ramenée à un `/32` (élargir shaperait les voisins de
l'abonné), un client déclaré garde le préfixe de sa fiche.

**Déclarer un client POSE sa file, tout de suite.** La réconciliation périodique
passe toutes les deux minutes et fait le travail — mais entre la saisie et son
passage, rien ne distinguait « ça arrive » de « ça n'arrivera jamais ». La
déclaration applique donc elle-même, et la réponse dit ce qui a été écrit :

| État rendu | Ce qu'il veut dire |
|---|---|
| **File posée** | la file existe sur le routeur, au débit de la fiche |
| **File à poser** | l'enforcement est désactivé : elle est calculée, rien n'est écrit |
| **Aucune file** | le planificateur l'a écartée, avec son motif — le plus souvent : aucun débit souscrit saisi |
| **PoP sans routeur** | aucun routeur collecté ne porte ce PoP. Le message **nomme ceux qui existent** |
| **Conflit** | une file tierce occupe déjà cette adresse (RouterOS n'applique que la première) |

**Seule la file de ce client est écrite.** Le plan est calculé en entier — il
faut les files parentes et les types CAKE — puis **restreint à son nom**.
Déclarer un abonné ne réécrit donc pas les files des autres, et retirer une fiche
retire sa file *immédiatement* sans qu'un `/ppp/active` vide au mauvais moment
puisse emporter le PoP avec elle, alors même que le plan est calculé avec `prune`.

Une déclaration n'échoue **jamais** parce qu'un routeur est muet : la fiche est
l'intention de l'exploitant, elle est enregistrée, et le rapport dit ce qui n'a
pas pu être écrit. La réconciliation repassera derrière.

**« Mon client ne remonte pas » : le PoP.** Le rapprochement fiche ↔ routeur se
faisait par **égalité de chaîne**. `francophonie` et `Francophonie` étaient donc
deux sites : le client n'avait ni collecteur, ni compteur, ni file — et un PoP
fantôme naissait en base, indiscernable du vrai. Rien ne tombait en panne, le
client ne remontait simplement jamais.

Le rapprochement tolère désormais la casse, les accents, la ponctuation et le mot
« PoP » lui-même : `PoP Francophonie`, `pop-francophonie` et `francophonie`
désignent le même site. Deux PoP **réellement distincts** qui se ressembleraient
après cette normalisation ne sont jamais fusionnés — la résolution rend
« ambigu » et n'en choisit aucun, parce que poser une file sur le mauvais site
est pire que de ne rien poser. L'égalité exacte garde la priorité.

Le nom retenu pour la mesure est celui du **routeur**, pas celui de la saisie :
c'est ce qui empêche le PoP fantôme de renaître au cycle suivant. Et la liste
déroulante du formulaire est alimentée par les **routeurs collectés**, non par la
table des PoP — proposer un PoP né d'une faute de frappe reproduirait l'erreur.

**La colonne « File » répond sans qu'on la pose.** L'inventaire affiche l'état
réel de la file de chaque fiche, lu sur les routeurs après l'affichage du
tableau (`GET /api/v1/static-clients/enforcement`). Le motif vient du
planificateur lui-même : il ne peut donc pas raconter autre chose que ce qui
serait réellement écrit.

**Rattachement topologique déclaré.** Aucun `caller-id` n'existe pour ces clients : la
jointure MAC ↔ station UISP ne peut pas les rattacher. Le secteur se saisit dans la
fiche. Ils apparaissent alors dans l'arbre avec leur propre nature (`static`, pas
`cpe` : ce n'est pas un équipement observé), ce qui les fait **compter dans le partage
d'un lien congestionné** — sans quoi les abonnés PPPoE du même secteur se feraient
rogner à leur place.

**Détection assistée : le contrôleur signale, l'humain décide.** Attendre qu'on
saisisse un client à l'aveugle est une mauvaise façon de travailler — encore
faut-il savoir qu'il est là. RouterOS n'a aucune table qui liste « les clients »
(la notion n'existe pas dans sa configuration), et chaque population laisse une
trace différente. Un job périodique **recense** donc le PoP (voir la section
suivante) et en tire deux lectures :

| Ce que le recensement montre | Ce qu'on en fait |
|---|---|
| une adresse **dans le bloc** d'un client déclaré | confirme sa présence — colonne « Vu actif » |
| une adresse qui **ne correspond à rien** | **candidat**, listé dans l'onglet Abonnés → « Détectés, non déclarés » |

Le rapprochement se fait par **contenance réseau** (`<<=`), pas par égalité : un
client déclaré en `/29` est reconnu quand n'importe laquelle de ses adresses
parle.

**Un candidat n'est pas un client, et rien ne peut le transformer tout seul.**
Une imprimante, une caméra ou l'équipement d'un autre opérateur laissent
exactement la même trace. Trois garanties, chacune vérifiée par un test :

- le job de détection **écrit** dans `vlan_sightings` et n'a même pas de méthode
  pour lire les candidats — aucun chemin de code ne peut en faire un abonné ;
- `ShapingService` ne reçoit pas le dépôt d'observations **au montage** : la
  séparation est structurelle, pas une règle dans un commentaire ;
- il n'existe **aucune route** « promouvoir ce candidat ». Déclarer passe par
  `POST /static-clients` avec un débit souscrit. Le bouton **Déclarer** de
  l'interface pré-remplit l'adresse, le VLAN et le PoP — la référence et le plan
  restent à saisir, parce que l'IP ne doit pas servir d'identité et que le débit
  vendu ne se devine pas.

Dans l'arbre, un candidat est une **troisième nature de nœud** (`candidate`),
distincte d'un voisin réseau (`/ip/neighbor`) et d'un abonné (`caller-id`). Il
est posé **à la lecture** du graphe et jamais persisté dans `topology_nodes` :
un candidat déclaré ou devenu muet disparaît de lui-même. Le nombre est plafonné
(`vlan_candidate_limit`) pour qu'une VLAN bavarde ne rende pas l'arbre illisible.

Réglages, tous pilotés depuis la base : `vlan_detect_enabled`,
`vlan_detect_interval_s`, `vlan_candidate_limit`, `vlan_sighting_retention_s`.

#### Localiser **tous** les clients d'un PoP, VLAN comprises

Chercher les clients par le **nom de leurs interfaces** ne marche pas. C'est la
question que posait la première version (« cette entrée ARP est-elle rattachée à
une `/interface/vlan` ? ») et elle rate exactement les montages les plus
répandus. Le recensement (`app/collectors/pop_census.py`) la pose autrement, en
trois temps.

**1. Le périmètre vient de l'adressage, pas des noms.** `/ip/address` dit quels
sous-réseaux ce routeur dessert vraiment. Chacun est classé, avec son motif :

| Classement | Sur quel signe | Conséquence |
|---|---|---|
| **client** | rien ne le range ailleurs | on y cherche des clients |
| point-à-point | préfixe `/30` ou plus étroit (`/126`+ en v6) | un lien entre deux équipements, pas de la desserte |
| transit | porte une adjacence OSPF/BGP établie, ou la passerelle par défaut | du transit, quelle que soit sa taille |
| pppoe | l'interface héberge un serveur PPPoE | ses abonnés ont déjà une identité |
| désactivé | adresse ou VLAN éteinte dans la configuration | ne dessert plus rien |

La question devient : *cette adresse tombe-t-elle dans un sous-réseau client de
ce PoP ?* Le nom de l'interface ne compte plus — **un client derrière un pont en
filtrage VLAN est vu comme les autres.** Le classement est délibérément large :
rater un sous-réseau client rend un client invisible, alors qu'un sous-réseau
d'infrastructure pris à tort pour de la desserte ne produit qu'un candidat qu'on
écarte d'un coup d'œil. Entre les deux erreurs, la seconde se répare.

**2. Sept sources, chacune couvrant l'angle mort des autres.**

| Source | Ce qu'elle seule apporte | Ce qu'elle ne voit pas |
|---|---|---|
| `/ip/arp` | toute machine qui a parlé récemment | s'efface après quelques minutes de silence |
| `/ip/dhcp-server/lease` | **survit au silence** du client, et porte souvent son nom (`host-name`, commentaire) | les clients à IP fixe |
| `/ppp/active` | l'identité des abonnés PPPoE — recensés pour être **écartés** des propositions | le reste |
| `/interface/bridge/host` | le **VLAN et le port physique** que l'ARP perd sous un pont (jointure par la MAC) | pas d'adresse IP |
| `/ip/route` | les **blocs routés derrière un CPE** (`/29` d'entreprise) : aucune table de présence ne les montre | ce qui n'est pas routé |
| `/queue/simple` | ce que l'exploitant a **déjà déclaré sur le routeur** — la source la plus qualifiée | là où aucune file n'existe |
| `/ip/neighbor` | l'identité et le modèle annoncés par le CPE | ce qui ne parle ni MNDP ni LLDP |

Les sources **s'ajoutent** : aucune ne peut faire disparaître ce qu'une autre a
vu, et chaque hôte porte la liste de celles qui l'ont trouvé. Ce champ est plus
utile qu'il n'en a l'air : un client connu par la seule table ARP disparaîtra
s'il se tait, un client qui porte aussi un bail restera. C'est ce qui dit quelle
confiance accorder à une **absence**.

Le VLAN est résolu du plus sûr au moins sûr, et sa provenance est rendue : une
`/interface/vlan` **porte** son numéro, la table de ponts l'**observe**, un pvid
le **suppose**. Chaque hôte sort donc avec son VLAN, son port physique, son
routeur, son PoP — et de quoi recouper tout cela sur l'équipement.

**3. Ce qu'on ne sait pas est dit.** C'est ce troisième temps qui rend la
méthode sûre : non pas la promesse de ne rien rater, mais la garantie que rien
ne se perd **en silence**. Le recensement rend l'état de chaque lecture et une
liste de remarques en clair : source illisible, sous-réseau client sans aucune
présence, adresse vue hors de tout sous-réseau connu (le PoP commute sans
router : l'adressage est sur un autre routeur, à déclarer), MAC vue en L2 sans
IP, clients tenus par la seule table ARP.

```bash
# Qui vit sur ce PoP, et lesquels ne sont pas dans l'inventaire
curl 'localhost:8000/api/v1/pops/census?pop_name=PoP%20Nord' | jq '.pops[0].counts'
{ "clients": 47, "pppoe": 31, "declares": 38, "non_declares": 9 }
```

Depuis l'interface : *Abonnés → Inventaire → **Recenser le PoP (toutes
sources)***. Chaque ligne non déclarée porte un bouton **Déclarer** qui
pré-remplit adresse, VLAN et PoP — la référence et le débit souscrit restent à
saisir, comme partout ailleurs.

**Une table absente ne fait perdre ni les autres, ni ce qui a été vu.** Un
routeur sans serveur PPPoE, sans OSPF ni BGP n'a tout simplement pas ces tables :
c'est normal et rien n'est signalé. Toute autre lecture qui échoue est une
dégradation : les observations obtenues sont **quand même enregistrées**, et
l'erreur remonte dans le journal du job (`PartialCensusError`). Jeter la liste
parce qu'une table sur seize a expiré punirait l'exploitant deux fois.

**Rien de tout cela ne crée quoi que ce soit.** Le recensement propose ; un
abonné PPPoE, une antenne de l'inventaire, un pair de routage et le routeur
lui-même ne sont **jamais** proposés à la déclaration, et seule la nature
`client-possible` devient une observation dans `vlan_sightings`.

**Pourquoi une ligne a été écartée** : *Abonnés → Inventaire → « Un client
manque ? Voir pourquoi »* rend le motif de chaque ligne ARP, et le champ qui
répond presque toujours — `reseaux_clients`. S'il est vide, `/ip/address` n'a
rien donné et seul le nom des interfaces sert de critère : c'est là qu'il faut
regarder. Les autres motifs appellent des gestes différents : VLAN **désactivée**
(la réactiver), interface hébergeant un **serveur PPPoE** (rejet voulu), adresse
**sans MAC** (cherchée, pas répondue).

Le diagnostic emprunte exactement le même chemin de décision que la détection
(`judge_arp_rows`, et le recensement de la même lecture) : un diagnostic qui
raconterait autre chose que ce que fait le code serait pire que pas de
diagnostic.

**Limite à connaître : la mesure dépend de la file.** Sans session PPPoE, aucune
interface ne porte le trafic de ce client ; le seul compteur par client dont on dispose
est celui de la file qui le vise (`/queue/simple`). Tant qu'aucune file n'existe sur
son adresse — enforcement désactivé, débit souscrit non saisi, PoP sans routeur — le
client apparaît avec son plan et son état, mais **sans débit**. Un trou est plus
honnête qu'un zéro, qui se lirait comme une absence de trafic. La colonne « File » de
l'inventaire dit alors laquelle de ces raisons s'applique. Une file posée à la main
par l'opérateur est lue aussi, si elle vise la même adresse.

```bash
# Declarer un client a IP fixe : la file est posee dans la foulee, la reponse
# porte 'enforcement' -- ce qui a ete ecrit, ou ce qui l'en empeche
curl -X POST localhost:8000/api/v1/static-clients -H 'content-type: application/json' -d '{
  "reference": "mairie-vitre", "label": "Mairie de Vitré",
  "pop_name": "PoP Nord", "address": "10.0.0.0/29", "vlan": 120,
  "sector_key": "uisp:ap-nord", "plan_down_mbps": 200, "plan_up_mbps": 50
}'

# Il devient un abonné comme un autre au cycle suivant
curl 'localhost:8000/api/v1/subscribers/latest?kind=static'
```

### Parité avec LibreQoS : ce qui est possible, ce qui ne l'est pas

L'interface reprend la lecture de LibreQoS, mais la contrainte hors-bande impose une
différence de fond qu'il vaut mieux connaître avant de comparer les deux :

| Signal | LibreQoS (inline) | freeQoS (hors-bande) |
|---|---|---|
| Débit par abonné | compteurs du shaper | `/interface` de la session PPPoE — **équivalent** |
| Débit par site / backhaul | arbre du shaper | agrégation PoP + capacité UISP — **équivalent** |
| Débit vs plan | oui | oui — **équivalent** |
| Shaping hiérarchique | HTB + CAKE, arbre du shaper | files simples RouterOS + CAKE, arbre issu de la topologie — **équivalent** |
| **RTT par abonné** | **passif**, horodatages TCP de chaque flux | **sonde active** `/ping` depuis le PoP, par lots |
| **Retransmissions TCP** | passif, eBPF | **impossible** — exige de voir les paquets |
| Latence **sous charge** (bufferbloat) | mesurée en continu sur le trafic réel | **dérivée** en corrélant RTT (sonde active) et débit du même échantillon — note A+…F par abonné |

Autrement dit : tout ce qui se lit dans des compteurs est à parité. Tout ce qui exige
d'inspecter les paquets ne l'est pas, et ne le sera jamais depuis une VM de management —
c'est le prix du hors-bande, pas une lacune d'implémentation.

La sonde RTT est **désactivée par défaut** : elle consomme du CPU routeur, contrairement à
la mesure passive. Une fois activée, elle sonde un lot d'abonnés par cycle en tourniquet, et
la mesure est rattachée à l'échantillon de débit suivant — une seule ligne par abonné et par
cycle, pas de lignes ne portant qu'un RTT. Un abonné qui bloque l'ICMP reste à `NULL` : pas
de valeur inventée.

**Elle se pilote depuis l'interface**, sans variable d'environnement : case *Sonde RTT* de
l'onglet **Exécutif**. Comme l'enforcement, c'est un drapeau persistant en base (`rtt_enabled`),
amorcé par `RTT_ENABLED` au premier démarrage puis basculable à chaud (la base fait foi). Le
job de sonde est toujours planifié ; le drapeau décide seulement s'il sonde.

```bash
# Amorçage initial uniquement (ensuite, tout se fait dans l'interface) :
RTT_ENABLED=true
RTT_INTERVAL_S=30      # période de sondage
RTT_BATCH_SIZE=20      # abonnés sondés par cycle
```

Le compte `qos-ro` doit posséder la politique `test` (elle est dans le groupe recommandé
plus bas).

Le tourniquet est **par PoP**, pas pour le parc entier : c'est le routeur de l'abonné qui
émet le ping, donc c'est son CPU qu'on ménage, et il n'existe aucune enveloppe globale à
partager. Conséquence voulue : le délai de re-sondage d'un abonné dépend du nombre d'abonnés
de **son** PoP, jamais de la taille totale du parc — un PoP de 3 abonnés n'attend plus
derrière un PoP de 800. Le nombre total de pings d'un cycle vaut donc
`RTT_BATCH_SIZE × nombre de PoPs sondés`.

### Score de QoE composite

La ligne *QoE* de l'écran Exécutif n'est plus un proxy latence. Elle l'a longtemps été — une
fonction affine du RTT brut, faute de latence sous charge — et c'était honnête tant que la
corrélation RTT ↔ débit n'existait pas. Elle existe (phase 3), donc le score la lit :

| Composante | Ce qu'elle mesure |
|---|---|
| Latence **à vide** | le plancher du chemin : distance, encapsulation, radio |
| **Bufferbloat** | ce que la charge **ajoute** à cette latence (note A+…F) |

C'est le **maillon faible** qui fait la note (`min` des deux) : un lien à 8 ms au repos qui
monte à 400 ms sous charge n'est pas « excellent », et un chemin intrinsèquement long reste
injouable même parfaitement géré. La sévérité d'une note issue du bufferbloat est celle de la
note A+…F prise telle quelle — un seul barème de couleur, pas deux.

Quand aucune charge n'est corrélable (abonné silencieux, sonde coupée), le score retombe sur
le proxy latence **et le dit** (`basis="latency"`, pas de note A+…F) : un repli annoncé vaut
mieux qu'un chiffre qu'on croit mesuré. Tout vit dans `app/services/qoe.py`, et c'est cette
**unique** fonction que lisent la heatmap Exécutif, l'API `/bufferbloat` et la boucle fermée
ci-dessous.

### Boucle fermée QoE (phase 4)

Jusqu'ici, la seule grandeur qui refermait une boucle était la **capacité backhaul mesurée** :
le planificateur pose la file du lien parent à `mesure × SHAPING_SAFETY_FACTOR`. C'est la
boucle centrale lente pour le goulot radio, et elle marche. Ce qu'elle ne voit pas : un
secteur dont la latence **gonfle** sous charge alors que la radio annonce toujours sa
capacité — le cas classique du buffer d'équipement trop gros. Le signal qui le dit existait
déjà, mais n'alimentait qu'un tableau de bord.

Le job `qoe_closed_loop` ferme ce circuit : lecture des scores de QoE, décision, plan,
application — le même enchaînement que `reconcile()`.

**Ce qui bouge, c'est l'enveloppe partagée du secteur**, jamais le plan souscrit d'un abonné.
Un abonné n'est pas responsable du bufferbloat de son secteur, et lui retirer le débit qu'il
paie serait la mauvaise réponse : on resserre la file du **lien** qui dessert le secteur, et
CAKE arbitre ensuite entre les circuits, comme d'habitude.

Trois principes assumés :

1. **Un abonné dégradé ne suffit pas.** Un seul abonné qui gonfle, c'est *son* dernier km
   (CPE, wifi domestique, pare-feu). C'est la corrélation entre plusieurs abonnés du même
   secteur qui désigne le secteur — d'où `QOE_MIN_DEGRADED_SUBSCRIBERS` (2 par défaut).
2. **On resserre vite, on relâche lentement.** Une dégradation agit dès le cycle suivant ; un
   retour à la normale doit tenir `QOE_RECOVERY_CYCLES` cycles avant de rendre **un** cran.
   Sans cette asymétrie la boucle oscillerait. Ce n'est pas un cliquet : ce qu'elle prend,
   elle le rend.
3. **Le resserrage est borné.** `QOE_TRIM_FLOOR` (50 % par défaut) : au-delà, le goulot n'est
   plus le buffer radio mais la capacité elle-même, et resserrer encore ne ferait que brider
   un secteur déjà à genoux. La boucle le signale au lieu de continuer.

Les garde-fous sont ceux des autres boucles automatiques : rien n'est écrit tant que
`ENFORCEMENT_ENABLED` est faux (la décision est quand même prise et le plan calculé, ce qui
permet de **lire** ce que la boucle ferait avant de lui donner la main), **jamais de purge**,
et le plan passe par `build_plan` comme tous les autres — donc diffable, journalisé dans
`enforcement_audit` sous l'auteur `system:qoe-loop`, et visible dans l'interface. Aucun
chemin d'écriture parallèle.

Le resserrage vit dans sa propre table (`qoe_link_states`), **séparée** des surcharges
manuelles : une surcharge est une décision d'exploitant, un resserrage une décision de la
boucle, et les confondre ferait qu'un cycle automatique écraserait un débit saisi à la main.
Les deux se composent dans le planificateur, elles ne se marchent jamais dessus.

```bash
QOE_LOOP_INTERVAL_S=300           # 0 désactive la boucle
QOE_WINDOW_MINUTES=15             # fenêtre d'observation
QOE_SCORE_THRESHOLD=55            # sous ce score, l'abonné est dégradé
QOE_MIN_DEGRADED_SUBSCRIBERS=2    # combien il en faut pour accuser le secteur
QOE_TRIM_STEP=0.10                # un cran de resserrage
QOE_TRIM_FLOOR=0.50               # jamais en dessous
QOE_RECOVERY_CYCLES=3             # cycles sains avant de rendre un cran
```

Lecture : `GET /api/v1/shaping/qoe` montre ce que la boucle a décidé par secteur, avec sa
raison. `POST /api/v1/shaping/qoe/run` déclenche un cycle hors cadence.

La boucle reste **inerte** tant que la sonde RTT ne fournit pas de latence à corréler : sans
score, aucun secteur n'est noté, donc aucune décision n'est prise. Elle n'invente pas de
dégradation.

### Développement

```bash
pip install -e ".[dev]"
make test      # 556 tests, ni base ni routeur requis
make lint
make dev       # uvicorn en rechargement à chaud
```

---

## Architecture

```
app/
├── config.py            Settings pydantic + inventaire des routeurs (YAML/JSON/env)
├── models.py            Objets de domaine (PppoeSession, SubscriberSample, Plan…)
├── container.py         Assemblage des dépendances
├── scheduler.py         Boucle périodique (un job = une tâche, sans chevauchement)
├── main.py              FastAPI + cycle de vie
├── db/
│   ├── schema.sql       Tables, hypertables, vues (idempotent)
│   ├── database.py      Pool asyncpg, migration, politiques Timescale
│   ├── directory.py     Référentiel : PoPs / abonnés / backhauls (upsert + cache)
│   ├── routers_repo.py  Inventaire dynamique des routeurs (secrets chiffrés)
│   ├── writer.py        Écriture des séries (+ double mémoire)
│   └── repository.py    Lectures agrégées pour l'API
├── collectors/
│   ├── mikrotik.py      RouterOS : /ppp/active + /interface (lecture seule)
│   ├── uisp.py          BackhaulCapacityProvider : UispProvider | MockBackhaulProvider
│   ├── topology.py      Découverte du graphe : voisins, capacités, jointure CPE
│   ├── radius.py        PlanProvider : FreeradiusSqlPlanProvider | MockPlanProvider
│   └── parsing.py       Normalisation des valeurs RouterOS/RADIUS
├── enforcement/         PHASE 2 — seul code qui écrit sur un équipement
│   ├── models.py        État désiré, actions, plan (données pures)
│   ├── planner.py       Désiré vs réel → commandes. Fonction pure, testée à 100 %
│   └── routeros.py      Exécution via qos-rw, dry-run, coupe-circuit
├── services/
│   ├── rates.py         Dérivation des débits + détection de reset de compteurs
│   ├── crypto.py        Chiffrement des identifiants routeur (Fernet)
│   ├── registry.py      Inventaire vivant : fusion fichier + base, rechargement à chaud
│   ├── rtt.py           Sonde de latence active, un tourniquet PAR PoP
│   ├── bufferbloat.py   Latence sous charge : corrélation RTT ↔ débit, note A+…F (pure)
│   ├── qoe.py           PHASE 3 — score composite, lu par la heatmap ET la boucle fermée
│   ├── qoe_loop.py      PHASE 4 — décision de la boucle fermée, par secteur (pure)
│   ├── heatmap.py       Bandes QoE / RTT / utilisation dans le temps (pure)
│   ├── shaping.py       Découverte, analyse de l'existant, plan, application, boucles
│   └── collection.py    Orchestration d'un cycle
└── web/                 Interface d'administration (SPA sans framework ni CDN)
    ├── ui.py            Squelette servi par FastAPI
    └── static/          app.css + app.js (graphes SVG faits main)
```

### Points techniques qui méritent attention

**`/ppp/active/print` ne porte pas les compteurs d'octets.** Sur RouterOS, chaque session
PPPoE crée une interface dynamique `<pppoe-LOGIN>` et ce sont ses compteurs qui portent
`rx-byte`/`tx-byte`. Le collecteur fait donc **deux lectures et les corrèle**. Le motif de
nommage est configurable par routeur (`pppoe_interface_pattern`), avec un repli par
recherche de nom — et un refus explicite de corréler si plusieurs interfaces
correspondent, plutôt que d'attribuer un débit au mauvais abonné.

**Convention de sens.** Partout — base, API, UI — `rx`/`tx` sont **du point de vue du
routeur** :

| | Signification | Côté abonné |
|---|---|---|
| `rx_bps` | le routeur reçoit de l'abonné | **upload** |
| `tx_bps` | le routeur émet vers l'abonné | **download** |

**Reconnexion PPPoE.** Les compteurs repartent à zéro à chaque nouvelle session. Trois
garde-fous : uptime qui recule (signal le plus fiable), compteur qui décroît, débit
au-delà d'un plafond configurable. En cas de doute **aucun débit n'est écrit** (`NULL`) :
un trou dans la série se voit et se comble, une valeur fausse pollue durablement les
moyennes et le futur score QoE. Les octets bruts sont conservés en base pour permettre un
recalcul a posteriori.

**Isolation des pannes.** Les routeurs sont interrogés en parallèle ; un PoP injoignable
n'empêche ni ne retarde la collecte des autres. `librouteros` étant synchrone, chaque
lecture part dans un thread avec un délai de garde — sans quoi un routeur muet figerait
la boucle asyncio et donc tous les autres PoPs.

**Pas de chevauchement.** Un job enchaîne `exécuter → dormir le reste de la période`.
Deux cycles ne peuvent jamais taper simultanément sur la même session API. Un dépassement
de période est compté et exposé (`overruns`), il n'empile pas d'exécutions concurrentes.

---

## Configuration

Tout passe par variables d'environnement ou `.env` (voir `.env.example` pour la liste
complète et commentée).

### Inventaire des routeurs

Deux formes, cumulables — l'environnement l'emporte sur le fichier, ce qui permet de
rediriger un PoP vers un CHR de lab sans toucher à l'inventaire :

```bash
ROUTERS_FILE=config/routers.yml
# ou
ROUTERS='[{"name":"pop-1","host":"10.10.0.11","password_env":"MT_POP_1_PASSWORD"}]'
```

```yaml
# config/routers.yml
routers:
  - name: pop-1
    host: 10.10.0.11
    port: 8728                # 8728 API binaire, 8729 api-ssl
    username: qos-ro          # LECTURE SEULE en phase 1
    password_env: MT_POP_1_PASSWORD   # nom de la variable, jamais le secret
    role: pop                 # pop | core | gateway
    pop_name: Site 1
    pppoe_interface_pattern: "<pppoe-{login}>"
    rw_username: qos-rw       # utilisé uniquement par l'enforcement
    rw_password_env: MT_POP_1_RW_PASSWORD

backhauls:
  - name: bh-1
    pop_name: Site 1
    uisp_device_id: 8a2f1c3e-…   # ou la MAC de la radio — voir ci-dessous
    nominal_capacity_mbps: 500
```

**`uisp_device_id` n'est pas décoratif : c'est lui qui fait descendre la capacité mesurée
dans la file.** Le contrôleur rapproche un backhaul du lien qui le porte par l'**identité
physique** de la radio — son identifiant UISP ou sa MAC, les deux formes étant acceptées
dans ce champ et comparées sans tenir compte de la casse ni des séparateurs. Le `name`, lui,
est votre libellé (`bh-1`) alors que le lien porte l'identité que la radio annonce en
MNDP/LLDP (`NanoBeam-Nord`) : les faire correspondre relèverait de la coïncidence. Sans
identité renseignée, le rapprochement retombe sur l'égalité des deux noms, et à défaut la
file parente garde le **débit négocié du port** — le plafond du câble ethernet, pas celui de
la parabole. Un backhaul dont aucun lien ne correspond est signalé dans le journal.

**Sécurité.** Un routeur déclare le *nom* de la variable d'environnement qui porte son mot
de passe, jamais le mot de passe. Un secret manquant est signalé au démarrage et le
routeur concerné est ignoré — les autres continuent. Les mots de passe sont des
`SecretStr` (absents des `repr` et des dumps) et l'API ne les expose nulle part
(test dédié).

### Comptes RouterOS

```
/user group add name=qos-ro policy=read,api,test
/user add name=qos-ro group=qos-ro password=…
# Phase 2 uniquement
/user group add name=qos-rw policy=read,write,api,test
/user add name=qos-rw group=qos-rw password=…
```

### Providers

| Variable | Valeurs | Effet |
|---|---|---|
| `BACKHAUL_PROVIDER` | `mock` \| `uisp` | Capacité radio simulée ou lue chez UISP |
| `PLAN_PROVIDER` | `mock` \| `freeradius_sql` | Plans simulés ou lus dans FreeRADIUS |

Le simulateur de backhaul est **déterministe** pour un couple (seed, device, instant) et
fait varier la capacité dans le temps (cycle principal + fade lent, déphasé par device),
avec un signal et un airtime corrélés. C'est ce qui permet de valider en lab la logique
que la boucle centrale devra suivre, sans radio.

---

## Réglages : la base fait foi, pas l'environnement

Les réglages d'exploitation — politique de **shaping**, options **CAKE**, garde-fous
d'écriture et **cadences** de collecte — vivent en **base**, pas dans l'environnement.
Ils se changent depuis l'onglet **Réglages** de l'interface et **prennent effet sans
redémarrage** : les options de shaping/CAKE au prochain plan, les cadences au prochain
tour de boucle du collecteur.

Les variables d'environnement correspondantes ne servent plus qu'à **amorcer** une
valeur au tout premier démarrage, quand la table est vide. Dès qu'une valeur est posée,
c'est elle qui gagne ; `GET /api/v1/settings` indique pour chaque réglage sa valeur, son
défaut et sa provenance (`db` ou `defaut`), et `DELETE /api/v1/settings/{nom}` le fait
revenir à son défaut.

| Méthode | Chemin | Description |
|---|---|---|
| `GET` | `/api/v1/settings` | Réglages en vigueur, par groupe, avec valeur / défaut / provenance |
| `PUT` | `/api/v1/settings/{nom}` | Fixer un réglage (validé, appliqué à chaud, puis persisté) |
| `DELETE` | `/api/v1/settings/{nom}` | Revenir au défaut |
| `GET` | `/api/v1/settings/history` | Qui a changé quel réglage, quand et pourquoi |

Restent dans l'environnement **uniquement** ce qu'il faut connaître *avant* de pouvoir
ouvrir la base — les y chercher serait circulaire : `DATABASE_URL`, `APP_SECRET_KEY`,
l'inventaire fichier (`ROUTERS_FILE`), et `APP_ENV` / `LOG_LEVEL` / `API_PREFIX`.

## Écriture (enforcement)

Le contrôleur reste **hors-bande** (jamais sur le chemin des paquets) mais **écrit**
désormais des files `/queue/simple` sur les routeurs, sous garde-fous : rien ne part tant
que `ENFORCEMENT_ENABLED` est faux, seules les files marquées `freeqos:managed` sont
touchées, et **toute commande est journalisée dans `enforcement_audit`** avec son origine
(`ui`, ou `system:reconcile` / `system:boost-expiry` pour les boucles automatiques) et le
détail des changements. La vérification TLS vers chaque routeur est configurable
(`tls_verify` : `strict` par défaut, `fingerprint`, ou `insecure` assumé).

> L'authentification de l'API n'est pas encore activée : les endpoints sont anonymes.

## Trafic : NetFlow

**Ce qu'il apporte.** Les compteurs d'une file RouterOS comptent ce qui traverse *cette*
file : ils repartent de zéro à chaque reconnexion PPPoE, ne survivent pas au redémarrage
du routeur, et ne disent rien du trafic d'un client dont la file n'existe pas encore.
NetFlow donne du **volume daté**, par abonné et par usage, sur tout ce qui passe — y
compris ce qu'on ne bride pas. C'est ce qui rend possibles la facturation au volume, les
quotas, et la question « de quoi est fait le trafic qui sature ce secteur ».

**Trois versions décodées**, sans dépendance externe : **NetFlow v5**, **NetFlow v9** et
**IPFIX (v10)**, y compris les modèles à champs constructeur et à longueur variable.

### Mettre en route

1. Rien à ouvrir : `NETFLOW_ENABLED` vaut **`true` par défaut**. Le collecteur *écoute* —
   il n'émet rien, n'interroge aucun équipement, n'ajoute aucune charge au réseau ; tant
   qu'aucun routeur n'exporte vers lui, il ne fait rien de plus qu'ouvrir un port UDP.
   L'inverse coûtait cher : un datagramme non reçu ne se rattrape pas, et le trafic était
   perdu **définitivement** pendant tout le temps où personne ne s'apercevait que le
   drapeau existait. Port par défaut : `2055/udp` (le trio `ENABLED`/`BIND`/`PORT` reste
   dans l'environnement — ouvrir une socket n'est pas un réglage qu'on bascule depuis une
   page web).

   > En Docker, le port est publié **en UDP** (`2055:2055/udp`). Sans le suffixe `/udp`,
   > Docker publie du TCP et les datagrammes n'atteignent jamais le collecteur — sans la
   > moindre erreur nulle part, juste un onglet qui reste vide.
2. **Rien à taper sur les routeurs.** Le contrôleur pose l'export lui-même —
   `/ip/traffic-flow` et sa cible — sur chaque routeur de l'inventaire, et
   revérifie périodiquement (`NETFLOW_EXPORT_INTERVAL_S`). *Trafic › Export sur les
   routeurs* montre l'état et permet de simuler puis de poser à la demande.

   L'adresse annoncée est calculée **par routeur** : celle que le système
   utiliserait pour le joindre. Sur un contrôleur multi-interfaces, une valeur
   unique serait fausse pour une partie du parc, et les flux partiraient dans le
   vide sans que rien ne le signale.

   **Les délais d'export sont posés eux aussi**, et c'est ce qui décide en combien de
   temps un flux devient visible. Le défaut RouterOS n'exporte un flux **encore actif**
   qu'au bout de **trente minutes** : une session de streaming, une visio, un
   téléchargement n'apparaissent pas avant une demi-heure, alors que le routeur s'affiche
   comme parfaitement configuré. Le contrôleur pose `active-flow-timeout=1m` et
   `inactive-flow-timeout=15s` — un ping apparaît donc une quinzaine de secondes après
   coup, pas une demi-heure.

   Comme toute écriture, celle-ci passe par `ENFORCEMENT_ENABLED`, un plan
   affichable et l'audit. Une cible déjà posée vers **un autre** collecteur n'est
   jamais touchée : envoyer ses flux à deux endroits est un choix légitime.
   À la main, si vous préférez :

   ```
   /ip/traffic-flow set enabled=yes interfaces=all
   /ip/traffic-flow/target add dst-address=<le collecteur> port=2055 version=9
   ```

3. **Déclarer d'où chaque exporteur regarde**, dans *Trafic › Exporteurs* : `edge` (en
   amont du cœur) ou `pop`. Un routeur configuré automatiquement est déclaré dans la
   foulée, avec le point de mesure déduit de son rôle. Un exporteur qui envoie sans être déclaré apparaît quand même,
   marqué `unknown` — un PoP mal configuré doit **se voir**, pas disparaître en silence.
4. Si l'équipement échantillonne, le dire (`sampling_rate`). Sans cela, un routeur en
   1:1000 rapporte un millième du trafic réel, et **rien ne le montre** : les chiffres
   restent plausibles, juste mille fois trop petits.

### « Aucun datagramme reçu » : les quatre causes

Le bandeau de l'onglet *Trafic* nomme celle qui s'applique et donne le geste. Dans
l'ordre où on les rencontre :

| Ce que dit le bandeau | Ce qui manque | Le geste |
|---|---|---|
| *Aucun routeur n'est déclaré* | Rien ne peut exporter : l'inventaire est vide | **Équipements** › ajouter un routeur (API RouterOS, compte lecture) |
| *N routeur(s) déclaré(s), aucun ne l'exporte* | L'export n'est pas encore posé | Bouton **Configurer l'export** — il descend au bloc *Export sur les routeurs* |
| *Écriture sur les routeurs désactivée* | Le contrôleur est en lecture seule | Bouton **Autoriser l'écriture** dans ce même bloc (ou *Réglages › Shaping et écriture*) |
| *Export posé sur N routeur(s), mais aucun datagramme n'arrive* | Le chemin réseau, **pas** la configuration | Port `2055/udp` publié ? pare-feu entre le PoP et le collecteur ? |

La dernière est la plus trompeuse, et son coupable le plus fréquent est Docker :
**sans le suffixe `/udp`**, `ports:` publie du TCP. Le routeur exporte, le collecteur
écoute, et rien ne se rencontre — sans erreur nulle part. Le `docker-compose.yml` du dépôt
publie bien `2055/udp`, et un test le verrouille.

Une fois l'export posé, comptez une quinzaine de secondes avant qu'un flux terminé
n'apparaisse (`inactive-flow-timeout`) : le routeur ne peut pas exporter un flux avant de
le considérer fini.

### « autre, 3 Kio » — mais **avec qui** ?

Le tableau par usage agrège justement ce détail, et celui par adresse fond tous les
clients ensemble. **Cliquer une famille d'usage** ouvre *Qui parle à qui* : une ligne par
conversation `client ↔ destination`, avec le service reconnu, le port, le volume, le
**débit moyen** sur la période, et le **débit en direct** pour les conversations qui se
tiennent à cet instant.

Un volume seul ne dit rien : « 3 Kio » sur une heure et « 3 Kio » en deux secondes
n'appellent pas la même réaction. La colonne *En direct* n'est remplie que pour les
conversations présentes dans la fenêtre en cours — la période dit ce qui **s'est passé**,
la fenêtre ce qui **se passe**.

Un clic sur l'adresse ouvre sa fiche complète (ci-dessous).

**Le trafic d'exploitation en est écarté.** Le contrôleur interroge les routeurs (port
8728), reçoit leurs flux (2055), et les routeurs se surveillent entre eux (BFD 3784, BGP
179) : ce trafic est le plus régulier du réseau, et il noyait le ping d'un client vers un
site. Trois règles le retirent de la liste des conversations :

| Écarté | Pourquoi |
|---|---|
| Les deux bouts dans l'espace client | Deux clients qui se parlent ne sont une destination ni pour l'un ni pour l'autre — et la conversation apparaissait **deux fois**, une par sens. La CGNAT (`100.64.0.0/10`) échappait au filtre standard : la bibliothèque la dit privée, un opérateur y met ses clients |
| Un bout est de l'infrastructure | Le contrôleur, les routeurs déclarés, les exporteurs. Cette liste se déduit de l'inventaire ; `NETFLOW_INFRASTRUCTURE_NETWORKS` en ajoute |
| Le port de service est du plan de gestion | API RouterOS, NetFlow, BFD, BGP, SNMP, RADIUS, syslog, Winbox. **SSH et telnet n'y sont pas** : un client s'en sert légitimement |

**Les volumes ne sont pas touchés** : on nettoie la liste des conversations, pas la
mesure. Le nombre de flux écartés est rendu par `/netflow/status` — un chiffre énorme veut
dire que le filtre est trop large, et il faut pouvoir s'en apercevoir.

**Le filtre nettoie aussi l'historique.** Refuser les nouvelles lignes ne suffit pas :
celles déjà écrites resteraient jusqu'à expiration de la rétention — une semaine pendant
laquelle la liste continue d'afficher le BFD entre routeurs. Chaque fenêtre repasse donc
sur `flow_destinations` avec les mêmes trois motifs, pour que l'historique et le direct
disent la même chose.

### Chercher dans les conversations

Deux cents conversations ne se consultent pas : ce qu'on cherche est toujours *ce PoP*,
*ce client*, *ce service*. Quatre filtres se cumulent — **recherche** (adresse du client,
login, adresse jointe, nom inverse, organisation, service), **PoP**, **catégorie**,
**usage** — et chaque valeur du tableau est cliquable : on filtre ce qu'on voit plutôt que
de le retrouver dans un menu.

Les listes ne proposent **que ce qui existe** dans les données affichées. Offrir tous les
PoPs de l'inventaire ferait choisir un filtre qui ne rend rien, et on chercherait la panne
plutôt que le filtre.

### Ce que le collecteur fait des flux

| Étape | Règle |
|---|---|
| **À qui appartient l'octet** | Le préfixe déclaré **le plus précis** qui contient l'adresse. Un `/32` à l'intérieur d'un `/29` désigne un autre abonné, et c'est lui qui gagne. |
| **Dans quel sens** | Déduit de l'**abonné**, jamais du numéro d'interface : destination dans son bloc = descendant, source dans son bloc = montant. Lire le sens sur l'interface obligerait à connaître le câblage de chaque exporteur, et se tromperait au premier recâblage. |
| **Où c'est compté** | Le point de mesure fait partie de la clé. `edge` et `pop` ne se mélangent jamais. |
| **Quand c'est écrit** | Une ligne par abonné et par fenêtre (60 s par défaut), pas une par flux. Un export de sortie internet porte des milliers de conversations par seconde. |
| **Ce qui n'est rattaché à rien** | Va dans une liste d'**hôtes vus**, qui sert à la saisie et à rien d'autre — et seulement si l'adresse tombe dans `NETFLOW_CUSTOMER_NETWORKS`, sinon chaque serveur contacté sur internet y apparaîtrait. |

**Un tableau vide a trois causes opposées** — collecteur coupé, rien qui parle, ou des flux
reçus dont on ne sait pas lire le modèle — et elles n'appellent pas le même geste.
`GET /api/v1/netflow/status` les distingue, et l'onglet *Trafic* affiche le diagnostic en
clair plutôt qu'un écran vide.

> **`orphan_records` qui monte puis se stabilise est normal.** En v9 et en IPFIX les
> données sont illisibles sans le modèle qui les décrit, et ce modèle arrive dans un
> datagramme séparé, réémis toutes les quelques minutes. Un compteur qui monte **sans
> cesse** veut dire que l'exporteur n'envoie jamais ses modèles (`template-refresh`).

---

## Qui se connecte à quoi (ipfinder)

NetFlow dit « 10.20.0.10 a échangé 4 Go avec 45.57.12.34 ». Personne ne sait de tête à qui
appartient 45.57.12.34 : **sans un nom, ce chiffre ne répond à aucune question
d'exploitation**. L'onglet *Services* met un nom sur l'autre bout — Netflix, YouTube,
Twitch, un CDN, un fournisseur de nuage — et c'est de là que se posent les restrictions.

> **Aucune inspection de contenu.** Le trafic est chiffré, il le reste. On ne regarde que
> l'adresse, son nom inverse et — si vous l'autorisez — ce que le registre en dit.

### Trois sources, une seule est gratuite

| Source | Ce qu'elle donne | Ce qu'elle coûte |
|---|---|---|
| **Catalogue embarqué** | Les blocs publiés par les opérateurs de service eux-mêmes (Netflix, Google, Twitch, Meta, les CDN…) | Rien. Instantané, et **fonctionne sur une VM coupée d'internet** |
| **Nom inverse (PTR)** | Suit un service qui **change de préfixe**, distingue YouTube du reste de Google, reconnaît un cache hébergé chez vous | Une requête DNS par adresse **nouvelle**, mise en cache ensuite |
| **RDAP** | Organisation, numéro d'AS, pays, bloc annoncé | Un appel HTTP sortant. **Coupé par défaut** |
| **Géolocalisation** | Pays, région, ville, coordonnées | Une base MaxMind **locale** si `IPFINDER_GEOIP_DB` la désigne (aucun appel sortant), sinon un service HTTP. **Coupé par défaut** |

La fiche d'une adresse donne : **domaine** (`wanadoo.fr` plutôt que
`lfbn-lyo-1-878-160.w86-194.abo.wanadoo.fr`, illisible), nom inverse complet, service
reconnu et *à quel titre*, organisation, AS, pays, ville, région, coordonnées, bloc
annoncé, volume, débit moyen, et la liste nominative de qui la joint.

> **RDAP et la géolocalisation sont actifs par défaut, et le compromis est réel.** Les
> interroger revient à **envoyer à un tiers les adresses que vos clients atteignent** —
> c'est une information sur eux. Deux façons de l'éviter : `IPFINDER_GEOIP_DB` pointant un
> fichier GeoLite2 (`pip install ".[geoip]"`) donne la même réponse **sans qu'aucun paquet
> ne sorte**, et `IPFINDER_GEOIP_ENABLED=false` / `IPFINDER_RDAP_ENABLED=false` coupe
> entièrement.
>
> Ce que NetFlow **ne peut pas** donner : le nom de domaine que le client a demandé. Un
> export de flux ne porte pas la requête DNS. Un ping vers `tatoulian.fr` se lit donc
> « 188.114.97.2, Cloudflare » — le nom inverse et le domaine décrivent l'hébergeur, pas
> le site visé.
>
> Chaque source est isolée : un résolveur qui casse, un registre qui limite le débit ou un
> service de localisation en panne ne coûte jamais le verdict que les autres ont rendu.

L'ordre de priorité n'est pas l'ordre du tableau : **le nom inverse l'emporte sur le bloc**.
Un cache Open Connect hébergé chez l'opérateur n'est dans aucun bloc publié — et c'est
justement le serveur qui porte le plus de trafic. Le champ `source` dit à quel titre
l'adresse a été nommée, et l'interface l'affiche à côté du nom.

### La découverte est dynamique, rien n'est à déclarer

1. Un client atteint une adresse. Le collecteur l'**inscrit** (`ip_intel`, `resolved_at`
   à NULL) — il ne résout rien lui-même : un résolveur lent ferait perdre des datagrammes,
   et un datagramme UDP perdu ne se rattrape pas.
2. Une boucle (`IPFINDER_INTERVAL_S`, 30 s par défaut) vient chercher un nom, par lots
   bornés, **les adresses les plus récentes d'abord**.
3. Si l'adresse relève d'un service restreint, la réconciliation l'ajoute à la liste posée
   sur le routeur au passage suivant. **Personne ne réécrit la règle.**

Une adresse **sans** nom inverse est quand même marquée résolue : « cette adresse n'a pas
de nom » est une réponse, et la majorité d'internet est dans ce cas. Sans cela, elle serait
redemandée à chaque passage, pour toujours (`IPFINDER_MAX_ATTEMPTS` borne les tentatives).

### Ce que l'onglet montre

> **Une machine sans fiche d'abonné compte aussi.** L'observation est « cette adresse a
> joint celle-là » ; le rattachement à un abonné est une *interprétation*, qui peut
> manquer (poste de supervision, caméra, routeur) ou changer (session PPPoE qui se
> reconnecte ailleurs). Exiger une fiche rendait invisible tout ce qui n'en a pas — à
> commencer par le ping qu'on lance pour vérifier que la mesure marche. La ligne apparaît
> alors sous l'adresse du client, marquée `non déclaré`.
>
> Le filtre reste `NETFLOW_CUSTOMER_NETWORKS` : sans lui, le trafic de transit ferait de
> ce tableau un annuaire d'internet. Si vos clients ont des adresses publiques, ajoutez
> leurs blocs à ce réglage.

| Bloc | Ce qu'il répond | D'où il vient |
|---|---|---|
| **Connexions en cours** | « Qu'est-ce que ce client fait *là, maintenant* » | L'agrégat **en mémoire** du collecteur : la seule vue réellement en direct. Elle se vide à chaque écriture de fenêtre puis se remplit — ce n'est pas une panne |
| **De quels services vient le trafic** | « Qui fait du streaming sur ce secteur » | La base, sur la période choisie |
| **Adresses atteintes** | La fiche complète d'une adresse : nom inverse, service **et à quel titre**, organisation, AS, pays, et **la liste nominative des abonnés qui la joignent** | La base + le catalogue, recalculé à la volée |

La part `non identifié` est affichée **comme les autres**. Une page qui ne montrerait que
ce qu'elle sait nommer laisserait croire que tout est reconnu, et la part réellement
inconnue — souvent la plus grosse — disparaîtrait de la discussion.

> **Un CDN n'est pas un service.** Cloudflare, Akamai et Fastly servent indifféremment un
> site de recettes, un catalogue vidéo et une mise à jour système. Ils portent la famille
> `cdn` et **jamais** `streaming` : une restriction posée dessus doit être un choix
> conscient, pas une surprise.

---

## Restrictions de trafic

Bloquer ou plafonner un trafic désigné par un **service** ou une **famille** — pour tous
les clients, ou pour certains.

```bash
# Plafonner le streaming à 3 Mbps pour deux clients
curl -s -X POST localhost:8000/api/v1/traffic-rules -H 'content-type: application/json' -d '{
  "name": "Streaming bridé - forfait éco",
  "action": "limit", "limit_down_mbps": 3,
  "categories": ["streaming"],
  "scope": "subscribers", "logins": ["dupont", "martin"]
}'

# Ce que la règle vise AUJOURD'HUI (elle grossit toute seule)
curl -s localhost:8000/api/v1/traffic-rules/1/preview

# Montrer le plan sans rien écrire, puis poser
curl -s -X POST localhost:8000/api/v1/traffic-rules/apply
curl -s -X POST 'localhost:8000/api/v1/traffic-rules/apply?dry_run=false'
```

### Une règle est un critère, pas une photo

L'ensemble d'adresses est **recalculé à chaque passage**, depuis deux sources qui se
complètent et dont aucune ne suffit :

- les **blocs publiés** couvrent les serveurs qu'aucun client n'a encore atteints. Sans
  eux, la toute première connexion vers chaque nouveau serveur passerait ;
- les **adresses découvertes** couvrent ce qui est *hors* des blocs publiés — un cache
  hébergé chez vous, un serveur loué chez un tiers. Sans elles, la règle laisserait passer
  exactement le trafic le plus volumineux.

Une adresse déjà contenue dans un bloc retenu n'est pas ajoutée : elle ne changerait rien
et ferait grossir une liste que le routeur parcourt **à chaque paquet**
(`RESTRICTION_ADDRESS_LIMIT` la borne).

### Ce qui est réellement posé sur le routeur

| Objet | Rôle |
|---|---|
| `/ip/firewall/address-list` | Les adresses du service visé. **Le seul objet qui bouge tout seul** : la réconciliation y pousse ce que NetFlow a découvert, et en retire ce qui n'en relève plus |
| `/ip/firewall/filter` × 2 | *Bloquer* : une règle `drop` **par sens**. Une seule laisserait passer le retour — pour du streaming, cela revient à ne rien bloquer |
| `/ip/firewall/mangle` + `/queue/tree` × 2 | *Plafonner* : marquage puis file accrochée à `global`. `/queue/simple` ne vise qu'une destination par file : plafonner cinquante blocs demanderait cinquante files par client |

**Deux limites assumées.** L'**IPv6 n'est pas posé** — `/ip/firewall/address-list` est une
table IPv4 ; les préfixes IPv6 d'une règle sont écartés et **le plan le dit** plutôt que de
faire semblant. Et rien qui ne porte pas `freeqos:managed` n'est touché : une règle de
pare-feu écrite par l'exploitant, une liste utilisée par son routage, le contrôleur ne les
lit même pas.

### Les garde-fous sont ceux des files, pas d'autres

- **Enregistrer une règle n'écrit rien.** La pose est un geste séparé. Croire qu'un trafic
  est bloqué alors qu'il ne l'est pas est l'erreur la plus coûteuse que ce produit puisse
  induire : la colonne *dernière pose* est la seule chose qui distingue une règle **saisie**
  d'une règle **posée**.
- L'écriture passe par `ShapingService.apply` : donc par `ENFORCEMENT_ENABLED`, par le
  coupe-circuit sur le nombre d'actions, et par l'audit `enforcement_audit`.
- **Une règle sans critère est refusée à la saisie.** Sans service, famille ni bloc, elle
  viserait tout internet — sur un routeur de sortie, l'appliquer couperait le réseau
  entier, et la règle aurait l'air parfaitement normale dans la liste.
- Suspendre une règle la **lève** réellement : elle n'est plus résolue, donc la
  réconciliation retire d'elle-même ce qu'elle avait posé.
- Supprimer une règle **ne nettoie pas le routeur en douce** : la réconciliation s'en charge
  au passage suivant, ou tout de suite si vous le demandez. Supprimer une fiche ne doit pas
  déclencher une écriture sur des équipements de production sans que personne ne l'ait
  demandée.

### Aucune règle ne se crée toute seule

Voir passer du streaming ne dit pas qu'il faut le brider : c'est une décision commerciale,
pas une déduction technique. Le contrôleur mesure, nomme et propose — il ne décide pas.

---

## Clients par VLAN : déclarés à la main

Un client sur VLAN routée **n'ouvre aucune session**, RADIUS ne le décrit pas, et rien sur
le réseau ne dit quel débit lui a été vendu. La saisie n'est pas un pis-aller en attendant
une intégration : **c'est la seule source qui existe**.

Ce que les flux — ou la table ARP — montrent, ce sont des **adresses qui parlent**. Une
imprimante, une caméra ou l'équipement d'un autre opérateur y ont exactement la même
apparence qu'un client professionnel. Une liste automatique donne donc l'illusion d'un
inventaire sans en être un, et fait perdre du temps à trier plutôt qu'à saisir.

Conséquences concrètes :

- `VLAN_DETECT_ENABLED` est **à `false` par défaut**. L'activer ajoute une lecture de
  `/ip/arp` par routeur, purement consultative.
- *Abonnés › Inventaire des clients à IP fixe* montre en tête **ce qui est déclaré**,
  rangé par VLAN (`GET /api/v1/static-clients/vlans`).
- Les adresses non rattachées sont reléguées dans un bloc replié, et dans l'onglet
  *Trafic*. **Déclarer** pré-remplit l'adresse et le VLAN ; la référence et le débit
  souscrit restent à saisir — eux, personne ne les devine.
- Aucune route ne « promeut » un candidat. Déclarer passe par `POST /api/v1/static-clients`.

---

## API publique : remplacer Preseem sans réécrire l'intégration

Ce qui coûte cher dans une bascule, ce n'est pas le contrôleur : c'est **tout ce qui lui
parle**. Splynx, UISP/UCRM, Powercode, Visp et les développements maison poussent déjà leur
inventaire vers l'API « model » de Preseem. freeQoS en **reprend la forme telle quelle** :
un intégrateur change l'URL de base et la clé, rien d'autre.

```bash
# Créer la clé : Réglages › Clés d'API (le secret n'est affiché qu'une fois)

# Un client, un forfait, un site, un secteur, une ligne vendue
curl -u "$CLE:" -X PUT https://freeqos.exemple.net/model/v1/accounts/cust-41 \
     -H 'Content-Type: application/json' \
     -d '{"name": "Mairie de Vitré"}'

curl -u "$CLE:" -X PUT https://freeqos.exemple.net/model/v1/packages/pack-100 \
     -H 'Content-Type: application/json' \
     -d '{"name": "100/20", "down_speed": 100000, "up_speed": 20000}'

curl -u "$CLE:" -X PUT https://freeqos.exemple.net/model/v1/sites/tour-nord \
     -H 'Content-Type: application/json' -d '{"name": "PoP Nord"}'

curl -u "$CLE:" -X PUT https://freeqos.exemple.net/model/v1/access_points/sect-n1 \
     -H 'Content-Type: application/json' \
     -d '{"name": "Secteur N1", "tower": "tour-nord", "ip_address": "10.10.5.2"}'

curl -u "$CLE:" -X PUT https://freeqos.exemple.net/model/v1/services/svc-4321 \
     -H 'Content-Type: application/json' \
     -d '{"account": "cust-41", "package": "pack-100",
          "parent_device_id": "sect-n1",
          "attachments": [{"cpe_mac": "00:10:0b:6e:4c:ff",
                           "network_prefixes": ["10.0.0.0/29"]}]}'

# La consommation, en octets
curl -u "$CLE:" 'https://freeqos.exemple.net/usage/v1/services?bucket=month'
```

| | |
|---|---|
| **Base** | `/model/v1` (référentiel) et `/usage/v1` (consommation) |
| **Authentification** | `Authorization: Basic base64(<clé>:)` — la clé tient lieu de nom d'utilisateur, mot de passe vide. `Bearer` et `X-API-Key` sont acceptés aussi. |
| **Collections** | `accounts`, `packages`, `sites`, `access_points`, `services` |
| **Méthodes** | `GET` (liste / fiche), `PUT /{id}` (crée ou remplace), `DELETE /{id}` |
| **Unités** | **kbit/s**, comme Preseem. La conversion vers les Mbit/s internes se fait à la frontière, et nulle part ailleurs. |
| **Portées** | `read` (les `GET`) et `write` (y ajoute `PUT` et `DELETE`) |

**Pourquoi `PUT` et pas `POST`.** La facturation est la source de vérité, et elle
resynchronise : elle doit pouvoir **rejouer son inventaire entier** sans se demander ce
qui existe déjà. `PUT` sur un identifiant choisi par l'appelant est la seule forme qui
rende ce rejeu inoffensif.

**Ce qui est refusé, et pourquoi.**

- L'identifiant de l'URI fait foi. Un corps qui en porte un autre est un **400** : deviner
  lequel des deux est le bon reviendrait à écrire au hasard sur le réseau d'un opérateur.
- Un service qui reprend l'identifiant d'une fiche **saisie à la main** est un **409**.
  L'API ne prend jamais la main sur un geste humain. L'inverse reste possible et assumé :
  un exploitant corrige à la main une fiche venue de l'API, et la synchronisation suivante
  le dira.
- Un service sans préfixe réseau est un **400** : sans adresse, rien ne peut être bridé, et
  accepter la fiche ferait croire à une ligne configurée.

**Où atterrit un service.** Dans `static_clients`, l'inventaire que l'interface montre déjà
— marqué `source='api'`. Pas de seconde table de clients : ce serait deux vérités sur le
même client, et une file sur le routeur qui ne saurait plus laquelle suivre. Le service est
**shapé dans la foulée** de son écriture, et la réponse porte `enforcement` : ce qui a été
écrit, ou ce qui l'en a empêché.

> Un service peut porter plusieurs préfixes. **Le premier est la cible de la file** (une
> file vise une cible) ; tous comptent dans la mesure de trafic.

**Les clés.** Le secret est tiré une fois, montré une fois, et la base n'en garde que
l'empreinte SHA-256 — y compris pour celui qui l'a créée. Une clé perdue se révoque et se
remplace. Un refus ne dit jamais *pourquoi* (inconnue, désactivée, expirée) : distinguer
les cas donnerait gratuitement un oracle à qui essaie des clés au hasard. Le journal, lui,
le dit.

---

## API

| Méthode | Chemin | Description |
|---|---|---|
| `GET` | `/health` | Liveness (le processus répond ; ne dépend ni de la base ni des collecteurs) |
| `GET` | `/health/ready` | Readiness : base + **fraîcheur de la donnée** (503 dès qu'un collecteur échoue durablement) |
| `GET` | `/api/v1/pops` | Liste des PoPs |
| `GET` | `/api/v1/subscribers` | Abonnés (filtres `pop_id`, `search`, `kind`) |
| `GET` | `/api/v1/subscribers/latest` | Dernier échantillon par abonné (top talkers ; filtre `kind`) |
| `GET` | `/api/v1/subscribers/{id}` | Fiche abonné |
| `GET` | `/api/v1/subscribers/{id}/metrics` | Série agrégée (`minutes`, `bucket_seconds`) |
| `GET` | `/api/v1/backhauls` · `/latest` · `/{id}/metrics` | Idem côté radio |
| `GET` | `/api/v1/overview` | Chiffres de tête du tableau de bord |
| `GET` | `/api/v1/throughput` | Débit agrégé du réseau dans le temps |
| `GET` | `/api/v1/bufferbloat` | Note de bufferbloat par abonné (latence à vide vs sous charge) |
| `GET` | `/api/v1/heatmap` | Heatmap exécutif : QoE / RTT / utilisation dans le temps |
| `GET` · `PUT` | `/api/v1/rtt` | Lire / basculer la sonde de latence (sans variable d'environnement) |
| `GET` | `/api/v1/network/tree` | Arbre PoP → backhauls, capacité et charge |
| `GET` | `/api/v1/capacity` | **Capacité** : survente par PoP, occupation des liens et heure de pointe, volumes consommés, lignes muettes |
| `GET` | `/api/v1/pops/health` | **Santé des routeurs** : CPU, mémoire, uptime, version — lu en direct |
| `GET` | `/api/v1/pops/routers` | Inventaire des routeurs (fichier + base) |
| `POST` | `/api/v1/pops/routers/test` | Teste une connexion **sans rien enregistrer** |
| `POST` | `/api/v1/pops/routers` | Enregistre un routeur |
| `PATCH` · `DELETE` | `/api/v1/pops/routers/{id}` | Modifie / retire un routeur |
| `POST` | `/api/v1/pops/routers/{id}/probe` | Teste un routeur enregistré |
| `GET` · `POST` | `/api/v1/static-clients` | Inventaire déclaratif des clients à IP fixe |
| `GET` | `/api/v1/static-clients/enforcement` | État réel de la file de chaque fiche, et le motif quand il n'y en a pas |
| `GET` | `/api/v1/pops/census` | **Recensement d'un PoP** : tous les clients localisés (sept sources), rapprochés de l'inventaire — lecture seule |
| `GET` | `/api/v1/static-clients/candidates/diagnostic` | Pourquoi une adresse n'est pas proposée (lecture seule, motif ligne par ligne) |
| `GET` | `/api/v1/static-clients/candidates` | Adresses détectées sur VLAN routée, non déclarées (consultation seule) |
| `PATCH` · `DELETE` | `/api/v1/static-clients/{id}` | Modifie / retire une fiche (l'historique de mesures est conservé) |
| `GET` | `/api/v1/topology` · `POST /topology/discover` | Graphe du réseau |
| `PATCH` | `/api/v1/topology/nodes/{key}` | Corriger le rôle d'un équipement |
| `PATCH` | `/api/v1/topology/nodes/{key}/layout` · `/parent` · `/visibility` | Position, rattachement forcé, masquage — arbre affiché seulement |
| `POST` · `DELETE` | `/api/v1/topology/links` · `/topology/links/{key}` | Créer / retirer un lien à la main (arbre affiché) |
| `POST` · `DELETE` | `/api/v1/topology/merge` · `/topology/merge/{alias_key}` | Fusion manuelle de deux cases (même équipement) / annulation |
| `POST` | `/api/v1/topology/forget-stale` | Oublier les cases plus revues depuis N minutes (`confirm=true`). Épargne toujours les routeurs déclarés et les liens posés à la main |
| `GET` | `/api/v1/topology/links/{key}/throughput` | Débit mesuré d'un lien + historique |
| `GET` | `/api/v1/topology/links/{key}/live` | Mesure instantanée (`/interface/monitor-traffic`) |
| `GET` | `/api/v1/topology/routers/{name}/export` | Config complète (`/export`) + son analyse |
| `GET` | `/api/v1/shaping/points` | **La carte du shaping** : où ça bride sur le réseau, à combien, et pourquoi pas ailleurs |
| `GET` | `/api/v1/shaping/state` | Ce qui est **déjà** configuré sur les routeurs |
| `PUT` · `DELETE` | `/api/v1/shaping/policies` | Fixer / retirer un débit imposé — **posé sur le routeur immédiatement** (`apply_now=false` pour seulement enregistrer) |
| `GET` | `/api/v1/shaping/limits` | **Les plafonds sont-ils réellement tenus ?** Vérification file par file sur le routeur |
| `POST` | `/api/v1/shaping/plan` | Commandes exactes, **sans rien envoyer** |
| `POST` | `/api/v1/shaping/apply` | Exécution (`dry_run` par défaut) |
| `GET` · `PUT` | `/api/v1/shaping/enforcement` | Lire / basculer l'autorisation d'écriture |
| `GET` · `POST` | `/api/v1/shaping/boosts` | Boosts en cours / en poser un |
| `DELETE` | `/api/v1/shaping/boosts/{login}` | Retirer un boost avant échéance |
| `DELETE` | `/api/v1/pops/{id}` | Retirer un site et tout son historique |
| `GET` | `/api/v1/shaping/audit` | Journal des commandes envoyées |
| `GET` | `/api/v1/static-clients/vlans` | **Clients déclarés, rangés par VLAN** + ce qui parle sans être déclaré |
| `GET` | `/api/v1/netflow/status` | État du collecteur de flux, et ce qui empêche de mesurer |
| `GET` · `POST` | `/api/v1/netflow/exporters` | Équipements qui exportent, et **d'où ils regardent** (`edge` / `pop`) |
| `PATCH` · `DELETE` | `/api/v1/netflow/exporters/{id}` | Corriger / retirer un exporteur |
| `GET` | `/api/v1/netflow/top` | Qui consomme, et combien, sur la période |
| `GET` | `/api/v1/netflow/applications` | Répartition du trafic par famille d'usage |
| `GET` | `/api/v1/netflow/subscribers/{id}/series` | Volume d'un abonné dans le temps |
| `GET` | `/api/v1/netflow/hosts` | Adresses vues, rattachées à **aucune** fiche (aide à la saisie) |
| `POST` | `/api/v1/netflow/flush` | Écrire la fenêtre en cours tout de suite |
| `GET` | `/api/v1/netflow/connections` | **Connexions clients en cours** : la fenêtre en mémoire, la seule vue en direct (filtre `app`) |
| `GET` | `/api/v1/netflow/pairs` | **Qui parle à qui** : une ligne par conversation, celles en cours marquées. Filtres `q`, `pop`, `category`, `app`, `service`, `client` |
| `GET` | `/api/v1/netflow/destinations` | Adresses atteintes sur la période, déjà nommées, + la répartition par service |
| `GET` | `/api/v1/netflow/destinations/{ip}` | **Fiche d'une adresse** : nom inverse, service et à quel titre, organisation, AS, pays, et qui la joint |
| `POST` | `/api/v1/netflow/destinations/{ip}/resolve` | Relancer l'analyse d'une adresse |
| `GET` | `/api/v1/netflow/catalogue` | Les services que le contrôleur sait reconnaître, et ce qu'il faut savoir avant de restreindre |
| `GET` | `/api/v1/netflow/intel` · `POST /intel/run` | État de l'identification / nommer les adresses en attente tout de suite |
| `GET` · `POST` | `/api/v1/traffic-rules` | **Restrictions de trafic** : lire / enregistrer une règle (**n'écrit rien sur les routeurs**) |
| `PATCH` · `DELETE` | `/api/v1/traffic-rules/{id}` | Modifier / suspendre / supprimer une restriction |
| `GET` | `/api/v1/traffic-rules/{id}/preview` | **Ce que la règle vise aujourd'hui** — elle grossit toute seule |
| `POST` | `/api/v1/traffic-rules/apply` | Poser les restrictions (`dry_run` par défaut) |
| `GET` | `/api/v1/netflow/export` | **Export NetFlow des routeurs** : qui exporte déjà, et vers quelle adresse |
| `POST` | `/api/v1/netflow/export/apply` | Poser `/ip/traffic-flow` et sa cible (`dry_run` par défaut) |
| `GET` · `POST` | `/api/v1/api-keys` | Clés d'API (le secret n'est rendu **qu'à la création**) |
| `PATCH` · `DELETE` | `/api/v1/api-keys/{id}` | Désactiver / révoquer une clé |
| `GET` | `/api/v1/status` · `/status/runs` · `/status/counters` | Exploitation |
| `POST` | `/api/v1/jobs/{job}/run` | Rejoue un cycle de **lecture** hors cadence |
| `GET` | `/` | Tableau de bord |

### API publique (clé requise) — contrat compatible Preseem

Volontairement **hors** du préfixe d'exploitation : ce chemin est le contrat que les
systèmes de facturation connaissent déjà, et le déplacer suffirait à casser la
compatibilité qui fait tout l'intérêt de ces routes.

| Méthode | Chemin | Description |
|---|---|---|
| `GET` | `/model/v1` | Collections disponibles — vérifie une clé d'un seul appel |
| `GET` | `/model/v1/{collection}` | Liste (`accounts`, `packages`, `sites`, `access_points`, `services`) |
| `GET` | `/model/v1/{collection}/{id}` | Fiche |
| `PUT` | `/model/v1/{collection}/{id}` | Crée ou remplace (idempotent). Un service est **shapé dans la foulée** |
| `DELETE` | `/model/v1/{collection}/{id}` | Retire |
| `GET` | `/usage/v1/services` | Consommation de tous les services, en octets (`bucket=total\|hour\|day\|month`) |
| `GET` | `/usage/v1/services/{id}` | Consommation d'un service |

Documentation interactive : `/docs`.

---

## Modèle de données

**Référentiel** — `pops`, `subscribers` (identité unique `login`, `kind`, plan, PoP,
`last_seen`), `static_clients` (inventaire déclaratif des clients à IP fixe),
`vlan_sightings` (présence observée par le recensement du PoP — ARP, baux DHCP, table de
ponts, routes, files : confirme un client déclaré, ou produit un candidat à déclarer),
`backhauls` (PoP, `uisp_device_id`, capacité nominale), `routers` (PoPs ajoutés depuis
l'interface, mot de passe chiffré, `loopback` unique qui identifie le routeur dans la
topologie, diagnostic de la dernière connexion),
`topology_nodes` / `topology_links` (graphe découvert ; `config_parent` porte le
rattachement prouvé par la table de routage ; `pos_x`/`pos_y`, `parent_override`
et `hidden` portent la disposition posée à la main dans l'éditeur d'arbre),
`subscriber_attachments`
(abonné → secteur radio), `shaping_policies` (débits imposés à la main),
`enforcement_audit` (journal des commandes envoyées), `runtime_flags` (drapeaux
basculables à chaud, dont l'autorisation d'écriture), `traffic_rules` (les restrictions
telles qu'elles sont **saisies** : un critère — un service, une famille, des blocs — et
jamais une liste d'adresses figée).

Le schéma comporte une section de **migrations de colonnes** (`ADD COLUMN IF NOT EXISTS`) :
`CREATE TABLE IF NOT EXISTS` ne touche pas une table déjà présente, une installation
existante ne recevrait donc jamais les colonnes ajoutées après coup. La colonne
`subscribers.pppoe_login` y est renommée en `login` (renommage gardé par une double
condition, donc rejouable) : elle devenait un mensonge dès qu'un client à IP fixe
entrait dans la table.

**Séries temporelles** (hypertables) :

| Table | Contenu |
|---|---|
| `subscriber_metrics` | `ts`, `subscriber_id`, `rx_bps`, `tx_bps`, `rx_bytes`, `tx_bytes`, `rtt_ms` (si la sonde est active), `session_uptime_s` — alimentée par `/ppp/active` pour un abonné PPPoE, par les compteurs de sa file pour un client à IP fixe |
| `backhaul_metrics` | `ts`, `backhaul_id`, capacité (globale/down/up), `signal_dbm`, `airtime_pct`, MCS, `online` |
| `interface_metrics` | `ts`, `router_name`, `interface`, débits et compteurs du port, `running`, `capacity_mbps` — la source du débit des liens |
| `qoe_scores` | `ts`, `subscriber_id`, `score`, `components` — phase 3 |
| `flow_metrics` · `flow_app_metrics` | Volume par abonné et par usage, **avec le point de mesure dans la clé** |

**Ce que les clients atteignent**, en dehors des hypertables parce que la question posée
n'est pas « combien d'octets à la minute près vers ce serveur » mais « qu'est-ce que cet
abonné atteint, et depuis quand » :

| Table | Contenu | Durée de vie |
|---|---|---|
| `flow_destinations` | Le couple (**adresse du client**, adresse atteinte) : volumes cumulés, dernier port et protocole vus, `first_seen`/`last_seen`. `subscriber_id` est *nullable* — une machine sans fiche est mesurée comme les autres | **Mesure** : purgée par `NETFLOW_DESTINATION_RETENTION_S` |
| `ip_intel` | Ce qu'on sait de l'adresse : nom inverse, service, famille, organisation, AS, pays, et **à quel titre** on le sait | **Connaissance** : conservée. Réapprendre à chaque purge que 45.57.12.34 est Netflix serait une requête DNS pour rien |

Une ligne `ip_intel` est créée avec `resolved_at` à NULL **au moment où un client atteint
l'adresse** : c'est la file d'attente de l'identification, et c'est ce qui rend la
découverte dynamique sans que personne n'ait rien à déclarer.

Deux vues, `subscriber_latest` et `backhaul_latest`, donnent le dernier point par série.

Compression (`compress_segmentby` par série) et rétention sont configurables
(`COMPRESSION_AFTER_DAYS`, `RETENTION_DAYS`, `CHUNK_INTERVAL_HOURS`) et appliquées au
démarrage. Les agrégations utilisent `date_bin` plutôt que `time_bucket` : résultat
identique sur hypertable, mais les mêmes requêtes fonctionnent sur un PostgreSQL sans
Timescale (CI, poste de dev). Le schéma dégrade d'ailleurs proprement en tables classiques
si l'extension est absente.

---

## Tests

```bash
make test        # 1334 tests, dont 1244 sans aucune infrastructure
```

Tout est mocké derrière des `Protocol` : faux routeur RouterOS (tables `/ppp/active` et
`/interface` réalistes, avec scénarios de reconnexion), `MockTransport` httpx pour UISP,
providers simulés, writer et référentiel en mémoire, horloge injectable.

Couverture notable : corrélation d'interface (y compris ambiguë), reconnexion PPPoE,
débits aberrants, isolation des pannes multi-routeurs, non-chevauchement du scheduler,
non-divulgation des secrets par l'API, absence de dépendance CDN dans l'UI, chiffrement
des identifiants, fusion et rechargement à chaud de l'inventaire, refus d'écrire un
secret sans clé de chiffrement, tourniquet et péremption des mesures de latence.

Côté enforcement, la couverture porte d'abord sur ce qui doit **empêcher** une écriture :
refus quand `ENFORCEMENT_ENABLED` est faux, absence de repli sur le compte de lecture,
files tierces jamais modifiées ni supprimées, coupe-circuit sur les gros plans, arrêt au
premier échec, idempotence du plan (rejouer ne produit rien), refus d'un boost sans
échéance, retour automatique au plan après expiration, et lecture des droits réels du
compte (y compris le cas indéterminable, qui ne doit pas bloquer), conversion des unités
et idempotence sur les petits débits (RouterOS relit `512k` là où on a écrit `512000`).

Côté **services atteints et restrictions**, la couverture porte sur les quatre façons de
se tromper qui coûtent le plus cher : nommer une adresse à tort (le suffixe de nom inverse
doit tomber sur une frontière de label, un CDN ne devient jamais « streaming » tout seul),
prendre l'infrastructure pour une destination (deux abonnés qui se parlent n'en sont une
pour personne), poser une règle dont le sens est inversé — elle s'afficherait comme posée
sans jamais rencontrer un paquet — et laisser une restriction figée : une adresse
nouvellement découverte doit **entrer** dans la liste du routeur, et une adresse qui n'en
relève plus doit en **sortir**.

### Tests d'intégration (optionnels)

Ils valident le SQL réel — schéma, vues, `date_bin`, upserts, cascade — y compris un test
bout en bout « faux routeur → service → base → API ». Ils tournent aussi bien sur
TimescaleDB que sur PostgreSQL nu :

```bash
export TEST_DATABASE_URL=postgresql://qos:qos@localhost:5432/qos_test
pytest tests/test_db_integration.py
```

Sans cette variable ils sont ignorés.

---

## Lab EVE-NG

Tout est prévu pour un lab CHR RouterOS 7.21.5 : IP, ports et identifiants sont en
configuration, aucune valeur codée en dur. Démarche conseillée :

1. `BACKHAUL_PROVIDER=mock`, `PLAN_PROVIDER=mock` — valider la chaîne complète sans radio.
2. Brancher un premier CHR : créer `qos-ro`, le déclarer dans `config/routers.yml`,
   ouvrir une session PPPoE, vérifier `/api/v1/subscribers/latest`.
3. Ajouter les autres PoPs, vérifier l'isolation en coupant volontairement un routeur
   (`/api/v1/status` doit montrer l'erreur sans perdre les autres).
4. Basculer `BACKHAUL_PROVIDER=uisp` quand l'API UISP est joignable.

---

## Non-objectifs assumés

- Aucun code sur le chemin des paquets.
- La radio n'est **jamais** pilotée : sa capacité est lue, point. L'écriture ne concerne
  que les files RouterOS.
- Aucune écriture RADIUS (CoA) : seule l'interface est posée.
- La boucle locale rapide du PoP n'est pas implémentée ici : cette application fixe les
  baselines que cette boucle respectera. La boucle fermée QoE de la phase 4 **n'y change
  rien** : elle tourne à l'échelle de la minute, sur des fenêtres de plusieurs minutes, et
  déplace des baselines de secteur. Rien en dessous de la seconde n'a sa place ici.
