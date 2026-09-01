"""Compact Orbit MCP App for current-task feedback.

The card is intentionally not an administration surface. Intent, approvals,
interrupt answers, and result interpretation belong in the host conversation;
the full browser UI owns catalogs, history, graphs, logs, and workflow
management. This View shows only the task that currently matters and sends a
small set of suggested actions back to the conversation.
"""

from pathlib import Path

# The host caches MCP App resources by URI. This URI intentionally changed
# after the dashboard was split from the workflow catalog so an older card
# cannot be reused for the current-task surface.
ORBIT_DASHBOARD_URI = "ui://orbit/current-task-v20.html"
ORBIT_DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"
# Bump the URI whenever the list card markup changes: Codex caches MCP App
# resources by URI and otherwise keeps rendering the previous document.
ORBIT_WORKFLOWS_URI = "ui://orbit/workflows-v6.html"
ORBIT_AUTHORING_URI = "ui://orbit/workflow-authoring.html"
ORBIT_RUN_URI = "ui://orbit/goal-run-v7.html"
ORBIT_GOALS_URI = "ui://orbit/goals-v1.html"

ORBIT_DASHBOARD_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="orbit-surface" content="mcp-app">
  <style>
    :root {
      color-scheme: light dark;
      font: 14px/1.45 Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      --bg: light-dark(#fff, #151517); --soft: light-dark(#f5f5f7, #1d1d20);
      --hover: light-dark(#ededf0, #252529); --line: light-dark(#dedee3, #303035);
      --text: light-dark(#202024, #e8e8eb); --muted: light-dark(#686871, #a0a0a9);
      --faint: light-dark(#86868f, #797982); --accent: #7772ff;
      --good: #54b878; --warn: #d99a35; --bad: #df6767;
    }
    * { box-sizing: border-box; }
    body { margin: 0; color: var(--text); background: var(--bg); }
    main { min-width: 0; padding: 16px; }
    header { display: flex; align-items: center; gap: 10px; }
    .mark { display: grid; width: 28px; height: 28px; place-items: center;
      border-radius: 8px; color: #fff; background: var(--accent); font-weight: 800; }
    .heading { min-width: 0; flex: 1; }
    h1 { margin: 0; font-size: 14px; font-weight: 650; }
    #updated { margin-top: 1px; color: var(--faint); font-size: 11px; }
    #refresh { width: 32px; height: 32px; border: 1px solid var(--line);
      border-radius: 8px; color: var(--muted); background: var(--soft); cursor: pointer; }
    #refresh:disabled { opacity: .55; cursor: default; }
    #card { margin-top: 14px; overflow: hidden; border: 1px solid var(--line);
      border-radius: 12px; background: var(--soft); }
    .summary { padding: 16px; }
    .statusLine { display: flex; align-items: center; gap: 8px; }
    .dot { width: 8px; height: 8px; flex: none; border-radius: 50%; background: var(--faint); }
    .dot.live { background: var(--accent); animation: pulse 1.5s ease-in-out infinite; }
    .dot.good { background: var(--good); } .dot.warn { background: var(--warn); }
    .dot.bad { background: var(--bad); }
    .status { color: var(--muted); font-size: 12px; font-weight: 620; }
    .goal { margin-top: 10px; font-size: 15px; font-weight: 600; line-height: 1.45;
      overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 3;
      -webkit-box-orient: vertical; overflow: hidden; }
    .meta { margin-top: 6px; color: var(--faint); font-size: 11px; overflow-wrap: anywhere; }
    .progress { height: 3px; margin-top: 14px; overflow: hidden; border-radius: 3px;
      background: var(--line); }
    .progress span { display: block; height: 100%; background: var(--accent); }
    .progressText { margin-top: 6px; color: var(--muted); font-size: 11px; }
    .notice { margin: 0 16px 14px; padding: 10px 12px; border: 1px solid
      color-mix(in srgb, var(--warn) 42%, var(--line)); border-radius: 8px;
      color: var(--warn); background: color-mix(in srgb, var(--warn) 8%, transparent);
      font-size: 12px; }
    .steps { border-top: 1px solid var(--line); }
    .step { display: grid; grid-template-columns: 16px minmax(0,1fr) auto;
      align-items: center; gap: 10px; min-height: 44px; padding: 8px 16px;
      border-bottom: 1px solid var(--line); }
    .step:last-child { border-bottom: 0; }
    .step .dot { width: 7px; height: 7px; }
    .stepName { min-width: 0; overflow: hidden; text-overflow: ellipsis;
      white-space: nowrap; font-size: 12px; }
    .stepState { color: var(--faint); font-size: 10px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px;
      border-top: 1px solid var(--line); background: var(--bg); }
    .idleActions { flex-wrap: nowrap; overflow-x: auto; }
    .idleActions .action { flex: 0 0 auto; }
    .action { min-height: 34px; padding: 7px 11px; border: 1px solid var(--line);
      border-radius: 8px; color: var(--text); background: var(--soft); cursor: pointer;
      font: inherit; font-size: 12px; }
    .action:hover { background: var(--hover); }
    .action.primary { border-color: transparent; color: #fff; background: var(--accent); }
    .viewHead { display: flex; align-items: center; gap: 8px; padding: 10px 12px;
      border-bottom: 1px solid var(--line); background: var(--bg); }
    .back { width: 30px; height: 30px; border: 1px solid var(--line); border-radius: 8px;
      color: var(--muted); background: var(--soft); cursor: pointer; }
    .viewTitle { min-width: 0; flex: 1; font-size: 12px; font-weight: 650; }
    .workflowChoice { display: grid; grid-template-columns: minmax(0, 1fr) auto;
      align-items: center; border-bottom: 1px solid var(--line); }
    .workflowChoice:last-child { border-bottom: 0; }
    .workflowRow { display: block; width: 100%; padding: 12px 14px; border: 0;
      color: inherit; text-align: left; background: transparent; cursor: pointer; }
    .workflowRow:hover { background: var(--hover); }
    .workflowGoal { margin-right: 12px; white-space: nowrap; }
    .workflowName { font-weight: 650; }
    .recentTitle { margin-bottom: 8px; color: var(--muted); font-size: 11px;
      font-weight: 650; letter-spacing: .02em; }
    .workflowDesc { margin-top: 3px; color: var(--muted); font-size: 11px; }
    .definition { border-top: 1px solid var(--line); }
    .definitionRow { padding: 10px 14px; border-bottom: 1px solid var(--line); }
    .definitionRow:last-child { border-bottom: 0; }
    .definitionName { font-size: 12px; font-weight: 620; }
    .definitionMeta { margin-top: 3px; color: var(--faint); font-size: 10px; }
    .agentRow { display: grid; grid-template-columns: minmax(0,1fr) auto auto;
      align-items: center; gap: 12px; min-height: 56px; padding: 10px 14px;
      border-bottom: 1px solid var(--line); }
    .agentRow:last-child { border-bottom: 0; }
    .agentIdentity { min-width: 0; }
    .agentName { overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      font-size: 12px; font-weight: 650; }
    .agentVersion { margin-top: 2px; color: var(--faint); font-size: 10px;
      overflow-wrap: anywhere; }
    .agentStat { min-width: 48px; color: var(--muted); text-align: right; font-size: 10px; }
    .agentStat strong { display: block; color: var(--text); font-size: 12px; }
    .agentStat.bad strong { color: var(--bad); }
    .empty { padding: 28px 18px; color: var(--muted); text-align: center; }
    .error { padding: 20px; color: var(--bad); text-align: center; font-size: 12px; }
    @keyframes pulse { 50% { opacity: .35; } }
    @media (prefers-reduced-motion: reduce) { .dot.live { animation: none; } }
  </style>
</head>
<body>
<main>
  <header><span class="mark">O</span><div class="heading"><h1>Orbit</h1>
    <div id="updated"></div></div><button id="refresh" type="button" aria-label="Refresh">↻</button></header>
  <section id="card" aria-live="polite"><div class="empty">Connecting…</div></section>
</main>
<script>
  const PROTOCOL = '2026-01-26';
  const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'unknown']);
  const DONE_STEPS = new Set(['succeeded', 'answered']);
  const ACTIVE_JOBS = new Set(['queued', 'running']);
  const RECENT_TASK_MS = 5 * 60 * 60 * 1000;
  const card = document.getElementById('card');
  const updated = document.getElementById('updated');
  const refreshButton = document.getElementById('refresh');
  let locale = navigator.language?.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en-US';
  let bridge = null, ready = null, poller = null, refreshing = false;
  let currentView = 'task', currentWorkflow = null;

  const S = {
    'en-US': {
      idle: 'Ready to start', idleHint: 'Run a goal with an existing workflow, or create a new workflow.',
      running: 'Running', waiting: 'Needs your input', completed: 'Completed', failed: 'Failed',
      cancelled: 'Cancelled', unknown: 'Needs review', queued: 'Workflow generation queued',
      authoring: 'Generating workflow', authoringDone: 'Workflow generated', authoringFailed: 'Workflow generation failed',
      progress: (done,total) => `${done} of ${total} steps completed`, waitingNotice: 'A workflow step is waiting for your response.',
      handle: 'Handle in chat', cancel: 'Request cancellation', explain: 'Explain result', selectWorkflow: 'Choose workflow', createWorkflow: 'Create workflow',
      workflows: 'Workflows', workflow: 'Workflow', back: 'Back', noWorkflows: 'No published workflows', noSteps: 'No steps', noAgents: 'No registered Agents', newGoal: 'New goal', modify: 'Modify', addAgent: 'Add Agent',
      history: 'History', agents: 'Agents',
      recentRun: 'Most recent run',
      runs: 'Runs', errors: 'Errors',
      open: 'Open full Orbit UI', refreshed: 'Updated just now', error: 'Could not read the current Orbit task.',
      status: { succeeded:'Done', answered:'Answered', running:'Running', waiting:'Waiting', failed:'Failed', unknown:'Review', cancelled:'Cancelled', not_reached:'Pending' },
      promptHandle: run => `Handle the pending human input for Orbit run ${run.run_id}. `
        + `Before resuming, inspect the run and use its current interrupt_id, revision, and output_ports. `
        + `For approval, submit the declared output port object (for example {"result":{"decision":"approve","value":null}}); do not invent top-level fields.`,
      promptCancel: id => `Cancel Orbit run ${id}.`, promptExplain: id => `Explain the result of Orbit run ${id}.`,
      promptSelectWorkflow: 'View the Orbit workflow list so I can choose a workflow for a new goal.', promptCreateWorkflow: 'Create an Orbit workflow from the following requirements:', promptAddAgent: '给Orbit添加Agent cli：', promptHistory: 'Open this address in the browser on the right side of the Codex app: http://127.0.0.1:8848/ui/#/goals', promptOpen: 'Open the full Orbit UI.',
    },
    'zh-CN': {
      idle: '准备开始', idleHint: '使用已有工作流执行目标，或创建新的工作流。',
      running: '运行中', waiting: '需要你的处理', completed: '已完成', failed: '失败',
      cancelled: '已取消', unknown: '需要检查', queued: '工作流生成已排队',
      authoring: '正在生成工作流', authoringDone: '工作流已生成', authoringFailed: '工作流生成失败',
      progress: (done,total) => `已完成 ${done}/${total} 个步骤`, waitingNotice: '有一个工作流步骤正在等待你的回复。',
      handle: '在聊天中处理', cancel: '请求取消', explain: '解释结果', selectWorkflow: '选择工作流', createWorkflow: '创建工作流',
      workflows: '工作流', workflow: '工作流详情', back: '返回', noWorkflows: '暂无已发布工作流', noSteps: '暂无步骤', noAgents: '暂无已注册 Agent', newGoal: '新目标', modify: '修改', addAgent: '添加 Agent',
      history: '历史记录', agents: 'Agents',
      recentRun: '最近一次执行',
      runs: '运行', errors: '错误',
      open: '打开完整 Orbit UI', refreshed: '刚刚更新', error: '无法读取当前 Orbit 任务。',
      status: { succeeded:'完成', answered:'已回答', running:'运行中', waiting:'等待', failed:'失败', unknown:'检查', cancelled:'取消', not_reached:'未开始' },
      promptHandle: run => `处理 Orbit 运行 ${run.run_id} 中等待人工输入的步骤。`
        + `恢复前请重新检查运行，并使用当前的 interrupt_id、revision 和 output_ports。`
        + `批准时提交已声明的输出端口对象（例如 {"result":{"decision":"approve","value":null}}），不要自创顶层字段。`,
      promptCancel: id => `取消 Orbit 运行 ${id}。`, promptExplain: id => `解释 Orbit 运行 ${id} 的结果。`,
      promptSelectWorkflow: '查看 Orbit 工作流列表，以便选择一个工作流开始新目标。', promptCreateWorkflow: '按照下面的要求创建 Orbit 工作流：', promptAddAgent: '给Orbit添加Agent cli：', promptHistory: '在 Codex App 右侧用浏览器打开地址：http://127.0.0.1:8848/ui/#/goals', promptOpen: '打开 Orbit 完整 UI。',
    },
  };
  const t = () => S[locale] || S['en-US'];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const list = (value, key) => Array.isArray(value?.[key]) ? value[key]
    : Array.isArray(value?.data?.[key]) ? value.data[key]
    : Array.isArray(value?.structuredContent?.[key]) ? value.structuredContent[key] : [];
  const cssFor = status => status === 'running' || status === 'queued' ? 'live'
    : status === 'waiting' ? 'warn' : status === 'completed' || status === 'succeeded' || status === 'answered' ? 'good'
    : status === 'failed' || status === 'cancelled' ? 'bad' : '';
  const updatedAt = item => Date.parse(item?.updated_at || item?.created_at || '') || 0;
  const isRecent = item => updatedAt(item) > 0 && Date.now() - updatedAt(item) <= RECENT_TASK_MS;

  function mcpBridge() {
    if (window.parent === window) return null;
    const pending = new Map(); let id = 0;
    window.addEventListener('message', event => {
      if (event.source !== window.parent || event.data?.jsonrpc !== '2.0') return;
      const message = event.data;
      if (message.id != null && pending.has(message.id)) {
        const item = pending.get(message.id); pending.delete(message.id);
        message.error ? item.reject(new Error(message.error.message || 'Host error')) : item.resolve(message.result);
      } else if (message.method === 'ui/notifications/host-context-changed') {
        const next = message.params?.locale || message.params?.language;
        if (String(next).toLowerCase().startsWith('zh')) locale = 'zh-CN';
        else if (next) locale = 'en-US';
      }
    });
    return {
      request: (method, params, timeout=15000) => new Promise((resolve,reject) => {
        const callId = ++id; pending.set(callId,{resolve,reject});
        window.parent.postMessage({jsonrpc:'2.0',id:callId,method,params},'*');
        setTimeout(() => { if (pending.delete(callId)) reject(new Error(`Timeout: ${method}`)); }, timeout);
      }),
      notify: (method,params={}) => window.parent.postMessage({jsonrpc:'2.0',method,params},'*'),
    };
  }

  async function ensureReady() {
    if (!bridge) throw new Error('No MCP App host');
    if (!ready) ready = bridge.request('ui/initialize', {
      appCapabilities: {}, appInfo: {name:'orbit-current-task',version:'1'}, protocolVersion:PROTOCOL,
    }, 4000).then(result => {
      const next = result?.hostContext?.locale || result?.hostContext?.language;
      if (String(next).toLowerCase().startsWith('zh')) locale = 'zh-CN';
      else if (next) locale = 'en-US';
      bridge.notify('ui/notifications/initialized', {}); return result;
    });
    return ready;
  }

  async function callTool(name, args={}) {
    if (window.openai?.callTool) {
      const result = await window.openai.callTool(name,args);
      return result?.structuredContent || result;
    }
    await ensureReady();
    const result = await bridge.request('tools/call',{name,arguments:args});
    return result?.structuredContent || result;
  }

  async function send(prompt) {
    if (bridge) {
      try { await ensureReady(); await bridge.request('ui/message',{role:'user',content:[{type:'text',text:prompt}]},10000); return; }
      catch (_) { /* compatibility path below */ }
    }
    if (window.openai?.sendFollowUpMessage) await window.openai.sendFollowUpMessage({prompt,scrollToBottom:true});
  }

  function action(label,prompt,primary=false) {
    return `<button class="action${primary?' primary':''}" type="button" data-prompt="${esc(prompt)}">${esc(label)}</button>`;
  }

  function idleActions() {
    return `<div class="actions idleActions"><button class="action primary" type="button" data-view-workflows>${esc(t().selectWorkflow)}</button>${action(t().createWorkflow,t().promptCreateWorkflow)}
      ${action(t().history,t().promptHistory)}
      <button class="action" type="button" data-view-agents>${esc(t().agents)}</button></div>`;
  }

  function renderIdle() {
    card.innerHTML = `<div class="empty"><strong>${esc(t().idle)}</strong><div>${esc(t().idleHint)}</div></div>
      ${idleActions()}`;
  }

  function viewHead(title,backView) {
    return `<div class="viewHead"><button class="back" type="button" data-back-view="${backView}" aria-label="${esc(t().back)}">←</button><div class="viewTitle">${esc(title)}</div></div>`;
  }

  function renderWorkflowList(workflows) {
    const rows = workflows.map(workflow => { const name = workflow.name || workflow.workflow_id; return `<div class="workflowChoice"><button class="workflowRow" type="button" data-workflow-id="${esc(workflow.workflow_id)}">
      <div class="workflowName">${esc(name)}</div>
      <div class="workflowDesc">${esc(workflow.description || `${workflow.node_count || 0} steps · v${workflow.latest_version || ''}`)}</div></button>
      ${action(t().newGoal,`使用工作流「${name}」（${workflow.workflow_id}）执行：`,true).replace('class="action primary"','class="action primary workflowGoal"')}</div>`; }).join('');
    card.innerHTML = `${viewHead(t().workflows,'task')}${rows || `<div class="empty">${esc(t().noWorkflows)}</div>`}`;
  }

  function renderWorkflowDetail(workflow) {
    const nodes = workflow.nodes || workflow.definition?.nodes || [];
    const rows = nodes.map(node => `<div class="definitionRow"><div class="definitionName">${esc(node.label || node.node_id || node.id)}</div>
      <div class="definitionMeta">${esc(node.kind || '')}${node.handler ? ` · ${esc(node.handler)}` : ''}</div></div>`).join('');
    const name = workflow.name || workflow.workflow_id;
    card.innerHTML = `${viewHead(t().workflow,'workflows')}<div class="summary"><div class="workflowName">${esc(name)}</div>
      <div class="workflowDesc">${esc(workflow.description || '')}</div><div class="meta">${esc(workflow.workflow_id)} · v${esc(workflow.latest_version || '')}</div></div>
      <div class="definition">${rows || `<div class="empty">${esc(t().noSteps)}</div>`}</div>
      <div class="actions">${action(t().newGoal,`使用工作流「${name}」（${workflow.workflow_id}）执行：`,true)}${action(t().modify,`按照下面的要求修改工作流「${name}」（${workflow.workflow_id}）：`)}</div>`;
  }

  function renderAgents(agents) {
    const rows = agents.map(agent => { const name = String(agent.name || '').replace(/^agent\./,''); return `<div class="agentRow">
      <div class="agentIdentity"><div class="agentName" title="${esc(agent.name)}">${esc(name || agent.name)}</div><div class="agentVersion">${esc(agent.version || '')}</div></div>
      <div class="agentStat"><strong>${esc(agent.attempt_count ?? 0)}</strong>${esc(t().runs)}</div>
      <div class="agentStat${agent.failed_count > 0 ? ' bad' : ''}"><strong>${esc(agent.failed_count ?? 0)}</strong>${esc(t().errors)}</div></div>`; }).join('');
    const head = viewHead(t().agents,'task').replace('</div>', `</div><button class="action primary" type="button" data-prompt="${esc(t().promptAddAgent)}">${esc(t().addAgent)}</button>`);
    card.innerHTML = `${head}${rows || `<div class="empty">${esc(t().noAgents)}</div>`}`;
  }

  function renderAuthoring(job) {
    const status = job.status === 'queued' ? t().queued : job.status === 'done' ? t().authoringDone
      : job.status === 'failed' ? t().authoringFailed : t().authoring;
    const prompt = job.prompt || job.requirements || '';
    card.innerHTML = `<div class="summary"><div class="statusLine"><span class="dot ${cssFor(job.status)}"></span>
      <span class="status">${esc(status)}</span></div><div class="goal">${esc(prompt || status)}</div>
      <div class="meta">${esc(job.job_id || '')}</div></div><div class="actions">${action(t().open,t().promptOpen)}</div>`;
  }

  function renderRun(run,steps) {
    const waiting = steps.some(step => step.status === 'waiting');
    const live = !TERMINAL.has(run.status); const statusKey = waiting ? 'waiting' : run.status;
    const done = steps.filter(step => DONE_STEPS.has(step.status)).length;
    const percent = steps.length ? Math.round(done * 100 / steps.length) : 0;
    const stepRows = steps.map(step => `<div class="step"><span class="dot ${cssFor(step.status)}"></span>
      <span class="stepName">${esc(step.label || step.node_id)}</span><span class="stepState">${esc(t().status[step.status] || step.status)}</span></div>`).join('');
    let actions = action(t().open,t().promptOpen);
    if (waiting) actions = action(t().handle,t().promptHandle(run),true) + actions;
    else if (live) actions = action(t().cancel,t().promptCancel(run.run_id)) + actions;
    else actions = action(t().explain,t().promptExplain(run.run_id),true) + actions;
    card.innerHTML = `<div class="summary"><div class="statusLine"><span class="dot ${cssFor(statusKey)}"></span>
      <span class="status">${esc(t()[statusKey] || statusKey)}</span></div>
      <div class="goal">${esc(run.goal || run.workflow_id || run.run_id)}</div>
      <div class="meta">${esc(run.workflow_id || '')} · ${esc(run.run_id)}</div>
      ${steps.length ? `<div class="progress"><span style="width:${percent}%"></span></div><div class="progressText">${esc(t().progress(done,steps.length))}</div>` : ''}</div>
      ${waiting ? `<div class="notice">${esc(t().waitingNotice)}</div>` : ''}
      ${stepRows ? `<div class="steps">${stepRows}</div>` : ''}<div class="actions">${actions}</div>`;
  }

  function renderRecentRun(run,workflowName) {
    const statusKey = run.status;
    card.innerHTML = `<div class="summary"><div class="recentTitle">${esc(t().recentRun)}</div>
      <div class="workflowName">${esc(workflowName || run.workflow_id)}</div>
      <div class="statusLine"><span class="dot ${cssFor(statusKey)}"></span>
      <span class="status">${esc(t()[statusKey] || statusKey)}</span></div></div>${idleActions()}`;
  }

  function bindActions() {
    card.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => send(button.dataset.prompt)));
    card.querySelectorAll('[data-view-workflows]').forEach(button => button.addEventListener('click',showWorkflows));
    card.querySelectorAll('[data-view-agents]').forEach(button => button.addEventListener('click',showAgents));
    card.querySelectorAll('[data-workflow-id]').forEach(button => button.addEventListener('click',() => showWorkflowDetail(button.dataset.workflowId)));
    card.querySelectorAll('[data-back-view]').forEach(button => button.addEventListener('click',() => {
      if (button.dataset.backView === 'workflows') showWorkflows();
      else { currentView = 'task'; currentWorkflow = null; refresh(); }
    }));
  }

  async function showWorkflows() {
    clearTimeout(poller); currentView = 'workflows'; currentWorkflow = null;
    refreshButton.disabled = true;
    try { const result = await callTool('list_workflows',{}); renderWorkflowList(list(result,'workflows')); bindActions(); updated.textContent = t().refreshed; }
    catch (_) { card.innerHTML = `${viewHead(t().workflows,'task')}<div class="error">${esc(t().error)}</div>`; bindActions(); }
    finally { refreshButton.disabled = false; }
  }

  async function showWorkflowDetail(workflowId) {
    clearTimeout(poller); currentView = 'workflow'; currentWorkflow = workflowId;
    refreshButton.disabled = true;
    try { const result = await callTool('get_workflow_definition',{workflow_id:workflowId}); renderWorkflowDetail(result); bindActions(); updated.textContent = t().refreshed; }
    catch (_) { card.innerHTML = `${viewHead(t().workflow,'workflows')}<div class="error">${esc(t().error)}</div>`; bindActions(); }
    finally { refreshButton.disabled = false; }
  }

  async function showAgents() {
    clearTimeout(poller); currentView = 'agents'; currentWorkflow = null;
    refreshButton.disabled = true;
    try { const result = await callTool('list_agents',{}); renderAgents(list(result,'agents')); bindActions(); updated.textContent = t().refreshed; }
    catch (_) { card.innerHTML = `${viewHead(t().agents,'task')}<div class="error">${esc(t().error)}</div>`; bindActions(); }
    finally { refreshButton.disabled = false; }
  }

  async function refresh() {
    if (currentView === 'workflows') return showWorkflows();
    if (currentView === 'workflow' && currentWorkflow) return showWorkflowDetail(currentWorkflow);
    if (currentView === 'agents') return showAgents();
    if (refreshing) return; refreshing = true; refreshButton.disabled = true; clearTimeout(poller);
    try {
      const [runResult,jobResult] = await Promise.all([
        callTool('list_runs',{limit:10}), callTool('list_authoring_jobs',{limit:10}),
      ]);
      const runs = list(runResult,'runs'); const jobs = list(jobResult,'jobs');
      const activeRun = runs.find(run => !TERMINAL.has(run.status));
      const activeJob = jobs.find(job => ACTIVE_JOBS.has(job.status));
      const recentRun = runs.find(run => TERMINAL.has(run.status) && isRecent(run));
      const recentJob = jobs.find(job => !ACTIVE_JOBS.has(job.status) && isRecent(job));
      const run = activeRun || (!activeJob && (!recentJob || updatedAt(recentRun) >= updatedAt(recentJob)) ? recentRun : null);
      if (run) {
        if (TERMINAL.has(run.status)) {
          const workflowResult = await callTool('list_workflows',{});
          const workflow = list(workflowResult,'workflows').find(item => item.workflow_id === run.workflow_id);
          renderRecentRun(run,workflow?.name);
        } else {
          const stepResult = await callTool('get_run_steps',{run_id:run.run_id});
          renderRun(run,list(stepResult,'steps'));
        }
      } else if (activeJob || recentJob) renderAuthoring(activeJob || recentJob);
      else renderIdle();
      bindActions(); updated.textContent = t().refreshed;
      poller = setTimeout(() => { if (currentView === 'task' && document.visibilityState === 'visible') refresh(); }, activeRun || activeJob ? 2000 : 15000);
    } catch (_) {
      card.innerHTML = `<div class="error">${esc(t().error)}</div><div class="actions">${action(t().open,t().promptOpen)}</div>`;
      bindActions(); poller = setTimeout(refresh,15000);
    } finally { refreshing = false; refreshButton.disabled = false; }
  }

  bridge = mcpBridge();
  refreshButton.addEventListener('click',refresh);
  document.addEventListener('visibilitychange',() => { if (document.visibilityState === 'visible') refresh(); });
  refresh();
</script>
</body>
</html>"""

_CARD_STYLE = r"""
  :root { color-scheme:light dark; font:14px/1.45 Inter,ui-sans-serif,-apple-system,
    BlinkMacSystemFont,"Segoe UI",sans-serif; --bg:light-dark(#fff,#151517);
    --soft:light-dark(#f5f5f7,#1d1d20); --hover:light-dark(#ededf0,#252529);
    --line:light-dark(#dedee3,#303035); --text:light-dark(#202024,#e8e8eb);
    --muted:light-dark(#686871,#a0a0a9); --accent:#7772ff; --good:#54b878;
    --warn:#d99a35; --bad:#df6767; }
  *{box-sizing:border-box} body{margin:0;color:var(--text);background:var(--bg)}
  main{padding:16px} header{display:flex;align-items:center;gap:10px;margin-bottom:14px}
  .mark{display:grid;width:28px;height:28px;place-items:center;border-radius:8px;color:#fff;
    background:var(--accent);font-weight:800} h1{margin:0;flex:1;font-size:14px}
  button{font:inherit}.icon{width:32px;height:32px;border:1px solid var(--line);border-radius:8px;
    color:var(--muted);background:var(--soft);cursor:pointer}.card{overflow:hidden;border:1px solid var(--line);
    border-radius:12px;background:var(--soft)} .empty,.error{padding:26px 16px;text-align:center;color:var(--muted)}
  .error{color:var(--bad)} .row{display:block;width:100%;padding:12px 14px;border:0;border-bottom:1px solid var(--line);
    color:inherit;text-align:left;background:transparent;cursor:pointer}.row:last-child{border-bottom:0}.row:hover{background:var(--hover)}
  .name{font-weight:650}.desc,.meta{margin-top:3px;color:var(--muted);font-size:11px;overflow-wrap:anywhere}
  .summary{padding:15px}.statusLine{display:flex;align-items:center;gap:8px}.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
  .dot.live{background:var(--accent);animation:pulse 1.4s infinite}.dot.good{background:var(--good)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
  .goal{margin-top:9px;font-size:14px;font-weight:600;overflow-wrap:anywhere}.progress{height:3px;margin-top:12px;border-radius:3px;background:var(--line);overflow:hidden}
  .progress span{display:block;height:100%;background:var(--accent)}.steps{border-top:1px solid var(--line)}
  .step{display:grid;grid-template-columns:14px minmax(0,1fr) auto;gap:8px;align-items:center;padding:9px 14px;border-bottom:1px solid var(--line);font-size:12px}
  .step:last-child{border-bottom:0}.result{padding:12px 14px;border-top:1px solid var(--line);white-space:pre-wrap;overflow-wrap:anywhere}
  .resultTitle{margin:0 0 6px;font-size:12px;font-weight:650}
  .detailPanel{height:420px;overflow:hidden}.detailPanel.definition{overflow-y:auto}
  .workflowGraphMount{width:100%;height:100%;min-width:0;min-height:0;background:var(--bg)}
  .tabs{display:flex;gap:20px;padding:0 14px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--bg)}
  .tab{position:relative;min-height:42px;padding:0 2px;border:0;color:var(--muted);background:transparent;cursor:pointer}
  .tab:hover{color:var(--text)}
  .tab::after{position:absolute;right:0;bottom:-1px;left:0;height:2px;border-radius:2px 2px 0 0;background:transparent;content:""}
  .tab[aria-selected="true"]{color:var(--text);font-weight:650}
  .tab[aria-selected="true"]::after{background:var(--accent)}
  .tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  [role="tabpanel"][hidden]{display:none}
  .actions{display:flex;flex-wrap:wrap;gap:8px;padding:12px 14px;border-top:1px solid var(--line);background:var(--bg)}
  .action{padding:7px 11px;border:1px solid var(--line);border-radius:8px;color:var(--text);background:var(--soft);cursor:pointer}
  .action.primary{border-color:transparent;color:#fff;background:var(--accent)}.action.danger{color:var(--bad)}
  @keyframes pulse{50%{opacity:.35}} @media(prefers-reduced-motion:reduce){.dot.live{animation:none}}
"""

_CARD_BRIDGE = r"""
const PROTOCOL='2026-01-26'; let bridge=null,ready=null,lastToolResult=null,hostTheme=null;const toolResultListeners=[],hostContextListeners=[];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const payload=v=>v?.structuredContent||v?.data||v||{};
function publishToolResult(value){lastToolResult=payload(value);toolResultListeners.forEach(fn=>fn(lastToolResult))}
function applyHostContext(context={}){const theme=context.theme||context.colorScheme;if(theme==='light'||theme==='dark'){hostTheme=theme;document.documentElement.style.colorScheme=theme}hostContextListeners.forEach(fn=>fn(context))}
function currentTheme(){return hostTheme||((window.openai?.theme==='light'||window.openai?.theme==='dark')?window.openai.theme:null)}
function onHostContext(fn){hostContextListeners.push(fn)}
function mcpBridge(){if(window.parent===window)return null;const pending=new Map();let id=0;
 window.addEventListener('message',e=>{if(e.source!==window.parent||e.data?.jsonrpc!=='2.0')return;const m=e.data;
  if(m.id!=null&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(new Error(m.error.message||'Host error')):p.resolve(m.result)}
  else if(m.method==='ui/notifications/tool-result')publishToolResult(m.params?.result||m.params)
  else if(m.method==='ui/notifications/host-context-changed')applyHostContext(m.params)});
 return{request:(method,params,timeout=15000)=>new Promise((resolve,reject)=>{const callId=++id;pending.set(callId,{resolve,reject});
  window.parent.postMessage({jsonrpc:'2.0',id:callId,method,params},'*');setTimeout(()=>{if(pending.delete(callId))reject(new Error(`Timeout: ${method}`))},timeout)}),
  notify:(method,params={})=>window.parent.postMessage({jsonrpc:'2.0',method,params},'*')}}
async function ensureReady(){if(!bridge)throw new Error('No MCP App host');if(!ready)ready=bridge.request('ui/initialize',{
 appCapabilities:{},appInfo:{name:'orbit-card',version:'1'},protocolVersion:PROTOCOL},4000).then(r=>{applyHostContext(r?.hostContext);bridge.notify('ui/notifications/initialized',{});return r});return ready}
async function callTool(name,args={}){if(window.openai?.callTool)return payload(await window.openai.callTool(name,args));await ensureReady();return payload(await bridge.request('tools/call',{name,arguments:args}))}
async function send(prompt){if(bridge){try{await ensureReady();await bridge.request('ui/message',{role:'user',content:[{type:'text',text:prompt}]},10000);return}catch(_){}}
 if(window.openai?.sendFollowUpMessage)await window.openai.sendFollowUpMessage({prompt,scrollToBottom:true})}
function initial(){return payload(window.openai?.toolOutput||window.openai?.toolResponse||lastToolResult||{})}
function onToolResult(fn){toolResultListeners.push(fn);const value=initial();if(Object.keys(value).length)fn(value)}
function bind(){document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>send(b.dataset.prompt))}
window.addEventListener('openai:set_globals',event=>{const globals=event.detail?.globals||{};
 if(Object.prototype.hasOwnProperty.call(globals,'toolOutput'))publishToolResult(globals.toolOutput);
 else if(Object.prototype.hasOwnProperty.call(globals,'toolResponse'))publishToolResult(globals.toolResponse);
 if(Object.prototype.hasOwnProperty.call(globals,'theme'))applyHostContext({theme:globals.theme})});
bridge=mcpBridge();
"""


_MCP_APP_ASSET_DIR = Path(__file__).resolve().parents[1] / "static" / "mcp-app"
_XYFLOW_STYLE = (_MCP_APP_ASSET_DIR / "workflow-detail.css").read_text(
    encoding="utf-8"
).replace("</style", "<\\/style")
_XYFLOW_SCRIPT = (_MCP_APP_ASSET_DIR / "workflow-detail.js").read_text(
    encoding="utf-8"
).replace("</script", "<\\/script")

_WORKFLOW_DETAIL_STYLE = _XYFLOW_STYLE + r"""
:root { --host-canvas: light-dark(#ffffff, #151515); }
html, body, main {
  background: var(--host-canvas) !important;
}
.card, .tabs, .actions, .workflowGraphMount, .mcp-xyflow-viewer {
  background: transparent !important;
}
.definitionItemToggle {
  width: 100%; border: 0; color: inherit; background: transparent;
  text-align: left; cursor: pointer; font: inherit;
}
.definitionItemToggle:hover { background: var(--hover); }
.definitionItemToggle:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.definitionDetails {
  padding: 12px 14px; border-bottom: 1px solid var(--line);
  color: var(--muted); font-size: 12px;
}
.definitionDetails pre {
  margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere;
  color: var(--text); font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
}
.confirmDialog {
  width: min(420px, calc(100% - 32px)); padding: 0; border: 1px solid var(--line);
  border-radius: 12px; color: var(--text); background: var(--soft);
  box-shadow: 0 18px 48px rgba(0,0,0,.32);
}
.confirmDialog::backdrop { background: rgba(0,0,0,.58); }
.confirmBody { padding: 18px; }
.confirmTitle { margin: 0; font-size: 15px; }
.confirmText { margin: 8px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.confirmActions { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 18px;
  border-top: 1px solid var(--line); }
"""


def _card(
    title: str,
    body: str,
    *,
    extra_style: str = "",
    extra_script: str = "",
) -> str:
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<meta name=\"orbit-surface\" content=\"mcp-app\"><style>{_CARD_STYLE}{extra_style}</style></head><body><main>
<header><span class=\"mark\">O</span><h1>{title}</h1><button id=\"refresh\" class=\"icon\" type=\"button\">↻</button></header>
<section id=\"card\" class=\"card\"><div class=\"empty\">Connecting…</div></section></main><script>{_CARD_BRIDGE}{extra_script}{body}</script></body></html>"""


_WORKFLOW_LIST_STYLE = r"""
:root { --workflow-card-height: 600px; }
#card.workflowList, #card.workflowDetail { height: var(--workflow-card-height); }
#card.workflowList { overflow-y: auto; }
#card.workflowDetail { display: flex; min-height: 0; flex-direction: column; }
#card.workflowDetail .detailPanel { flex: 1 1 auto; height: auto; min-height: 0; }
.workflowRow { position: relative; border-bottom: 1px solid var(--line); }
.workflowRow:last-child { border-bottom: 0; }
.workflowRow .row { min-height: 68px; padding-right: 104px; border-bottom: 0; }
.listGoal { position: absolute; top: 50%; right: 12px; transform: translateY(-50%);
  min-height: 32px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 7px;
  color: light-dark(#303034, #e4e4e8); background: light-dark(#e5e5e8, #303034) !important;
  cursor: pointer; }
.listGoal:hover { background: light-dark(#d9d9dd, #3a3a40) !important; }
.viewHead { display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  border-bottom: 1px solid var(--line); background: var(--bg); }
.back { width: 30px; height: 30px; border: 1px solid var(--line); border-radius: 8px;
  color: var(--muted); background: var(--soft); cursor: pointer; }
.viewTitle { min-width: 0; flex: 1; font-size: 12px; font-weight: 650; }
"""


ORBIT_WORKFLOWS_HTML = _card("Orbit · Workflows", r"""
const card=document.getElementById('card');let current=null;
function graphMarkup(graph){return graph?.nodes?.length?'<div class="workflowGraphMount" data-workflow-graph aria-label="Workflow graph"></div>':'<div class="empty">No graph</div>'}
function mountGraph(graph){const element=card.querySelector('[data-workflow-graph]');if(element&&globalThis.OrbitWorkflowGraph?.mount)globalThis.OrbitWorkflowGraph.mount(element,graph,currentTheme())}
function bindTabs(){const tabs=[...card.querySelectorAll('[role="tab"]')];
 function select(tab){tabs.forEach(item=>{const selected=item===tab;item.setAttribute('aria-selected',String(selected));item.tabIndex=selected?0:-1;const panel=document.getElementById(item.getAttribute('aria-controls'));if(panel)panel.hidden=!selected});if(tab.id==='workflowGraphTab')window.dispatchEvent(new Event('resize'))}
 tabs.forEach((tab,index)=>{tab.onclick=()=>select(tab);tab.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();let next=index;if(event.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;if(event.key==='ArrowRight')next=(index+1)%tabs.length;if(event.key==='Home')next=0;if(event.key==='End')next=tabs.length-1;select(tabs[next]);tabs[next].focus()}})}
function bindDefinitionItems(){card.querySelectorAll('.definitionItemToggle').forEach(button=>{button.onclick=()=>{const details=button.nextElementSibling;const expanded=button.getAttribute('aria-expanded')!=='true';button.setAttribute('aria-expanded',String(expanded));if(details)details.hidden=!expanded}})}
function bindDeleteConfirmation(w){const dialog=document.getElementById('deleteWorkflowDialog'),open=document.getElementById('openDeleteWorkflowDialog'),cancel=document.getElementById('cancelDeleteWorkflow'),confirm=document.getElementById('confirmDeleteWorkflow');
 if(!dialog||!open||!cancel||!confirm)return;open.onclick=()=>dialog.showModal();cancel.onclick=()=>dialog.close();
 dialog.onclick=event=>{if(event.target===dialog)dialog.close()};confirm.onclick=()=>{dialog.close();send(`我确认删除工作流${w.workflow_id}（${w.name||w.workflow_id}）。请重新读取其最新版本，并使用授权的 delete_workflow 工具和新的幂等键执行删除。`)}}
function drawList(rows){current=null;
 card.className='card workflowList';
 card.innerHTML=rows.length?rows.map(w=>`<div class="workflowRow"><button class="row" type="button" data-open-id="${esc(w.workflow_id)}"><div class="name">${esc(w.name)}</div>
 <div class="desc">${esc(w.description||`${w.node_count||0} steps · v${w.latest_version||''}`)}</div></button><button class="listGoal" type="button" data-goal-id="${esc(w.workflow_id)}" data-goal-name="${esc(w.name||w.workflow_id)}">新目标</button></div>`).join(''):'<div class="empty">No workflows</div>';
 card.querySelectorAll('[data-open-id]').forEach(b=>b.onclick=()=>openDetail(b.dataset.openId));
 card.querySelectorAll('[data-goal-id]').forEach(b=>b.onclick=event=>{event.stopPropagation();send(`使用工作流「${b.dataset.goalName}」（${b.dataset.goalId}）执行：`) });
}
function drawDetail(w){
 card.className='card workflowDetail';
 const nodes=w.nodes||w.definition?.nodes||[];const rows=nodes.map(n=>`<div class="definitionItem"><button class="step definitionItemToggle" type="button" aria-expanded="false"><span class="dot"></span><span>${esc(n.label||n.node_id||n.id)}</span><span class="meta">${esc(n.kind)}</span></button><div class="definitionDetails" hidden><div>处理器：${esc(n.handler||'—')}</div><pre>${esc(n.prompt||'无提示词')}</pre></div></div>`).join('');
 card.innerHTML=`<div class="viewHead"><button id="workflowBack" class="back" type="button" aria-label="返回工作流列表">‹</button><span class="viewTitle">工作流详情</span></div><div class="summary"><div class="name">${esc(w.name)}</div><div class="desc">${esc(w.description||'')}</div>
 <div class="meta">${esc(w.workflow_id)} · v${esc(w.latest_version)}</div></div>
 <div class="tabs" role="tablist" aria-label="工作流详情视图"><button id="workflowGraphTab" class="tab" type="button" role="tab" aria-selected="true" aria-controls="workflowGraphPanel">流程图</button><button id="workflowDefinitionTab" class="tab" type="button" role="tab" aria-selected="false" aria-controls="workflowDefinitionPanel" tabindex="-1">定义列表</button></div>
 <div id="workflowGraphPanel" class="detailPanel" role="tabpanel" aria-labelledby="workflowGraphTab">${graphMarkup(w.graph)}</div>
 <div id="workflowDefinitionPanel" class="detailPanel definition" role="tabpanel" aria-labelledby="workflowDefinitionTab" hidden>${rows?`<div class="steps">${rows}</div>`:'<div class="empty">No definitions</div>'}</div>
 <div class="actions"><button class="action primary" data-prompt="使用工作流「${esc(w.name||w.workflow_id)}」（${esc(w.workflow_id)}）执行：">新目标</button>
 <button class="action" data-prompt="按照下面的要求修改工作流「${esc(w.name||w.workflow_id)}」（${esc(w.workflow_id)}）：">修改</button>
 <button id="openDeleteWorkflowDialog" class="action danger" type="button">删除</button></div>
 <dialog id="deleteWorkflowDialog" class="confirmDialog" aria-labelledby="deleteWorkflowTitle"><div class="confirmBody"><h2 id="deleteWorkflowTitle" class="confirmTitle">确认删除工作流？</h2><p class="confirmText">${esc(w.name||w.workflow_id)}<br>${esc(w.workflow_id)}</p></div><div class="confirmActions"><button id="cancelDeleteWorkflow" class="action" type="button">取消</button><button id="confirmDeleteWorkflow" class="action danger" type="button">确认删除</button></div></dialog>`;document.getElementById('workflowBack').onclick=showList;bind();bindTabs();bindDefinitionItems();bindDeleteConfirmation(w);mountGraph(w.graph)}
async function showList(){try{const data=await callTool('list_workflows',{});drawList(Array.isArray(data.workflows)?data.workflows:[])}catch(e){card.innerHTML=`<div class="error">${esc(e.message)}</div>`}}
async function openDetail(workflowId){try{current=await callTool('get_workflow_definition',{workflow_id:workflowId});drawDetail(current)}catch(e){card.innerHTML=`<div class="error">${esc(e.message)}</div>`}}
async function refresh(){if(current?.workflow_id)await openDetail(current.workflow_id);else await showList()}
document.getElementById('refresh').onclick=refresh;onHostContext(()=>mountGraph(current?.graph));onToolResult(value=>{if(Array.isArray(value?.workflows))drawList(value.workflows);else if(value?.workflow_id){current=value;drawDetail(current)}});refresh();
""", extra_style=_WORKFLOW_LIST_STYLE + _WORKFLOW_DETAIL_STYLE, extra_script=_XYFLOW_SCRIPT)

ORBIT_AUTHORING_HTML = _card("Orbit · Workflow generation", r"""
const card=document.getElementById('card');let job=initial(),timer=null;
const terminal=new Set(['done','failed','cancelled']);
function css(s){return s==='running'||s==='queued'?'live':s==='done'?'good':s==='failed'||s==='cancelled'?'bad':''}
function draw(j){const result=j.result||{};card.innerHTML=`<div class="summary"><div class="statusLine"><span class="dot ${css(j.status)}"></span><span>${esc(j.status||'Preparing')}</span></div>
 <div class="goal">${esc(j.prompt||'Workflow generation')}</div><div class="meta">${esc(j.job_id||'')}</div></div>
 <div class="steps"><div class="step"><span class="dot ${j.status==='queued'?'live':'good'}"></span><span>Prepare request</span><span></span></div>
 <div class="step"><span class="dot ${j.status==='running'?'live':j.status==='queued'?'':'good'}"></span><span>Generate and validate</span><span>${esc(j.attempts||'')}</span></div>
 <div class="step"><span class="dot ${j.status==='done'?'good':j.status==='failed'?'bad':''}"></span><span>Publish workflow</span><span></span></div></div>
 ${result.workflow_id?`<div class="result"><strong>${esc(result.name||'Generated')}</strong>\n${esc(result.workflow_id)}</div>`:''}
 ${j.error?`<div class="result">${esc(j.error.message||j.error.code)}</div>`:''}`}
async function refresh(){try{if(!job?.job_id){const data=await callTool('list_authoring_jobs',{limit:1});job=data.jobs?.[0]||{}}
 else job=await callTool('get_authoring_job',{job_id:job.job_id});draw(job);clearTimeout(timer);if(!terminal.has(job.status))timer=setTimeout(refresh,2000)}
 catch(e){card.innerHTML=`<div class="error">${esc(e.message)}</div>`}}
document.getElementById('refresh').onclick=refresh;onToolResult(value=>{if(value?.job_id){job=value;refresh()}});refresh();
""")

_RUN_STYLE = r"""
:root { --goal-run-card-max-height: 600px; }
#card.goalRun { height: auto; max-height: var(--goal-run-card-max-height);
  overflow-y: auto; overscroll-behavior: contain; }
.goal { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3;
  overflow: hidden; }
"""

ORBIT_RUN_HTML = _card("Orbit · Goal execution", r"""
const card=document.getElementById('card');card.className='card goalRun';let run=initial(),timer=null,firstPaint=true;const terminal=new Set(['completed','failed','cancelled','unknown']);
function css(s){return s==='running'||s==='queued'?'live':s==='waiting'?'warn':s==='completed'||s==='succeeded'||s==='answered'?'good':s==='failed'||s==='cancelled'?'bad':''}
function failureMessage(value){const error=value?.error;return typeof error==='string'?error:error?.message||error?.code||''}
async function resultText(r){const id=r.result?.artifact_id;if(!id)return '';try{const a=await callTool('read_artifact_content',{artifact_id:id,max_bytes:262144});return a.encoding==='base64'?decodeURIComponent(escape(atob(a.content))):a.content||''}catch(_){return ''}}
async function draw(r,steps){
 const rows=steps.map(s=>`<div class="step"><span class="dot ${css(s.status)}"></span><span>${esc(s.label||s.node_id)}</span><span class="meta">${esc(s.status)}</span></div>`).join('');
 const output=terminal.has(r.status)?await resultText(r):'';card.innerHTML=`<div class="summary"><div class="statusLine"><span class="dot ${css(r.status)}"></span><span>${esc(r.status||'Preparing')}</span></div>
 <div class="goal">${esc(r.goal||r.workflow_id||'Goal')}</div><div class="meta">${esc(r.run_id||'')}</div></div>
 ${rows?`<div class="steps">${rows}</div>`:''}${output?`<div class="result"><h2 class="resultTitle">执行结果</h2><div>${esc(output)}</div></div>`:''}`}
async function refresh(){try{const failure=failureMessage(run);if(failure){clearTimeout(timer);card.innerHTML=`<div class="error">${esc(failure)}</div>`;return}if(firstPaint&&run?.run_id&&run.status==='failed'){firstPaint=false;await draw({...run,status:'running'},[]);clearTimeout(timer);timer=setTimeout(refresh,2000);return}firstPaint=false;if(!run?.run_id){const data=await callTool('list_runs',{limit:1});run=data.runs?.[0]||{}}
 else run=await callTool('inspect_run',{run_id:run.run_id});const data=run.run_id?await callTool('get_run_steps',{run_id:run.run_id}):{steps:[]};await draw(run,data.steps||[]);
 clearTimeout(timer);if(run.run_id&&!terminal.has(run.status))timer=setTimeout(refresh,2000)}catch(e){card.innerHTML=`<div class="error">${esc(e.message)}</div>`}}
document.getElementById('refresh').onclick=refresh;onToolResult(value=>{if(value?.run_id||failureMessage(value)){run=value;firstPaint=true;refresh()}});refresh();
""", extra_style=_RUN_STYLE)

_GOALS_STYLE = r"""
.goalRow { width: 100%; min-height: 76px; padding: 12px 14px; border: 0;
  border-bottom: 1px solid var(--line); color: inherit; background: transparent;
  text-align: left; cursor: pointer; }
.goalRow:last-child { border-bottom: 0; }
.goalRow:hover { background: var(--hover); }
.goalTop { display: flex; align-items: center; gap: 8px; }
.goalTitle { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-weight: 650; }
.goalStatus { flex: 0 0 auto; color: var(--muted); font-size: 12px; }
.goalMeta { margin-top: 5px; color: var(--faint); font-size: 11px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
"""

ORBIT_GOALS_HTML = _card("Orbit · Goals", r"""
const card=document.getElementById('card');let timer=null;
const live=new Set(['running','queued','waiting','interrupted']);
function css(s){return s==='running'||s==='queued'?'live':s==='waiting'||s==='interrupted'?'warn':s==='completed'?'good':s==='failed'||s==='cancelled'||s==='unknown'?'bad':''}
function draw(rows){card.innerHTML=rows.length?rows.map(r=>`<button class="goalRow" type="button" data-run-id="${esc(r.run_id)}"><span class="goalTop"><span class="dot ${css(r.status)}"></span><span class="goalTitle">${esc(r.goal||r.workflow_id||'Goal')}</span><span class="goalStatus">${esc(r.status||'')}</span></span><span class="goalMeta">${esc(r.workflow_id||'')} · ${esc(r.updated_at||'')}</span></button>`).join(''):'<div class="empty">No goals yet</div>';
 card.querySelectorAll('[data-run-id]').forEach(button=>button.onclick=()=>send(`查看 Orbit 目标运行 ${button.dataset.runId}，使用目标执行卡片展示详情。`))}
async function refresh(){try{const data=await callTool('list_runs',{limit:100});const rows=Array.isArray(data.runs)?data.runs:[];draw(rows);clearTimeout(timer);timer=setTimeout(refresh,rows.some(r=>live.has(r.status))?2000:15000)}catch(e){card.innerHTML=`<div class="error">${esc(e.message)}</div>`}}
document.getElementById('refresh').onclick=refresh;document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refresh()});onToolResult(value=>{if(Array.isArray(value?.runs))draw(value.runs)});refresh();
""", extra_style=_GOALS_STYLE)

ORBIT_MCP_APP_RESOURCES = (
    {"uri": ORBIT_DASHBOARD_URI, "name": "Orbit dashboard", "description": "Current Orbit task, step progress, and attention state.", "html": ORBIT_DASHBOARD_HTML, "prefers_border": False},
    {"uri": ORBIT_WORKFLOWS_URI, "name": "Orbit workflows", "description": "Published workflow list.", "html": ORBIT_WORKFLOWS_HTML, "prefers_border": False},
    {"uri": ORBIT_AUTHORING_URI, "name": "Orbit workflow generation", "description": "Workflow generation progress and result.", "html": ORBIT_AUTHORING_HTML, "prefers_border": False},
    {"uri": ORBIT_RUN_URI, "name": "Orbit goal execution", "description": "Goal execution progress and result.", "html": ORBIT_RUN_HTML, "prefers_border": False},
    {"uri": ORBIT_GOALS_URI, "name": "Orbit goals", "description": "Recent goal runs and their current status.", "html": ORBIT_GOALS_HTML, "prefers_border": False},
)
