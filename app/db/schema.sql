-- =============================================================================
-- freeQoS - schema TimescaleDB
--
-- Entierement idempotent : rejoue a chaque demarrage si DB_AUTO_MIGRATE=true.
-- Tout ce qui est specifique a Timescale est conditionne a la presence de
-- l'extension, pour que le schema s'applique aussi sur un PostgreSQL nu
-- (integration continue, poste de dev).
--
-- CONVENTION DE SENS : rx/tx sont du point de vue du ROUTEUR.
--   rx = recu depuis l'abonne  -> upload abonne
--   tx = emis vers l'abonne    -> download abonne
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Referentiel
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pops (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    router_host  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscribers (
    id              BIGSERIAL PRIMARY KEY,
    -- Identite STABLE de l'abonne, tous types confondus. Pour un abonne PPPoE
    -- c'est son login ; pour un client statique, la reference choisie par
    -- l'operateur dans static_clients. Un seul espace de noms, parce que
    -- RouterOS n'en a qu'un seul pour les files : 'freeqos-<slug(login)>'.
    -- Deux abonnes homonymes produiraient la meme file et se battraient a
    -- chaque cycle de reconciliation ; l'unicite l'interdit ici.
    login           TEXT NOT NULL UNIQUE,
    -- 'pppoe'  : decouvert dans /ppp/active, adresse donnee par la session.
    -- 'static' : declare a la main dans static_clients, adresse fixe.
    kind            TEXT NOT NULL DEFAULT 'pppoe' CHECK (kind IN ('pppoe', 'static')),
    pop_id          INTEGER REFERENCES pops(id) ON DELETE SET NULL,
    plan_down_mbps  DOUBLE PRECISION,
    plan_up_mbps    DOUBLE PRECISION,
    plan_source     TEXT,
    last_ip         INET,
    last_seen       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Inventaire DECLARATIF des clients a IP fixe (non PPPoE)
--
-- Il n'existe aucune source automatique pour ces clients : ils n'ouvrent pas de
-- session, RADIUS ne les connait pas, et rien dans RouterOS ne dit "cette IP
-- appartient a tel client avec tel plan". C'est donc une SAISIE MANUELLE
-- assumee, dans le meme esprit que l'inventaire de routeurs : l'operateur
-- declare ce qu'il a vendu, le controleur s'en sert comme d'une verite.
--
-- Cette table porte l'INTENTION. Les lignes 'subscribers' de kind='static' en
-- sont materialisees a chaque cycle de collecte, exactement comme les lignes
-- PPPoE sont materialisees depuis /ppp/active.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS static_clients (
    id              BIGSERIAL PRIMARY KEY,
    -- Reference stable, reprise telle quelle dans subscribers.login. Elle ne
    -- doit PAS encoder l'IP ni le VLAN : ce sont des faits mutables, et la cle
    -- de reconciliation de la file doit survivre a un changement d'adresse.
    reference       TEXT NOT NULL UNIQUE,
    label           TEXT,
    pop_name        TEXT NOT NULL,
    -- IP fixe (10.0.0.5) ou sous-reseau attribue au client (10.0.0.0/29).
    -- Le prefixe est CONSERVE tel quel dans la cible de file, a la difference
    -- d'une session PPPoE qui est toujours ramenee a un /32.
    address         INET NOT NULL,
    vlan            INTEGER CHECK (vlan IS NULL OR (vlan BETWEEN 1 AND 4094)),
    -- Rattachement topologique declare : aucun caller-id n'existe pour ces
    -- clients, l'operateur dit lui-meme sous quel secteur ils sont.
    sector_key      TEXT,
    plan_down_mbps  DOUBLE PRECISION,
    plan_up_mbps    DOUBLE PRECISION,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_static_clients_pop ON static_clients (pop_name);

-- -----------------------------------------------------------------------------
-- Presence observee sur les VLAN routees
--
-- Ce que la table ARP d'un routeur a montre : une adresse a parle sur une VLAN
-- qui n'heberge pas de serveur PPPoE. Deux lectures s'en deduisent, et AUCUNE
-- ne cree quoi que ce soit toute seule :
--
--   - l'adresse tombe dans le bloc d'un client declare -> confirmation de
--     presence ("vu actif a telle heure") ;
--   - elle ne correspond a rien de declare -> CANDIDAT, propose a l'operateur.
--
-- Un candidat n'est pas un client. Une imprimante, une camera ou un routeur de
-- passage laissent la meme trace. Seul un humain, en saisissant un plan, le
-- transforme en fiche.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vlan_sightings (
    router_name    TEXT NOT NULL,
    address        INET NOT NULL,
    mac            TEXT,
    vlan_interface TEXT NOT NULL,
    vlan_id        INTEGER,
    pop_name       TEXT,
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (router_name, address)
);

CREATE INDEX IF NOT EXISTS idx_vlan_sightings_seen ON vlan_sightings (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_vlan_sightings_addr ON vlan_sightings (address);


CREATE INDEX IF NOT EXISTS idx_subscribers_pop      ON subscribers (pop_id);
CREATE INDEX IF NOT EXISTS idx_subscribers_lastseen ON subscribers (last_seen DESC);

-- Routeurs ajoutes depuis l'interface. Ceux de l'inventaire fichier ne sont PAS
-- stockes ici : ils gardent leurs secrets en variables d'environnement.
-- Le mot de passe est chiffre au repos (cf. app/services/crypto.py) ; la cle vit
-- dans l'environnement, jamais en base.
CREATE TABLE IF NOT EXISTS routers (
    id                       SERIAL PRIMARY KEY,
    name                     TEXT NOT NULL UNIQUE,
    host                     TEXT NOT NULL,
    port                     INTEGER NOT NULL DEFAULT 8728,
    username                 TEXT NOT NULL DEFAULT 'qos-ro',
    password_enc             TEXT NOT NULL,
    role                     TEXT NOT NULL DEFAULT 'pop',
    pop_name                 TEXT,
    enabled                  BOOLEAN NOT NULL DEFAULT TRUE,
    use_ssl                  BOOLEAN NOT NULL DEFAULT FALSE,
    -- Posture TLS par routeur (strict / fingerprint / insecure). Defaut sur : la
    -- verification n'est plus desactivee en dur cote code.
    tls_verify               TEXT NOT NULL DEFAULT 'strict',
    tls_fingerprint          TEXT,
    -- Identite du routeur dans la topologie : unique par construction dans un
    -- reseau d'operateur, et independante des interfaces. NULL = a deduire.
    loopback                 INET,
    timeout_s                DOUBLE PRECISION NOT NULL DEFAULT 5.0,
    pppoe_interface_pattern  TEXT NOT NULL DEFAULT '<pppoe-{login}>',
    -- Diagnostic de la derniere tentative de connexion, affiche dans l'interface.
    last_ok_at               TIMESTAMPTZ,
    last_error               TEXT,
    identity                 TEXT,
    board_name               TEXT,
    routeros_version         TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS backhauls (
    id                     SERIAL PRIMARY KEY,
    pop_id                 INTEGER REFERENCES pops(id) ON DELETE CASCADE,
    name                   TEXT NOT NULL,
    uisp_device_id         TEXT UNIQUE,
    nominal_capacity_mbps  DOUBLE PRECISION,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pop_id, name)
);

-- Routeurs de l'inventaire FICHIER masques a la main depuis l'interface. Le
-- fichier reste la source de verite, mais un routeur qu'on ne veut plus voir
-- (secret retire, PoP demantele) peut etre ecarte sans editer le YAML ni
-- redemarrer : le registre ignore les noms presents ici. C'est le seul moyen,
-- cote base, de passer outre la regle "le fichier gagne", et il est reversible.
CREATE TABLE IF NOT EXISTS hidden_file_routers (
    name        TEXT PRIMARY KEY,
    reason      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Antennes Ubiquiti ajoutees depuis l'interface, interrogees DIRECTEMENT sur
-- leur API airOS locale (aucun UISP requis). Meme principe que la table routers :
-- le mot de passe est chiffre au repos (cf. app/services/crypto.py), la cle vit
-- dans l'environnement, jamais en base. Ajouter une antenne ici suffit a la
-- collecter : rien a activer par variable d'environnement.
CREATE TABLE IF NOT EXISTS airos_antennas (
    id                     SERIAL PRIMARY KEY,
    name                   TEXT NOT NULL UNIQUE,
    pop_name               TEXT NOT NULL,
    host                   TEXT NOT NULL,
    username               TEXT NOT NULL DEFAULT 'ubnt',
    password_enc           TEXT,
    verify_tls             BOOLEAN NOT NULL DEFAULT FALSE,
    -- Cle stable du lien (souvent la MAC) : sert de cle en base metrique et a la
    -- jointure de topologie. A defaut, le nom fait office de cle.
    device_key             TEXT,
    nominal_capacity_mbps  DOUBLE PRECISION,
    enabled                BOOLEAN NOT NULL DEFAULT TRUE,
    timeout_s              DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    -- Diagnostic de la derniere lecture, affiche dans l'interface.
    last_ok_at             TIMESTAMPTZ,
    last_error             TEXT,
    last_capacity_mbps     DOUBLE PRECISION,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Topologie decouverte (phase 2)
-- -----------------------------------------------------------------------------

-- Un equipement vu sur le reseau. La cle est prefixee par sa source
-- ("mac:AA:BB:..", "router:pop-nord") pour rester stable entre deux decouvertes.
CREATE TABLE IF NOT EXISTS topology_nodes (
    key             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'unknown',
    mac             TEXT,
    address         TEXT,
    platform        TEXT,
    version         TEXT,
    router_name     TEXT,
    uisp_device_id  TEXT,
    -- Role corrige a la main dans l'interface : il prime sur l'heuristique.
    kind_override   TEXT,
    -- Parent PROUVE par la table de routage de l'equipement. Colonne dediee et
    -- non attribut JSONB : les attributs sont FUSIONNES a l'ecriture, donc un
    -- parent devenu faux y survivrait a la route qui l'avait justifie.
    config_parent   TEXT,
    attributes      JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_topology_nodes_mac ON topology_nodes (mac);

-- Une adjacence. capacity_mbps est le plafond PHYSIQUE (debit negocie du port,
-- ou capacite radio du moment), pas le debit shape.
CREATE TABLE IF NOT EXISTS topology_links (
    key             TEXT PRIMARY KEY,
    source_key      TEXT NOT NULL,
    target_key      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    interface       TEXT,
    capacity_mbps   DOUBLE PRECISION,
    discovered_by   TEXT,
    attributes      JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_topology_links_source ON topology_links (source_key);
CREATE INDEX IF NOT EXISTS idx_topology_links_target ON topology_links (target_key);

-- Fusions declarees par l'operateur : quand la reconciliation automatique ne
-- peut PAS prouver que deux cases sont le meme equipement (identite generique
-- "MikroTik", pas de MAC commune), l'operateur tranche a la main. alias_key
-- devient canonical_key. C'est le dernier mot : la precision maximale de l'arbre
-- passe par ce levier. Reversible (on efface la ligne pour re-separer).
CREATE TABLE IF NOT EXISTS topology_aliases (
    alias_key     TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rattachement abonne -> secteur radio, issu de la jointure caller-id / UISP.
CREATE TABLE IF NOT EXISTS subscriber_attachments (
    subscriber_id  BIGINT PRIMARY KEY REFERENCES subscribers(id) ON DELETE CASCADE,
    sector_key     TEXT,
    cpe_mac        TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Politique de shaping (phase 2)
-- -----------------------------------------------------------------------------

-- Surcharges posees depuis l'interface. Sans ligne ici, le debit vient du plan
-- RADIUS (abonne) ou de la capacite mesuree (lien).
CREATE TABLE IF NOT EXISTS shaping_policies (
    id              BIGSERIAL PRIMARY KEY,
    scope           TEXT NOT NULL CHECK (scope IN ('link', 'subscriber')),
    target_key      TEXT NOT NULL,
    max_down_mbps   DOUBLE PRECISION,
    max_up_mbps     DOUBLE PRECISION,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    -- Coup de boost temporaire. Il prime sur la surcharge permanente tant que
    -- boost_expires_at n'est pas depasse, puis s'efface tout seul.
    boost_down_mbps DOUBLE PRECISION,
    boost_up_mbps   DOUBLE PRECISION,
    boost_expires_at TIMESTAMPTZ,
    boost_reason    TEXT,
    note            TEXT,
    updated_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope, target_key)
);

-- Etat de la BOUCLE FERMEE QoE (phase 4), un enregistrement par lien de secteur.
--
-- Le controleur ne garde ici QUE ce qu'il ne peut pas recalculer : de combien un
-- secteur est resserre en ce moment (trim_factor) et depuis combien de cycles sa
-- QoE est revenue a la normale (healthy_cycles, le delai de garde avant de rendre
-- un cran). Les scores, eux, se relisent a la demande depuis subscriber_metrics :
-- les dupliquer ici aurait cree une seconde verite.
--
-- Volontairement SEPAREE de shaping_policies : une surcharge est une decision
-- d'exploitant, un resserrage est une decision de la boucle. Les melanger ferait
-- qu'un cycle automatique ecraserait un debit saisi a la main -- exactement ce
-- qu'il ne faut pas. Les deux se composent dans le planificateur (cf.
-- shaped_capacity), elles ne se marchent jamais dessus.
--
-- last_action / last_reason / last_trigger_at sont la trace : une boucle qui
-- resserre sans dire pourquoi est une boucle que personne ne laissera active.
CREATE TABLE IF NOT EXISTS qoe_link_states (
    link_key         TEXT PRIMARY KEY,
    sector_key       TEXT,
    -- Fraction du debit qu'on appliquerait sans la boucle. 1.0 = rien de resserre.
    trim_factor      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    healthy_cycles   INTEGER NOT NULL DEFAULT 0,
    scored_count     INTEGER NOT NULL DEFAULT 0,
    degraded_count   INTEGER NOT NULL DEFAULT 0,
    worst_score      DOUBLE PRECISION,
    last_action      TEXT,
    last_reason      TEXT,
    -- Dernier cycle ou le resserrage a REELLEMENT bouge (resserre ou relache).
    last_trigger_at  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Drapeaux modifiables a chaud depuis l'interface. Ils sont amorces par les
-- variables d'environnement au premier demarrage, puis c'est la base qui fait
-- foi : basculer l'enforcement ne doit pas demander un redemarrage.
CREATE TABLE IF NOT EXISTS runtime_flags (
    name        TEXT PRIMARY KEY,
    value       BOOLEAN NOT NULL,
    updated_by  TEXT,
    reason      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Journal de TOUTE commande envoyee a un equipement. C'est la trace dont on a
-- besoin le jour ou il faut expliquer pourquoi un abonne a change de debit.
--
-- ``author`` dit D'OU vient la commande : "ui" pour une action lancee depuis
-- l'interface, ou "system:reconcile" / "system:boost-expiry" pour les ecritures
-- automatiques. Sans lui, le journal disait ce qui a ete fait mais jamais par
-- quel chemin.
-- ``changes`` porte le detail champ par champ ({champ: [avant, apres]}) : la
-- commande finale seule ne permet pas de diagnostiquer un ecart depuis
-- l'interface (pourquoi ce set ? qu'est-ce qui a change ?).
CREATE TABLE IF NOT EXISTS enforcement_audit (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    router_name  TEXT NOT NULL,
    verb         TEXT NOT NULL,
    path         TEXT NOT NULL,
    command      TEXT NOT NULL,
    dry_run      BOOLEAN NOT NULL,
    ok           BOOLEAN NOT NULL,
    detail       TEXT,
    author       TEXT,
    changes      JSONB
);

-- Reglages d'exploitation pilotes depuis l'interface. Meme regle que les
-- drapeaux ci-dessus, generalisee : l'environnement ne sert qu'a AMORCER une
-- valeur au premier demarrage ; des qu'une ligne existe ici, c'est elle qui fait
-- foi, et la changer ne demande pas de redemarrage. Absence de ligne = on garde
-- le defaut. La valeur est en JSONB pour distinguer un reglage volontairement
-- VIDE (JSON null, "ne pose pas ce champ") d'un reglage non surcharge.
CREATE TABLE IF NOT EXISTS runtime_settings (
    name        TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_by  TEXT,
    reason      TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_enforcement_audit_ts ON enforcement_audit (ts DESC);


-- -----------------------------------------------------------------------------
-- Series temporelles
-- -----------------------------------------------------------------------------

-- On stocke a la fois les compteurs bruts et le debit calcule :
--  - les bps servent au pilotage et aux graphes ;
--  - les octets bruts permettent de recalculer apres coup si la logique de
--    derivation change, et de diagnostiquer une remise a zero de compteur.
CREATE TABLE IF NOT EXISTS subscriber_metrics (
    ts               TIMESTAMPTZ NOT NULL,
    subscriber_id    BIGINT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    rx_bps           DOUBLE PRECISION,
    tx_bps           DOUBLE PRECISION,
    rx_bytes         BIGINT,
    tx_bytes         BIGINT,
    rtt_ms           DOUBLE PRECISION,   -- phase 3 : latence sous charge
    session_uptime_s INTEGER,
    PRIMARY KEY (subscriber_id, ts)
);

CREATE TABLE IF NOT EXISTS backhaul_metrics (
    ts                  TIMESTAMPTZ NOT NULL,
    backhaul_id         INTEGER NOT NULL REFERENCES backhauls(id) ON DELETE CASCADE,
    capacity_mbps       DOUBLE PRECISION,
    capacity_down_mbps  DOUBLE PRECISION,
    capacity_up_mbps    DOUBLE PRECISION,
    signal_dbm          DOUBLE PRECISION,
    airtime_pct         DOUBLE PRECISION,
    mcs_down            TEXT,
    mcs_up              TEXT,
    online              BOOLEAN,
    PRIMARY KEY (backhaul_id, ts)
);

-- Debit mesure d'un PORT de routeur.
--
-- La cle est (routeur, interface) et NON le lien de topologie : RouterOS compte
-- les octets par interface, pas par adjacence. Quand plusieurs voisins sont vus
-- sur le meme port (un switch entre les deux), tous les liens correspondants
-- partagent ce chiffre -- l'interface le signale plutot que d'attribuer le meme
-- trafic a chacun.
--
-- Le routeur est designe par son NOM et non par une cle etrangere : l'inventaire
-- peut vivre dans un fichier, sans ligne dans la table routers. C'est aussi ce
-- nom que porte topology_links.discovered_by, donc la jointure est directe.
CREATE TABLE IF NOT EXISTS interface_metrics (
    ts             TIMESTAMPTZ NOT NULL,
    router_name    TEXT NOT NULL,
    interface      TEXT NOT NULL,
    rx_bps         DOUBLE PRECISION,   -- le routeur RECOIT depuis le voisin
    tx_bps         DOUBLE PRECISION,   -- le routeur EMET vers le voisin
    rx_bytes       BIGINT,
    tx_bytes       BIGINT,
    running        BOOLEAN,
    capacity_mbps  DOUBLE PRECISION,   -- debit negocie du port au moment de la mesure
    PRIMARY KEY (router_name, interface, ts)
);

-- Phase 3 : creee des maintenant pour figer la retention et eviter une migration.
CREATE TABLE IF NOT EXISTS qoe_scores (
    ts             TIMESTAMPTZ NOT NULL,
    subscriber_id  BIGINT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    score          DOUBLE PRECISION NOT NULL,
    components     JSONB,
    PRIMARY KEY (subscriber_id, ts)
);

-- Observabilite du collecteur : une ligne par execution de job.
CREATE TABLE IF NOT EXISTS collector_runs (
    id           BIGSERIAL PRIMARY KEY,
    job          TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    duration_s   DOUBLE PRECISION NOT NULL,
    ok           BOOLEAN NOT NULL,
    items        INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_collector_runs_job_ts ON collector_runs (job, started_at DESC);

-- -----------------------------------------------------------------------------
-- Migrations de colonnes
--
-- CREATE TABLE IF NOT EXISTS ne touche pas une table deja presente : sans cette
-- section, une installation existante ne recevrait jamais les colonnes ajoutees
-- apres coup. Chaque ligne est idempotente et peut etre rejouee sans risque.
-- -----------------------------------------------------------------------------

-- Identite d'abonne neutralisee : la colonne s'appelait 'pppoe_login', ce qui
-- devenait un mensonge des qu'un client a IP fixe entre dans la table. Le
-- renommage est garde par une double condition pour rester rejouable.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'subscribers' AND column_name = 'pppoe_login')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'subscribers' AND column_name = 'login')
    THEN
        ALTER TABLE subscribers RENAME COLUMN pppoe_login TO login;
    END IF;
END
$$;

ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'pppoe';

-- La contrainte est posee en ligne sur une base neuve ; ce bloc ne sert qu'aux
-- installations existantes, ou ADD CONSTRAINT n'a pas de IF NOT EXISTS.
DO $$
BEGIN
    ALTER TABLE subscribers ADD CONSTRAINT subscribers_kind_check
        CHECK (kind IN ('pppoe', 'static'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

ALTER TABLE subscriber_metrics ADD COLUMN IF NOT EXISTS rtt_ms           DOUBLE PRECISION;
ALTER TABLE subscriber_metrics ADD COLUMN IF NOT EXISTS session_uptime_s INTEGER;

ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS kind_override TEXT;
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS config_parent TEXT;
-- Position posee a la main dans l'editeur d'arbre, et parent force en glissant
-- une case sous une autre. NULL = disposition/orientation automatique. Ces
-- champs ne changent que l'arbre AFFICHE : ils ne pilotent aucun routeur.
-- Un PoP peut venir d'un ROUTEUR declare ou d'un VLAN qui porte des clients.
-- Un VLAN, chez un operateur radio, porte un village ou un relais : le routeur
-- n'en est que la tete. Ces colonnes sont ce qui permet a un site de VLAN de
-- dire quel routeur le dessert -- sans quoi ses abonnes ne seraient rattaches a
-- aucun routeur, donc jamais shapes.
-- Position des cases qui n'existent PAS dans topology_nodes : les abonnes de
-- l'arbre, calcules a l'affichage depuis la liste des abonnes. Elles n'ont pas
-- d'equipement derriere elles, donc pas de ligne a porter leur position -- et
-- c'est pour cela qu'elles etaient les seules a ne pas pouvoir etre deplacees.
-- La cle est celle que l'arbre leur donne ('abos:<pop>|<login>').
CREATE TABLE IF NOT EXISTS topology_layout (
    key        TEXT PRIMARY KEY,
    pos_x      DOUBLE PRECISION,
    pos_y      DOUBLE PRECISION,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE pops ADD COLUMN IF NOT EXISTS kind            TEXT NOT NULL DEFAULT 'router';
ALTER TABLE pops ADD COLUMN IF NOT EXISTS router_name     TEXT;
ALTER TABLE pops ADD COLUMN IF NOT EXISTS vlan_id         INTEGER;
ALTER TABLE pops ADD COLUMN IF NOT EXISTS vlan_interface  TEXT;

ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS pos_x           DOUBLE PRECISION;
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS pos_y           DOUBLE PRECISION;
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS parent_override TEXT;
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS hidden          BOOLEAN NOT NULL DEFAULT FALSE;
-- Lien retire a la main (adjacence erronee de la decouverte) ou, a l'inverse,
-- lien cree a la main quand la decouverte l'a manque. hidden ecarte un lien de
-- l'affichage sans le supprimer, discovered_by='manual' marque les liens poses
-- a la main. Purement affichage : aucun equipement n'est reconfigure.
ALTER TABLE topology_links ADD COLUMN IF NOT EXISTS hidden          BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_down_mbps  DOUBLE PRECISION;
ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_up_mbps    DOUBLE PRECISION;
ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_expires_at TIMESTAMPTZ;
ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_reason     TEXT;

ALTER TABLE routers ADD COLUMN IF NOT EXISTS identity         TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS board_name       TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS routeros_version TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS tls_verify      TEXT NOT NULL DEFAULT 'strict';
ALTER TABLE routers ADD COLUMN IF NOT EXISTS tls_fingerprint TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS loopback        INET;
-- Unicite : c'est la promesse du modele. Deux routeurs qui la partagent sont
-- une erreur de configuration, et l'index la fait remonter a la saisie plutot
-- qu'a la reconciliation, ou elle fusionnerait silencieusement deux routeurs.
CREATE UNIQUE INDEX IF NOT EXISTS idx_routers_loopback
    ON routers (loopback) WHERE loopback IS NOT NULL;

-- Auteur de la commande et detail des changements : ajoutes apres coup, donc via
-- ALTER pour les installations existantes (cf. table enforcement_audit ci-dessus).
ALTER TABLE enforcement_audit ADD COLUMN IF NOT EXISTS author  TEXT;
ALTER TABLE enforcement_audit ADD COLUMN IF NOT EXISTS changes JSONB;

-- Depend d'une colonne ci-dessus : ne peut etre cree qu'apres les migrations.
CREATE INDEX IF NOT EXISTS idx_shaping_boost_expiry
    ON shaping_policies (boost_expires_at)
    WHERE boost_expires_at IS NOT NULL;

-- =============================================================================
-- API PUBLIQUE ET INTEGRATION FACTURATION (compatible Preseem)
--
-- Le controleur doit pouvoir REMPLACER Preseem dans une chaine d'exploitation
-- existante. Les systemes de facturation (Splynx, UISP/UCRM, Powercode, Visp,
-- developpements maison) poussent deja leur inventaire vers l'API "model" de
-- Preseem : cinq collections, une methode d'ecriture idempotente par
-- identifiant, une cle d'API en authentification Basic.
--
-- On reprend ce contrat A L'IDENTIQUE. Un integrateur change l'URL de base et
-- la cle, rien d'autre. C'est la seule facon de rendre la bascule rapide : la
-- valeur n'est pas dans l'invention d'un modele, elle est dans le fait de
-- n'avoir rien a reecrire cote facturation.
-- =============================================================================

-- Cles d'API. Le secret n'est JAMAIS stocke : seul son SHA-256 l'est, et il
-- n'est montre qu'une fois, a la creation. Le prefixe (visible, non secret)
-- sert a retrouver la ligne sans parcourir la table et a nommer la cle dans
-- l'interface ("fqos_a1b2c3d4...") sans jamais la reveler.
CREATE TABLE IF NOT EXISTS api_keys (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    prefix       TEXT NOT NULL UNIQUE,
    key_hash     TEXT NOT NULL,
    -- 'read' donne les GET ; 'write' ajoute PUT et DELETE. Deux portees
    -- suffisent : une integration de facturation ecrit, une supervision lit.
    scopes       TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[],
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    note         TEXT,
    created_by   TEXT,
    expires_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Le CLIENT au sens facturation : une personne, une entreprise. Il peut porter
-- plusieurs services (plusieurs sites, plusieurs lignes).
CREATE TABLE IF NOT EXISTS model_accounts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- L'OFFRE souscrite (debits en kbit/s, comme Preseem). Un service peut porter
-- ses propres debits ; sans eux, ceux du forfait s'appliquent.
CREATE TABLE IF NOT EXISTS model_packages (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    down_kbps   BIGINT,
    up_kbps     BIGINT,
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Le SITE (un point haut, un PoP). Son nom est repris tel quel comme pop_name
-- des clients qui en dependent : c'est la jointure avec le reste du controleur.
CREATE TABLE IF NOT EXISTS model_sites (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Le POINT D'ACCES radio, rattache a un site ('tower' chez Preseem). C'est le
-- parent d'un service : les clients d'un meme secteur se partagent son
-- enveloppe.
CREATE TABLE IF NOT EXISTS model_access_points (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    site_id     TEXT,
    ip_address  INET,
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_model_ap_site ON model_access_points (site_id);

-- -----------------------------------------------------------------------------
-- Ce qu'un SERVICE devient chez nous
--
-- Un service Preseem est exactement ce que static_clients porte deja : une
-- adresse (ou un bloc), un debit souscrit, un rattachement. On ne cree donc PAS
-- une seconde table de clients -- ce serait deux verites, et la file posee sur
-- le routeur ne saurait plus laquelle suivre. L'API ecrit dans l'inventaire
-- existant, par les colonnes ajoutees ci-dessous.
--
-- 'source' dit QUI a ecrit la fiche. C'est ce qui permet a l'API de ne jamais
-- ecraser silencieusement une saisie humaine, et a l'interface de montrer d'ou
-- vient chaque ligne.
-- -----------------------------------------------------------------------------
ALTER TABLE static_clients ADD COLUMN IF NOT EXISTS source           TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE static_clients ADD COLUMN IF NOT EXISTS account_ref      TEXT;
ALTER TABLE static_clients ADD COLUMN IF NOT EXISTS package_ref      TEXT;
ALTER TABLE static_clients ADD COLUMN IF NOT EXISTS access_point_ref TEXT;
ALTER TABLE static_clients ADD COLUMN IF NOT EXISTS site_ref         TEXT;
ALTER TABLE static_clients ADD COLUMN IF NOT EXISTS cpe_mac          TEXT;
-- Un service peut porter plusieurs prefixes. Le premier est l'adresse shapee
-- (une file vise une cible) ; les autres comptent dans la mesure de trafic.
ALTER TABLE static_clients ADD COLUMN IF NOT EXISTS extra_prefixes   JSONB NOT NULL DEFAULT '[]'::jsonb;

DO $$
BEGIN
    ALTER TABLE static_clients ADD CONSTRAINT static_clients_source_check
        CHECK (source IN ('manual', 'api'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE INDEX IF NOT EXISTS idx_static_clients_account ON static_clients (account_ref);
CREATE INDEX IF NOT EXISTS idx_static_clients_vlan    ON static_clients (vlan);

-- =============================================================================
-- NETFLOW -- MESURE DU TRAFIC SANS ETRE SUR LE CHEMIN DES PAQUETS
--
-- OU CE CONTROLEUR SE PLACE, ET POURQUOI.
--
-- Il ne s'insere pas dans le chemin des paquets. Il se place EN AMONT DU COEUR,
-- juste derriere la sortie internet, et AU PoP -- aux deux extremites du
-- reseau, jamais au milieu. Ces deux points voient tout ce qui compte :
--
--   - en amont du coeur (vantage 'edge') : le trafic tel qu'il entre et sort du
--     reseau. C'est la mesure de reference pour la consommation d'un abonne.
--   - au PoP (vantage 'pop') : le meme trafic, mais vu la ou le dernier
--     kilometre commence, donc avec le VLAN et le secteur.
--
-- Entre les deux, le coeur ne porte AUCUNE charge supplementaire : il
-- n'exporte rien, on ne l'interroge pas, on ne fait transiter aucune sonde par
-- lui. C'est tout l'interet du flux exporte plutot que du miroir de port -- un
-- span doublerait le trafic sur le lien de collecte, dans les deux sens.
--
-- COROLLAIRE : LE MEME OCTET EST VU DEUX FOIS. Un flux qui traverse le PoP puis
-- la sortie internet est exporte par les deux. Les additionner donnerait le
-- double du trafic reel. On enregistre donc le point de mesure avec la mesure,
-- et la consommation d'un abonne se lit depuis UN SEUL point (cf.
-- NETFLOW_ACCOUNTING_VANTAGE).
-- =============================================================================

-- Qui a le droit d'exporter, et ce que ses chiffres veulent dire. Un exporteur
-- inconnu est enregistre en 'unknown' plutot qu'ignore : il faut que
-- l'exploitant VOIE qu'une machine exporte vers lui, sinon un PoP mal declare
-- reste invisible pendant des semaines.
CREATE TABLE IF NOT EXISTS netflow_exporters (
    id             SERIAL PRIMARY KEY,
    address        INET NOT NULL UNIQUE,
    name           TEXT,
    -- 'edge' = en amont du coeur (sortie internet) ; 'pop' = au PoP.
    vantage        TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (vantage IN ('edge', 'pop', 'unknown')),
    pop_name       TEXT,
    -- Echantillonnage declare sur l'equipement (1 = tout). Les octets lus sont
    -- multiplies par ce facteur : sans lui, un routeur en 1:1000 rapporterait
    -- un millieme du trafic reel, ce qui ne se voit pas a l'oeil nu.
    sampling_rate  INTEGER NOT NULL DEFAULT 1 CHECK (sampling_rate >= 1),
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    note           TEXT,
    last_version   TEXT,
    last_seen      TIMESTAMPTZ,
    packets_seen   BIGINT NOT NULL DEFAULT 0,
    flows_seen     BIGINT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trafic agrege par abonne et par point de mesure. Le point de mesure est DANS
-- la cle : melanger 'edge' et 'pop' dans une meme serie reviendrait a compter
-- deux fois le meme octet.
CREATE TABLE IF NOT EXISTS flow_metrics (
    ts            TIMESTAMPTZ NOT NULL,
    subscriber_id BIGINT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    vantage       TEXT NOT NULL,
    down_bytes    BIGINT NOT NULL DEFAULT 0,
    up_bytes      BIGINT NOT NULL DEFAULT 0,
    down_packets  BIGINT NOT NULL DEFAULT 0,
    up_packets    BIGINT NOT NULL DEFAULT 0,
    flows         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subscriber_id, vantage, ts)
);

-- Repartition par usage. Volontairement GROSSIERE (une dizaine de familles) :
-- l'inspection fine de protocole n'a pas sa place dans un controleur de debit,
-- et le numero de port suffit a repondre a la seule question utile -- "de quoi
-- est fait le trafic qui sature ce secteur".
CREATE TABLE IF NOT EXISTS flow_app_metrics (
    ts            TIMESTAMPTZ NOT NULL,
    subscriber_id BIGINT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    app           TEXT NOT NULL,
    down_bytes    BIGINT NOT NULL DEFAULT 0,
    up_bytes      BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (subscriber_id, app, ts)
);

-- Hotes vus dans les flux et rattaches a AUCUNE fiche.
--
-- C'est une AIDE A LA SAISIE, jamais un inventaire. Une adresse qui parle sur
-- une VLAN peut etre un client, une imprimante, une camera ou l'equipement d'un
-- autre operateur : rien dans un flux ne permet de trancher, et surtout rien
-- n'y dit quel debit a ete vendu. Aucune ligne d'ici ne devient jamais une
-- fiche toute seule -- un humain la declare, ou elle expire.
CREATE TABLE IF NOT EXISTS flow_hosts (
    address     INET NOT NULL,
    -- 0 = AUCUNE ETIQUETTE VLAN, et non "VLAN inconnue". Un routeur purement L3
    -- -- typiquement celui de la sortie internet -- n'en voit jamais, donc ce
    -- cas est le plus courant, pas un cas limite.
    --
    -- Pourquoi une sentinelle plutot que NULL : la cle primaire interdit NULL,
    -- et un index unique ordinaire considere deux NULL comme DISTINCTS -- il
    -- laisserait donc proliferer une ligne par fenetre pour chaque adresse sans
    -- VLAN. 0 n'est pas une VLAN valide (elles vont de 1 a 4094), la valeur est
    -- donc sans ambiguite, et la lecture la retraduit en NULL.
    vlan_id     INTEGER NOT NULL DEFAULT 0
                CHECK (vlan_id = 0 OR vlan_id BETWEEN 1 AND 4094),
    exporter    INET,
    pop_name    TEXT,
    down_bytes  BIGINT NOT NULL DEFAULT 0,
    up_bytes    BIGINT NOT NULL DEFAULT 0,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (address, vlan_id)
);

CREATE INDEX IF NOT EXISTS idx_flow_hosts_seen ON flow_hosts (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_flow_hosts_vlan ON flow_hosts (vlan_id);

-- =============================================================================
-- CE QU'UN CLIENT ATTEINT, ET QUI SE CACHE DERRIERE
-- =============================================================================
--
-- NetFlow dit "10.20.0.10 a echange 4 Go avec 45.57.12.34". Tant que personne
-- ne sait a qui appartient 45.57.12.34, ce chiffre ne repond a aucune question
-- d'exploitation. Ces deux tables mettent un nom sur l'autre bout :
--
--   flow_destinations : QUI a parle a QUOI, combien, et par quel port.
--   ip_intel          : ce qu'on sait de l'adresse atteinte (nom inverse,
--                       service reconnu, organisation, AS, pays).
--
-- La separation est la meme que partout ailleurs : la MESURE s'efface avec la
-- retention, la CONNAISSANCE se garde. Reapprendre a chaque purge que
-- 45.57.12.34 est Netflix serait une requete DNS pour rien.

-- Le couple (abonne, adresse atteinte). Pas de serie temporelle : la question
-- posee est "qu'est-ce que cet abonne atteint, et depuis quand", pas "combien
-- d'octets a la minute pres vers ce serveur precis". Une ligne par couple, des
-- compteurs cumules, une date de derniere vue -- et une purge par anciennete.
-- LA CLE EST L'ADRESSE DU CLIENT, PAS SON IDENTIFIANT D'ABONNE.
--
-- L'observation est "cette adresse a joint celle-la". Le rattachement a une
-- fiche d'abonne est une INTERPRETATION : elle peut manquer (machine non
-- declaree) ou changer (session PPPoE qui se reconnecte ailleurs). Exiger un
-- abonne rendait invisible tout ce qui n'en a pas -- un poste de supervision,
-- un routeur, une camera, et le ping qu'on lance pour verifier que ca marche.
CREATE TABLE IF NOT EXISTS flow_destinations (
    client        INET NOT NULL,
    address       INET NOT NULL,
    -- Nullable, et ON DELETE SET NULL : supprimer une fiche d'abonne ne doit
    -- pas effacer la mesure de ce que son adresse a joint.
    subscriber_id BIGINT REFERENCES subscribers(id) ON DELETE SET NULL,
    -- Dernier port de service vu (le plus petit des deux, cf. services/flows.py)
    -- et son protocole. Ils servent a expliquer la ligne, pas a la compter :
    -- un meme serveur peut etre atteint sur plusieurs ports.
    port          INTEGER NOT NULL DEFAULT 0,
    protocol      INTEGER NOT NULL DEFAULT 0,
    app           TEXT,
    down_bytes    BIGINT NOT NULL DEFAULT 0,
    up_bytes      BIGINT NOT NULL DEFAULT 0,
    flows         BIGINT NOT NULL DEFAULT 0,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client, address)
);

-- Passage de l'ancienne forme (cle = abonne) a la nouvelle (cle = adresse du
-- client). CREATE TABLE IF NOT EXISTS ne touche pas une table deja presente :
-- sans ce bloc, une installation existante garderait la cle qui rend les
-- machines non declarees invisibles.
--
-- La table est RECREEE plutot que migree colonne par colonne : les lignes
-- anciennes n'ont pas d'adresse client a recuperer (elle n'etait pas stockee),
-- donc rien ne serait conserve de toute facon. C'est de la mesure pure, avec
-- une retention de quelques jours : elle se reconstruit a la fenetre suivante.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'flow_destinations' AND column_name = 'client'
    ) THEN
        RAISE NOTICE 'flow_destinations recree : la cle passe a (client, address).';
        DROP TABLE flow_destinations;
        CREATE TABLE flow_destinations (
            client        INET NOT NULL,
            address       INET NOT NULL,
            subscriber_id BIGINT REFERENCES subscribers(id) ON DELETE SET NULL,
            port          INTEGER NOT NULL DEFAULT 0,
            protocol      INTEGER NOT NULL DEFAULT 0,
            app           TEXT,
            down_bytes    BIGINT NOT NULL DEFAULT 0,
            up_bytes      BIGINT NOT NULL DEFAULT 0,
            flows         BIGINT NOT NULL DEFAULT 0,
            first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (client, address)
        );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_flow_destinations_seen ON flow_destinations (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_flow_destinations_addr ON flow_destinations (address);
CREATE INDEX IF NOT EXISTS idx_flow_destinations_sub  ON flow_destinations (subscriber_id);

-- Ce qu'on sait d'une adresse atteinte.
--
-- UNE LIGNE EST CREEE DES QU'UNE ADRESSE EST VUE POUR LA PREMIERE FOIS, avec
-- resolved_at a NULL : c'est la file d'attente de l'enrichissement. Le job qui
-- resout pioche exactement la-dedans, ce qui rend la decouverte DYNAMIQUE --
-- une adresse nouvelle est nommee a la fenetre suivante, sans intervention.
--
-- 'attempts' existe pour qu'une adresse sans nom inverse (le cas le plus
-- courant) ne soit pas redemandee indefiniment : au-dela d'un seuil on la
-- laisse tranquille, et le verdict du catalogue -- s'il y en a un -- suffit.
CREATE TABLE IF NOT EXISTS ip_intel (
    address     INET PRIMARY KEY,
    hostname    TEXT,
    -- Cle du catalogue ('netflix', 'youtube'...) et sa famille ('streaming').
    service     TEXT,
    category    TEXT,
    -- D'ou vient le verdict : 'catalogue', 'nom inverse', 'registre'.
    source      TEXT NOT NULL DEFAULT 'inconnu',
    org         TEXT,
    asn         INTEGER,
    country     TEXT,
    network     TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    resolved_at TIMESTAMPTZ,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Localisation. AJOUTEE APRES COUP : CREATE TABLE IF NOT EXISTS ne touche pas
-- une table deja presente, une installation existante ne recevrait donc jamais
-- ces colonnes. Elles restent vides tant que la geolocalisation n'est pas
-- activee -- c'est un appel sortant, et il se decide.
ALTER TABLE ip_intel ADD COLUMN IF NOT EXISTS city      TEXT;
ALTER TABLE ip_intel ADD COLUMN IF NOT EXISTS region    TEXT;
ALTER TABLE ip_intel ADD COLUMN IF NOT EXISTS latitude  DOUBLE PRECISION;
ALTER TABLE ip_intel ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
-- Relocalisation : une adresse nommee mais restee SANS POSITION (service de
-- localisation limite ou muet a ce moment-la) est redemandee plus tard, un
-- nombre borne de fois.
ALTER TABLE ip_intel ADD COLUMN IF NOT EXISTS geo_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ip_intel ADD COLUMN IF NOT EXISTS geo_tried_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_ip_intel_service ON ip_intel (service);
CREATE INDEX IF NOT EXISTS idx_ip_intel_category ON ip_intel (category);
-- La file d'attente de l'enrichissement : les adresses jamais resolues, les
-- plus recentes d'abord. Index partiel -- il ne porte que ce qui reste a faire.
CREATE INDEX IF NOT EXISTS idx_ip_intel_pending ON ip_intel (last_seen DESC)
    WHERE resolved_at IS NULL;

-- =============================================================================
-- RESTRICTIONS DE TRAFIC
-- =============================================================================
--
-- Une regle dit : "ce trafic-la, pour ces clients-la, est bloque (ou plafonne
-- a tant)". Elle est DECLARATIVE : rien n'est ecrit sur un routeur par le seul
-- fait de l'enregistrer. Comme pour les files, l'ecriture passe par un plan
-- affichable, le drapeau ENFORCEMENT_ENABLED et l'audit.
--
-- CE QUI REND UNE REGLE VIVANTE. Sa cible n'est pas une liste d'adresses figee
-- mais un CRITERE ('netflix', ou la famille 'streaming'). L'ensemble d'adresses
-- est recalcule a chaque reconciliation depuis le catalogue ET depuis ce que
-- NetFlow a decouvert : une adresse nouvelle rejoint donc la liste posee sur le
-- routeur toute seule, sans que personne ne reecrive la regle.
CREATE TABLE IF NOT EXISTS traffic_rules (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    -- 'block' = rejet ; 'limit' = plafond de debit sur ce trafic seulement.
    action          TEXT NOT NULL DEFAULT 'block'
                    CHECK (action IN ('block', 'limit')),
    limit_down_mbps DOUBLE PRECISION,
    limit_up_mbps   DOUBLE PRECISION,
    -- Criteres. Tous facultatifs, mais au moins un doit designer du trafic :
    -- sinon la regle viserait TOUT, ce que le service refuse d'enregistrer.
    services        JSONB NOT NULL DEFAULT '[]'::jsonb,
    categories      JSONB NOT NULL DEFAULT '[]'::jsonb,
    prefixes        JSONB NOT NULL DEFAULT '[]'::jsonb,
    protocol        TEXT,
    ports           TEXT,
    -- 'all' = tous les clients du routeur ; 'subscribers' = ceux listes.
    scope           TEXT NOT NULL DEFAULT 'all'
                    CHECK (scope IN ('all', 'subscribers')),
    logins          JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Routeurs vises. Vide = tous ceux de l'inventaire actif.
    routers         JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    note            TEXT,
    last_applied_at TIMESTAMPTZ,
    last_state      TEXT,
    last_detail     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Hypertables + politiques (uniquement si TimescaleDB est disponible)
-- -----------------------------------------------------------------------------

DO $$
DECLARE
    has_timescale BOOLEAN;
    tbl TEXT;
BEGIN
    SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') INTO has_timescale;
    IF NOT has_timescale THEN
        RAISE NOTICE 'TimescaleDB absent : tables conservees en PostgreSQL standard.';
        RETURN;
    END IF;

    FOREACH tbl IN ARRAY ARRAY['subscriber_metrics', 'backhaul_metrics',
                               'interface_metrics', 'qoe_scores',
                               'flow_metrics', 'flow_app_metrics'] LOOP
        BEGIN
            -- Signature historique, toujours supportee en 2.x.
            PERFORM create_hypertable(
                tbl::regclass, 'ts',
                chunk_time_interval => INTERVAL '24 hours',
                if_not_exists       => TRUE,
                migrate_data        => TRUE
            );
        EXCEPTION WHEN OTHERS THEN
            BEGIN
                -- Signature generique introduite en 2.13.
                PERFORM create_hypertable(
                    tbl::regclass,
                    by_range('ts', INTERVAL '24 hours'),
                    if_not_exists => TRUE,
                    migrate_data  => TRUE
                );
            EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'Hypertable % non creee (%) : la table reste une table simple.',
                    tbl, SQLERRM;
            END;
        END;
    END LOOP;
END
$$;

-- Compression : segmentee par serie, ordonnee par ts decroissant. Sur des metriques
-- a 10 s le gain est de l'ordre de 10-20x, ce qui rend 90 jours de retention tenable.
DO $$
DECLARE
    has_timescale BOOLEAN;
BEGIN
    SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') INTO has_timescale;
    IF NOT has_timescale THEN
        RETURN;
    END IF;

    BEGIN
        EXECUTE $sql$
            ALTER TABLE subscriber_metrics SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'subscriber_id',
                timescaledb.compress_orderby   = 'ts DESC'
            )$sql$;
        EXECUTE $sql$
            ALTER TABLE backhaul_metrics SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'backhaul_id',
                timescaledb.compress_orderby   = 'ts DESC'
            )$sql$;
        EXECUTE $sql$
            ALTER TABLE interface_metrics SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'router_name, interface',
                timescaledb.compress_orderby   = 'ts DESC'
            )$sql$;
        EXECUTE $sql$
            ALTER TABLE qoe_scores SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'subscriber_id',
                timescaledb.compress_orderby   = 'ts DESC'
            )$sql$;
    EXCEPTION WHEN OTHERS THEN
        -- Selon l'edition/version de Timescale la compression peut etre indisponible :
        -- ce n'est pas une raison de bloquer le demarrage du controleur.
        RAISE NOTICE 'Compression non activee : %', SQLERRM;
    END;
END
$$;

-- Vue pratique : dernier echantillon connu par abonne (utilisee par l'API et l'UI).
-- DROP puis CREATE : la vue a gagne des colonnes en cours de route, et
-- CREATE OR REPLACE n'autorise que l'ajout en fin de liste.
DROP VIEW IF EXISTS subscriber_latest;
CREATE VIEW subscriber_latest AS
SELECT DISTINCT ON (m.subscriber_id)
       m.subscriber_id,
       s.login,
       s.kind,
       s.pop_id,
       p.name AS pop_name,
       s.plan_down_mbps,
       s.plan_up_mbps,
       m.ts,
       m.rx_bps,
       m.tx_bps,
       m.rtt_ms,
       m.session_uptime_s,
       -- Adresse de la derniere session connue. C'est elle que vise la file
       -- de l'abonne ; l'interface doit pouvoir la montrer avant d'ecrire.
       s.last_ip
FROM subscriber_metrics m
JOIN subscribers s ON s.id = m.subscriber_id
LEFT JOIN pops p   ON p.id = s.pop_id
ORDER BY m.subscriber_id, m.ts DESC;

-- Derniere mesure connue par port. Le DISTINCT ON s'appuie sur l'index de cle
-- primaire (router_name, interface, ts) : pas de tri supplementaire.
CREATE OR REPLACE VIEW interface_latest AS
SELECT DISTINCT ON (router_name, interface)
       router_name,
       interface,
       ts,
       rx_bps,
       tx_bps,
       rx_bytes,
       tx_bytes,
       running,
       capacity_mbps
FROM interface_metrics
ORDER BY router_name, interface, ts DESC;

CREATE OR REPLACE VIEW backhaul_latest AS
SELECT DISTINCT ON (m.backhaul_id)
       m.backhaul_id,
       b.name,
       b.pop_id,
       p.name AS pop_name,
       b.uisp_device_id,
       b.nominal_capacity_mbps,
       m.ts,
       m.capacity_mbps,
       m.capacity_down_mbps,
       m.capacity_up_mbps,
       m.signal_dbm,
       m.airtime_pct,
       m.online
FROM backhaul_metrics m
JOIN backhauls b ON b.id = m.backhaul_id
LEFT JOIN pops p ON p.id = b.pop_id
ORDER BY m.backhaul_id, m.ts DESC;
