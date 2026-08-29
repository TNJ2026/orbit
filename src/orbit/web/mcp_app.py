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
<html lang="zh-CN">
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
    footer { margin-top: 14px; padding-top: 10px; font-size: 12px; line-height: 1.5;
      border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent);
      color: color-mix(in srgb, CanvasText 55%, transparent); }
  </style>
</head>
<body>
<main>
  <header><h1>Orbit Workflows</h1><button id="refresh" type="button">刷新</button></header>
  <div id="status">正在连接 Orbit…</div>
  <section id="items"></section>
  <footer>启动目标、生成或修改工作流,请在对话中交给 Agent。此面板只读。</footer>
</main>
<script>
  const items = document.getElementById('items');
  const status = document.getElementById('status');
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

  // The two surfaces answer in different shapes: the tool returns the list at
  // the top level, the HTTP projection wraps it in `data`. Neither is wrong,
  // so the page reads both rather than either caller reshaping for it.
  function workflowsIn(payload) {
    return payload?.workflows
      || payload?.structuredContent?.workflows
      || payload?.data?.workflows
      || [];
  }

  // `node_count` is lifted out of `summary` by the tool and left in place by
  // the HTTP projection.
  const stepCount = workflow =>
    Number(workflow.node_count ?? workflow.summary?.node_count ?? 0);

  function render(payload) {
    const workflows = workflowsIn(payload);
    status.textContent = `${workflows.length} 个已发布工作流`;
    items.innerHTML = workflows.length ? workflows.map(workflow => `
      <article title="${escapeHtml(workflow.workflow_id)}">
        <div class="row"><h2>${escapeHtml(workflow.name || workflow.workflow_id)}</h2>
          <span class="dot ${workflow.goal_readiness === 'ready' ? 'ready' : ''}"></span></div>
        ${workflow.description ? `<p>${escapeHtml(workflow.description)}</p>` : ''}
        <div class="meta">${stepCount(workflow)} 个步骤 · ${escapeHtml(workflow.goal_readiness || 'unknown')}</div>
      </article>`).join('') : '<div class="empty">还没有已发布的工作流</div>';
  }

  // The host bridge when there is one, and this page's own origin when there
  // is not. Read either way: the panel has nothing to send.
  async function load() {
    if (window.openai?.callTool) {
      const result = await window.openai.callTool('list_workflows', {});
      return result?.structuredContent || result;
    }
    const response = await fetch('/api/v1/workflows', {
      headers: { accept: 'application/json' },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  }

  async function refresh() {
    status.textContent = '正在刷新…';
    try {
      render(await load());
    } catch (error) {
      status.textContent = `刷新失败：${error?.message || error}`;
    }
  }

  document.getElementById('refresh').addEventListener('click', refresh);
  window.addEventListener('openai:set_globals', event => {
    const globals = event.detail?.globals || event.detail || {};
    if (globals.toolOutput) render(globals.toolOutput);
  });
  if (window.openai?.toolOutput) render(window.openai.toolOutput);
  else refresh();
</script>
</body>
</html>"""
