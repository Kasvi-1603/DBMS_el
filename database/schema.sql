-- ============================================================================
-- Insurance Fraud Ring Detection — Database Schema (PostgreSQL)
-- Normalized to 3NF. FRESH INSTALL ONLY:
--     psql -U postgres -d fraud_db -f database/schema.sql
--
-- WARNING: the DROP below deletes every table and all claims in them. To
-- upgrade an existing database, do NOT run this file — run the idempotent
-- migrations instead (the API also applies them on startup):
--     psql "$DATABASE_URL" -f database/migrations/001_integrity_and_networks.sql
-- ============================================================================

DROP TABLE IF EXISTS claim_witnesses, claims, policies, claimants,
                      vehicles, garages, doctors, witnesses, analysis_runs CASCADE;

-- ---------------------------------------------------------------------------
-- Claimants: one row per person. Never repeated on the Claims table (2NF/3NF).
-- ---------------------------------------------------------------------------
CREATE TABLE claimants (
    claimant_id     SERIAL PRIMARY KEY,
    full_name       VARCHAR(120)  NOT NULL,
    phone           VARCHAR(20)   NOT NULL,
    address         VARCHAR(255)  NOT NULL,
    date_of_birth   DATE,
    created_at      TIMESTAMP     DEFAULT NOW()
);
CREATE INDEX idx_claimants_phone   ON claimants(phone);
CREATE INDEX idx_claimants_address ON claimants(address);

-- ---------------------------------------------------------------------------
-- Vehicles / Garages / Doctors / Witnesses: shared "entity" tables.
-- Fraud rings are found by claims that repeat rows in these tables.
-- ---------------------------------------------------------------------------
CREATE TABLE vehicles (
    vehicle_id      SERIAL PRIMARY KEY,
    vin             VARCHAR(32)  UNIQUE NOT NULL,
    make            VARCHAR(60),
    model           VARCHAR(60),
    plate_number    VARCHAR(20)
);

CREATE TABLE garages (
    garage_id       SERIAL PRIMARY KEY,
    name            VARCHAR(150) NOT NULL,
    address         VARCHAR(255),
    phone           VARCHAR(20)
);

CREATE TABLE doctors (
    doctor_id       SERIAL PRIMARY KEY,
    full_name       VARCHAR(120) NOT NULL,
    clinic_name     VARCHAR(150),
    phone           VARCHAR(20)
);

CREATE TABLE witnesses (
    witness_id      SERIAL PRIMARY KEY,
    full_name       VARCHAR(120) NOT NULL,
    phone           VARCHAR(20),
    address         VARCHAR(255)
);

-- ---------------------------------------------------------------------------
-- Policies: one claimant can hold many policies.
-- ---------------------------------------------------------------------------
CREATE TABLE policies (
    policy_id       SERIAL PRIMARY KEY,
    claimant_id     INTEGER NOT NULL REFERENCES claimants(claimant_id),
    policy_number   VARCHAR(40) UNIQUE NOT NULL,
    premium_amount  NUMERIC(12,2) NOT NULL CHECK (premium_amount >= 0),
    start_date      DATE NOT NULL,
    end_date        DATE
);
CREATE INDEX idx_policies_claimant ON policies(claimant_id);

-- ---------------------------------------------------------------------------
-- Claims: the central fact table. Links to policy, vehicle, garage, doctor.
-- ---------------------------------------------------------------------------
CREATE TABLE claims (
    claim_id        SERIAL PRIMARY KEY,
    policy_id       INTEGER NOT NULL REFERENCES policies(policy_id),
    vehicle_id      INTEGER REFERENCES vehicles(vehicle_id),
    garage_id       INTEGER REFERENCES garages(garage_id),
    doctor_id       INTEGER REFERENCES doctors(doctor_id),
    claim_amount    NUMERIC(12,2) NOT NULL CHECK (claim_amount >= 0),
    incident_date   DATE NOT NULL,
    category        VARCHAR(60) NOT NULL,          -- collision, medical, theft, fire...
    status          VARCHAR(30) DEFAULT 'open',    -- open, under_review, closed, flagged
    description     TEXT,                           -- free-text incident description
    risk_score      NUMERIC(5,2),
    ml_anomaly      BOOLEAN,                        -- set by ml/anomaly_detection.py
    ml_anomaly_score NUMERIC(8,3),
    created_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_claims_policy  ON claims(policy_id);
CREATE INDEX idx_claims_vehicle ON claims(vehicle_id);
CREATE INDEX idx_claims_garage  ON claims(garage_id);
CREATE INDEX idx_claims_doctor  ON claims(doctor_id);
CREATE INDEX idx_claims_category ON claims(category);

-- ---------------------------------------------------------------------------
-- Claim_Witnesses: junction table (many-to-many).
-- ---------------------------------------------------------------------------
CREATE TABLE claim_witnesses (
    claim_id        INTEGER NOT NULL REFERENCES claims(claim_id),
    witness_id      INTEGER NOT NULL REFERENCES witnesses(witness_id),
    PRIMARY KEY (claim_id, witness_id)
);

-- ============================================================================
-- Views (claim_customer_info, claim_edges), normalisation functions,
-- constraints and the analysis columns all live in the migration, so a fresh
-- install and an upgraded database end up identical. \ir resolves the path
-- relative to this file.
-- ============================================================================
\ir migrations/001_integrity_and_networks.sql

-- ============================================================================
-- Recursive CTE: given a starting claim, find the full connected network
-- reachable through any shared entity, however many hops away. This is the
-- query in backend/networks.py. UNION (not UNION ALL) on claim_id alone is
-- what stops it on cycles: a claim already in the result is never re-added,
-- so the recursion ends when no new claim is reached — no depth cap needed.
-- ============================================================================
-- WITH RECURSIVE adjacency(src, dst) AS (
--     SELECT claim_a, claim_b FROM claim_edges
--     UNION SELECT claim_b, claim_a FROM claim_edges
-- ), network(claim_id) AS (
--     SELECT :start_claim_id
--     UNION
--     SELECT a.dst FROM adjacency a JOIN network n ON a.src = n.claim_id
-- )
-- SELECT claim_id FROM network ORDER BY claim_id;
