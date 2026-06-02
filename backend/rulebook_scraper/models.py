from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any


@dataclass
class Node:
    id: str
    node_type: str
    stable_key: str
    title: str
    text: str = ""
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Edge:
    id: str
    from_node_id: str
    to_node_id: str
    edge_type: str
    source_method: str
    confidence: float = 1.0
    evidence_text: str = ""
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
