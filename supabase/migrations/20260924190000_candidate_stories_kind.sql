-- The candidate's own professional stories become a fourth private knowledge kind.
-- Like facts and voice samples, a story source has no job scope, source URL or indexed
-- job. No data changes; row-level security, grants, the chunks table and the three
-- column checks (candidate_id, source_id, source_version) are untouched.
--
-- Idempotent: the two kind-related CHECK constraints are dropped if present and
-- recreated in one ALTER TABLE statement (one atomic step), so applying this file
-- again, or a later `supabase db push` after a manual application, cannot break.
-- `sources_kind_check` keeps its original automatic name; the table-level check that
-- CREATE TABLE named `sources_check` is recreated as `sources_scope_check`.
ALTER TABLE imx_knowledge.sources
    DROP CONSTRAINT IF EXISTS sources_kind_check,
    DROP CONSTRAINT IF EXISTS sources_check,
    DROP CONSTRAINT IF EXISTS sources_scope_check,
    ADD CONSTRAINT sources_kind_check CHECK (kind IN ('fact', 'job', 'voice', 'story')),
    ADD CONSTRAINT sources_scope_check CHECK (
        (kind = 'job' AND job_scope ~ '^[a-f0-9]{64}$'
         AND length(source_url) > 0 AND length(indexed_job_id) > 0)
        OR (kind IN ('fact', 'voice', 'story') AND job_scope = ''
            AND source_url = '' AND indexed_job_id = ''));
