"""What a per-mode Workflow library holds, and what it must never hold.

The two authoring products keep separate catalogs, and separate means
separate: a single-agent library must not acquire multi-agent workflows, at
creation or ever. An earlier attempt seeded a new library from the shared one
so that upgrading did not open onto an empty catalog; the cost was a
single-agent product whose first view was somebody else's twelve multi-agent
workflows, each needing archiving by hand. The old library stays reachable
with `--ui-mode multi-agent`, which is the answer to "where did they go"
without being an answer that mixes them in.

What does still carry forward is the project database: definitions published
into the Runtime's own database belong in the library that Runtime reads.
That back-fill was gated on `legacy_execution`, which `orbit serve` hard-codes
to False, so it had never run at all and a Workflow published by an earlier
build simply stopped being visible.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orbit.web.app import create_app
from orbit.workflow.application.workflows import (
    WorkflowCatalogs, WorkflowDefinitionService,
)
from orbit.workflow.catalogs import InMemoryHandlerCatalog, InMemorySchemaCatalog
from orbit.workflow.catalogs.extensions import InMemoryExtensionRegistry
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore

from tests.test_workflow_authoring_jobs import MANIFEST, dsl


def _publish(path: Path, name: str, expected_latest_version: int) -> str:
    """One published version in `path`, under its own Workflow id."""

    document = dsl()
    document["metadata"] = {**document.get("metadata", {}), "id": name, "name": name}
    service = WorkflowDefinitionService(
        WorkflowCatalogs(
            InMemoryHandlerCatalog([MANIFEST]),
            InMemorySchemaCatalog({
                "example://integer/1.0": {"type": "integer"},
                "schema://object/1.0": {"type": "object"},
            }),
            InMemoryExtensionRegistry(),
        ),
        SQLiteWorkflowVersionStore(path),
    )
    published = service.publish_workflow(
        json.dumps(document), source_name="<test>", source_format="json",
        expected_latest_version=expected_latest_version, actor="author",
    )
    return getattr(published.workflow_id, "value", published.workflow_id)


def _workflow_ids(path: Path) -> set[str]:
    from orbit.workflow.persistence.database import connect_workflow_database

    if not path.is_file():
        return set()
    with connect_workflow_database(path, read_only=True) as connection:
        return {
            row["workflow_id"]
            for row in connection.execute("SELECT workflow_id FROM workflow_versions")
        }


class WorkflowLibraryIndependenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.runtime = self.root / "runtime.db"
        self.shared = self.root / "library.db"
        self.library = self.root / "single-agent-library.db"

    def boot(self) -> None:
        create_app(
            str(self.runtime),
            workflow_db_path=self.library,
            # What `orbit serve` passes. The back-fill below used to be gated
            # on this being true, so testing with the default would have
            # watched the merge succeed on a path no product takes.
        )

    def test_a_new_library_takes_nothing_from_the_other_product(self) -> None:
        """Independent from the first boot, not independent after a grace period."""

        other = _publish(self.shared, "authored-in-multi-agent", 0)
        self.boot()
        self.assertEqual(set(), _workflow_ids(self.library))
        self.assertEqual({other}, _workflow_ids(self.shared))

    def test_the_other_library_is_never_written_to(self) -> None:
        existing = _publish(self.shared, "authored-in-multi-agent", 0)
        self.boot()
        _publish(self.library, "authored-here", 0)
        self.boot()
        self.assertEqual({existing}, _workflow_ids(self.shared))

    def test_the_project_database_is_always_carried_forward(self) -> None:
        """Definitions published into the runtime database itself.

        This is the back-fill `legacy_execution` gated out of existence. It
        has to run on every boot, because a project database keeps receiving
        publishes after the library exists.
        """

        self.boot()
        published = _publish(self.runtime, "published-into-the-project", 0)
        self.boot()
        self.assertIn(published, _workflow_ids(self.library))

    def test_an_explicit_database_is_its_own_library_and_merges_nothing(self) -> None:
        """`--db X` is self-contained, so there are not two paths to reconcile."""

        create_app(str(self.runtime), workflow_db_path=self.runtime)
        published = _publish(self.runtime, "self-contained", 0)
        self.assertEqual({published}, _workflow_ids(self.runtime))


if __name__ == "__main__":
    unittest.main()
