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

    FOREACH tbl IN ARRAY ARRAY['subscriber_metrics', 'backhaul_metrics', 'qoe_scores'] LOOP
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
       m.session_uptime_s
FROM subscriber_metrics m
JOIN subscribers s ON s.id = m.subscriber_id
LEFT JOIN pops p   ON p.id = s.pop_id
ORDER BY m.subscriber_id, m.ts DESC;

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
