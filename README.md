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
  mesuré** (couleur de charge), et chaque PoP ses abonnés (nœud agrégé avec le débit total).
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
  révèle que la MAC de l'interface en face). Un PoP **injoignable** reste affiché (badge
  *injoignable*) plutôt que de disparaître silencieusement. Quand l'automatique ne peut pas
  *prouver* l'identité (nom générique « MikroTik », pas de MAC commune), l'opérateur tranche
  à la main : *Même équipement que…* replie une case sur une autre, *Séparer* défait la
  fusion. L'arbre **signale aussi les doublons probables** — deux cases aux mêmes mots-clés
  dans un ordre différent (« CCR DS » / « DS-CCR ») — avec un bouton *Fusionner* en un clic ;
  il ne les fusionne pas d'office, car deux bouts d'un même lien peuvent être deux vrais
  routeurs. Rôles, position, rattachements, liens et fusions manuels sont enregistrés, mais ne
  changent que l'arbre **affiché** — aucun équipement n'est reconfiguré.
- **Abonnés** — sessions filtrables **par PoP** et par login, avec débit vs plan, latence,
  **note de bufferbloat** (latence sous charge), boost en cours et son décompte. Un clic
  ouvre la série de l'abonné ; les boutons *Débit* et *Boost* agissent directement.
- **Topologie** — le tableau technique des liens : **débit mesuré**, charge vs capacité du
  port, capacité négociée et débit imposé. Le bouton *Débit* ouvre l'historique d'un lien
  et permet une mesure instantanée ; *Bande passante* enregistre une intention de shaping.
- **Équipements** — ajout d'un routeur ou d'une antenne **via leur API** ; chaque ajout
  **analyse la configuration et (re)construit l'arbre tout seul**. Inventaire des sites et
  routeurs en bas de page.
- **Connexion à distance** — vue façon LibreQoS de toutes les intégrations distantes
  (RouterOS, airOS Ubiquiti, UISP, FreeRADIUS) : joignabilité par famille d'API et détail
  par équipement. En lecture seule.

Aucune dépendance externe : ni framework, ni CDN, ni chaîne de build. Les graphes sont du
SVG généré à la main, pour que le contrôleur reste utilisable sur une VM de management
coupée d'internet.

### Comprendre la topologie : quel lien va où

C'est la question qui conditionne tout le reste — sans elle, impossible de savoir quel
backhaul un abonné traverse, donc quelle file doit être son parent.

#### La topologie se découvre toute seule

Les onglets **Topologie** et **Arbre réseau** lisent le graphe en base. Ce graphe
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
| `/ip/arp` (VLAN sans PPPoE) | présence d'une adresse **non identifiée** : confirme un client déclaré, ou propose un candidat. La seule source qui ne dit pas *qui* est en face |

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
prime sur la détection.

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

**Autoriser l'écriture.** L'interrupteur de l'onglet Shaping bascule
`ENFORCEMENT_ENABLED` **sans redémarrage** : la variable d'environnement ne sert plus
qu'à l'amorçage, ensuite c'est la base qui fait foi et la bascule survit au redémarrage.
Activer demande une confirmation et un motif, tracé dans le journal ; couper est immédiat
et sans cérémonie. `ENFORCEMENT_LOCKED=true` interdit la bascule depuis l'interface, pour
qui préfère garder la friction du redémarrage.

**Le parcours, en trois temps volontairement séparés :**

```
GET  /api/v1/shaping/state    ce qui est DÉJÀ configuré sur le routeur
POST /api/v1/shaping/plan     ce qu'il faudrait changer, commandes exactes
POST /api/v1/shaping/apply    exécution — dry_run:true par défaut
```

Dans l'interface : onglet **Shaping** → *Analyser l'existant* → *Calculer le plan* →
*Appliquer*. Le plan affiche chaque commande RouterOS telle qu'elle sera envoyée, avec sa
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
# → /queue/simple/… max-limit=128000/512000
```

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
L'onglet Shaping → *Analyser l'existant* affiche le verdict lu sur le routeur : le compte
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

**Rattachement topologique déclaré.** Aucun `caller-id` n'existe pour ces clients : la
jointure MAC ↔ station UISP ne peut pas les rattacher. Le secteur se saisit dans la
fiche. Ils apparaissent alors dans l'arbre avec leur propre nature (`static`, pas
`cpe` : ce n'est pas un équipement observé), ce qui les fait **compter dans le partage
d'un lien congestionné** — sans quoi les abonnés PPPoE du même secteur se feraient
rogner à leur place.

**Détection assistée : le contrôleur signale, l'humain décide.** Attendre qu'on
saisisse un client à l'aveugle est une mauvaise façon de travailler — encore
faut-il savoir qu'il est là. RouterOS n'a aucune table qui liste « les VLAN
clientes » (la notion n'existe pas dans sa configuration), mais il a un signal
de présence fiable : la table ARP. Un job périodique lit `/ip/arp`, ne garde que
les interfaces de `/interface/vlan` qui **n'hébergent pas** de serveur PPPoE, et
en tire deux lectures :

| Ce que l'ARP montre | Ce qu'on en fait |
|---|---|
| une adresse **dans le bloc** d'un client déclaré | confirme sa présence — colonne « Vu actif » |
| une adresse qui **ne correspond à rien** | **candidat**, listé dans l'onglet Abonnés → « Détectés, non déclarés » |

Le rapprochement se fait par **contenance réseau** (`<<=`), pas par égalité : un
client déclaré en `/29` est reconnu quand n'importe laquelle de ses adresses
parle.

**Un candidat n'est pas un client, et rien ne peut le transformer tout seul.**
Une imprimante, une caméra ou l'équipement d'un autre opérateur laissent
exactement la même trace ARP. Trois garanties, chacune vérifiée par un test :

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

**Limite à connaître : la détection ne voit que l'adressage porté par une
`/interface/vlan`.** Un client n'est proposé en candidat que si son entrée ARP
est rattachée à une interface déclarée dans `/interface/vlan`. Sur un **pont en
filtrage VLAN** qui porte lui-même l'adressage client, `/ip/arp` nomme le pont —
et le client reste invisible. Ce n'est pas une panne, c'est le périmètre actuel.

Plutôt que de laisser deviner, le filtre **s'explique** : *Abonnés → Inventaire →
« Un client manque ? Voir pourquoi »* lit `/ip/arp` en direct et rend le motif de
chaque ligne écartée, avec le champ qui répond presque toujours —
`interfaces_hors_vlan`, les interfaces vues dans ARP mais absentes de
`/interface/vlan`. Une interface qui y apparaît avec plusieurs adresses est la
réponse.

Les motifs sont distincts, parce qu'ils appellent des gestes différents : VLAN
absente (chercher où est l'adressage), VLAN déclarée mais **désactivée** (la
réactiver), interface hébergeant un **serveur PPPoE** (rejet voulu, ces abonnés
ont déjà une identité), adresse **sans MAC** (cherchée, pas répondue).

Le diagnostic emprunte exactement le même chemin de décision que la détection
(`judge_arp_rows`) : un diagnostic qui raconterait autre chose que ce que fait
le code serait pire que pas de diagnostic.

**Limite à connaître : la mesure dépend de la file.** Sans session PPPoE, aucune
interface ne porte le trafic de ce client ; le seul compteur par client dont on dispose
est celui de la file qui le vise (`/queue/simple`). Tant qu'aucune file n'existe sur
son adresse — enforcement désactivé, ou premier cycle — le client apparaît avec son
plan et son état, mais **sans débit**. Un trou est plus honnête qu'un zéro, qui se
lirait comme une absence de trafic. Une file posée à la main par l'opérateur est lue
aussi, si elle vise la même adresse.

```bash
# Déclarer un client à IP fixe
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
| `GET` | `/api/v1/remote/status` | État des connexions distantes par intégration (RouterOS, airOS, UISP, RADIUS) |
| `GET` | `/api/v1/pops/routers` | Inventaire des routeurs (fichier + base) |
| `POST` | `/api/v1/pops/routers/test` | Teste une connexion **sans rien enregistrer** |
| `POST` | `/api/v1/pops/routers` | Enregistre un routeur |
| `PATCH` · `DELETE` | `/api/v1/pops/routers/{id}` | Modifie / retire un routeur |
| `POST` | `/api/v1/pops/routers/{id}/probe` | Teste un routeur enregistré |
| `GET` · `POST` | `/api/v1/static-clients` | Inventaire déclaratif des clients à IP fixe |
| `GET` | `/api/v1/static-clients/candidates/diagnostic` | Pourquoi une adresse n'est pas proposée (lecture seule de `/ip/arp`) |
| `GET` | `/api/v1/static-clients/candidates` | Adresses détectées sur VLAN routée, non déclarées (consultation seule) |
| `PATCH` · `DELETE` | `/api/v1/static-clients/{id}` | Modifie / retire une fiche (l'historique de mesures est conservé) |
| `GET` | `/api/v1/topology` · `POST /topology/discover` | Graphe du réseau |
| `PATCH` | `/api/v1/topology/nodes/{key}` | Corriger le rôle d'un équipement |
| `PATCH` | `/api/v1/topology/nodes/{key}/layout` · `/parent` · `/visibility` | Position, rattachement forcé, masquage — arbre affiché seulement |
| `POST` · `DELETE` | `/api/v1/topology/links` · `/topology/links/{key}` | Créer / retirer un lien à la main (arbre affiché) |
| `POST` · `DELETE` | `/api/v1/topology/merge` · `/topology/merge/{alias_key}` | Fusion manuelle de deux cases (même équipement) / annulation |
| `GET` | `/api/v1/topology/links/{key}/throughput` | Débit mesuré d'un lien + historique |
| `GET` | `/api/v1/topology/links/{key}/live` | Mesure instantanée (`/interface/monitor-traffic`) |
| `GET` | `/api/v1/topology/routers/{name}/export` | Config complète (`/export`) + son analyse |
| `GET` | `/api/v1/shaping/state` | Ce qui est **déjà** configuré sur les routeurs |
| `PUT` · `DELETE` | `/api/v1/shaping/policies` | Fixer / retirer un débit imposé |
| `POST` | `/api/v1/shaping/plan` | Commandes exactes, **sans rien envoyer** |
| `POST` | `/api/v1/shaping/apply` | Exécution (`dry_run` par défaut) |
| `GET` · `PUT` | `/api/v1/shaping/enforcement` | Lire / basculer l'autorisation d'écriture |
| `GET` · `POST` | `/api/v1/shaping/boosts` | Boosts en cours / en poser un |
| `DELETE` | `/api/v1/shaping/boosts/{login}` | Retirer un boost avant échéance |
| `DELETE` | `/api/v1/pops/{id}` | Retirer un site et tout son historique |
| `GET` | `/api/v1/shaping/audit` | Journal des commandes envoyées |
| `GET` | `/api/v1/status` · `/status/runs` · `/status/counters` | Exploitation |
| `POST` | `/api/v1/jobs/{job}/run` | Rejoue un cycle de **lecture** hors cadence |
| `GET` | `/` | Tableau de bord |

Documentation interactive : `/docs`.

---

## Modèle de données

**Référentiel** — `pops`, `subscribers` (identité unique `login`, `kind`, plan, PoP,
`last_seen`), `static_clients` (inventaire déclaratif des clients à IP fixe),
`vlan_sightings` (présence observée dans `/ip/arp` : confirme un client déclaré, ou
produit un candidat à déclarer),
`backhauls` (PoP, `uisp_device_id`, capacité nominale), `routers` (PoPs ajoutés depuis
l'interface, mot de passe chiffré, `loopback` unique qui identifie le routeur dans la
topologie, diagnostic de la dernière connexion),
`topology_nodes` / `topology_links` (graphe découvert ; `config_parent` porte le
rattachement prouvé par la table de routage ; `pos_x`/`pos_y`, `parent_override`
et `hidden` portent la disposition posée à la main dans l'éditeur d'arbre),
`subscriber_attachments`
(abonné → secteur radio), `shaping_policies` (débits imposés à la main),
`enforcement_audit` (journal des commandes envoyées), `runtime_flags` (drapeaux
basculables à chaud, dont l'autorisation d'écriture).

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
make test        # 803 tests, dont 742 sans aucune infrastructure
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
