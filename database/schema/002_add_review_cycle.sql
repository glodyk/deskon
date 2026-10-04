--
-- 002_add_review_cycle.sql
-- Add review cycle number and historical review type to review.claim_reviews.
--
-- No explicit BEGIN/COMMIT: the migration runner owns the transaction.
-- All names are schema-qualified (baseline sets search_path to '').
--

--
-- Step 1: add columns (nullable for backfill)
--

ALTER TABLE review.claim_reviews
    ADD COLUMN cycle_no integer,
    ADD COLUMN review_type character varying(50);


--
-- Step 2: backfill existing rows
--   cycle_no    = order of reviews per claim, by id (1, 2, 3, ...)
--   review_type = current core.claims.claim_status at migration time
--

UPDATE review.claim_reviews AS cr
SET cycle_no = numbered.cycle_no
FROM (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY claim_id ORDER BY id) AS cycle_no
    FROM review.claim_reviews
) AS numbered
WHERE cr.id = numbered.id;

UPDATE review.claim_reviews AS cr
SET review_type = c.claim_status
FROM core.claims AS c
WHERE c.id = cr.claim_id;


--
-- Step 3: enforce
--

ALTER TABLE review.claim_reviews
    ALTER COLUMN cycle_no SET NOT NULL,
    ALTER COLUMN review_type SET NOT NULL;

ALTER TABLE review.claim_reviews
    ADD CONSTRAINT ck_claim_reviews_review_type CHECK (((review_type)::text = ANY ((ARRAY['PENDING'::character varying, 'VERIFIKASI_PASCA_KLAIM'::character varying, 'AUDIT_ADMINISTRASI_KLAIM'::character varying])::text[])));

ALTER TABLE review.claim_reviews
    ADD CONSTRAINT ck_claim_reviews_cycle_no CHECK ((cycle_no >= 1));

ALTER TABLE ONLY review.claim_reviews
    ADD CONSTRAINT uq_claim_reviews_cycle UNIQUE (claim_id, cycle_no);


--
-- Step 4: comments
--

COMMENT ON COLUMN review.claim_reviews.cycle_no IS 'Review cycle number per claim, starting at 1. Unique per claim_id.';

COMMENT ON COLUMN review.claim_reviews.review_type IS 'Historical type/purpose of this review cycle, fixed when the cycle is opened. Does NOT follow later changes to core.claims.claim_status.';

COMMENT ON COLUMN core.claims.claim_status IS 'Current state of the claim. Not the same as review.claim_reviews.review_type, which records the type of each review cycle.';
