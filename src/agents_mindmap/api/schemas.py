"""Request and response models. Text limits are byte limits where the database checks bytes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from agents_mindmap.db.directions import DirectionRow
from agents_mindmap.db.edges import EdgeRow
from agents_mindmap.db.growth import GrowthRow
from agents_mindmap.db.keys import fmt_key
from agents_mindmap.db.links import LinkRow
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
TaskKey = Annotated[str, StringConstraints(pattern=r"^T-[1-9][0-9]{0,17}$")]
RefKey = Annotated[str, StringConstraints(pattern=r"^(T|S)-[1-9][0-9]{0,17}$")]
GrowthText = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
EdgeNote = Annotated[str, Field(max_length=500)]
Text8k = Annotated[str, StringConstraints(min_length=1, max_length=8192), _max_bytes(8192)]
Resolution = Annotated[str, Field(max_length=4096), _max_bytes(4096)]
MapKind = Literal["research", "knowledge"]
MapStatus = Literal["active", "archived"]
NodeStatus = Literal["seed", "growing", "settled", "stale", "archived"]
GrowthStatus = Literal["open", "taken", "grown", "dropped"]
EdgeKind = Literal[
    "relates_to", "builds_on", "derived_from", "contradicts", "supersedes", "see_also"
]
LinkRelation = Literal["researched_by", "informs", "grown_by", "implements", "cites", "uses"]
DirectionAction = Literal["acknowledge", "resolve", "withdraw"]
DirectionStatus = Literal["open", "acknowledged", "resolved", "withdrawn"]


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


class LinkIn(Body):
    ref_key: RefKey
    relation: LinkRelation = "researched_by"
    note: EdgeNote = ""


class EdgeSpec(Body):
    """An edge from the node being created to `dst`."""

    dst: NodeKey
    kind: EdgeKind = "relates_to"
    note: EdgeNote = ""


class NodeIn(Body):
    parent: NodeKey | None = None
    topic: Title
    notes: Notes = ""
    summary: Summary = "created"
    status: NodeStatus = "seed"
    client_key: ClientKey | None = None
    growth_areas: list[GrowthText] = Field(default_factory=list, max_length=50)
    links: list[LinkIn] = Field(default_factory=list, max_length=20)
    edges: list[EdgeSpec] = Field(default_factory=list, max_length=20)


class NodePatch(Body):
    version: int = Field(ge=1)
    topic: Title | None = None
    notes: Notes | None = None
    summary: Summary | None = None
    status: NodeStatus | None = None
    parent: NodeKey | None = None


class GrowthIn(Body):
    text: GrowthText


class GrowthPatch(Body):
    version: int = Field(ge=1)
    status: GrowthStatus | None = None
    task_key: TaskKey | None = None
    text: GrowthText | None = None
    position: int | None = Field(default=None, ge=0, le=10000)
    grown_node: NodeKey | None = None


class GrowthView(BaseModel):
    key: str
    node: str
    text: str
    status: str
    task_key: str | None
    grown_node: str | None
    direction: str | None
    position: int
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int

    @classmethod
    def of(cls, row: GrowthRow) -> GrowthView:
        return cls(
            key=row.key,
            node=fmt_key("N", row.node_id),
            text=row.text,
            status=row.status,
            task_key=row.task_key,
            grown_node=None if row.grown_node_id is None else fmt_key("N", row.grown_node_id),
            direction=None if row.direction_id is None else fmt_key("D", row.direction_id),
            position=row.position,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            version=row.version,
        )


class EdgeIn(Body):
    src: NodeKey
    dst: NodeKey
    kind: EdgeKind = "relates_to"
    note: EdgeNote = ""


class EdgeView(BaseModel):
    key: str
    src: str
    src_project: str
    dst: str
    dst_project: str
    kind: str
    note: str
    created_by: str
    created_at: datetime

    @classmethod
    def of(cls, row: EdgeRow) -> EdgeView:
        return cls(
            key=row.key,
            src=fmt_key("N", row.src_node_id),
            src_project=row.src_project,
            dst=fmt_key("N", row.dst_node_id),
            dst_project=row.dst_project,
            kind=row.kind,
            note=row.note,
            created_by=row.created_by,
            created_at=row.created_at,
        )


class LinkView(BaseModel):
    id: int
    map: str
    node: str | None
    project: str
    ref_key: str
    relation: str
    note: str
    created_by: str
    created_at: datetime

    @classmethod
    def of(cls, row: LinkRow) -> LinkView:
        return cls(
            id=row.id,
            map=row.map_key,
            node=row.node_key,
            project=row.project,
            ref_key=row.ref_key,
            relation=row.relation,
            note=row.note,
            created_by=row.created_by,
            created_at=row.created_at,
        )


class DirectionIn(Body):
    node: NodeKey | None = None
    text: Text8k
    growth_areas: list[GrowthText] = Field(default_factory=list, max_length=20)
    task: TaskKey | None = None


class DirectionPatch(Body):
    version: int = Field(ge=1)
    action: DirectionAction
    resolution: Resolution = ""


class DirectionView(BaseModel):
    key: str
    map: str
    node: str | None
    project: str
    text: str
    origin: str
    status: str
    created_by: str
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    resolution: str
    resolved_by: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    growth_areas: list[str]

    @classmethod
    def of(cls, row: DirectionRow, growth_areas: list[str] | None = None) -> DirectionView:
        return cls(
            key=row.key,
            map=fmt_key("M", row.map_id),
            node=None if row.node_id is None else fmt_key("N", row.node_id),
            project=row.project,
            text=row.text,
            origin=row.origin,
            status=row.status,
            created_by=row.created_by,
            acknowledged_by=row.acknowledged_by,
            acknowledged_at=row.acknowledged_at,
            resolution=row.resolution,
            resolved_by=row.resolved_by,
            resolved_at=row.resolved_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            version=row.version,
            growth_areas=growth_areas or [],
        )


class KnowledgeNoteIn(Body):
    project: Project | None = None
    parent: NodeKey | None = None
    topic: Title
    notes: Notes
    summary: Summary
    client_key: ClientKey | None = None
    links: list[LinkIn] = Field(default_factory=list, max_length=20)
    edges: list[EdgeSpec] = Field(default_factory=list, max_length=20)


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
    growth_areas: list[GrowthView] = Field(default_factory=list)
    edges: list[EdgeView] = Field(default_factory=list)
    links: list[LinkView] = Field(default_factory=list)
    directions: list[DirectionView] = Field(default_factory=list)

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
