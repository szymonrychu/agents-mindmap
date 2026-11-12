# agents-mindmap memory

Lessons that outlive a session. Newest first.

## 2026-11-12 - scaffold decisions (research-mindmaps Task 7)

- Copy sources: agents-vault (CI, Dockerfile, scripts, mise tasks, OpenAPI export) and agents-taskboard (three listeners: web allowlist, delegate bearer list, service relay bearer). Unlike taskboard, `mindmap serve` runs all listeners in one process with one pool (spec 3.1 "api (three listeners)"); the delegate listener starts only when `MINDMAP_DELEGATE_TOKENS` is set.
- Path prefix `/mindmap/api/v1` on every listener (the agents-web basePath; the ingress routes `/mindmap` without a rewrite, as taskboard does with `/taskboard/api/v1`). The orchestrator relay (Task 15) must target `<mindmap_url>/mindmap/api/v1`.
- Error body follows the siblings: `{"error": {"code", "message", ...extra}}` (the plan line `{"error": code, "message": text}` was read as shorthand for that shape, so web and plugin clients parse one shape across services).
- `idle_in_transaction_session_timeout` is set through the libpq `options` startup parameter on every pool connection (`runtime.open_pool`), not a `SET` in a configure callback: it survives reconnects and leaves no transaction open on a fresh connection.
- Settings: every token pair distinct (secrets invariant 9), the error names both env vars only. Taskboard token required when the taskboard url is set. `public_base_url` is the agents-web host, not the api.
- CLI registers only `migrate` and `serve`; `notifier` (Task 13), `embedder` and `reembed` (Task 21) are registered with their code, so no release carries a stub.
- Tests override the agents_common `pg_url` fixture with `pgvector/pgvector:0.8.7-pg16` (pinned): pgvector must be created by a superuser, as CNPG `postInitApplicationSQL` does live.
- Chart skeleton: one `api` Deployment (`mindmap serve`, migrate init container), Services `-web` (80), `-service` (8082) and `-delegate` (8081, only with `delegate.tokensSecret.name`), secret values only from external Secrets (no chart Secret). Task 11 completes it (oauth2-proxy sidecar, notifier and embedder Deployments).
