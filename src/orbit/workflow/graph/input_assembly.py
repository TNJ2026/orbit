"""Deterministic multi-source Join input assembly."""

from __future__ import annotations

from typing import Any, Mapping

from ..domain.graph import JoinMergeMode
from ..domain.serialization import freeze_json


class InputAssemblyError(ValueError):
    code = "mapping_failed"


def assemble_join_inputs(
    merge_mode: JoinMergeMode,
    values_by_edge: Mapping[str, Any],
    ordered_edge_ids: tuple[str, ...],
) -> Any:
    values = [(edge_id, values_by_edge[edge_id]) for edge_id in ordered_edge_ids if edge_id in values_by_edge]
    if not values:
        raise InputAssemblyError("join has no selected input values")
    if merge_mode is JoinMergeMode.SINGLE:
        if len(values) != 1:
            raise InputAssemblyError("single merge requires exactly one value")
        return freeze_json(values[0][1])
    if merge_mode is JoinMergeMode.FIRST_BY_PRIORITY:
        return freeze_json(values[0][1])
    if merge_mode is JoinMergeMode.ARRAY_BY_EDGE:
        return freeze_json([value for _, value in values])
    if merge_mode is JoinMergeMode.OBJECT_BY_EDGE:
        return freeze_json({edge_id: value for edge_id, value in values})
    raise InputAssemblyError(f"unsupported merge mode {merge_mode}")
