"""The read-only Orbit panel shown beside a conversation.

One page, reached two ways. A host that implements MCP Apps mounts it from the
resource below and feeds it through the host bridge; a host that does not can
open the same page over HTTP at `/panel`, where it reads `/api/v1/workflows`
itself. Both surfaces have to stay the same page, because what makes this a
panel rather than the UI is what it leaves out.

What it leaves out is every way to change something. There is no goal box, no
Generate field, no delete: starting a run and writing a workflow are asked of
the Agent in the conversation, which holds the skill that knows how. The full
browser UI at `/ui` remains the place for direct operation.
"""

ORBIT_DASHBOARD_URI = "ui://orbit/workflows.html"
ORBIT_DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"
ORBIT_PANEL_PATH = "/panel"

ORBIT_DASHBOARD_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root { color-scheme: light dark; font: 14px/1.45 ui-sans-serif, system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; color: CanvasText; background: transparent; }
    main { padding: 14px; overflow-x: hidden; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    h1 { margin: 0; font-size: 16px; font-weight: 650; }
    button { border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); border-radius: 999px;
      padding: 5px 10px; color: inherit; background: color-mix(in srgb, Canvas 90%, CanvasText 10%); cursor: pointer; }
    #status { margin: 10px 0 0; color: color-mix(in srgb, CanvasText 65%, transparent); font-size: 12px; }
    #items { display: grid; gap: 8px; margin-top: 12px; }
    article { min-width: 0; padding: 11px 12px; border: 1px solid color-mix(in srgb, CanvasText 14%, transparent);
      border-radius: 12px; background: color-mix(in srgb, Canvas 96%, CanvasText 4%); }
    .row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    h2 { min-width: 0; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
    p { margin: 5px 0 0; color: color-mix(in srgb, CanvasText 70%, transparent); overflow-wrap: anywhere; }
    .meta { margin-top: 8px; font-size: 12px; }
    .dot { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 50%; background: #8a8a8a; }
    .dot.ready { background: #20a464; }
    .empty { padding: 24px 8px; text-align: center; color: color-mix(in srgb, CanvasText 60%, transparent); }
    #activity { margin-top: 12px; padding: 11px 12px; border-radius: 12px;
      border: 1px solid color-mix(in srgb, CanvasText 16%, transparent);
      background: color-mix(in srgb, Canvas 92%, CanvasText 8%); }
    .label { font-size: 12px; color: color-mix(in srgb, CanvasText 60%, transparent); }
    .goal { margin: 4px 0 0; font-size: 13px; overflow-wrap: anywhere;
      display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .pill { flex: 0 0 auto; padding: 2px 8px; border-radius: 999px; font-size: 11px;
      background: color-mix(in srgb, CanvasText 12%, transparent); }
    .pill.live { background: color-mix(in srgb, #20a464 26%, transparent); }
    .pill.attention { background: color-mix(in srgb, #d08b1c 30%, transparent); }
    .pill.bad { background: color-mix(in srgb, #d0453b 26%, transparent); }
    .track { height: 3px; margin-top: 9px; border-radius: 999px; overflow: hidden;
      background: color-mix(in srgb, CanvasText 12%, transparent); }
    .track span { display: block; height: 100%; background: #20a464; }
    footer { margin-top: 14px; padding-top: 10px; font-size: 12px; line-height: 1.5;
      border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
      color: color-mix(in srgb, CanvasText 55%, transparent); }
  </style>
</head>
<body>
<main>
  <header><h1 id="title"></h1><button id="refresh" type="button"></button></header>
  <section id="activity" hidden></section>
  <div id="status"></div>
  <section id="items"></section>
  <footer id="note"></footer>
</main>
<script>
  const items = document.getElementById('items');
  const status = document.getElementById('status');
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

  // -- what it says -------------------------------------------------------
  // The full UI keeps its catalogs beside it as assets and has a test that
  // refuses monolingual text. This page cannot share them: it ships as one
  // self-contained document, so it carries its own. Same rule, smaller table.
  //
  // `readiness` covers the states the projection actually produces and falls
  // back to the raw token, because a state nobody translated should read as
  // an untranslated state rather than silently as an ordinary one.
  const STRINGS = {
    'en-US': {
      title: 'Orbit Workflows', refresh: 'Refresh',
      connecting: 'Connecting to Orbit…', refreshing: 'Refreshing…',
      published: count => `${count} published workflow${count === 1 ? '' : 's'}`,
      steps: count => `${count} step${count === 1 ? '' : 's'}`,
      empty: 'No published workflows yet',
      failed: detail => `Refresh failed: ${detail}`,
      noAnswer: method => `the host did not answer ${method}`,
      hostError: 'the host returned an error',
      readiness: {
        ready: 'ready', needs_upgrade: 'upgrade needed',
        needs_migration: 'cannot upgrade',
      },
      currentGoal: 'Current goal', lastGoal: 'Last goal',
      progress: (done, total) => `step ${done} of ${total}`,
      runStatus: {
        running: 'running', waiting: 'waiting', interrupted: 'needs you',
        completed: 'completed', failed: 'failed', cancelled: 'cancelled',
        unknown: 'outcome unknown',
      },
      note: 'Starting a goal and writing or changing a workflow are asked of '
        + 'the Agent in the conversation. This panel is read-only.',
    },
    'zh-CN': {
      title: 'Orbit 工作流', refresh: '刷新',
      connecting: '正在连接 Orbit…', refreshing: '正在刷新…',
      published: count => `${count} 个已发布工作流`,
      steps: count => `${count} 个步骤`,
      empty: '还没有已发布的工作流',
      failed: detail => `刷新失败：${detail}`,
      noAnswer: method => `宿主未响应 ${method}`,
      hostError: '宿主返回错误',
      readiness: {
        ready: '可启动', needs_upgrade: '需要升级',
        needs_migration: '无法升级',
      },
      currentGoal: '当前目标', lastGoal: '上一个目标',
      progress: (done, total) => `第 ${done} 步 / 共 ${total}`,
      runStatus: {
        running: '运行中', waiting: '等待中', interrupted: '待你处理',
        completed: '已完成', failed: '已失败', cancelled: '已取消',
        unknown: '结果未知',
      },
      note: '启动目标、生成或修改工作流，请在对话中交给 Agent。此面板只读。',
    },
  };

  const localeFor = tag => String(tag || '').toLowerCase().startsWith('zh')
    ? 'zh-CN' : 'en-US';
  let locale = localeFor(navigator.language);
  const t = () => STRINGS[locale];

  function applyLocale() {
    document.documentElement.lang = locale;
    document.getElementById('title').textContent = t().title;
    document.getElementById('refresh').textContent = t().refresh;
    document.getElementById('note').textContent = t().note;
  }

  // The host tells the View its theme, and the page is drawn in system colors
  // — so honouring it is one property, and the whole panel follows. Without
  // this the page reads the operating system while the conversation around it
  // reads the host, and the two disagree exactly when a user has overridden
  // one of them.
  function applyHostContext(context) {
    if (!context) return;
    if (context.theme) document.documentElement.style.colorScheme = context.theme;
    if (context.locale && localeFor(context.locale) !== locale) {
      locale = localeFor(context.locale);
      applyLocale();
      // Redraw what is already on screen; the host may change its mind about
      // the language long after the list arrived.
      if (lastPayload !== null) render();
    }
  }

  // The two surfaces answer in different shapes: the tool returns the list at
  // the top level, the HTTP projection wraps it in `data`. Neither is wrong,
  // so the page reads both rather than either caller reshaping for it. One
  // reader, because workflows, runs and steps all arrive this way.
  const listIn = (payload, key) => payload?.[key]
    || payload?.structuredContent?.[key] || payload?.data?.[key] || [];
  const workflowsIn = payload => listIn(payload, 'workflows');

  // `node_count` is lifted out of `summary` by the tool and left in place by
  // the HTTP projection.
  const stepCount = workflow =>
    Number(workflow.node_count ?? workflow.summary?.node_count ?? 0);

  const readinessLabel = workflow => {
    const state = workflow.goal_readiness || 'unknown';
    return t().readiness[state] || state;
  };

  let lastPayload = null;

  function render(payload) {
    if (payload !== undefined) lastPayload = payload;
    const workflows = workflowsIn(lastPayload);
    status.textContent = t().published(workflows.length);
    items.innerHTML = workflows.length ? workflows.map(workflow => `
      <article title="${escapeHtml(workflow.workflow_id)}">
        <div class="row"><h2>${escapeHtml(workflow.name || workflow.workflow_id)}</h2>
          <span class="dot ${workflow.goal_readiness === 'ready' ? 'ready' : ''}"></span></div>
        ${workflow.description ? `<p>${escapeHtml(workflow.description)}</p>` : ''}
        <div class="meta">${escapeHtml(t().steps(stepCount(workflow)))} · ${escapeHtml(readinessLabel(workflow))}</div>
      </article>`).join('') : `<div class="empty">${escapeHtml(t().empty)}</div>`;
  }

  // -- the ways in --------------------------------------------------------
  // Three, and the page has to work on all of them: the OpenAI Apps SDK
  // global that Codex provides, the MCP Apps postMessage bridge (SEP-1865),
  // and plain HTTP when this page is opened at /panel. The ext-apps SDK would
  // supply the middle one, but this file ships as a single self-contained
  // document with no build step and no reachable CDN, so the handshake is
  // written out. Protocol revision 2026-01-26.
  const MCP_UI_PROTOCOL = '2026-01-26';

  function mcpAppsBridge() {
    // A View is always framed. Top-level means /panel, where HTTP is the way.
    if (window.parent === window) return null;
    const pending = new Map();
    let nextId = 0;
    let deliverResult = () => {};
    window.addEventListener('message', event => {
      const message = event.data;
      if (!message || message.jsonrpc !== '2.0') return;
      if (message.id != null && pending.has(message.id)) {
        const settle = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) settle.reject(new Error(message.error.message || t().hostError));
        else settle.resolve(message.result);
      } else if (message.method === 'ui/notifications/tool-result') {
        deliverResult(message.params);
      } else if (message.method === 'ui/notifications/host-context-changed') {
        // A partial context: only what changed, merged onto what is held.
        applyHostContext(message.params);
      }
    });
    const post = message => window.parent.postMessage(message, '*');
    // Every request is timed. Being framed says nothing about who is out
    // there, and a parent that never answers must not leave the panel waiting
    // on it forever — there is another way to read, and it cannot be taken if
    // the first attempt never settles.
    return {
      request: (method, params, timeoutMs) => new Promise((resolve, reject) => {
        const id = ++nextId;
        pending.set(id, { resolve, reject });
        post({ jsonrpc: '2.0', id, method, params });
        setTimeout(() => {
          if (!pending.delete(id)) return;
          reject(new Error(t().noAnswer(method)));
        }, timeoutMs);
      }),
      notify: method => post({ jsonrpc: '2.0', method }),
      onResult: handler => { deliverResult = handler; },
    };
  }

  // Reassigned, not fixed: what looked like a host may turn out not to be one,
  // and the page stops asking it once that is known.
  let hostBridge = mcpAppsBridge();

  // A handshake is instant when there is somebody to shake hands with, so a
  // short deadline separates an MCP Apps host from a plain iframe embed.
  async function connectHost() {
    const result = await hostBridge.request('ui/initialize', {
      capabilities: {},
      clientInfo: { name: 'orbit-panel', version: '1' },
      protocolVersion: MCP_UI_PROTOCOL,
    }, 4000);
    // Theme and language arrive with the handshake, before anything is drawn.
    applyHostContext(result?.hostContext);
    // The host sends nothing before this, tool-input and tool-result included.
    hostBridge.notify('ui/notifications/initialized');
  }

  // One reader for every read the panel makes: same tool name on both
  // bridges, same projection over HTTP. Read-only throughout — these are the
  // only three the panel is allowed to name.
  async function load(tool, args, httpPath) {
    if (window.openai?.callTool) {
      const result = await window.openai.callTool(tool, args);
      return result?.structuredContent || result;
    }
    if (hostBridge) {
      try {
        // Standard MCP, proxied to the originating server by the host.
        return await hostBridge.request('tools/call', {
          name: tool, arguments: args,
        }, 15000);
      } catch (error) {
        // Whatever is out there is not answering as a host. Give up on it for
        // good rather than spending another deadline on the next refresh.
        hostBridge = null;
      }
    }
    const response = await fetch(httpPath, {
      headers: { accept: 'application/json' },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  }

  const loadWorkflows = () => load('list_workflows', {}, '/api/v1/workflows');
  const loadRuns = () => load('list_runs', { limit: 5 }, '/api/v1/langgraph-runs?limit=5');
  const loadSteps = runId => load(
    'get_run_steps', { run_id: runId },
    `/api/v1/langgraph-runs/${encodeURIComponent(runId)}/steps`,
  );

  // -- what Orbit is doing now --------------------------------------------
  // The catalogue is what Orbit *can* do; beside a conversation the more
  // useful question is what it is doing, so the run sits above the list. Only
  // one goal is active at a time, which is why this is a card and not a list.
  const activity = document.getElementById('activity');
  const ACTIVE = new Set(['running', 'waiting', 'interrupted']);
  const PILL = {
    running: 'live', waiting: 'live', interrupted: 'attention',
    failed: 'bad', unknown: 'bad',
  };

  function renderActivity(runs, progress) {
    // The active one if there is one, else the newest, which is how a panel
    // answers "did that goal finish?" after the run has ended.
    const run = runs.find(item => ACTIVE.has(item.status)) || runs[0] || null;
    activity.hidden = run === null;
    if (run === null) return null;
    const live = ACTIVE.has(run.status);
    const status_ = run.status || 'unknown';
    activity.innerHTML = `
      <div class="row">
        <span class="label">${escapeHtml(live ? t().currentGoal : t().lastGoal)}</span>
        <span class="pill ${PILL[status_] || ''}">${escapeHtml(
          t().runStatus[status_] || status_)}</span>
      </div>
      <p class="goal">${escapeHtml(run.goal || run.workflow_id || run.run_id)}</p>
      ${progress ? `
        <div class="meta">${escapeHtml(t().progress(progress.done, progress.total))}${
          progress.label ? ` · ${escapeHtml(progress.label)}` : ''}</div>
        <div class="track"><span style="width:${
          Math.round(100 * progress.done / progress.total)}%"></span></div>` : ''}`;
    return live ? run : null;
  }

  // Steps are a second read, so they are fetched only for a run still going.
  // A finished goal has nothing left to watch, and its card says so already.
  async function progressOf(run) {
    const steps = listIn(await loadSteps(run.run_id), 'steps');
    if (!steps.length) return null;
    const done = steps.filter(step => step.status === 'succeeded').length;
    const current = steps.find(step => step.status === 'running')
      || steps.find(step => step.status !== 'succeeded');
    return { done, total: steps.length, label: current?.label || '' };
  }

  // Polling, because it is the only way that works on all three surfaces: a
  // sandboxed View cannot open the Runtime's WebSocket.
  //
  // Two speeds, and an idle panel keeps polling rather than stopping. The
  // whole point of this card is the goal a person asks the Agent to start
  // *while looking at it*, and a panel that only watches runs already under
  // way when it loaded would never show one — it would sit on the last
  // finished goal forever. So idle still looks, just rarely.
  //
  // Neither speed is free: each poll is a tool call the host may show its
  // user. Nothing polls while the panel is hidden, and nothing polls faster
  // than a person reads.
  const POLL_LIVE_MS = 5000;
  const POLL_IDLE_MS = 20000;
  let poller = null;

  function schedulePoll(live) {
    clearTimeout(poller);
    poller = setTimeout(() => {
      if (document.visibilityState === 'hidden') return schedulePoll(live);
      refreshActivity();
    }, live ? POLL_LIVE_MS : POLL_IDLE_MS);
  }

  async function refreshActivity() {
    try {
      const runs = listIn(await loadRuns(), 'runs');
      const live = renderActivity(runs, null);
      schedulePoll(live !== null);
      if (live === null) return;
      renderActivity(runs, await progressOf(live));
    } catch (error) {
      // Supplementary to the catalogue: leave the last state on screen rather
      // than replacing a good answer with a transient failure, and look again
      // at the idle rate rather than giving up on the panel for good.
      schedulePoll(false);
    }
  }

  async function refresh() {
    status.textContent = t().refreshing;
    try {
      render(await loadWorkflows());
    } catch (error) {
      status.textContent = t().failed(error?.message || error);
    }
    await refreshActivity();
  }

  applyLocale();
  status.textContent = t().connecting;
  document.getElementById('refresh').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refreshActivity();
  });
  window.addEventListener('openai:set_globals', event => {
    const globals = event.detail?.globals || event.detail || {};
    if (globals.toolOutput) render(globals.toolOutput);
  });

  // The catalogue arrives unasked on both bridges: Codex leaves it on the
  // global, an MCP Apps host pushes it as tool-result once the handshake is
  // done, and only HTTP has to go and get it. What Orbit is *doing* is never
  // pushed, so that is a read this page makes for itself on every surface.
  if (window.openai?.toolOutput) {
    render(window.openai.toolOutput);
    refreshActivity();
  } else if (hostBridge) {
    hostBridge.onResult(payload => { render(payload); refreshActivity(); });
    connectHost().catch(() => {
      // Not an MCP Apps host after all. Same origin may still answer, but
      // only once the parent is out of the way.
      hostBridge = null;
      refresh();
    });
  } else {
    refresh();
  }
</script>
</body>
</html>"""
