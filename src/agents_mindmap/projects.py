"""Project validation against the orchestrator (copy of the taskboard lookup).

`GET /api/v1/projects/{slug}` and `GET /api/v1/projects` on the orchestrator service port.
Only active answers are cached (60 s); unknown and archived projects are asked again every
time, so a restore or a create shows up on the next call.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from agents_mindmap.api.errors import ApiError

CACHE_SECONDS = 60.0


class ProjectLookup:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )
        self._clock = clock
        self._active: dict[str, float] = {}
        self._listed: tuple[float, list[str]] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def require_active(self, slug: str) -> None:
        """Return when `slug` is an active project; ApiError 422 `project_unknown` or
        `project_archived`, or 503 `upstream_unavailable` when the orchestrator cannot answer."""
        now = self._clock()
        if self._active.get(slug, float("-inf")) > now:
            return
        try:
            resp = await self._http.get(f"/api/v1/projects/{slug}")
        except httpx.HTTPError:
            raise _unavailable() from None
        if resp.status_code == 404:
            raise ApiError(422, "project_unknown", "unknown project")
        if resp.status_code != 200:
            raise _unavailable()
        try:
            archived = resp.json()["archived"]
        except (ValueError, KeyError, TypeError):
            raise _unavailable() from None
        if not isinstance(archived, bool):
            raise _unavailable()
        if archived:
            raise ApiError(422, "project_archived", "project is archived")
        self._active[slug] = now + CACHE_SECONDS

    async def active_slugs(self) -> list[str]:
        """The slugs of every active project (cached 60 s); 503 when the orchestrator cannot
        answer."""
        now = self._clock()
        if self._listed is not None and self._listed[0] > now:
            return self._listed[1]
        try:
            resp = await self._http.get("/api/v1/projects")
            if resp.status_code != 200:
                raise _unavailable()
            slugs = sorted(str(p["slug"]) for p in resp.json())
        except httpx.HTTPError:
            raise _unavailable() from None
        except (ValueError, KeyError, TypeError):
            raise _unavailable() from None
        self._listed = (now + CACHE_SECONDS, slugs)
        return slugs


def _unavailable() -> ApiError:
    return ApiError(503, "upstream_unavailable", "the orchestrator cannot validate the project")
