-- agents-mindmap schema 0001: maps, nodes with versioned notes, growth areas, edges, links,
-- directions, semantic chunks, outboxes and the event log.
-- Conventions: text + CHECK instead of enums, identity ids, optimistic `version` on mutable rows,
-- project explicit on every scoped row and kept consistent by composite foreign keys.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- The app role cannot create the untrusted `vector`; the cluster bootstrap does
-- (initdb.postInitApplicationSQL).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'vector extension missing: create it with postInitApplicationSQL';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS maps (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project text NOT NULL CHECK (project ~ '^[a-z0-9][a-z0-9-]{0,39}$'),
    slug text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
    kind text NOT NULL DEFAULT 'research' CHECK (kind IN ('research', 'knowledge')),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    description text NOT NULL DEFAULT '' CHECK (octet_length(description) <= 8192),
    root_node_id bigint NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_by text NOT NULL CHECK (created_by ~ '^(user|session):.+'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version int NOT NULL DEFAULT 1,
    client_key text NULL CHECK (char_length(client_key) BETWEEN 1 AND 128),
    UNIQUE (project, slug),
    UNIQUE (id, project)
);
CREATE UNIQUE INDEX IF NOT EXISTS maps_one_knowledge_per_project
    ON maps (project) WHERE kind = 'knowledge';
CREATE UNIQUE INDEX IF NOT EXISTS maps_client_key
    ON maps (created_by, client_key) WHERE client_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS maps_project_status ON maps (project, status);

CREATE TABLE IF NOT EXISTS nodes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    map_id bigint NOT NULL,
    project text NOT NULL,
    parent_id bigint NULL,
    topic text NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 200),
    notes text NOT NULL DEFAULT '' CHECK (octet_length(notes) <= 65536),
    current_version int NOT NULL DEFAULT 1,
    content_rev int NOT NULL DEFAULT 1,
    embedded_rev int NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'seed'
        CHECK (status IN ('seed', 'growing', 'settled', 'stale', 'archived')),
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', topic), 'A') || setweight(to_tsvector('simple', notes), 'B')
    ) STORED,
    created_by text NOT NULL CHECK (created_by ~ '^(user|session):.+'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL CHECK (updated_by ~ '^(user|session):.+'),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version int NOT NULL DEFAULT 1,
    client_key text NULL CHECK (char_length(client_key) BETWEEN 1 AND 128),
    CHECK (parent_id IS NULL OR parent_id <> id),
    UNIQUE (id, project),
    UNIQUE (id, map_id),
    FOREIGN KEY (map_id, project) REFERENCES maps (id, project) ON DELETE CASCADE,
    FOREIGN KEY (parent_id, map_id) REFERENCES nodes (id, map_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS nodes_tsv ON nodes USING gin (tsv);
CREATE INDEX IF NOT EXISTS nodes_topic_trgm ON nodes USING gin (topic gin_trgm_ops);
CREATE INDEX IF NOT EXISTS nodes_map_parent ON nodes (map_id, parent_id);
CREATE INDEX IF NOT EXISTS nodes_project_status ON nodes (project, status);
CREATE INDEX IF NOT EXISTS nodes_pending_embedding ON nodes (id) WHERE embedded_rev < content_rev;
CREATE UNIQUE INDEX IF NOT EXISTS nodes_client_key
    ON nodes (created_by, client_key) WHERE client_key IS NOT NULL;

-- The circular reference: a map names its root node, set in the transaction that creates it.
ALTER TABLE maps
    ADD CONSTRAINT maps_root_node_fk FOREIGN KEY (root_node_id) REFERENCES nodes (id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS node_versions (
    node_id bigint NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    version int NOT NULL,
    topic text NOT NULL,
    notes text NOT NULL,
    author text NOT NULL CHECK (author ~ '^(user|session):.+'),
    summary text NOT NULL CHECK (char_length(summary) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, version)
);

CREATE TABLE IF NOT EXISTS directions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    map_id bigint NOT NULL,
    project text NOT NULL,
    node_id bigint NULL,
    text text NOT NULL CHECK (octet_length(text) BETWEEN 1 AND 8192),
    origin text NOT NULL CHECK (origin IN ('user', 'agent')),
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'acknowledged', 'resolved', 'withdrawn')),
    created_by text NOT NULL CHECK (created_by ~ '^(user|session):.+'),
    acknowledged_by text NULL,
    acknowledged_at timestamptz NULL,
    resolution text NOT NULL DEFAULT '' CHECK (octet_length(resolution) <= 4096),
    resolved_by text NULL,
    resolved_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version int NOT NULL DEFAULT 1,
    FOREIGN KEY (map_id, project) REFERENCES maps (id, project) ON DELETE CASCADE,
    FOREIGN KEY (node_id, map_id) REFERENCES nodes (id, map_id) ON DELETE SET NULL (node_id)
);
CREATE INDEX IF NOT EXISTS directions_project_status ON directions (project, status);
CREATE INDEX IF NOT EXISTS directions_map ON directions (map_id);

CREATE TABLE IF NOT EXISTS growth_areas (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    node_id bigint NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 500),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'taken', 'grown', 'dropped')),
    task_key text NULL CHECK (task_key ~ '^T-[1-9][0-9]*$'),
    grown_node_id bigint NULL REFERENCES nodes (id) ON DELETE SET NULL,
    direction_id bigint NULL,
    position int NOT NULL DEFAULT 0,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    created_by text NOT NULL CHECK (created_by ~ '^(user|session):.+'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    version int NOT NULL DEFAULT 1
);
ALTER TABLE growth_areas
    ADD CONSTRAINT growth_areas_direction_fk FOREIGN KEY (direction_id)
    REFERENCES directions (id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS growth_areas_node ON growth_areas (node_id, position);
CREATE INDEX IF NOT EXISTS growth_areas_tsv ON growth_areas USING gin (tsv);

CREATE TABLE IF NOT EXISTS edges (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    src_node_id bigint NOT NULL,
    src_project text NOT NULL,
    dst_node_id bigint NOT NULL,
    dst_project text NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'relates_to', 'builds_on', 'derived_from', 'contradicts', 'supersedes', 'see_also')),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    created_by text NOT NULL CHECK (created_by ~ '^(user|session):.+'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (src_node_id <> dst_node_id),
    UNIQUE (src_node_id, dst_node_id, kind),
    FOREIGN KEY (src_node_id, src_project) REFERENCES nodes (id, project) ON DELETE CASCADE,
    FOREIGN KEY (dst_node_id, dst_project) REFERENCES nodes (id, project) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS edges_dst ON edges (dst_node_id);
CREATE INDEX IF NOT EXISTS edges_src_project ON edges (src_project);
CREATE INDEX IF NOT EXISTS edges_dst_project ON edges (dst_project);

CREATE TABLE IF NOT EXISTS links (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    map_id bigint NOT NULL,
    project text NOT NULL,
    node_id bigint NULL,
    ref_key text NOT NULL CHECK (ref_key ~ '^(T|S)-[1-9][0-9]*$'),
    relation text NOT NULL CHECK (relation IN (
        'researched_by', 'informs', 'grown_by', 'implements', 'cites', 'uses')),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    created_by text NOT NULL CHECK (created_by ~ '^(user|session):.+'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (map_id, node_id, ref_key, relation),
    FOREIGN KEY (map_id, project) REFERENCES maps (id, project) ON DELETE CASCADE,
    FOREIGN KEY (node_id, map_id) REFERENCES nodes (id, map_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS links_ref_key ON links (ref_key);
CREATE INDEX IF NOT EXISTS links_map ON links (map_id);

-- The model name and dimension of the stored vectors. The column type below is the same
-- literal: `test_sql_dim_matches_constant` keeps all three in step.
CREATE TABLE IF NOT EXISTS embedding_config (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    model text NOT NULL,
    dim int NOT NULL
);
INSERT INTO embedding_config (model, dim)
VALUES ('intfloat/multilingual-e5-large-instruct', 1024)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS node_chunks (
    node_id bigint NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    ord int NOT NULL,
    project text NOT NULL,
    content_rev int NOT NULL,
    text text NOT NULL,
    embedding vector(1024) NOT NULL,
    PRIMARY KEY (node_id, ord)
);
CREATE INDEX IF NOT EXISTS node_chunks_embedding
    ON node_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS node_chunks_project ON node_chunks (project);

CREATE TABLE IF NOT EXISTS task_notices (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_key text NOT NULL CHECK (task_key ~ '^T-[1-9][0-9]*$'),
    map_id bigint NOT NULL REFERENCES maps (id) ON DELETE CASCADE,
    actor_session text NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dead')),
    attempts int NOT NULL DEFAULT 0,
    changes int NOT NULL DEFAULT 1,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    due_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS task_notices_one_pending
    ON task_notices (task_key) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS direction_notices (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    direction_id bigint NOT NULL REFERENCES directions (id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dead')),
    attempts int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz NULL
);
CREATE INDEX IF NOT EXISTS direction_notices_pending
    ON direction_notices (next_attempt_at) WHERE status = 'pending';

-- No foreign keys: the log outlives a deleted map or node. `detail` holds ids, keys and field
-- names only, never note text.
CREATE TABLE IF NOT EXISTS events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project text NOT NULL,
    map_id bigint NULL,
    node_id bigint NULL,
    kind text NOT NULL,
    actor text NOT NULL,
    actor_session text NULL,
    at timestamptz NOT NULL DEFAULT now(),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS events_project_id ON events (project, id);
