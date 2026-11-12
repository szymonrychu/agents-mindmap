# agents-mindmap memory

Lessons that outlive a session. Newest first.

## 2026-11-12 - schema, maps, nodes (research-mindmaps Task 8)

- `0001_mindmap.sql` hardcodes `vector(1024)` and the seeded `embedding_config` row (a static .sql cannot read Python); `test_sql_dim_matches_constant` ties them to `EMBED_DIM`/`EMBED_MODEL`. Provisional until the TEI spike (Task 20) confirms; changing it before the first deploy edits 0001, after it needs a new migration plus `reembed`. `serve` refuses to start on a mismatch (`verify_embedding_config`, names `MINDMAP_EMBED_DIM` / `MINDMAP_EMBEDDER_MODEL`).
- Tests: the session container's `template1` carries `vector` (CNPG does this at bootstrap); `bare_db_url` clones `template0` for the guard test.
- Two counters on a node: `version` is the optimistic row guard sent by clients on every PATCH (bumped by any change); `current_version` is the latest `node_versions` row (bumped only by a topic or notes change). A PATCH locks the row (`FOR UPDATE`), so a concurrent loser sees the new version and answers 409 `version_conflict` with `version` (the row guard, same name as the request field) in the error body; `current_version` only ever means the note version. `content_rev` is kept next to `current_version` on purpose (2026-11-12): the spec bumps it for growth-area text changes too (embedding drive), so the two diverge once growth areas land.
- A PATCH that changes nothing returns the node unchanged without bumping. A topic or notes change without `summary` is 422 `summary_required`.
- Reparenting takes `pg_advisory_xact_lock(map_id)` so two concurrent moves cannot build a cycle; the cycle check is a recursive CTE up the parents. The root cannot be moved or deleted alone (422 `root_immovable`); deleting a node deletes its subtree.
- Events survive deletes (no foreign keys); `detail` holds keys and field names only. The validation handler no longer repeats an unknown field name (caller text).
- User author: `user:<who>` from `X-Forwarded-Email`, else `X-Forwarded-User`, else `user:unknown` (agents-vault `actor_of` order and limits: printable, at most 200 chars; the anonymous value is `user:unknown` not `user` because the created_by CHECK needs `user:.+`); delegate callers without a proxy are `user:unknown`. Reads by key (maps, nodes, versions) answer 404 for rows of inactive projects, as the list routes hide them (2026-11-12). `ProjectLookup` raises `project_unknown` (the spec code; the taskboard copy says `unknown_project`) and lists active slugs for `readable`; Task 9 moves scope into `Actor`.

## 2026-11-12 - scaffold decisions (research-mindmaps Task 7)

- Copy sources: agents-vault (CI, Dockerfile, scripts, mise tasks, OpenAPI export) and agents-taskboard (three listeners: web allowlist, delegate bearer list, service relay bearer). Unlike taskboard, `mindmap serve` runs all listeners in one process with one pool (spec 3.1 "api (three listeners)"); the delegate listener starts only when `MINDMAP_DELEGATE_TOKENS` is set.
- Path prefix `/mindmap/api/v1` on every listener (the agents-web basePath; the ingress routes `/mindmap` without a rewrite, as taskboard does with `/taskboard/api/v1`). The orchestrator relay (Task 15) must target `<mindmap_url>/mindmap/api/v1`.
- Error body follows the siblings: `{"error": {"code", "message", ...extra}}` (the plan line `{"error": code, "message": text}` was read as shorthand for that shape, so web and plugin clients parse one shape across services).
- `idle_in_transaction_session_timeout` is set through the libpq `options` startup parameter on every pool connection (`runtime.open_pool`), not a `SET` in a configure callback: it survives reconnects and leaves no transaction open on a fresh connection.
- Settings: every token pair distinct (secrets invariant 9), the error names both env vars only. Taskboard token required when the taskboard url is set. `public_base_url` is the agents-web host, not the api.
- CLI registers only `migrate` and `serve`; `notifier` (Task 13), `embedder` and `reembed` (Task 21) are registered with their code, so no release carries a stub.
- Tests override the agents_common `pg_url` fixture with `pgvector/pgvector:0.8.7-pg16` (pinned): pgvector must be created by a superuser, as CNPG `postInitApplicationSQL` does live.
- Chart skeleton: one `api` Deployment (`mindmap serve`, migrate init container), Services `-web` (80), `-service` (8082) and `-delegate` (8081, only with `delegate.tokensSecret.name`), secret values only from external Secrets (no chart Secret). Task 11 completes it (oauth2-proxy sidecar, notifier and embedder Deployments).
