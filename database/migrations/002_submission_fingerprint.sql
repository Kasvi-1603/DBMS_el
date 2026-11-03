-- ============================================================================
-- 002 — idempotency fingerprint. Idempotent and additive.
--
-- submission_key alone let a retry with DIFFERENT values return the earlier
-- claim as a "duplicate", silently dropping the new values. The SHA-256 of the
-- submitted body is stored with the key so a reused key with a different body
-- is rejected (409) instead. Claims saved before this migration keep NULL and
-- are treated as matching, since their original body is unknown.
-- ============================================================================
ALTER TABLE claims ADD COLUMN IF NOT EXISTS submission_hash TEXT;
