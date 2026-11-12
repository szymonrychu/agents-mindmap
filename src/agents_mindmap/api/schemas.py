"""Request and response models. Text limits are byte limits where the database checks bytes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from agents_mindmap.db.maps import MapRow
from agents_mindmap.db.nodes import NodeRow
from agents_mindmap.db.versions import VersionMeta, VersionRow

SLUG = r"^[a-z0-9][a-z0-9-]{0,39}$"
MAP_SLUG = r"^[a-z0-9][a-z0-9-]{0,63}$"
MAP_KEY = r"^M-[1-9][0-9]{0,17}$"
NODE_KEY = r"^N-[1-9][0-9]{0,17}$"


def _max_bytes(limit: int):
    def check(value: str) -> str:
        if len(value.encode()) > limit:
            raise ValueError(f"longer than {limit} bytes")
        return value

    return AfterValidator(check)


Project = Annotated[str, StringConstraints(pattern=SLUG)]
Title = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
Summary = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
Description = Annotated[str, Field(max_length=8192), _max_bytes(8192)]
Notes = Annotated[str, Field(max_length=65536), _max_bytes(65536)]
ClientKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]
MapKey = Annotated[str, StringConstraints(pattern=MAP_KEY)]
NodeKey = Annotated[str, StringConstraints(pattern=NODE_KEY)]
MapKind = Literal["research", "knowledge"]
MapStatus = Literal["active", "archived"]
NodeStatus = Literal["seed", "growing", "settled", "stale", "archived"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MapIn(Body):
    project: Project
    slug: Annotated[str, StringConstraints(pattern=MAP_SLUG)]
    title: Title
    description: Description = ""
    kind: MapKind = "research"
    root_topic: Title | None = None
    client_key: ClientKey | None = None


class MapPatch(Body):
    version: int = Field(ge=1)
    title: Title | None = None
    description: Description | None = None
    status: MapStatus | None = None


class MapView(BaseModel):
    key: str
    project: str
    slug: str
    kind: str
    title: str
    description: str
    root: str | None
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int

    @classmethod
    def of(cls, row: MapRow) -> MapView:
        return cls(
            key=row.key,
            project=row.project,
            slug=row.slug,
            kind=row.kind,
            title=row.title,
            description=row.description,
            root=None if row.root_node_id is None else f"N-{row.root_node_id}",
            status=row.status,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            version=row.version,
        )


class NodeIn(Body):
    parent: NodeKey | None = None
    topic: Title
    notes: Notes = ""
    summary: Summary = "created"
    status: NodeStatus = "seed"
    client_key: ClientKey | None = None


class NodePatch(Body):
    version: int = Field(ge=1)
    topic: Title | None = None
    notes: Notes | None = None
    summary: Summary | None = None
    status: NodeStatus | None = None
    parent: NodeKey | None = None


class NodeView(BaseModel):
    key: str
    map: str
    project: str
    parent: str | None
    topic: str
    notes: str
    status: str
    current_version: int
    content_rev: int
    embedding_pending: bool
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    version: int

    @classmethod
    def of(cls, row: NodeRow) -> NodeView:
        return cls(
            key=row.key,
            map=row.map_key,
            project=row.project,
            parent=row.parent_key,
            topic=row.topic,
            notes=row.notes,
            status=row.status,
            current_version=row.current_version,
            content_rev=row.content_rev,
            embedding_pending=row.embedded_rev < row.content_rev,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
            version=row.version,
        )


class VersionMetaView(BaseModel):
    version: int
    author: str
    summary: str
    created_at: datetime

    @classmethod
    def of(cls, row: VersionMeta) -> VersionMetaView:
        return cls(**row.__dict__)


class VersionView(VersionMetaView):
    topic: str
    notes: str

    @classmethod
    def of(cls, row: VersionRow) -> VersionView:  # type: ignore[override]
        return cls(**row.__dict__)


class ProjectCount(BaseModel):
    project: str
    maps: int
    nodes: int
