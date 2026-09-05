"""Read-only landing page for the machine's running workspace Runtimes."""

from html import escape
from urllib.parse import urlsplit


def render_hub_ui(runtimes: list[dict[str, str]]) -> str:
    rows = []
    for runtime in runtimes:
        path = escape(runtime['path'])
        url = escape(runtime['url'], quote=True)
        port = urlsplit(runtime['url']).port
        rows.append(f'<tr><td><code>{path}</code></td><td><span class="status">运行中</span></td>'
                    f'<td><code>{port}</code></td><td><a href="{url}">打开 UI ↗</a></td></tr>')
    content = ('<div class="table"><table><thead><tr><th>Workspace 路径</th><th>状态</th>'
               '<th>UI 端口</th><th>入口</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>') if rows else (
                   '<div class="empty">暂无运行中的 Runtime。<p>启动项目的 Orbit 服务后，刷新此页即可看到入口。</p></div>')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Orbit Hub</title>
<style>
:root{color-scheme:light dark;--bg:#ffffff;--panel:#ffffff;--text:#20242c;--muted:#637083;--line:#e3e7ec;--link:#315fc9;--control-bg:#f1f3f8;--control-raised:#e4e8f0;--control-fg:#171a21}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,sans-serif}
main{max-width:1120px;margin:64px auto;padding:0 24px}header{display:flex;align-items:center;justify-content:space-between;gap:24px}
h1{font-size:30px;margin:0;letter-spacing:-1px}.sub{color:var(--muted);margin:8px 0 30px}a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}a:focus-visible{outline:2px solid var(--link);outline-offset:5px}
.refresh{border:0;padding:8px 12px;border-radius:12px;background:var(--control-bg);color:var(--control-fg);font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}.refresh:hover{background:var(--control-raised);text-decoration:none}
.table{overflow:auto;border:0;border-radius:12px;background:#212121;padding:0 20px}table{border-collapse:collapse;width:100%;text-align:left}th{font-size:12px;color:var(--muted);font-weight:500}th,td{padding:18px 2px;border-bottom:1px solid rgba(255,255,255,.05)}tbody tr:last-child td{border-bottom:0}td:first-child{min-width:250px;overflow-wrap:anywhere}td:not(:first-child){white-space:nowrap}code{font:13px/1.6 ui-monospace,monospace}.status{color:#258353;font-size:13px}.status:before{content:'●';margin-right:7px;font-size:10px}.empty{background:var(--panel);border:0;padding:48px 24px;border-radius:12px;text-align:center}.empty p,footer{color:var(--muted);font-size:13px}footer{margin-top:20px}
@media(prefers-color-scheme:dark){:root{--bg:#181818;--panel:#181818;--text:#edf0f5;--muted:#9aa6b7;--line:#303641;--link:#91b2ff;--control-bg:#2a2a2a;--control-raised:#333333;--control-fg:#e1e2ec}.status{color:#75c99c}}
@media(max-width:600px){main{margin:28px auto;padding:0 16px}.table{padding:0 14px}th,td{padding:14px 2px}h1{font-size:26px}}
</style></head><body><main><header><h1>Orbit Hub</h1><a class="refresh" href="/ui">刷新列表</a></header>
''' + f'<p class="sub">{len(runtimes)} 个运行中的 Runtime · 选择工作区进入 UI</p>' + content + '''
<footer>列表仅检查运行中的服务。端口可能在重启后变化，请收藏此 Hub 页面。</footer>
</main></body></html>'''
