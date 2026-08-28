"""Self-contained MCP App shown by Codex beside the conversation."""

ORBIT_DASHBOARD_URI = "ui://orbit/workflows.html"
ORBIT_DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"

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
  </style>
</head>
<body>
<main>
  <header><h1>Orbit Workflows</h1><button id="refresh" type="button">刷新</button></header>
  <div id="status">正在连接 Orbit…</div>
  <section id="items"></section>
</main>
<script>
  const items = document.getElementById('items');
  const status = document.getElementById('status');
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

  function render(output) {
    const workflows = output?.workflows || output?.structuredContent?.workflows || [];
    status.textContent = `${workflows.length} 个已发布工作流`;
    items.innerHTML = workflows.length ? workflows.map(workflow => `
      <article title="${escapeHtml(workflow.workflow_id)}">
        <div class="row"><h2>${escapeHtml(workflow.name || workflow.workflow_id)}</h2>
          <span class="dot ${workflow.goal_readiness === 'ready' ? 'ready' : ''}"></span></div>
        ${workflow.description ? `<p>${escapeHtml(workflow.description)}</p>` : ''}
        <div class="meta">${Number(workflow.node_count || 0)} 个步骤 · ${escapeHtml(workflow.goal_readiness || 'unknown')}</div>
      </article>`).join('') : '<div class="empty">还没有已发布的工作流</div>';
  }

  async function refresh() {
    status.textContent = '正在刷新…';
    try {
      const result = await window.openai.callTool('list_workflows', {});
      render(result?.structuredContent || result);
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
