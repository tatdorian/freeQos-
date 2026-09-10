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
    pppoe_login     TEXT NOT NULL UNIQUE,
    pop_id          INTEGER REFERENCES pops(id) ON DELETE SET NULL,
    plan_down_mbps  DOUBLE PRECISION,
    plan_up_mbps    DOUBLE PRECISION,
    plan_source     TEXT,
    last_ip         INET,
    last_seen       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
CREATE TABLE IF NOT EXISTS enforcement_audit (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    router_name  TEXT NOT NULL,
    verb         TEXT NOT NULL,
    path         TEXT NOT NULL,
    command      TEXT NOT NULL,
    dry_run      BOOLEAN NOT NULL,
    ok           BOOLEAN NOT NULL,
    detail       TEXT
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

ALTER TABLE subscriber_metrics ADD COLUMN IF NOT EXISTS rtt_ms           DOUBLE PRECISION;
ALTER TABLE subscriber_metrics ADD COLUMN IF NOT EXISTS session_uptime_s INTEGER;

ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS kind_override TEXT;
-- Position posee a la main dans l'editeur d'arbre, et parent force en glissant
-- une case sous une autre. NULL = disposition/orientation automatique. Ces
-- champs ne changent que l'arbre AFFICHE : ils ne pilotent aucun routeur.
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS pos_x           DOUBLE PRECISION;
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS pos_y           DOUBLE PRECISION;
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS parent_override TEXT;
ALTER TABLE topology_nodes ADD COLUMN IF NOT EXISTS hidden          BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_down_mbps  DOUBLE PRECISION;
ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_up_mbps    DOUBLE PRECISION;
ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_expires_at TIMESTAMPTZ;
ALTER TABLE shaping_policies ADD COLUMN IF NOT EXISTS boost_reason     TEXT;

ALTER TABLE routers ADD COLUMN IF NOT EXISTS identity         TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS board_name       TEXT;
ALTER TABLE routers ADD COLUMN IF NOT EXISTS routeros_version TEXT;

-- Depend d'une colonne ci-dessus : ne peut etre cree qu'apres les migrations.
CREATE INDEX IF NOT EXISTS idx_shaping_boost_expiry
    ON shaping_policies (boost_expires_at)
    WHERE boost_expires_at IS NOT NULL;

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
                               'interface_metrics', 'qoe_scores'] LOOP
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
CREATE OR REPLACE VIEW subscriber_latest AS
SELECT DISTINCT ON (m.subscriber_id)
       m.subscriber_id,
       s.pppoe_login,
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
