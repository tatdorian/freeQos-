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
| **2** | Topologie, analyse de l'existant, files CAKE par abonné et parent backhaul | **fait** |
| 3 | Score QoE (latence **sous charge**) | RTT collecté, corrélation au débit à faire |
| 4 | Boucle fermée (ajustement selon QoE + capacité radio) | à venir |

L'enforcement existe désormais, mais reste **désactivé par défaut** : `ENFORCEMENT_ENABLED`
doit être passé à `true` explicitement, et chaque plan demande une application distincte.

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
- **Arbre réseau** — la vraie hiérarchie `gateway → cœur → PoP → radio → abonnés`,
  repliable, reconstruite depuis `/ip/neighbor`. La découverte de voisinage étant
  **symétrique**, le sens amont/aval est déduit du rôle de chaque équipement : un PoP qui
  voit son gateway produirait sinon un gateway *sous* le PoP. Les abonnés sont regroupés
  sous un nœud repliable, avec leurs totaux. **Chaque lien porte son débit mesuré** et sa
  charge vs la capacité du port ; le bouton *Débit* ouvre son historique.
- **Abonnés** — sessions filtrables **par PoP** et par login, avec débit vs plan, latence,
  boost en cours et son décompte. Un clic ouvre la série de l'abonné ; les boutons *Débit*
  et *Boost* agissent directement.
- **Topologie** — le graphe par rôle, corrigeable à la main, et le tableau des liens avec
  leur **débit mesuré**, leur charge et leur capacité.
- **PoPs** — inventaire et connexion d'un routeur.

Aucune dépendance externe : ni framework, ni CDN, ni chaîne de build. Les graphes sont du
SVG généré à la main, pour que le contrôleur reste utilisable sur une VM de management
coupée d'internet.

### Comprendre la topologie : quel lien va où

C'est la question qui conditionne tout le reste — sans elle, impossible de savoir quel
backhaul un abonné traverse, donc quelle file doit être son parent.

**Cinq sources, réconciliées** :

| Source | Ce qu'elle apporte |
|---|---|
| `/ip/neighbor` | **source maîtresse** : MNDP, LLDP et CDP. Pour chaque interface locale, l'équipement d'en face (identité, plateforme, MAC, IP) |
| `/interface/ethernet` | débit négocié = plafond physique du lien |
| `/ip/address` | segment L3 auquel appartient le lien |
| UISP `/devices` | liens radio PtP/PtMP, capacité du moment, rattachement station → AP |
| `/ppp/active` → `caller-id` | **la jointure clé** : la MAC du CPE de l'abonné |
| `/interface` `rx-byte`/`tx-byte` | **le débit réellement mesuré** sur le port qui porte le lien |

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
| Latence **sous charge** | mesurée en continu sur le trafic réel | à dériver en corrélant RTT et débit (phase 3) |

Autrement dit : tout ce qui se lit dans des compteurs est à parité. Tout ce qui exige
d'inspecter les paquets ne l'est pas, et ne le sera jamais depuis une VM de management —
c'est le prix du hors-bande, pas une lacune d'implémentation.

La sonde RTT est **désactivée par défaut** (`RTT_ENABLED=false`) : elle consomme du CPU
routeur, contrairement à la mesure passive. Une fois activée, elle sonde un lot d'abonnés
par cycle en tourniquet, et la mesure est rattachée à l'échantillon de débit suivant —
une seule ligne par abonné et par cycle, pas de lignes ne portant qu'un RTT. Un abonné
qui bloque l'ICMP reste à `NULL` : pas de valeur inventée.

```bash
RTT_ENABLED=true
RTT_INTERVAL_S=30      # période de sondage
RTT_BATCH_SIZE=20      # abonnés sondés par cycle
```

Le compte `qos-ro` doit posséder la politique `test` (elle est dans le groupe recommandé
plus bas).

### Développement

```bash
pip install -e ".[dev]"
make test      # 129 tests, ni base ni routeur requis
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
│   ├── shaping.py       Découverte, analyse de l'existant, plan, application
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
    uisp_device_id: 8a2f1c3e-…
    nominal_capacity_mbps: 500
```

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

## API

| Méthode | Chemin | Description |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/health/ready` | Readiness : base + fraîcheur des cycles (503 si dégradé) |
| `GET` | `/api/v1/pops` | Liste des PoPs |
| `GET` | `/api/v1/subscribers` | Abonnés (filtres `pop_id`, `search`) |
| `GET` | `/api/v1/subscribers/latest` | Dernier échantillon par abonné (top talkers) |
| `GET` | `/api/v1/subscribers/{id}` | Fiche abonné |
| `GET` | `/api/v1/subscribers/{id}/metrics` | Série agrégée (`minutes`, `bucket_seconds`) |
| `GET` | `/api/v1/backhauls` · `/latest` · `/{id}/metrics` | Idem côté radio |
| `GET` | `/api/v1/overview` | Chiffres de tête du tableau de bord |
| `GET` | `/api/v1/throughput` | Débit agrégé du réseau dans le temps |
| `GET` | `/api/v1/network/tree` | Arbre PoP → backhauls, capacité et charge |
| `GET` | `/api/v1/pops/routers` | Inventaire des routeurs (fichier + base) |
| `POST` | `/api/v1/pops/routers/test` | Teste une connexion **sans rien enregistrer** |
| `POST` | `/api/v1/pops/routers` | Enregistre un routeur |
| `PATCH` · `DELETE` | `/api/v1/pops/routers/{id}` | Modifie / retire un routeur |
| `POST` | `/api/v1/pops/routers/{id}/probe` | Teste un routeur enregistré |
| `GET` | `/api/v1/topology` · `POST /topology/discover` | Graphe du réseau |
| `PATCH` | `/api/v1/topology/nodes/{key}` | Corriger le rôle d'un équipement |
| `GET` | `/api/v1/topology/links/{key}/throughput` | Débit mesuré d'un lien + historique |
| `GET` | `/api/v1/topology/links/{key}/live` | Mesure instantanée (`/interface/monitor-traffic`) |
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

**Référentiel** — `pops`, `subscribers` (login PPPoE unique, plan, PoP, `last_seen`),
`backhauls` (PoP, `uisp_device_id`, capacité nominale), `routers` (PoPs ajoutés depuis
l'interface, mot de passe chiffré, diagnostic de la dernière connexion),
`topology_nodes` / `topology_links` (graphe découvert), `subscriber_attachments`
(abonné → secteur radio), `shaping_policies` (débits imposés à la main),
`enforcement_audit` (journal des commandes envoyées), `runtime_flags` (drapeaux
basculables à chaud, dont l'autorisation d'écriture).

Le schéma comporte une section de **migrations de colonnes** (`ADD COLUMN IF NOT EXISTS`) :
`CREATE TABLE IF NOT EXISTS` ne touche pas une table déjà présente, une installation
existante ne recevrait donc jamais les colonnes ajoutées après coup.

**Séries temporelles** (hypertables) :

| Table | Contenu |
|---|---|
| `subscriber_metrics` | `ts`, `subscriber_id`, `rx_bps`, `tx_bps`, `rx_bytes`, `tx_bytes`, `rtt_ms` (si la sonde est active), `session_uptime_s` |
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
make test        # 363 tests, dont 339 sans aucune infrastructure
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
  baselines que cette boucle respectera.
