"""Public keys: `M-<id>` maps, `N-<id>` nodes, `G-<id>` growth areas, `E-<id>` edges,
`D-<id>` directions. A malformed key is the same 404 as an unknown one."""

import re

from agents_mindmap.api.errors import ApiError

_KEY = re.compile(r"([A-Z])-([1-9][0-9]{0,17})")


def fmt_key(prefix: str, id_: int) -> str:
    return f"{prefix}-{id_}"


def parse_key(prefix: str, key: str) -> int:
    """The numeric id of a `<prefix>-<id>` key; ApiError 404 `not_found` otherwise."""
    match = _KEY.fullmatch(key)
    if match is None or match.group(1) != prefix:
        raise ApiError(404, "not_found", "not found")
    return int(match.group(2))
