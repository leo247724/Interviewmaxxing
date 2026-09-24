-- Private, disposable projection. Canonical candidate verification stays local.
-- Clients use a private PostgreSQL connection; no REST/RPC surface is created.
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
CREATE SCHEMA IF NOT EXISTS imx_knowledge;
REVOKE ALL ON SCHEMA imx_knowledge FROM PUBLIC, anon, authenticated;

CREATE TABLE imx_knowledge.sources (
    candidate_id text NOT NULL CHECK (length(candidate_id) > 0),
    kind text NOT NULL CHECK (kind IN ('fact', 'job', 'voice')),
    job_scope text NOT NULL DEFAULT '',
    source_id text NOT NULL CHECK (length(source_id) > 0),
    source_version text NOT NULL CHECK (source_version ~ '^[a-f0-9]{64}$'),
    source_url text NOT NULL DEFAULT '',
    indexed_job_id text NOT NULL DEFAULT '',
    indexed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (candidate_id, kind, job_scope, source_id),
    UNIQUE (candidate_id, kind, job_scope, source_id, source_version),
    CHECK ((kind = 'job' AND job_scope ~ '^[a-f0-9]{64}$'
            AND length(source_url) > 0 AND length(indexed_job_id) > 0)
           OR (kind IN ('fact', 'voice') AND job_scope = ''
               AND source_url = '' AND indexed_job_id = ''))
);

CREATE TABLE imx_knowledge.chunks (
    id text PRIMARY KEY,
    candidate_id text NOT NULL,
    kind text NOT NULL,
    job_scope text NOT NULL,
    source_id text NOT NULL,
    source_version text NOT NULL,
    chunk_number integer NOT NULL CHECK (chunk_number >= 0 AND chunk_number < 64),
    body text NOT NULL CHECK (length(body) > 0 AND length(body) <= 1800),
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    embedding extensions.vector(1536) NOT NULL,
    embedding_model text NOT NULL CHECK (embedding_model = 'openai/text-embedding-3-small'),
    search_terms tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, body)) STORED,
    UNIQUE (candidate_id, kind, job_scope, source_id, chunk_number),
    FOREIGN KEY (candidate_id, kind, job_scope, source_id, source_version)
        REFERENCES imx_knowledge.sources
            (candidate_id, kind, job_scope, source_id, source_version) ON DELETE CASCADE
);

-- The composite UNIQUE index serves candidate/kind/job filters and source deletes.
-- Exact vector ranking is appropriate for these small, private scoped corpora.
CREATE INDEX knowledge_chunks_search_idx ON imx_knowledge.chunks USING gin (search_terms);

ALTER TABLE imx_knowledge.sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE imx_knowledge.sources FORCE ROW LEVEL SECURITY;
ALTER TABLE imx_knowledge.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE imx_knowledge.chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY knowledge_sources_candidate ON imx_knowledge.sources
    USING (candidate_id = current_setting('imx_knowledge.candidate_id', true))
    WITH CHECK (candidate_id = current_setting('imx_knowledge.candidate_id', true));
CREATE POLICY knowledge_chunks_candidate ON imx_knowledge.chunks
    USING (candidate_id = current_setting('imx_knowledge.candidate_id', true))
    WITH CHECK (candidate_id = current_setting('imx_knowledge.candidate_id', true));

REVOKE ALL ON ALL TABLES IN SCHEMA imx_knowledge FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA imx_knowledge FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA imx_knowledge FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA imx_knowledge
    REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA imx_knowledge
    REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA imx_knowledge
    REVOKE ALL ON FUNCTIONS FROM PUBLIC, anon, authenticated;
