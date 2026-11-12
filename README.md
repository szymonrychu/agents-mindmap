# agents-mindmap

Part of the workdir project (python).

## What this is

Research mind maps per project for the agents system: maps, nodes with versioned notes, growth
areas, edges, links to tasks and specs, directions, lexical and semantic search. The service
has its own Postgres database (pgvector). Agents reach it through the orchestrator relay, users
through the Mindmaps page of agents-web.

Released so far: maps, nodes with versions, growth areas, edges, links, directions, knowledge
notes, traversal, the change feed, the chart, the task-notice notifier, and hybrid search
(lexical plus semantic) with its embedder process.

## Search and embeddings

`GET /search?q=&mode=hybrid|lexical|semantic` fuses up to three ranked lists with reciprocal
rank fusion (k = 60): full text (`websearch_to_tsquery`, every word must match), fuzzy topic
(pg_trgm) and chunk similarity (pgvector cosine, top 50 chunks, best chunk per node,
`hnsw.ef_search = 80`). Each hit names its `signals`; the snippet is a `[[`/`]]` headline, else
the best chunk's text, else the topic. The query is embedded before a database connection is
taken, with a 2 s limit; when the embedder is unset, down or slow the answer carries
`semantic: false` and lexical results (also for `mode=semantic`), and `pending_embedding`
counts the readable nodes whose chunks lag their content.

`mindmap embedder` (one replica, advisory lock) chunks every node whose `embedded_rev` lags
`content_rev`: chunk 0 is the topic, the growth-area texts and the first notes paragraph, the
rest is split on headings and paragraphs into about 350 tokens with a 50-token overlap. It
claims a few nodes in a short transaction, calls `POST <MINDMAP_EMBEDDER_URL>/v1/embeddings`
(TEI or any OpenAI-shaped server) with no transaction open, then replaces the chunks only if
the node's `content_rev` is unchanged (else the node stays pending). While the server is down
it backs off from `MINDMAP_EMBEDDER_POLL_SECONDS` doubling up to
`MINDMAP_EMBEDDER_BACKOFF_MAX_SECONDS`. Queries use the e5-instruct form
`Instruct: <task>\nQuery: <q>`; passages are sent unprefixed. Prometheus metrics
`mindmap_embed_pending` and `mindmap_embed_seconds` are served on `MINDMAP_METRICS_PORT`
(`/metrics`, embedder process only).

A model change is a migration of `embedding_config` and the vector column plus
`mindmap reembed`, which marks every node pending; the old chunks serve until replaced.

Golden-set evaluation (`eval/golden.yaml`, 28 English and Polish queries over 30 notes):
CI runs the harness with a fake embedder (`tests/test_golden_eval.py`); against a live server
run `uv run python eval/run.py --embedder-url http://127.0.0.1:8089` (after
`kubectl -n agents port-forward svc/agents-embeddings 8089:80`), which seeds a throwaway
pgvector container and exits 1 below the `live` floors (recall@10 0.7, MRR 0.5).

## Notices

Every write appends an event through one function, which also enqueues a `task_notices` row
for each task (`T-n`) linked to the map (map or node level); changes coalesce into one pending
row per task, due 60 s (`MINDMAP_NOTICE_DEBOUNCE_SECONDS`) after the first. `mindmap notifier`
(one replica, advisory lock) sends `POST /taskboard/api/v1/tasks/{key}/external-changes` with
`{"source": "mindmap", "ref": "M-3", "actor_session": ...}`: 2xx marks sent, 404 dead, anything
else backs off and retries (at-least-once). The taskboard computes the audience and sends its
usual `Task T-n was updated` notice; the writing session is excluded when all coalesced changes
are its own. The embedder's `node_embedded` events never notify. A direction on a map with no
linked task is announced instead by one content-free message to the live sessions of the map's
project (keys and a link, no direction text).

## Development

Run from this directory with mise:

- `mise run test` - tests (the database tests start `pgvector/pgvector:0.8.7-pg16` through
  testcontainers and need Docker)
- `mise run lint` - ruff check and format check
- `mise run fmt` - ruff format
- `mise run test:chart` / `mise run lint:chart` - chart render tests and helm lint

## Commands

The `mindmap` entry point (`uv run mindmap <command>`):

- `mindmap migrate` - apply the schema migrations to the service's own database and exit.
- `mindmap serve-web` - serve the web listener on `MINDMAP_HTTP_PORT` (source allowlist).
- `mindmap serve-service` - serve the service listener on `MINDMAP_SERVICE_PORT` (bearer
  `MINDMAP_RELAY_TOKEN`, required here only).
- `mindmap serve-delegate` - serve the delegate listener on `MINDMAP_DELEGATE_PORT` (bearer
  `MINDMAP_DELEGATE_TOKENS`, required here only).
- `mindmap notifier` - drain the task-notice and direction outboxes (needs
  `MINDMAP_TASKBOARD_URL`).
- `mindmap embedder` - chunk and embed changed nodes (needs `MINDMAP_EMBEDDER_URL`).
- `mindmap reembed` - mark every node for re-embedding and exit.

The listeners and the notifier call the orchestrator and need `MINDMAP_ORCHESTRATOR_TOKEN`;
`migrate`, `embedder` and `reembed` run without it.

Each listener runs in its own process and pod, so a pod holds only the tokens it uses.

## Configuration

Every process reads one validated `MINDMAP_*` environment. A bad or missing variable makes the
process exit with code 78 and one line per variable, naming variables only, never values.

| Variable | Default | Meaning |
|---|---|---|
| `MINDMAP_DATABASE_URL` | required | Postgres DSN of the service's own database |
| `MINDMAP_ORCHESTRATOR_URL` | required | orchestrator service port (projects, sessions, messages) |
| `MINDMAP_ORCHESTRATOR_TOKEN` | - | token mindmap sends to the orchestrator; this or the `_FILE` variable is required by the listeners and the notifier |
| `MINDMAP_ORCHESTRATOR_TOKEN_FILE` | - | path of a file holding that token |
| `MINDMAP_PUBLIC_BASE_URL` | required | agents-web base URL; fallback links are `<base>/mindmaps/M-<n>` |
| `MINDMAP_RELAY_TOKEN` | required | bearer token the service listener accepts (the orchestrator relay) |
| `MINDMAP_DELEGATE_TOKENS` | empty | comma-separated bearer tokens of the delegate listener; empty turns it off |
| `MINDMAP_TASKBOARD_URL` | empty | taskboard delegate listener for task notices; empty turns notices off |
| `MINDMAP_TASKBOARD_TOKEN` | - | token mindmap sends to the taskboard; required when the url is set |
| `MINDMAP_EMBEDDER_URL` | empty | OpenAI-shaped embeddings endpoint; empty turns semantic search off |
| `MINDMAP_EMBEDDER_MODEL` | `intfloat/multilingual-e5-large-instruct` | embedding model |
| `MINDMAP_EMBED_DIM` | `1024` | embedding dimension |
| `MINDMAP_EMBEDDER_TIMEOUT_SECONDS` | `120` | embedder process request timeout (queries wait 2 s) |
| `MINDMAP_EMBEDDER_BATCH_SIZE` | `8` | texts per embeddings request |
| `MINDMAP_EMBEDDER_POLL_SECONDS` | `5` | embedder idle poll and first backoff step |
| `MINDMAP_EMBEDDER_BACKOFF_MAX_SECONDS` | `300` | embedder backoff cap while the server is down |
| `MINDMAP_METRICS_PORT` | `9102` | Prometheus port of the embedder process |
| `MINDMAP_NOTICE_DEBOUNCE_SECONDS` | `60` | debounce of task notices |
| `MINDMAP_NOTIFIER_POLL_SECONDS` | `5` | notifier poll interval |
| `MINDMAP_NOTIFIER_BACKOFF_MAX_SECONDS` | `60` | notifier retry backoff cap |
| `MINDMAP_IDLE_IN_TRANSACTION_TIMEOUT_MS` | `30000` | `idle_in_transaction_session_timeout` of every pool connection |
| `MINDMAP_ALLOWED_CIDRS` | `127.0.0.1/32` | comma-separated CIDRs allowed on the web listener |
| `MINDMAP_HTTP_HOST` | `0.0.0.0` | bind address of every listener |
| `MINDMAP_HTTP_PORT` | `8080` | web listener port |
| `MINDMAP_DELEGATE_PORT` | `8081` | delegate listener port |
| `MINDMAP_SERVICE_PORT` | `8082` | service listener port |
| `MINDMAP_LOG_LEVEL` | `INFO` | log level |
| `MINDMAP_SHUTDOWN_GRACE_SECONDS` | `20` | graceful shutdown wait |

The ports must differ. The relay token, every delegate token, the orchestrator token and the
taskboard token must be pairwise different: a token mindmap sends never opens one of its own
listeners.

`GET /healthz` and `GET /readyz` (`503 not_ready` when the database is unreachable) are served
on every listener under no prefix and are exempt from every gate. The routes live under
`/mindmap/api/v1`. Every error answers `{"error": {"code": "...", "message": "..."}}`.

## API

Base path `/mindmap/api/v1`; JSON in and out; guarded updates carry `version` (409
`version_conflict` answers with the current `version`); creates take an optional `client_key`
for idempotence. Two machine-readable contracts, regenerated with
`uv run python -m agents_mindmap.api.openapi > openapi.json` and `... --service >
openapi-service.json`; a test fails when either drifts from the apps or when a route is missing
from the lists below.

### Web and delegate routes

The web listener refuses a socket peer outside `MINDMAP_ALLOWED_CIDRS` with `403 forbidden`
(forwarding headers are never read); the delegate listener serves the same routes as the user
actor behind `Authorization: Bearer <delegate token>` (`401 unauthorized` otherwise).

- `GET /projects` - projects with maps, with counts
- `GET /maps` - map list (`project`, `status`, `kind`)
- `POST /maps` - create a map with its root node
- `GET /maps/{key}`, `PATCH /maps/{key}`, `DELETE /maps/{key}` - read, edit or archive, hard delete
- `GET /maps/{key}/graph` - tree nodes, edges and cross-map stubs
- `POST /maps/{key}/nodes` - create a node
- `GET /nodes/{key}`, `PATCH /nodes/{key}`, `DELETE /nodes/{key}` - read, edit, hard delete
- `GET /nodes/{key}/versions`, `GET /nodes/{key}/versions/{v}` - note history
- `GET /nodes/{key}/traverse` - bounded traversal
- `POST /nodes/{key}/growth-areas`, `PATCH /growth-areas/{key}`, `DELETE /growth-areas/{key}` - growth areas
- `POST /edges`, `DELETE /edges/{key}` - typed edges
- `GET /links`, `POST /maps/{key}/links`, `POST /nodes/{key}/links`, `DELETE /links/{link_id}` - task and spec links
- `GET /directions`, `POST /maps/{key}/directions`, `PATCH /directions/{key}` - directions
- `POST /knowledge/notes` - capture a knowledge note
- `GET /search` - hybrid search (lexical and semantic)
- `GET /events` - change feed as JSON: events past `after` in id order (`project`, `limit`)
- `GET /stream` - change feed as server-sent events (`project`, `after` or `Last-Event-ID`)

The stream sends `mindmap_changed` events with `id: <events.id>` and data
`{"project", "map", "node", "kind", "id"}` (keys and kind only, never note text). A client that
reconnects with `Last-Event-ID` first gets one event per map and node changed since, then the
live feed; a cursor older than the kept events gets one `reset`. The feed is on the web and
delegate listeners only.

### Service routes

The service listener answers `401 unauthorized` unless `Authorization: Bearer <relay token>`
matches and takes the acting agent from `X-Orch-Session` (required), `X-Orch-Slug` and
`X-Orch-Projects` (`*` or 1..10 distinct project slugs, home first; `400 actor_required`). It
serves the routes above except hard deletes and the change feed.

- `GET /projects` - projects with maps, with counts
- `GET /maps`, `POST /maps`, `GET /maps/{key}`, `PATCH /maps/{key}` - maps
- `GET /maps/{key}/graph` - tree nodes, edges and cross-map stubs
- `POST /maps/{key}/nodes` - create a node
- `GET /nodes/{key}`, `PATCH /nodes/{key}` - read, edit
- `GET /nodes/{key}/versions`, `GET /nodes/{key}/versions/{v}` - note history
- `GET /nodes/{key}/traverse` - bounded traversal
- `POST /nodes/{key}/growth-areas`, `PATCH /growth-areas/{key}` - growth areas
- `POST /edges`, `DELETE /edges/{key}` - typed edges
- `GET /links`, `POST /maps/{key}/links`, `POST /nodes/{key}/links`, `DELETE /links/{link_id}` - task and spec links
- `GET /directions`, `POST /maps/{key}/directions`, `PATCH /directions/{key}` - directions
- `POST /knowledge/notes` - capture a knowledge note
- `GET /search` - hybrid search (lexical and semantic)

## Conventions

- Reads by key (`GET /maps/{key}`, `/nodes/{key}`, `/nodes/{key}/versions[/v]`) are unscoped for the user (web, delegate), including archived projects; list routes show active projects.
- User identity: `X-Forwarded-Email`, else `X-Forwarded-User`, else the anonymous author `user:unknown` (agents-vault order and limits; the DB check needs `user:.+`).
- A 409 `version_conflict` carries `version`, the current optimistic row guard (the field name clients send on PATCH). `current_version` on a node is the latest note version; `content_rev` counts content changes for re-embedding and also covers growth-area text later.
