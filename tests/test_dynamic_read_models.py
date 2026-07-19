from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.api.dto import CursorError
from orbit.workflow.api.dynamic_read_models import DynamicReadModelService
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.migrations import migrate_workflow_database


NOW = "2026-07-19T00:00:00+00:00"
RUN = EntityId.parse("run:parent")


class DynamicReadModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.db"
        with connect_workflow_database(self.path) as db:
            migrate_workflow_database(db)
            db.execute(
                "INSERT INTO workflow_definitions(workflow_id,name,created_at,created_by)"
                " VALUES ('workflow:demo','Demo',?,'test')", (NOW,),
            )
            db.execute(
                "INSERT INTO workflow_versions(workflow_id,version,definition_hash,dsl_version,"
                "ir_version,compiler_version,canonical_ir_json,source_format,source_text,"
                "catalog_fingerprint,created_at,created_by) VALUES"
                " ('workflow:demo',1,'sha256:workflow','1.2','1.2','1.2','{}','json',NULL,"
                "'sha256:catalog',?,'test')", (NOW,),
            )
            for run_id in ("run:parent", "run:child"):
                db.execute(
                    "INSERT INTO workflow_runs(run_id,workflow_id,workflow_version,definition_hash,"
                    "status,aggregate_version,correlation_id,created_at,updated_at,display_name)"
                    " VALUES (?,'workflow:demo',1,'sha256:workflow','running',1,?,?,?,?)",
                    (run_id, run_id, NOW, NOW, run_id),
                )
            db.commit()
        self.service = DynamicReadModelService(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_planner_projection_never_returns_raw_response_or_lease_material(self):
        with connect_workflow_database(self.path) as db:
            db.execute(
                "INSERT INTO planner_attempts(attempt_id,run_id,attempt_number,status,context_json,"
                "context_hash,prompt_hash,capability_manifest_hash,model_id,provider_id,"
                "request_fingerprint,raw_response,raw_response_checksum,provider_request_id,"
                "usage_json,proposal_id,error_json,lease_owner,lease_token_hash,fencing_token,"
                "lease_expires_at,aggregate_version,created_at,updated_at) VALUES"
                " ('planner_attempt:one',?,1,'accepted','{}','sha256:context','sha256:prompt',"
                "'sha256:cap','model','provider','sha256:req','TOP SECRET','sha256:raw','req-1',"
                "?,NULL,NULL,NULL,NULL,1,NULL,4,?,?)",
                (str(RUN), json.dumps({"input_tokens": 3, "output_tokens": 5,
                                      "cost_microunits": 7, "incomplete": False}), NOW, NOW),
            )
            db.execute(
                """INSERT INTO planner_proposals(
                       proposal_id,attempt_id,run_id,base_plan_version,status,
                       proposal_json,action_json,reason,content_hash,
                       validation_json,raw_response_checksum,created_at
                   ) VALUES ('proposal:one','planner_attempt:one',?,1,'protocol_accepted',
                       '{}',?,'dispatch safely','sha256:proposal','{}','sha256:raw',?)""",
                (str(RUN), json.dumps({
                    "kind": "dispatch", "arguments": {
                        "handler": "tool@1.0.0",
                        "inputs": {"password": "DO NOT LEAK"},
                        "config": {"token": "ALSO SECRET"},
                    },
                }), NOW),
            )
            db.commit()
        items, cursor = self.service.planner_decisions(RUN)
        self.assertIsNone(cursor)
        self.assertEqual(7, items[0]["usage"]["cost_microunits"])
        rendered = json.dumps(items)
        self.assertNotIn("TOP SECRET", rendered)
        self.assertNotIn("lease", rendered)
        self.assertNotIn("provider_request_id", rendered)
        self.assertNotIn("DO NOT LEAK", rendered)
        self.assertNotIn("ALSO SECRET", rendered)
        self.assertEqual("tool@1.0.0", items[0]["proposal"]["action"]["handler"])

    def test_foreach_groups_and_sensitive_items_have_stable_pagination(self):
        with connect_workflow_database(self.path) as db:
            db.execute(
                "INSERT INTO foreach_groups(group_id,run_id,node_run_id,source_checksum,plan_version,"
                "status,failure_policy,concurrency_limit,item_count,aggregate_json,aggregate_checksum,"
                "aggregate_version,created_at,updated_at) VALUES"
                " ('foreach_group:one',?,NULL,'sha256:source',1,'running','continue',2,3,NULL,NULL,1,?,?)",
                (str(RUN), NOW, NOW),
            )
            for index, status in enumerate(("succeeded", "running", "pending")):
                db.execute(
                    "INSERT INTO foreach_items(item_id,group_id,run_id,item_key,item_index,status,"
                    "input_json,output_json,error_json,retry_count,aggregate_version,created_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,0,1,?,?)",
                    (f"foreach_item:{index}", "foreach_group:one", str(RUN), str(index), index,
                     status, json.dumps({"value": index}),
                     json.dumps({"value": index * 2}) if status == "succeeded" else None,
                     None, NOW, NOW),
                )
            db.commit()
        groups, _ = self.service.foreach_groups(RUN)
        self.assertEqual({"pending": 1, "running": 1, "succeeded": 1, "failed": 0}, groups[0]["counts"])
        first, cursor = self.service.foreach_items(
            RUN, EntityId.parse("foreach_group:one"), limit=2
        )
        second, end = self.service.foreach_items(
            RUN, EntityId.parse("foreach_group:one"), cursor=cursor, limit=2
        )
        self.assertEqual([0, 1], [item["item_index"] for item in first])
        self.assertEqual([2], [item["item_index"] for item in second])
        self.assertIsNone(end)
        with self.assertRaises(CursorError):
            self.service.subflows(RUN, cursor=cursor)

    def test_subflow_projection_is_visible_from_parent_and_child(self):
        with connect_workflow_database(self.path) as db:
            db.execute(
                "INSERT INTO subflow_links(link_id,parent_run_id,child_run_id,parent_node_run_id,"
                "workflow_id,workflow_version,status,correlation_id,propagation_policy_json,"
                "input_mapping_json,output_mapping_json,artifact_scope_json,recursion_depth,"
                "aggregate_version,created_at,updated_at) VALUES"
                " ('subflow_link:one',?,'run:child',NULL,'workflow:demo',1,'running',?,"
                "'{}','{}','{}','[]',1,1,?,?)",
                (str(RUN), str(RUN), NOW, NOW),
            )
            db.commit()
        parent, _ = self.service.subflows(RUN)
        child, _ = self.service.subflows(EntityId.parse("run:child"))
        self.assertEqual("subflow_link:one", parent[0]["link_id"])
        self.assertEqual(parent, child)
