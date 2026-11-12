# agents-mindmap

Part of the workdir project (python).

## What this is

Research mind maps per project for the agents system: maps, nodes with versioned notes, growth
areas, edges, links to tasks and specs, directions, lexical and semantic search. The service
has its own Postgres database (pgvector). Agents reach it through the orchestrator relay, users
through the Mindmaps page of agents-web.

This is the scaffold: settings, the three listeners with their probes and gates, the error
shape, the OpenAPI export and the chart skeleton. The routes come in later releases.

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
- `mindmap serve` - serve the web listener on `MINDMAP_HTTP_PORT` (source allowlist), the
  service listener on `MINDMAP_SERVICE_PORT` (bearer `MINDMAP_RELAY_TOKEN`) and, when
  `MINDMAP_DELEGATE_TOKENS` is set, the delegate listener on `MINDMAP_DELEGATE_PORT`.

## Configuration

Every process reads one validated `MINDMAP_*` environment. A bad or missing variable makes the
process exit with code 78 and one line per variable, naming variables only, never values.

| Variable | Default | Meaning |
|---|---|---|
| `MINDMAP_DATABASE_URL` | required | Postgres DSN of the service's own database |
| `MINDMAP_ORCHESTRATOR_URL` | required | orchestrator service port (projects, sessions, messages) |
| `MINDMAP_ORCHESTRATOR_TOKEN` | - | token mindmap sends to the orchestrator; exactly one of this and the `_FILE` variable is required |
| `MINDMAP_ORCHESTRATOR_TOKEN_FILE` | - | path of a file holding that token |
| `MINDMAP_PUBLIC_BASE_URL` | required | agents-web base URL; fallback links are `<base>/mindmaps/M-<n>` |
| `MINDMAP_RELAY_TOKEN` | required | bearer token the service listener accepts (the orchestrator relay) |
| `MINDMAP_DELEGATE_TOKENS` | empty | comma-separated bearer tokens of the delegate listener; empty turns it off |
| `MINDMAP_TASKBOARD_URL` | empty | taskboard delegate listener for task notices; empty turns notices off |
| `MINDMAP_TASKBOARD_TOKEN` | - | token mindmap sends to the taskboard; required when the url is set |
| `MINDMAP_EMBEDDER_URL` | empty | OpenAI-shaped embeddings endpoint; empty turns semantic search off |
| `MINDMAP_EMBEDDER_MODEL` | `intfloat/multilingual-e5-large-instruct` | embedding model |
| `MINDMAP_EMBED_DIM` | `1024` | embedding dimension |
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

## OpenAPI

`uv run python -m agents_mindmap.api.openapi > openapi.json` documents the web listener;
`--service` documents the service listener.
