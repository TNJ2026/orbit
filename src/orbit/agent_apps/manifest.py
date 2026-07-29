"""Validation for the declarative local Agent App contract."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


_APP_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ManifestError(ValueError):
    """A manifest is malformed or asks the host to cross a safety boundary."""


@dataclass(frozen=True)
class ServiceSpec:
    command: tuple[str, ...]
    cwd: Path
    environment: tuple[str, ...]
    ready_url: str
    timeout_seconds: float


@dataclass(frozen=True)
class McpSpec:
    url: str


@dataclass(frozen=True)
class EventSpec:
    url: str


@dataclass(frozen=True)
class AgentAppManifest:
    path: Path
    app_id: str
    scope: str
    service: ServiceSpec
    ui_url: str
    mcp: McpSpec | None
    events: EventSpec | None


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{label} must be a non-empty string")
    return value


def _loopback_url(value: Any, label: str) -> str:
    raw = _string(value, label)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOOPBACK_HOSTS:
        raise ManifestError(f"{label} must be an HTTP(S) loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ManifestError(f"{label} must not contain credentials, query, or fragment")
    return raw


def _loopback_websocket_url(value: Any, label: str) -> str:
    raw = _string(value, label)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in _LOOPBACK_HOSTS:
        raise ManifestError(f"{label} must be a WebSocket loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ManifestError(f"{label} must not contain credentials, query, or fragment")
    return raw


def _service_cwd(value: Any, root: Path) -> Path:
    relative = Path(_string(value, "service.cwd"))
    if relative.is_absolute():
        raise ManifestError("service.cwd must be relative to the manifest")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestError("service.cwd must stay inside the manifest directory") from exc
    if not resolved.is_dir():
        raise ManifestError("service.cwd must name an existing directory")
    return resolved


def _command(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ManifestError("service.command must be a non-empty argv array")
    command = tuple(_string(item, "service.command item") for item in value)
    if any("\x00" in item for item in command):
        raise ManifestError("service.command items must not contain NUL")
    return command


def _environment(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError("service.environment must be an array of variable names")
    if any(_ENV_NAME.fullmatch(item) is None for item in value):
        raise ManifestError("service.environment contains an invalid variable name")
    if len(value) != len(set(value)):
        raise ManifestError("service.environment must not contain duplicates")
    return tuple(value)


def load_manifest(path: Path | str) -> AgentAppManifest:
    """Load a version-1 manifest without permitting shell or network escape."""

    manifest_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest does not exist: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc.msg}") from exc
    root = manifest_path.parent
    document = _mapping(payload, "manifest")
    if document.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")
    app_id = _string(document.get("id"), "id")
    if _APP_ID.fullmatch(app_id) is None:
        raise ManifestError("id must be lowercase letters, digits, and hyphens")
    scope = document.get("scope")
    if scope not in {"workspace", "global"}:
        raise ManifestError("scope must be workspace or global")
    service_data = _mapping(document.get("service"), "service")
    timeout = service_data.get("timeout_seconds", 30)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300:
        raise ManifestError("service.timeout_seconds must be between 0 and 300")
    service = ServiceSpec(
        command=_command(service_data.get("command")),
        cwd=_service_cwd(service_data.get("cwd", "."), root),
        environment=_environment(service_data.get("environment")),
        ready_url=_loopback_url(service_data.get("ready_url"), "service.ready_url"),
        timeout_seconds=float(timeout),
    )
    ui_data = _mapping(document.get("ui"), "ui")
    mcp_data = document.get("mcp")
    mcp = None
    if mcp_data is not None:
        mcp_mapping = _mapping(mcp_data, "mcp")
        if mcp_mapping.get("transport") != "http-jsonrpc":
            raise ManifestError("mcp.transport must be http-jsonrpc")
        mcp = McpSpec(url=_loopback_url(mcp_mapping.get("url"), "mcp.url"))
    event_data = document.get("events")
    events = None
    if event_data is not None:
        event_mapping = _mapping(event_data, "events")
        if event_mapping.get("transport") != "websocket":
            raise ManifestError("events.transport must be websocket")
        events = EventSpec(
            url=_loopback_websocket_url(event_mapping.get("url"), "events.url")
        )
    return AgentAppManifest(
        path=manifest_path,
        app_id=app_id,
        scope=scope,
        service=service,
        ui_url=_loopback_url(ui_data.get("url"), "ui.url"),
        mcp=mcp,
        events=events,
    )
