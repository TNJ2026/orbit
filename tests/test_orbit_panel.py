"""Contract tests for the compact Orbit current-task MCP App."""

from __future__ import annotations

import re
import unittest

from orbit.web.mcp_app import (
    ORBIT_AUTHORING_HTML,
    ORBIT_AUTHORING_URI,
    ORBIT_DASHBOARD_HTML,
    ORBIT_DASHBOARD_URI,
    ORBIT_GOALS_HTML,
    ORBIT_GOALS_URI,
    ORBIT_MCP_APP_RESOURCES,
    ORBIT_RUN_HTML,
    ORBIT_RUN_URI,
    ORBIT_WORKFLOWS_HTML,
    ORBIT_WORKFLOWS_URI,
)


class CurrentTaskCardTests(unittest.TestCase):
    def test_it_keeps_current_task_as_the_default_resource(self) -> None:
        self.assertEqual("ui://orbit/current-task-v20.html", ORBIT_DASHBOARD_URI)
        self.assertEqual(ORBIT_DASHBOARD_URI, ORBIT_MCP_APP_RESOURCES[0]["uri"])

    def test_it_publishes_dedicated_cards(self) -> None:
        self.assertEqual(
            {
                ORBIT_DASHBOARD_URI, ORBIT_WORKFLOWS_URI,
                ORBIT_AUTHORING_URI, ORBIT_RUN_URI,
                ORBIT_GOALS_URI,
            },
            {item["uri"] for item in ORBIT_MCP_APP_RESOURCES},
        )

    def test_it_reads_current_task_and_embedded_workflow_views(self) -> None:
        calls = set(re.findall(r"callTool\('([a-z_]+)'", ORBIT_DASHBOARD_HTML))
        self.assertEqual(
            {
                "list_runs", "list_authoring_jobs", "get_run_steps",
                "list_workflows", "get_workflow_definition", "list_agents",
            },
            calls,
        )
        for absent in ("read_run_output", "read_authoring_output"):
            self.assertNotIn(absent, ORBIT_DASHBOARD_HTML)

    def test_workflow_selection_switches_views_inside_the_card(self) -> None:
        for marker in (
            "data-view-workflows", "showWorkflows", "showWorkflowDetail",
            "callTool('list_workflows'", "callTool('get_workflow_definition'",
            "data-back-view", "renderWorkflowList", "renderWorkflowDetail",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)

    def test_agents_switches_to_a_list_inside_the_dashboard_card(self) -> None:
        for marker in (
            "data-view-agents", "showAgents", "renderAgents",
            "callTool('list_agents'", 'class="agentRow"',
            "currentView === 'agents'", "data-back-view",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)

    def test_add_agent_opens_a_prefilled_follow_up_prompt(self) -> None:
        for marker in (
            "addAgent: 'Add Agent'", "addAgent: '添加 Agent'",
            "promptAddAgent: '给Orbit添加Agent cli：'",
            "const head = viewHead(t().agents,'task').replace",
            'data-prompt="${esc(t().promptAddAgent)}"',
            "button.addEventListener('click', () => send(button.dataset.prompt))",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)
        for absent in ("window.prompt", "data-add-agent-form", "data-add-agent-input"):
            self.assertNotIn(absent, ORBIT_DASHBOARD_HTML)

    def test_embedded_workflow_list_offers_new_goal_directly(self) -> None:
        for marker in (
            'class="workflowChoice"', "workflowGoal", "t().newGoal",
            "使用工作流「${name}」（${workflow.workflow_id}）执行：",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)

    def test_it_contains_no_administration_surface(self) -> None:
        for absent in (
            "workflowDelete", "deleteWorkflow", "tabWorkflows", "tabHistory",
            "tabAgents", "workflowGenerator", "authoringConsole", "stepOutput",
        ):
            self.assertNotIn(absent, ORBIT_DASHBOARD_HTML)

    def test_it_shows_progress_and_attention(self) -> None:
        for marker in (
            "DONE_STEPS", "waitingNotice", 'class="progress"',
            'class="steps"', "step.status === 'waiting'",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)

    def test_recent_completed_run_is_a_compact_summary_with_idle_actions(self) -> None:
        for marker in (
            "function renderRecentRun(run,workflowName)",
            "recentRun: 'Most recent run'",
            "recentRun: '最近一次执行'",
            'class="recentTitle">${esc(t().recentRun)}',
            'class="workflowName">${esc(workflowName || run.workflow_id)}',
            "if (TERMINAL.has(run.status))",
            "renderRecentRun(run,workflow?.name)",
            "function idleActions()",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)

        recent = ORBIT_DASHBOARD_HTML.split(
            "function renderRecentRun(run,workflowName) {", 1
        )[1].split("function bindActions", 1)[0]
        for absent in (
            "run.goal", "run.run_id", 'class="progress"',
            'class="steps"', "promptExplain", "promptOpen",
        ):
            self.assertNotIn(absent, recent)

        idle_actions = ORBIT_DASHBOARD_HTML.split(
            "function idleActions() {", 1
        )[1].split("function renderIdle", 1)[0]
        for marker in (
            "data-view-workflows", "t().createWorkflow",
            "t().history", "data-view-agents",
        ):
            self.assertIn(marker, idle_actions)

    def test_goal_run_card_paints_an_initial_failure_as_running(self) -> None:
        """A synchronous failed start must not make the card open as failed."""

        for marker in (
            "firstPaint&&run?.run_id&&run.status==='failed'",
            "draw({...run,status:'running'},[])",
            "timer=setTimeout(refresh,2000)",
        ):
            self.assertIn(marker, ORBIT_RUN_HTML)

    def test_goal_run_card_keeps_a_rejected_start_as_its_own_error(self) -> None:
        for marker in (
            "function failureMessage(value)",
            "if(failure){clearTimeout(timer)",
            "value?.run_id||failureMessage(value)",
        ):
            self.assertIn(marker, ORBIT_RUN_HTML)

    def test_idle_state_does_not_promote_stale_tasks(self) -> None:
        for marker in (
            "RECENT_TASK_MS = 5 * 60 * 60 * 1000", "isRecent", "recentRun",
            "recentJob", "准备开始", "选择工作流", "创建工作流",
            "promptSelectWorkflow", "promptCreateWorkflow",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)

    def test_idle_goal_flow_selects_a_workflow_first(self) -> None:
        self.assertNotIn("promptStart", ORBIT_DASHBOARD_HTML)
        self.assertIn(
            "查看 Orbit 工作流列表，以便选择一个工作流开始新目标。",
            ORBIT_DASHBOARD_HTML,
        )

    def test_suggested_actions_return_to_the_conversation(self) -> None:
        self.assertIn("'ui/message'", ORBIT_DASHBOARD_HTML)
        self.assertIn("sendFollowUpMessage", ORBIT_DASHBOARD_HTML)
        for prompt in (
            "promptHandle", "promptCancel", "promptExplain",
            "promptSelectWorkflow", "promptCreateWorkflow", "promptOpen",
        ):
            self.assertIn(prompt, ORBIT_DASHBOARD_HTML)

    def test_human_action_tells_chat_to_use_the_declared_output_port(self) -> None:
        for marker in (
            "promptHandle: run =>",
            "current interrupt_id, revision, and output_ports",
            '"result":{"decision":"approve","value":null}',
            "当前的 interrupt_id、revision 和 output_ports",
            "不要自创顶层字段",
            "t().promptHandle(run)",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)

    def test_history_asks_the_host_to_open_the_full_ui_while_agents_stays_in_card(self) -> None:
        for marker in (
            'class="actions idleActions"',
            "action(t().history,t().promptHistory)",
            "data-view-agents",
            "http://127.0.0.1:8848/ui/#/goals",
            "'ui/message'", "sendFollowUpMessage",
            ".idleActions { flex-wrap: nowrap; overflow-x: auto; }",
        ):
            self.assertIn(marker, ORBIT_DASHBOARD_HTML)
        idle = ORBIT_DASHBOARD_HTML.split('function idleActions() {', 1)[1].split(
            'function renderIdle', 1
        )[0]
        self.assertLess(idle.index('t().createWorkflow'), idle.index('t().history'))
        self.assertLess(idle.index('t().history'), idle.index('data-view-agents'))

    def test_it_does_not_request_a_large_display_surface(self) -> None:
        self.assertNotIn("request-display-mode", ORBIT_DASHBOARD_HTML)
        self.assertNotIn("fullscreen", ORBIT_DASHBOARD_HTML)

    def test_it_has_no_direct_mutation_path(self) -> None:
        self.assertNotIn("start_run", ORBIT_DASHBOARD_HTML)
        self.assertNotIn("cancel_run", ORBIT_DASHBOARD_HTML)
        self.assertNotIn("idempotency-key", ORBIT_DASHBOARD_HTML)
        self.assertNotIn("await fetch(", ORBIT_DASHBOARD_HTML)

    def test_it_supports_chinese_and_english(self) -> None:
        self.assertIn("'en-US'", ORBIT_DASHBOARD_HTML)
        self.assertIn("'zh-CN'", ORBIT_DASHBOARD_HTML)


class DedicatedCardTests(unittest.TestCase):
    def test_goals_card_lists_runs_without_embedding_the_browser_ui(self) -> None:
        self.assertIn("callTool('list_runs',{limit:100})", ORBIT_GOALS_HTML)
        self.assertIn("data-run-id", ORBIT_GOALS_HTML)
        self.assertIn("目标执行卡片", ORBIT_GOALS_HTML)
        self.assertNotIn("<iframe", ORBIT_GOALS_HTML)
        self.assertNotIn("127.0.0.1:8848/ui", ORBIT_GOALS_HTML)

    def test_workflow_list_contains_only_catalog_calls(self) -> None:
        self.assertIn("callTool('list_workflows'", ORBIT_WORKFLOWS_HTML)
        self.assertNotIn("list_runs", ORBIT_WORKFLOWS_HTML)
        self.assertNotIn("list_authoring_jobs", ORBIT_WORKFLOWS_HTML)

    def test_workflow_list_items_offer_the_same_new_goal_prompt(self) -> None:
        for marker in (
            'class="listGoal"', 'data-goal-id="${esc(w.workflow_id)}"',
            'data-goal-name="${esc(w.name||w.workflow_id)}"',
            "event.stopPropagation()",
            "使用工作流「${b.dataset.goalName}」（${b.dataset.goalId}）执行：",
            "background: light-dark(#e5e5e8, #303034) !important",
            "background: light-dark(#d9d9dd, #3a3a40) !important",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)

    def test_workflow_item_switches_to_detail_inside_the_list_card(self) -> None:
        for marker in (
            "b.onclick=()=>openDetail(b.dataset.openId)",
            "callTool('get_workflow_definition',{workflow_id:workflowId})",
            "function drawDetail(w)", 'id="workflowBack"',
            "document.getElementById('workflowBack').onclick=showList",
            "else if(value?.workflow_id){current=value;drawDetail(current)}",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)
        self.assertNotIn("使用工作流详情卡片展示", ORBIT_WORKFLOWS_HTML)

    def test_workflow_detail_returns_mutations_to_chat(self) -> None:
        self.assertIn("get_workflow_definition", ORBIT_WORKFLOWS_HTML)
        for label in ("新目标", "修改", "删除"):
            self.assertIn(label, ORBIT_WORKFLOWS_HTML)
        self.assertIn(
            'data-prompt="使用工作流「${esc(w.name||w.workflow_id)}」（${esc(w.workflow_id)}）执行："',
            ORBIT_WORKFLOWS_HTML,
        )
        self.assertIn(
            'data-prompt="按照下面的要求修改工作流「${esc(w.name||w.workflow_id)}」（${esc(w.workflow_id)}）："',
            ORBIT_WORKFLOWS_HTML,
        )
        self.assertNotIn("callTool('start_run'", ORBIT_WORKFLOWS_HTML)
        self.assertNotIn("callTool('delete", ORBIT_WORKFLOWS_HTML)

    def test_workflow_delete_requires_card_confirmation_then_returns_to_chat(self) -> None:
        for marker in (
            'id="deleteWorkflowDialog"', "showModal()", "确认删除工作流？",
            'id="cancelDeleteWorkflow"', 'id="confirmDeleteWorkflow"',
            "我确认删除工作流", "重新读取其最新版本", "新的幂等键",
            "bindDeleteConfirmation(w)",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)
        self.assertNotIn("callTool('delete_workflow'", ORBIT_WORKFLOWS_HTML)

    def test_workflow_detail_uses_the_bundled_xyflow_viewer(self) -> None:
        for marker in (
            "OrbitWorkflowGraph", "OrbitWorkflowGraph.mount",
            'data-workflow-graph aria-label="Workflow graph"',
            "react-flow__controls", "react-flow__background",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)
        for absent in ('class="graphEdge', "function bindGraph()", "forceSimulation"):
            self.assertNotIn(absent, ORBIT_WORKFLOWS_HTML)

    def test_workflow_detail_embeds_assets_without_remote_runtime_dependencies(self) -> None:
        self.assertNotRegex(ORBIT_WORKFLOWS_HTML, r'<script[^>]+src=')
        self.assertNotRegex(ORBIT_WORKFLOWS_HTML, r'<link[^>]+href=')
        self.assertRegex(ORBIT_WORKFLOWS_HTML, r"(?:const|var) OrbitWorkflowGraph=")

    def test_workflow_detail_defaults_to_graph_and_tabs_to_definitions(self) -> None:
        for marker in (
            'role="tablist"', 'id="workflowGraphTab"',
            'aria-selected="true" aria-controls="workflowGraphPanel"',
            'id="workflowDefinitionTab"', 'aria-controls="workflowDefinitionPanel"',
            'id="workflowDefinitionPanel" class="detailPanel definition" role="tabpanel"',
            "function bindTabs()", "ArrowLeft", "ArrowRight", "Home", "End",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)
        self.assertIn(
            'id="workflowDefinitionPanel" class="detailPanel definition" role="tabpanel" '
            'aria-labelledby="workflowDefinitionTab" hidden',
            ORBIT_WORKFLOWS_HTML,
        )
        self.assertIn('.tab[aria-selected="true"]::after{background:var(--accent)}', ORBIT_WORKFLOWS_HTML)
        self.assertNotIn('.tab[aria-selected="true"]{color:var(--text);background:', ORBIT_WORKFLOWS_HTML)

    def test_workflow_list_and_detail_share_a_stable_card_height(self) -> None:
        self.assertIn(":root { --workflow-card-height: 600px; }", ORBIT_WORKFLOWS_HTML)
        self.assertIn(
            "#card.workflowList, #card.workflowDetail { height: var(--workflow-card-height); }",
            ORBIT_WORKFLOWS_HTML,
        )
        self.assertIn("#card.workflowList { overflow-y: auto; }", ORBIT_WORKFLOWS_HTML)
        self.assertIn("card.className='card workflowList'", ORBIT_WORKFLOWS_HTML)
        self.assertIn("card.className='card workflowDetail'", ORBIT_WORKFLOWS_HTML)
        self.assertIn(
            "#card.workflowDetail .detailPanel { flex: 1 1 auto; height: auto; min-height: 0; }",
            ORBIT_WORKFLOWS_HTML,
        )

    def test_cards_receive_late_codex_tool_output(self) -> None:
        self.assertIn("openai:set_globals", ORBIT_WORKFLOWS_HTML)
        self.assertIn("globals.toolOutput", ORBIT_WORKFLOWS_HTML)
        self.assertIn("publishToolResult", ORBIT_WORKFLOWS_HTML)
        self.assertIn(".detailPanel{height:420px;overflow:hidden}", ORBIT_WORKFLOWS_HTML)
        self.assertIn(".detailPanel.definition{overflow-y:auto}", ORBIT_WORKFLOWS_HTML)

    def test_workflow_graph_supports_zoom_and_horizontal_pan(self) -> None:
        for marker in (
            "react-flow__controls-button", "react-flow__controls-zoomin",
            "react-flow__controls-zoomout", "react-flow__controls-fitview",
            "panOnScroll", "Horizontal", "maxZoom", "minZoom",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)

    def test_workflow_graph_tracks_the_codex_host_theme(self) -> None:
        for marker in (
            "host-context-changed", "applyHostContext", "currentTheme()",
            "document.documentElement.style.colorScheme=theme",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)

    def test_workflow_detail_uses_the_host_background(self) -> None:
        self.assertIn("--host-canvas: light-dark(#ffffff, #151515)", ORBIT_WORKFLOWS_HTML)
        self.assertIn("html, body, main", ORBIT_WORKFLOWS_HTML)
        self.assertIn("background: var(--host-canvas) !important", ORBIT_WORKFLOWS_HTML)
        self.assertIn(
            ".card, .tabs, .actions, .workflowGraphMount, .mcp-xyflow-viewer",
            ORBIT_WORKFLOWS_HTML,
        )
        self.assertIn("background: transparent !important", ORBIT_WORKFLOWS_HTML)

    def test_definition_items_expand_to_show_node_details(self) -> None:
        for marker in (
            "definitionItemToggle", 'aria-expanded="false"',
            "bindDefinitionItems()", "n.handler", "n.prompt",
        ):
            self.assertIn(marker, ORBIT_WORKFLOWS_HTML)

    def test_authoring_card_is_scoped_to_authoring(self) -> None:
        for marker in ("get_authoring_job", "list_authoring_jobs", "Publish workflow"):
            self.assertIn(marker, ORBIT_AUTHORING_HTML)
        self.assertNotIn("list_runs", ORBIT_AUTHORING_HTML)

    def test_run_card_is_scoped_to_one_run_and_its_result(self) -> None:
        for marker in ("inspect_run", "get_run_steps", "read_artifact_content"):
            self.assertIn(marker, ORBIT_RUN_HTML)
        self.assertNotIn("list_authoring_jobs", ORBIT_RUN_HTML)

    def test_run_card_labels_its_result(self) -> None:
        self.assertIn('<h2 class="resultTitle">执行结果</h2>', ORBIT_RUN_HTML)
        self.assertIn(".resultTitle{margin:0 0 6px", ORBIT_RUN_HTML)

    def test_run_card_clamps_the_goal_and_has_no_progress_bar(self) -> None:
        for marker in (
            "-webkit-line-clamp: 3", "-webkit-box-orient: vertical",
            'class="goal"', 'class="steps"',
        ):
            self.assertIn(marker, ORBIT_RUN_HTML)
        for absent in ('class="progress"', "Math.round", "steps.length} steps"):
            self.assertNotIn(absent, ORBIT_RUN_HTML)

    def test_run_card_uses_content_height_up_to_a_600px_maximum(self) -> None:
        for marker in (
            ":root { --goal-run-card-max-height: 600px; }",
            "#card.goalRun { height: auto; max-height: var(--goal-run-card-max-height);",
            "card.className='card goalRun'",
            "overflow-y: auto;",
            "overscroll-behavior: contain;",
        ):
            self.assertIn(marker, ORBIT_RUN_HTML)
        self.assertNotIn("--goal-run-card-height", ORBIT_RUN_HTML)


if __name__ == "__main__":
    unittest.main()
