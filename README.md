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
export MT_POP_NORD_PASSWORD='...'          # jamais dans un fichier versionné

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

Prérequis, une seule fois :

```bash
python -m app.services.crypto     # génère une clé Fernet
# la coller dans APP_SECRET_KEY du .env, puis redémarrer
```

Sans cette clé l'API **refuse** d'enregistrer un PoP et le dit dans l'interface : elle
n'écrira jamais un mot de passe de routeur en clair dans PostgreSQL. Les mots de passe
enregistrés sont chiffrés au repos ; la clé, elle, reste dans l'environnement.

Les deux inventaires coexistent :

| Source | Secrets | Modifiable dans l'UI |
|---|---|---|
| `config/routers.yml` (fichier) | variables d'environnement | non — signalé « édité dans routers.yml » |
| Interface | chiffrés en base (Fernet) | oui — tester / désactiver / retirer |

En cas d'homonymie **le fichier gagne** : une déclaration versionnée et revue prime sur
une saisie au clavier.

### Interface d'administration

Quatre vues, thème sombre, à `http://localhost:8000/` :

- **Tableau de bord** — débit global (download/upload en miroir, graphe live), abonnés en
  ligne, débit vendu et son taux d'utilisation, capacité backhaul, top consommateurs avec
  barre d'usage vs plan, cartes backhaul (capacité vs nominal, charge vs capacité).
- **Arbre réseau** — PoP → backhaul, charge réelle face à la capacité radio du moment.
  C'est ce rapport qui déterminera le débit parent du shaping en phase 2.
- **Abonnés** — table filtrable ; un clic ouvre la série de débit de l'abonné.
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

### Piloter les files

**Le principe.** Preseem et LibreQoS reposent sur la même idée : pour qu'une gestion de
file serve à quelque chose, il faut que **le goulot soit chez nous**. On shape donc
légèrement **sous** la capacité réelle du lien (`SHAPING_SAFETY_FACTOR`, 90 % par défaut),
pour que la file se forme dans CAKE — où on la contrôle — plutôt que dans le buffer de la
radio, où on ne peut rien.

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
/queue/simple/add name=freeqos-dupont target="<pppoe-dupont>" max-limit=20000000/100000000 …
```

**Pour changer une bande passante** : cliquer *Bande passante* sur un lien (onglet
Topologie) ou *Débit* sur un abonné. Enregistrer **n'écrit rien sur le routeur** — cela
enregistre l'intention. Le plan montre ensuite ce qui en découle.

**Cinq garde-fous, dans cet ordre :**

1. **Marquage de propriété.** Seules les files portant `comment=freeqos:managed` sont
   modifiées ou supprimées. Une file posée à la main ou par RADIUS n'est **jamais** touchée ;
   si son nom entre en collision avec un nom voulu, le plan signale un conflit et s'abstient.
2. **Plan avant exécution.** Le planificateur est une fonction pure : il produit des
   commandes comme données. Rien ne part tant qu'on n'a pas appliqué.
3. **`dry_run` par défaut**, et l'application réelle exige `confirm: true`.
4. **`ENFORCEMENT_ENABLED`**, le drapeau global : à `false`, aucune écriture ne part,
   quelle que soit la confirmation.
5. **Comptes séparés.** L'écriture passe par `qos-rw`, jamais `qos-ro`, sans repli
   possible. Ne pas déclarer `rw_username` met un PoP hors de portée de toute écriture.

Plus un **coupe-circuit** : au-delà de `ENFORCEMENT_MAX_ACTIONS` (500), le plan est refusé —
un plan anormalement gros signale presque toujours un état désiré mal calculé.

Toute commande envoyée, y compris simulée, est journalisée dans `enforcement_audit` et
visible dans l'interface.

**Compte d'écriture RouterOS :**

```
/user group add name=qos-rw policy=read,write,api,test
/user add name=qos-rw group=qos-rw password=…
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
ROUTERS='[{"name":"pop-nord","host":"10.10.0.11","password_env":"MT_POP_NORD_PASSWORD"}]'
```

```yaml
# config/routers.yml
routers:
  - name: pop-nord
    host: 10.10.0.11
    port: 8728                # 8728 API binaire, 8729 api-ssl
    username: qos-ro          # LECTURE SEULE en phase 1
    password_env: MT_POP_NORD_PASSWORD   # nom de la variable, jamais le secret
    role: pop                 # pop | core | gateway
    pop_name: PoP Nord
    pppoe_interface_pattern: "<pppoe-{login}>"
    rw_username: qos-rw       # phase 2, non utilisé aujourd'hui
    rw_password_env: MT_POP_NORD_RW_PASSWORD

backhauls:
  - name: bh-nord-pri
    pop_name: PoP Nord
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
| `GET` | `/api/v1/shaping/state` | Ce qui est **déjà** configuré sur les routeurs |
| `PUT` · `DELETE` | `/api/v1/shaping/policies` | Fixer / retirer un débit imposé |
| `POST` | `/api/v1/shaping/plan` | Commandes exactes, **sans rien envoyer** |
| `POST` | `/api/v1/shaping/apply` | Exécution (`dry_run` par défaut) |
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
`enforcement_audit` (journal des commandes envoyées).

**Séries temporelles** (hypertables) :

| Table | Contenu |
|---|---|
| `subscriber_metrics` | `ts`, `subscriber_id`, `rx_bps`, `tx_bps`, `rx_bytes`, `tx_bytes`, `rtt_ms` (si la sonde est active), `session_uptime_s` |
| `backhaul_metrics` | `ts`, `backhaul_id`, capacité (globale/down/up), `signal_dbm`, `airtime_pct`, MCS, `online` |
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
make test        # 271 tests, dont 252 sans aucune infrastructure
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
premier échec, idempotence du plan (rejouer ne produit rien).

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
