"""Durable, fail-closed Artifact storage owned by the LangGraph adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from ..artifacts.local_cas import LocalCASBackend
from ..domain.data import PortTransport


class LangGraphArtifactAccessDenied(PermissionError):
    pass


class LangGraphArtifactStore:
    """CAS bytes plus isolated metadata; no dependency on legacy Run rows."""

    def __init__(self, database: Path | str, blob_root: Path | str) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.backend = LocalCASBackend(blob_root)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS langgraph_artifacts("
                "artifact_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,"
                "attempt_id TEXT NOT NULL,node_id TEXT NOT NULL,port_id TEXT NOT NULL,"
                "schema_id TEXT NOT NULL,content_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,"
                "blob_key TEXT NOT NULL,status TEXT NOT NULL,filename TEXT)"
            )

    def _connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def access(self, *, run_id, node_id, attempt_id, output_ports, inputs):
        authorized = set()
        for value in inputs.values():
            if isinstance(value, Mapping) and isinstance(value.get("artifact_id"), str):
                authorized.add(value["artifact_id"])
        policies = {
            port.id: port
            for port in output_ports
            if getattr(port.data_policy.transport, "value", port.data_policy.transport)
            == PortTransport.ARTIFACT_REF.value
        }
        return _ArtifactAccess(
            self, run_id, node_id, attempt_id, policies, authorized,
        )

    def _record(self, artifact_id: str):
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM langgraph_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()

    def commit(self, artifact_ids) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for artifact_id in artifact_ids:
                changed = connection.execute(
                    "UPDATE langgraph_artifacts SET status='committed'"
                    " WHERE artifact_id=? AND status='staged'",
                    (artifact_id,),
                ).rowcount
                if changed != 1:
                    connection.rollback()
                    raise LangGraphArtifactAccessDenied(
                        "Artifact is not staged by this attempt"
                    )
            connection.commit()

    def abandon(self, artifact_ids) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE langgraph_artifacts SET status='abandoned'"
                " WHERE artifact_id=? AND status='staged'",
                ((item,) for item in artifact_ids),
            )
            connection.commit()


class _ArtifactAccess:
    def __init__(self, store, run_id, node_id, attempt_id, policies, authorized):
        self.store = store
        self.run_id, self.node_id, self.attempt_id = run_id, node_id, attempt_id
        self.policies, self.authorized = policies, authorized
        self._produced: dict[str, str] = {}

    @property
    def produced_artifact_ids(self):
        return tuple(self._produced[key] for key in sorted(self._produced))

    def write(self, *, name, content, content_type, filename=None):
        port = self.policies.get(name)
        if port is None:
            raise LangGraphArtifactAccessDenied("Artifact output port was not declared")
        normalized = content_type.strip().lower()
        policy = port.data_policy
        if normalized not in policy.content_types:
            raise LangGraphArtifactAccessDenied("Artifact content type is not allowed")
        artifact_id = "langgraph_artifact:" + hashlib.sha256(
            f"{self.attempt_id}|{name}".encode("utf-8")
        ).hexdigest()
        receipt = self.store.backend.write(
            content, max_size_bytes=policy.max_size_bytes
        )
        with self.store._connect() as connection:
            prior = connection.execute(
                "SELECT blob_key,status FROM langgraph_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if prior is None:
                connection.execute(
                    "INSERT INTO langgraph_artifacts VALUES (?,?,?,?,?,?,?,?,?,"
                    "'staged',?)",
                    (
                        artifact_id, self.run_id, self.attempt_id, self.node_id,
                        name, port.schema_id, normalized, receipt.size_bytes,
                        receipt.blob_key, filename,
                    ),
                )
                connection.commit()
            elif prior["blob_key"] != receipt.blob_key:
                raise LangGraphArtifactAccessDenied(
                    "Artifact output port was written with different content"
                )
            elif prior["status"] not in {"staged", "committed"}:
                raise LangGraphArtifactAccessDenied("Artifact was abandoned")
        self._produced[name] = artifact_id
        return artifact_id

    def read(self, artifact_id, *, max_size_bytes=None):
        value = str(artifact_id)
        if value not in self.authorized:
            raise LangGraphArtifactAccessDenied(
                "Artifact was not authorized by node input"
            )
        record = self.store._record(value)
        if (
            record is None
            or record["status"] != "committed"
            or record["run_id"] != self.run_id
        ):
            raise LangGraphArtifactAccessDenied("Artifact is not committed for this run")
        limit = record["size_bytes"]
        if max_size_bytes is not None:
            limit = min(limit, max_size_bytes)
        return self.store.backend.read(record["blob_key"], max_size_bytes=limit)
