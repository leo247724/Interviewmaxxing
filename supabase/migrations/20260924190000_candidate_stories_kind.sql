-- The candidate's own professional stories become a fourth private knowledge kind.
-- Like facts and voice samples, a story source has no job scope, source URL or indexed
-- job. No data changes; row-level security, grants and the chunks table are untouched.
-- The CHECK constraints of imx_knowledge.sources are recreated by name-independent
-- lookup because CREATE TABLE named them automatically.
DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'imx_knowledge.sources'::regclass AND contype = 'c'
    LOOP
        EXECUTE format('ALTER TABLE imx_knowledge.sources DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

ALTER TABLE imx_knowledge.sources
    ADD CONSTRAINT sources_candidate_id_check CHECK (length(candidate_id) > 0),
    ADD CONSTRAINT sources_kind_check CHECK (kind IN ('fact', 'job', 'voice', 'story')),
    ADD CONSTRAINT sources_source_id_check CHECK (length(source_id) > 0),
    ADD CONSTRAINT sources_source_version_check CHECK (source_version ~ '^[a-f0-9]{64}$'),
    ADD CONSTRAINT sources_scope_check CHECK (
        (kind = 'job' AND job_scope ~ '^[a-f0-9]{64}$'
         AND length(source_url) > 0 AND length(indexed_job_id) > 0)
        OR (kind IN ('fact', 'voice', 'story') AND job_scope = ''
            AND source_url = '' AND indexed_job_id = ''));
