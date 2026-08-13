"""What a per-mode Workflow library starts life holding.

Per-mode libraries were introduced so the two authoring products would not
show each other's catalogs. Nothing carried the operator's existing
definitions into the new file, and the only back-fill in the code was gated on
`legacy_execution`, which `orbit serve` hard-codes to False — so upgrading and
opening the UI showed an empty catalog, with every Workflow still sitting in a
library nothing read any more.

Seeding fixes that without giving the isolation away: a library that does not
exist yet has nothing to be isolated from, so it takes its seeds once. Doing it
on every boot would copy the other product's catalog back in forever, which is
not isolation with a grace period — it is no isolation at all.
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


class WorkflowLibrarySeedingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.runtime = self.root / "runtime.db"
        self.seed = self.root / "library.db"
        self.library = self.root / "single-agent-library.db"

    def boot(self) -> None:
        create_app(
            str(self.runtime),
            workflow_db_path=self.library,
            workflow_seed_libraries=(self.seed,),
            worker_count=0,
            # What `orbit serve` passes. The back-fill used to be gated on
            # this being true, so testing with the default would have watched
            # the merge succeed on a path no product takes.
            legacy_execution=False,
        )

    def test_a_new_library_takes_what_the_operator_already_had(self) -> None:
        carried = _publish(self.seed, "existing", 0)
        self.boot()
        self.assertIn(carried, _workflow_ids(self.library))

    def test_the_seed_library_is_left_exactly_as_it_was(self) -> None:
        carried = _publish(self.seed, "existing", 0)
        self.boot()
        _publish(self.library, "authored-here", 0)
        self.assertEqual({carried}, _workflow_ids(self.seed))

    def test_seeding_happens_once_and_not_on_every_boot(self) -> None:
        """Otherwise the other product's catalog reappears forever."""

        _publish(self.seed, "existing", 0)
        self.boot()
        later = _publish(self.seed, "published-elsewhere-later", 0)
        self.boot()
        self.assertNotIn(later, _workflow_ids(self.library))

    def test_an_absent_seed_library_is_not_conjured_into_existence(self) -> None:
        """An operator with no such library had no Workflows to carry."""

        self.boot()
        self.assertFalse(self.seed.exists())

    def test_the_project_database_is_always_carried_forward(self) -> None:
        """Definitions published into the runtime database itself.

        This is the back-fill that `legacy_execution` gated out of existence:
        it has to run on every boot, because a project database keeps
        receiving publishes after the library exists.
        """

        self.boot()
        published = _publish(self.runtime, "published-into-the-project", 0)
        self.boot()
        self.assertIn(published, _workflow_ids(self.library))


if __name__ == "__main__":
    unittest.main()
