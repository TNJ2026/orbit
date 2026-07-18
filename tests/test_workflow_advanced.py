from __future__ import annotations

from datetime import datetime,timedelta,timezone
import itertools
import json
from pathlib import Path
import tempfile
import unittest

from orbit.workflow.api import build_workflow_api
from orbit.workflow.application import BudgetService,ForeachService,HumanTaskService,PlanService,RunViewService,SubflowService
from orbit.workflow.application.planner_service import PlannerApplicationService
from orbit.workflow.application.runtime_service import RuntimeApplicationService
from orbit.workflow.capacity import CapacityHarness,ReservationEstimateCase,SLO,bounded_estimate,evaluate_estimator
from orbit.workflow.catalogs import InMemoryHandlerCatalog,InMemorySchemaCatalog
from orbit.workflow.domain.envelopes import CommandEnvelope
from orbit.workflow.domain.execution_plan import GraphExecutionPlan
from orbit.workflow.domain.foreach import ForeachFailurePolicy,stable_aggregate
from orbit.workflow.domain.graph import EdgeRoute,PlanEdge
from orbit.workflow.domain.human import HumanTaskKind,HumanTaskStatus,QuorumKind
from orbit.workflow.domain.ids import EntityId
from orbit.workflow.domain.plan_patch import AgenticRegion,PatchOperation,PatchOperationKind,PlanPatch
from orbit.workflow.domain.policy import PolicyEffect,PolicyRule,evaluate_policy
from orbit.workflow.domain.planner import PlannerUsage
from orbit.workflow.domain.serialization import definition_hash,to_primitive
from orbit.workflow.domain.schemas import validate_contract
from orbit.workflow.domain.states import NodeRunStatus
from orbit.workflow.domain.subflow import PropagationPolicy,SubflowStatus
from orbit.workflow.domain.versions import AggregateVersion,Revision
from orbit.workflow.dsl import compile_source
from orbit.workflow.observability import MetricRegistry,StructuredLogger
from orbit.workflow.persistence.database import connect_workflow_database
from orbit.workflow.persistence.integrity import check_database
from orbit.workflow.persistence.workflow_versions import SQLiteWorkflowVersionStore
from orbit.workflow.planner.plan_compiler import PatchValidationError,compile_patch
from orbit.workflow.planner import FakePlannerProvider,PlannerProviderResponse,build_planning_context
from orbit.workflow.recovery import RecoveryManager,RepairAction,RepairManager
from orbit.workflow.runtime.plan_instantiator import instantiate_execution_plan
from orbit.workflow.security import CapabilityDenied,CapabilityService,Redactor,SandboxPolicy,run_sandboxed


NOW=datetime(2026,7,17,18,tzinfo=timezone.utc)
PORT=[{"id":"value","schema_id":"schema://value/1.0"}]


class AdvancedWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.path=self.root/"advanced.db"
        source={"dsl_version":"1.2","metadata":{"id":"advanced","name":"Advanced"},"nodes":[{"id":"choose","kind":"decision","inputs":PORT,"outputs":PORT},{"id":"done","kind":"terminal","inputs":PORT}],"edges":[{"id":"finish","from":{"node":"choose","port":"value"},"to":{"node":"done","port":"value"},"priority":0}],"entry":["choose"],"terminals":["done"]}
        self.compiled=compile_source(json.dumps(source),InMemoryHandlerCatalog([]),InMemorySchemaCatalog({"schema://value/1.0":{}}),source_format="json")
        SQLiteWorkflowVersionStore(self.path).publish(self.compiled,expected_latest_version=0,source_format="json",source_text=None,actor="test")
        self.run_id=EntityId("run","advanced")
        self._start(self.run_id,"start-a")

    def _start(self,run_id,key):
        return RuntimeApplicationService(self.path).submit(CommandEnvelope(EntityId("command",key),"start_run",run_id,run_id,AggregateVersion(0),key,"test",NOW,{"workflow_id":self.compiled.ir.workflow_id,"workflow_version":1,"definition_hash":self.compiled.definition_hash.value,"input":{"value":1}}))

    def test_policy_is_pure_fail_closed_and_approval_scoped(self):
        patch_id=EntityId("plan_patch","p")
        rules=(PolicyRule("allow","1","read",PolicyEffect.ALLOW),PolicyRule("approve","1","write",PolicyEffect.REQUIRE_APPROVAL,True))
        denied=evaluate_policy(run_id=self.run_id,patch_id=patch_id,required_capabilities=("read","write","missing"),rules=rules)
        self.assertFalse(denied.allowed);self.assertTrue(denied.requires_approval)
        allowed=evaluate_policy(run_id=self.run_id,patch_id=patch_id,required_capabilities=("read","write"),rules=reversed(rules),approval_capabilities=("write",))
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed,evaluate_policy(run_id=self.run_id,patch_id=patch_id,required_capabilities=("write","read"),rules=rules,approval_capabilities=("write",)))

    def test_dynamic_patch_is_pending_only_atomic_and_deterministic(self):
        base=instantiate_execution_plan(self.compiled.ir,run_id=self.run_id,plan_id=EntityId("plan","base"),workflow_version=Revision(1),workflow_definition_hash=self.compiled.definition_hash)
        full_ports=to_primitive(base.node("choose").inputs)
        node={"node_id":"work","kind":"action","handler_name":"tool","handler_version":"1","handler_manifest_fingerprint":"sha256:"+"a"*64,"inputs":full_ports,"outputs":full_ports,"config":{"capabilities":["read"]}}
        edge=lambda edge_id,source,target,priority:{"edge_id":edge_id,"source_node_id":source,"target_node_id":target,"route":"success","priority":priority,"source_port":"value","target_port":"value","condition":None,"mapping":None,"back_edge":False,"policy_ref":None}
        patch=PlanPatch(EntityId("plan_patch","insert"),EntityId("proposal","insert"),self.run_id,Revision(1),"insert work",(PatchOperation(PatchOperationKind.REMOVE_PENDING_EDGE,"finish"),PatchOperation(PatchOperationKind.ADD_NODE,"work",node),PatchOperation(PatchOperationKind.ADD_EDGE,"to-work",edge("to-work","choose","work",0)),PatchOperation(PatchOperationKind.ADD_EDGE,"to-done",edge("to-done","work","done",0))))
        validate_contract(to_primitive(patch),"plan-patch/1.0")
        region=AgenticRegion("main",("choose","done"));statuses={"choose":"pending","done":"pending"}
        one=compile_patch(base,patch,region,statuses);two=compile_patch(base,patch,region,statuses)
        self.assertEqual(one,two);self.assertEqual(2,one.plan_version.value);self.assertEqual({"choose","work","done"},set(one.ordered_node_ids))
        with self.assertRaisesRegex(PatchValidationError,"pending_only"):compile_patch(base,patch,region,{"choose":"ready","done":"pending"})

    def test_plan_commit_consumes_proposal_versions_plan_and_is_idempotent(self):
        context=build_planning_context(run_id=self.run_id,plan_version=Revision(1),goal="add work",graph_summary={"status":"waiting","plan_version":1,"nodes":[],"tokens":[],"joins":[],"waiting_reason":"planner"},available_capabilities=("read",),remaining_limits={"decisions":2})
        raw=json.dumps({"schema_version":"1.0","proposal_id":"proposal:dynamic","run_id":str(self.run_id),"base_plan_version":1,"action":{"kind":"dispatch","arguments":{"handler":"tool","inputs":{},"config":{}}},"reason":"add work"})
        planner=PlannerApplicationService(self.path,provider=FakePlannerProvider([PlannerProviderResponse(raw,"request",PlannerUsage())]));planner.request_decision(context,prompt_hash=definition_hash("prompt"),capability_manifest_hash=definition_hash("caps"),model_id="fake",provider_id="fake",now=NOW);planner.execute_claimed(planner.claim("worker",NOW),NOW)
        with connect_workflow_database(self.path) as db:db.execute("UPDATE node_runs SET status='pending' WHERE run_id=?",(str(self.run_id),))
        with connect_workflow_database(self.path,read_only=True) as db:base_row=json.loads(db.execute("SELECT canonical_plan_json FROM execution_plans WHERE run_id=? AND plan_version=1",(str(self.run_id),)).fetchone()[0])
        base=__import__('orbit.workflow.domain.execution_plan',fromlist=['execution_plan_from_primitive']).execution_plan_from_primitive(base_row);ports=to_primitive(base.node("choose").inputs)
        node={"node_id":"work","kind":"action","handler_name":"tool","handler_version":"1","handler_manifest_fingerprint":"sha256:"+"a"*64,"inputs":ports,"outputs":ports,"config":{"capabilities":["read"]}}
        edge=lambda edge_id,source,target:{"edge_id":edge_id,"source_node_id":source,"target_node_id":target,"route":"success","priority":0,"source_port":"value","target_port":"value","condition":None,"mapping":None,"back_edge":False,"policy_ref":None}
        patch=PlanPatch(EntityId("plan_patch","dynamic"),EntityId("proposal","dynamic"),self.run_id,Revision(1),"insert",(PatchOperation(PatchOperationKind.REMOVE_PENDING_EDGE,"finish"),PatchOperation(PatchOperationKind.ADD_NODE,"work",node),PatchOperation(PatchOperationKind.ADD_EDGE,"a",edge("a","choose","work")),PatchOperation(PatchOperationKind.ADD_EDGE,"b",edge("b","work","done"))))
        service=PlanService(self.path,rules=(PolicyRule("read","1","read",PolicyEffect.ALLOW),));plan=service.commit(patch,AgenticRegion("r",("choose","done")),actor="planner",now=NOW);same=service.commit(patch,AgenticRegion("r",("choose","done")),actor="planner",now=NOW)
        self.assertEqual(plan,same);self.assertEqual(2,plan.plan_version.value)
        with connect_workflow_database(self.path,read_only=True) as db:self.assertEqual("consumed",db.execute("SELECT status FROM planner_proposals WHERE proposal_id='proposal:dynamic'").fetchone()[0]);self.assertEqual(2,db.execute("SELECT COUNT(*) FROM execution_plans WHERE run_id=?",(str(self.run_id),)).fetchone()[0])

    def test_budget_reservation_streaming_overrun_release_and_add(self):
        service=BudgetService(self.path);account=service.open_account(self.run_id,100,actor="owner",now=NOW);self.assertEqual(100,account.remaining_microunits)
        reservation=service.reserve(self.run_id,EntityId("attempt","a"),80,actor="worker",now=NOW)
        service.report_usage(reservation.reservation_id,1,50,actor="worker",now=NOW)
        same=service.report_usage(reservation.reservation_id,1,50,actor="worker",now=NOW);self.assertEqual(50,same.consumed_microunits)
        over=service.report_usage(reservation.reservation_id,2,130,actor="worker",now=NOW);self.assertTrue(over.exhausted)
        settled=service.settle(reservation.reservation_id,actor="worker",now=NOW);self.assertEqual(0,settled.reserved_microunits)
        restored=service.add_budget(self.run_id,100,actor="owner",now=NOW);self.assertEqual(70,restored.remaining_microunits)
        other=service.reserve(self.run_id,EntityId("attempt","b"),20,actor="worker",now=NOW);released=service.release(other.reservation_id,actor="worker",now=NOW);self.assertEqual(70,released.remaining_microunits)

    def test_single_human_model_quorum_form_deadline_and_one_time_token(self):
        service=HumanTaskService(self.path);task,token=service.create(self.run_id,HumanTaskKind.APPROVAL,{"request":"deploy"},actor="planner",now=NOW,participants=("alice","bob"),quorum=QuorumKind.ALL)
        self.assertIs(HumanTaskStatus.WAITING,service.submit(task,token,"approve",{},actor="alice",expected_version=1,now=NOW))
        self.assertIs(HumanTaskStatus.COMPLETED,service.submit(task,token,"approve",{},actor="bob",expected_version=2,now=NOW))
        with self.assertRaises(ValueError):service.submit(task,token,"approve",{},actor="bob",expected_version=3,now=NOW)
        input_task,input_token=service.create(self.run_id,HumanTaskKind.INPUT,{"question":"n"},actor="planner",now=NOW,assignee="alice",form_schema={"type":"object","required":["n"],"properties":{"n":{"type":"integer"}},"additionalProperties":False},deadline_at=NOW+timedelta(seconds=1))
        with self.assertRaises(Exception):service.submit(input_task,input_token,"provide_input",{"n":"bad"},actor="alice",expected_version=1,now=NOW)
        self.assertEqual((input_task,),service.expire_due(NOW+timedelta(seconds=2)))

    def test_foreach_slots_failure_and_aggregate_permutations(self):
        service=ForeachService(self.path);group=service.create_group(self.run_id,"each",[3,1,2],keys=("c","a","b"),plan_version=Revision(1),failure_policy=ForeachFailurePolicy.PARTIAL_SUCCESS,concurrency_limit=2,actor="test",now=NOW)
        first=service.claim_ready(group,limit=10,actor="worker",now=NOW);self.assertEqual(2,len(first))
        service.complete_item(first[1],output={"v":1},actor="worker",now=NOW);service.complete_item(first[0],error={"code":"x"},actor="worker",now=NOW)
        last=service.claim_ready(group,limit=10,actor="worker",now=NOW);self.assertEqual(1,len(last));service.complete_item(last[0],output={"v":2},actor="worker",now=NOW)
        aggregate=service.aggregate(group,actor="test",now=NOW);self.assertTrue(aggregate["partial"]);self.assertEqual(["c","a","b"],[item["key"] for item in aggregate["items"]])
        facts=((2,"b","succeeded",2,None),(0,"c","failed",None,"x"),(1,"a","succeeded",1,None));checks={definition_hash(stable_aggregate(tuple(p))).value for p in itertools.permutations(facts)};self.assertEqual(1,len(checks))

    def test_subflow_fixed_version_and_failure_propagation(self):
        child=EntityId("run","child");self._start(child,"start-child")
        with connect_workflow_database(self.path) as db:db.execute("UPDATE workflow_runs SET status='running' WHERE run_id=?",(str(self.run_id),))
        service=SubflowService(self.path);link=service.link(self.run_id,child,workflow_id=EntityId.parse(self.compiled.ir.workflow_id),workflow_version=Revision(1),input_mapping={},output_mapping={},propagation=PropagationPolicy(child_failure="fail_parent"),actor="test",now=NOW)
        service.propagate(link,SubflowStatus.FAILED,actor="system",now=NOW)
        with connect_workflow_database(self.path,read_only=True) as db:self.assertEqual("failed",db.execute("SELECT status FROM workflow_runs WHERE run_id=?",(str(self.run_id),)).fetchone()[0])

    def test_security_redaction_capability_acl_and_sandbox(self):
        redactor=Redactor(("secret-value",));self.assertEqual({"x":"[REDACTED]"},redactor.redact({"x":"secret-value"}))
        service=CapabilityService(self.path);cap=service.issue("agent","run:advanced",("read","execute"),actor="admin",now=NOW,run_id=self.run_id);self.assertEqual(cap,service.authorize("agent","run:advanced/node:x","read",now=NOW))
        service.revoke(cap,actor="admin",now=NOW,run_id=self.run_id)
        with self.assertRaises(CapabilityDenied):service.authorize("agent","run:advanced","read",now=NOW)
        result=run_sandboxed(("printf","ok"),SandboxPolicy(self.root,("printf",),timeout_seconds=2,trusted_first_party=True));self.assertEqual(b"ok",result.stdout)
        with self.assertRaises(PermissionError):run_sandboxed(("curl","https://example.com"),SandboxPolicy(self.root,("curl",)))

    def test_recovery_dry_run_observability_capacity_runview_and_api(self):
        report=RecoveryManager(self.path).scan(NOW,limit=10);self.assertGreaterEqual(report.scanned_runs,0)
        action=RepairAction("r1","unsupported",str(self.run_id),str(self.run_id),0,"test");dry=RepairManager({}).execute((action,),now=NOW);self.assertTrue(dry.dry_run);self.assertEqual((),dry.applied)
        logs=[];StructuredLogger(logs.append,Redactor(("secret",))).emit("info","secret",fields={"status":"ok"});self.assertNotIn("secret",logs[0])
        metrics=MetricRegistry();metrics.add("jobs",status="ready");self.assertEqual(1,metrics.snapshot()[0][2])
        with self.assertRaises(ValueError):metrics.add("bad",run_id="x")
        capacity=CapacityHarness.run("noop",lambda:None,samples=10,slo=SLO(100,100,1));self.assertTrue(capacity.passed)
        estimate=evaluate_estimator((ReservationEstimateCase(100,120,90),ReservationEstimateCase(100,100,100)));self.assertTrue(estimate.safe);self.assertEqual(100,bounded_estimate(100,120))
        view=RunViewService(self.path).get(self.run_id);self.assertEqual(str(self.run_id),view["overview"]["run_id"]);self.assertIn("timeline",view)
        api=build_workflow_api(self.path);self.assertEqual(8,len(api.routes));self.assertEqual("/api/v1/workflows",api.routes[0].path)
        self.assertTrue(check_database(self.path).ok)


if __name__=="__main__":unittest.main()
