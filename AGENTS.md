# agents-mindmap

Component `agents-mindmap` (python) of the workdir project. Its own tests, `MEMORY.md` and `ROADMAP.md` live here.

Run `mise run test`, `mise run lint` and `mise run fmt` from this directory.

## Commands

- `mise run test` / `lint` / `fmt`; `mise run test:chart` / `lint:chart` for the chart.
- `mindmap migrate | serve-web | serve-service | serve-delegate | notifier | embedder | reembed` (see README); the image entrypoint is `mindmap`.
- Release: the `release` job of `.github/workflows/ci.yaml` runs last on a push to main and tags the image and the repo `vX.Y.Z` from conventional commits (`scripts/next_version.py`, byte-identical in every component that releases this way). Deploy only semver tags.
- OpenAPI: `uv run python -m agents_mindmap.api.openapi > openapi.json` and `--service > openapi-service.json`; every route is listed in `ROUTE_ERRORS` (`USER_ONLY` routes are absent from the service document) and in the README API section; tests fail on drift.
- Change feed: `GET /events` and `GET /stream` (web and delegate only); `api/stream.py` hub, NOTIFY is a hint, the events table is the source.

## Hard rules

- Tokens are `SecretStr` with `repr=False`; the relay token, delegate tokens, orchestrator token and taskboard token are pairwise distinct. Errors name env vars, never values. Never log settings, request bodies or upstream bodies.
- Web listener: source allowlist on the socket peer only (never Forwarded headers). Delegate listener: bearer on the delegate token list. Service listener: bearer on the relay token. Probes are exempt.
- Error bodies are `{"error": {"code", "message", ...}}` with fixed messages; never echo caller input.
- Never hold a database transaction across an outbound call (orchestrator, taskboard, embedder); every pool connection carries `idle_in_transaction_session_timeout`.
- Only implemented CLI commands are registered: no stub ships in a release.
- Chart: secret values only from external Secrets; the chart renders no Secret; `image.tag` must be semver `X.Y.Z`; notifier and embedder Deployments stay off until enabled.
