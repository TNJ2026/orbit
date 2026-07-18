"""Strict, side-effect-free YAML and JSON parsers for Workflow DSL."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from ..domain.serialization import freeze_json
from .diagnostics import Diagnostic, DiagnosticError, JsonPath, SourceRange


MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_YAML_ALIASES = 50
MAX_DOCUMENT_DEPTH = 128
MAX_DOCUMENT_NODES = 20_000


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


class _StrictSafeLoader(yaml.SafeLoader):
    pass


_StrictSafeLoader.yaml_implicit_resolvers = {
    key: list(value) for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for resolver_key, resolvers in list(_StrictSafeLoader.yaml_implicit_resolvers.items()):
    _StrictSafeLoader.yaml_implicit_resolvers[resolver_key] = [
        item
        for item in resolvers
        if item[0] not in {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool"}
    ]
_StrictSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_mapping(loader: _StrictSafeLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "workflow object keys must be strings",
                key_node.start_mark,
            )
        if key in result:
            raise _DuplicateKey(key)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


@dataclass(frozen=True)
class ParsedDslDocument:
    data: Mapping[str, Any]
    source_name: str
    source_format: str
    source_map: Mapping[JsonPath, SourceRange]


def _source_range(source: str, node: yaml.Node) -> SourceRange:
    return SourceRange(
        source=source,
        start_line=node.start_mark.line + 1,
        start_column=node.start_mark.column + 1,
        end_line=max(node.end_mark.line + 1, 1),
        end_column=max(node.end_mark.column + 1, 1),
    )


def _node_source_map(
    source: str,
    root: yaml.Node,
    *,
    limit_code: str,
) -> Mapping[JsonPath, SourceRange]:
    locations: dict[JsonPath, SourceRange] = {}
    active: set[int] = set()
    visited_nodes = 0
    stack: list[tuple[yaml.Node, JsonPath, int, bool]] = [(root, (), 0, True)]
    while stack:
        node, path, depth, entering = stack.pop()
        node_id = id(node)
        if not entering:
            active.remove(node_id)
            continue
        if depth > MAX_DOCUMENT_DEPTH:
            raise DiagnosticError(
                [
                    Diagnostic(
                        limit_code,
                        f"document nesting exceeds depth limit {MAX_DOCUMENT_DEPTH}",
                        "parse",
                        path,
                        source_range=_source_range(source, node),
                    )
                ]
            )
        visited_nodes += 1
        if visited_nodes > MAX_DOCUMENT_NODES:
            raise DiagnosticError(
                [
                    Diagnostic(
                        limit_code,
                        f"document exceeds node limit {MAX_DOCUMENT_NODES}",
                        "parse",
                        path,
                        source_range=_source_range(source, node),
                    )
                ]
            )
        if node_id in active:
            raise DiagnosticError(
                [Diagnostic("DSL_UNSAFE_YAML", "recursive YAML aliases are forbidden", "parse", path)]
            )
        locations[path] = _source_range(source, node)
        active.add(node_id)
        stack.append((node, path, depth, False))
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            children: list[tuple[yaml.Node, JsonPath]] = []
            for key_node, value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                key = key_node.value
                if key in seen:
                    raise DiagnosticError(
                        [
                            Diagnostic(
                                "DSL_DUPLICATE_KEY",
                                f"duplicate key {key!r}",
                                "parse",
                                path + (key,),
                                source_range=_source_range(source, key_node),
                            )
                        ]
                    )
                seen.add(key)
                children.append((value_node, path + (key,)))
            for child, child_path in reversed(children):
                stack.append((child, child_path, depth + 1, True))
        elif isinstance(node, yaml.SequenceNode):
            for index in range(len(node.value) - 1, -1, -1):
                stack.append((node.value[index], path + (index,), depth + 1, True))
    return MappingProxyType(locations)


def _parse_json(text: str, source_name: str) -> ParsedDslDocument:
    try:
        root = yaml.compose(text, Loader=_StrictSafeLoader)
        if root is None:
            raise DiagnosticError(
                [Diagnostic("DSL_PARSE_ERROR", "workflow document is empty", "parse")]
            )
        source_map = _node_source_map(
            source_name,
            root,
            limit_code="DSL_PARSE_ERROR",
        )
        value = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except DiagnosticError:
        raise
    except _DuplicateKey as exc:
        raise DiagnosticError(
            [Diagnostic("DSL_DUPLICATE_KEY", f"duplicate key {exc.key!r}", "parse")]
        ) from None
    except (json.JSONDecodeError, ValueError, yaml.YAMLError, RecursionError) as exc:
        source_range = None
        if isinstance(exc, json.JSONDecodeError):
            source_range = SourceRange(source_name, exc.lineno, exc.colno, exc.lineno, exc.colno)
        raise DiagnosticError(
            [Diagnostic("DSL_PARSE_ERROR", str(exc), "parse", source_range=source_range)]
        ) from None
    if not isinstance(value, dict):
        raise DiagnosticError(
            [Diagnostic("DSL_PARSE_ERROR", "workflow document must be an object", "parse")]
        )
    return ParsedDslDocument(
        data=freeze_json(value),
        source_name=source_name,
        source_format="json",
        source_map=source_map,
    )


def _parse_yaml(text: str, source_name: str) -> ParsedDslDocument:
    try:
        aliases = sum(isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(text))
        if aliases > MAX_YAML_ALIASES:
            raise DiagnosticError(
                [
                    Diagnostic(
                        "DSL_UNSAFE_YAML",
                        f"YAML alias limit exceeded ({aliases} > {MAX_YAML_ALIASES})",
                        "parse",
                    )
                ]
            )
        root = yaml.compose(text, Loader=_StrictSafeLoader)
        if root is None:
            raise DiagnosticError(
                [Diagnostic("DSL_PARSE_ERROR", "workflow document is empty", "parse")]
            )
        source_map = _node_source_map(
            source_name,
            root,
            limit_code="DSL_UNSAFE_YAML",
        )
        value = yaml.load(text, Loader=_StrictSafeLoader)
    except DiagnosticError:
        raise
    except _DuplicateKey as exc:
        raise DiagnosticError(
            [Diagnostic("DSL_DUPLICATE_KEY", f"duplicate key {exc.key!r}", "parse")]
        ) from None
    except (yaml.YAMLError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        source_range = None
        if mark is not None:
            source_range = SourceRange(source_name, mark.line + 1, mark.column + 1, mark.line + 1, mark.column + 1)
        raise DiagnosticError(
            [Diagnostic("DSL_PARSE_ERROR", str(exc), "parse", source_range=source_range)]
        ) from None
    if not isinstance(value, dict):
        raise DiagnosticError(
            [Diagnostic("DSL_PARSE_ERROR", "workflow document must be an object", "parse")]
        )
    return ParsedDslDocument(
        data=freeze_json(value),
        source_name=source_name,
        source_format="yaml",
        source_map=source_map,
    )


def parse_dsl(text: str, *, source_name: str = "<memory>", source_format: str | None = None) -> ParsedDslDocument:
    if not isinstance(text, str):
        raise TypeError("workflow source must be text")
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise DiagnosticError(
            [Diagnostic("DSL_UNSAFE_YAML", f"workflow source exceeds {MAX_INPUT_BYTES} bytes", "parse")]
        )
    selected = source_format
    if selected is None:
        selected = "json" if text.lstrip().startswith(("{", "[")) else "yaml"
    if selected == "json":
        return _parse_json(text, source_name)
    if selected in {"yaml", "yml"}:
        return _parse_yaml(text, source_name)
    raise ValueError(f"unsupported workflow source format: {selected}")


def parse_dsl_file(path: Path | str) -> ParsedDslDocument:
    source = Path(path)
    if source.suffix.lower() not in {".json", ".yaml", ".yml"}:
        raise ValueError("workflow file must use .json, .yaml, or .yml")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DiagnosticError(
            [Diagnostic("DSL_PARSE_ERROR", f"workflow source must be UTF-8: {exc}", "parse")]
        ) from None
    return parse_dsl(
        text,
        source_name=str(source),
        source_format="json" if source.suffix.lower() == ".json" else "yaml",
    )
