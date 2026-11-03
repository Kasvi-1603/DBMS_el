-- ============================================================================
-- Migration 001 — data integrity, idempotent submissions, analysis metadata,
-- normalized shared-entity matching and the claim_edges graph.
--
-- Safe to run repeatedly against an existing database: it never drops a
-- table, never deletes rows and never rewrites an ID. Views are dropped and
-- re-created (views hold no data). app.py and seed_postgres.py apply this
-- file on startup; you can also run it by hand:
--     psql "$DATABASE_URL" -f database/migrations/001_integrity_and_networks.sql
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Columns added by earlier app versions (kept so very old databases upgrade)
-- ---------------------------------------------------------------------------
ALTER TABLE claims ADD COLUMN IF NOT EXISTS description      TEXT;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS ml_anomaly       BOOLEAN;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS ml_anomaly_score NUMERIC(8,3);

-- Client-generated key for one form submission. A retried request with the
-- same key returns the claim that was already created instead of a duplicate.
ALTER TABLE claims ADD COLUMN IF NOT EXISTS submission_key VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS uq_claims_submission_key
    ON claims(submission_key) WHERE submission_key IS NOT NULL;

-- When the stored risk_score / ml_anomaly were last computed, and why the last
-- attempt failed (NULL when it succeeded). A claim with analyzed_at IS NULL has
-- been saved but not analysed yet.
ALTER TABLE claims ADD COLUMN IF NOT EXISTS analyzed_at    TIMESTAMP;
ALTER TABLE claims ADD COLUMN IF NOT EXISTS analysis_error TEXT;
UPDATE claims SET analyzed_at = COALESCE(created_at, NOW())
 WHERE analyzed_at IS NULL AND risk_score IS NOT NULL AND analysis_error IS NULL;

-- One row per portfolio analysis run (risk scoring + Isolation Forest), so the
-- dashboard can state exactly what the current ML flags are based on.
CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id            SERIAL PRIMARY KEY,
    run_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    trigger           VARCHAR(60) NOT NULL,
    claims_evaluated  INTEGER NOT NULL,
    ml_available      BOOLEAN NOT NULL,
    ml_flagged        INTEGER,
    ml_threshold      NUMERIC(8,4),
    ml_contamination  VARCHAR(20),
    ml_unavailable_reason TEXT,
    networks_found    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- Constraints (added only if missing; existing rows are validated, so the
-- migration fails loudly instead of silently accepting bad historical data)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'policies_dates_check') THEN
        ALTER TABLE policies ADD CONSTRAINT policies_dates_check
            CHECK (end_date IS NULL OR end_date >= start_date);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_status_check') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_status_check
            CHECK (status IN ('open', 'under_review', 'flagged', 'closed'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_risk_score_range') THEN
        ALTER TABLE claims ADD CONSTRAINT claims_risk_score_range
            CHECK (risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_claim_witnesses_witness ON claim_witnesses(witness_id);

-- policy_number is already UNIQUE; this also stops 'pol-1' and 'POL-1' coexisting.
CREATE UNIQUE INDEX IF NOT EXISTS uq_policies_number_ci ON policies (upper(policy_number));

-- ---------------------------------------------------------------------------
-- Normalization used ONLY for matching. Stored display values are untouched.
--
-- norm_phone: keep digits; the last 10 digits identify an Indian mobile number
-- regardless of a leading 0 or +91 (so 09236409075 = +91 92364 09075). Fewer
-- than 7 digits, or one repeated digit (0000000000), is a placeholder -> NULL,
-- and NULL never matches anything.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION norm_phone(p TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
             WHEN length(d) < 7 OR d ~ '^(\d)\1*$' THEN NULL
             WHEN length(d) > 10 THEN right(d, 10)
             ELSE d
           END
    FROM (SELECT regexp_replace(COALESCE(p, ''), '[^0-9]', '', 'g') AS d) s
$$;

-- norm_address: lower-case, punctuation -> space, collapse whitespace. An
-- address with no house/plot number (e.g. just "Bengaluru", "N/A", "unknown")
-- is too vague to tie two customers together -> NULL.
CREATE OR REPLACE FUNCTION norm_address(a TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN n ~ '[0-9]' AND length(n) >= 6 THEN n ELSE NULL END
    FROM (SELECT btrim(regexp_replace(regexp_replace(lower(COALESCE(a, '')),
                 '[^a-z0-9]+', ' ', 'g'), '\s+', ' ', 'g')) AS n) s
$$;

-- ---------------------------------------------------------------------------
-- Graph views
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS claim_edges;
DROP VIEW IF EXISTS claim_customer_info;

CREATE VIEW claim_customer_info AS
    SELECT c.claim_id, cl.claimant_id, cl.full_name, cl.phone, cl.address,
           norm_phone(cl.phone)     AS phone_norm,
           norm_address(cl.address) AS address_norm
    FROM claims c
    JOIN policies p   ON c.policy_id = p.policy_id
    JOIN claimants cl ON p.claimant_id = cl.claimant_id;

-- One row per (pair of claims, shared entity). claim_a < claim_b, so each
-- undirected relationship appears exactly once per entity.
--   customer  — both claims were filed by the same customer record (same
--               claimant_id; never inferred from a matching name)
--   phone     — DIFFERENT customer records with the same normalized phone
--   address   — DIFFERENT customer records with the same normalized address
-- Phone/address edges between claims of the same customer would just restate
-- the 'customer' edge, so they are excluded.
CREATE VIEW claim_edges AS
    SELECT a.claim_id AS claim_a, b.claim_id AS claim_b,
           'garage'::text AS link_type, a.garage_id::text AS shared_entity
    FROM claims a JOIN claims b ON a.garage_id = b.garage_id AND a.claim_id < b.claim_id
    WHERE a.garage_id IS NOT NULL
UNION ALL
    SELECT a.claim_id, b.claim_id, 'doctor', a.doctor_id::text
    FROM claims a JOIN claims b ON a.doctor_id = b.doctor_id AND a.claim_id < b.claim_id
    WHERE a.doctor_id IS NOT NULL
UNION ALL
    SELECT a.claim_id, b.claim_id, 'vehicle', a.vehicle_id::text
    FROM claims a JOIN claims b ON a.vehicle_id = b.vehicle_id AND a.claim_id < b.claim_id
    WHERE a.vehicle_id IS NOT NULL
UNION ALL
    SELECT cw1.claim_id, cw2.claim_id, 'witness', cw1.witness_id::text
    FROM claim_witnesses cw1 JOIN claim_witnesses cw2
      ON cw1.witness_id = cw2.witness_id AND cw1.claim_id < cw2.claim_id
UNION ALL
    SELECT a.claim_id, b.claim_id, 'customer', a.claimant_id::text
    FROM claim_customer_info a JOIN claim_customer_info b
      ON a.claimant_id = b.claimant_id AND a.claim_id < b.claim_id
UNION ALL
    SELECT a.claim_id, b.claim_id, 'phone', a.phone_norm
    FROM claim_customer_info a JOIN claim_customer_info b
      ON a.phone_norm = b.phone_norm AND a.claim_id < b.claim_id
     AND a.claimant_id <> b.claimant_id
    WHERE a.phone_norm IS NOT NULL
UNION ALL
    SELECT a.claim_id, b.claim_id, 'address', a.address_norm
    FROM claim_customer_info a JOIN claim_customer_info b
      ON a.address_norm = b.address_norm AND a.claim_id < b.claim_id
     AND a.claimant_id <> b.claimant_id
    WHERE a.address_norm IS NOT NULL;
