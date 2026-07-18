"""Bounded, cycle-safe Value and Artifact lineage queries."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.data import ArtifactLinkType


@dataclass(frozen=True)
class LineageGraph:
    root_id: object
    nodes: tuple[object, ...]
    links: tuple[object, ...]
    truncated: bool = False


class LineageQueryService:
    def __init__(self, uow_factory) -> None: self.uow_factory = uow_factory

    def artifact(self, artifact_id, *, direction="both", max_depth=20, max_nodes=1000):
        if direction not in {"upstream", "downstream", "both"}: raise ValueError("invalid lineage direction")
        with self.uow_factory() as uow:
            root = uow.artifacts.get(artifact_id, committed_only=True)
            if root is None: raise KeyError(str(artifact_id))
            seen, frontier, links, truncated = {artifact_id}, [(artifact_id, 0)], [], False
            while frontier:
                current, depth = frontier.pop(0)
                if depth >= max_depth: truncated = True; continue
                for link in uow.artifact_links.list_for_artifact(current):
                    if link.link_type is ArtifactLinkType.DERIVED_FROM and direction in {"upstream", "both"}:
                        other = link.target_id
                    else:
                        links.append(link); continue
                    links.append(link)
                    if other not in seen:
                        if len(seen) >= max_nodes: truncated = True; continue
                        seen.add(other); frontier.append((other, depth + 1))
                if direction in {"downstream", "both"}:
                    for candidate in uow.artifacts.list_by_run(root.run_id):
                        for link in uow.artifact_links.list_for_artifact(candidate.artifact_id, link_type=ArtifactLinkType.DERIVED_FROM):
                            if link.target_id == current:
                                links.append(link)
                                if candidate.artifact_id not in seen and len(seen) < max_nodes:
                                    seen.add(candidate.artifact_id); frontier.append((candidate.artifact_id, depth + 1))
            return LineageGraph(artifact_id, tuple(sorted(seen, key=str)), tuple(dict.fromkeys(links)), truncated)

    def value(self, value_id, *, direction="both"):
        with self.uow_factory() as uow:
            if uow.values.get(value_id) is None: raise KeyError(str(value_id))
            links = uow.value_links.list_for_value(value_id, direction=direction)
            nodes = {value_id}
            for link in links: nodes.update((link.source_value_id, link.target_value_id))
            return LineageGraph(value_id, tuple(sorted(nodes, key=str)), links)
