import { createElement, useEffect, useRef, useState } from 'react';
export const orbitRunDefinition = {
    kind: 'orbit-run', target: 'chat',
    match: event => event.type === 'orbit/run-started'
        ? { id: String(event.data.runId), role: 'start' }
        : event.type === 'orbit/run-checkpoint' || event.type === 'orbit/run-ended'
            ? { id: String(event.data.runId), role: 'update' } : null,
    start: (_reader, match) => {
        if (match.event.type !== 'orbit/run-started')
            throw new Error('orbit-run requires run-started');
        const data = match.event.data;
        return { runId: data.runId, workspaceId: data.workspaceId, goal: data.goal, status: data.status, artifactCount: 0, revision: data.revision, terminal: false, sourcePosition: data.sourcePosition };
    },
    update: (context, match) => {
        if (match.event.type !== 'orbit/run-checkpoint' && match.event.type !== 'orbit/run-ended')
            return context.state;
        const data = match.event.data;
        if (data.sourcePosition <= context.state.sourcePosition)
            return context.state;
        return { ...context.state, status: data.status, artifactCount: data.artifactCount, revision: data.revision, terminal: match.event.type === 'orbit/run-ended', sourcePosition: data.sourcePosition };
    },
    publication: match => match.event.type === 'orbit/run-checkpoint' ? 'animation-frame' : 'immediate',
    buildViewNode: context => context.state === undefined ? null : ({
        key: context.key, kind: 'orbit-run', id: context.id, target: 'chat',
        anchorSeq: context.start?.event.seq ?? context.matches[0]?.event.seq ?? 0,
        location: context.start?.location ?? { kind: 'unresolved' }, visibility: 'visible',
        data: { runId: context.state.runId, workspaceId: context.state.workspaceId, goal: context.state.goal, status: context.state.status, artifactCount: context.state.artifactCount, revision: context.state.revision, terminal: context.state.terminal },
    }),
};
function drawerStyle() { return { position: 'fixed', inset: '0 0 0 auto', width: 'min(720px, 92vw)', zIndex: 1000, overflow: 'auto', background: 'var(--background, #fff)', color: 'inherit', borderLeft: '1px solid #ddd', padding: 20, boxShadow: '-12px 0 30px #0002' }; }
function mergeOutput(previous, next) {
    const chunks = new Map(previous.chunks.map(chunk => [chunk.chunk_id, chunk]));
    for (const chunk of next.chunks)
        chunks.set(chunk.chunk_id, chunk);
    return { chunks: [...chunks.values()].sort((a, b) => a.chunk_id - b.chunk_id), after: Math.max(previous.after, next.after), has_more: next.has_more };
}
function StepOutput({ remote, workspace, sessionId, runId, nodeId, active }) {
    const [expanded, setExpanded] = useState(false);
    const [page, setPage] = useState({ chunks: [], after: 0, has_more: false });
    const [error, setError] = useState('');
    const load = () => {
        const controller = new AbortController();
        remote.orbit.readOutput(workspace, sessionId, runId, page.after, nodeId, controller.signal)
            .then(next => setPage(current => mergeOutput(current, next)))
            .catch(reason => { if (!controller.signal.aborted)
            setError(String(reason)); });
        return controller;
    };
    useEffect(() => {
        if (!expanded)
            return;
        const controller = load();
        return () => controller.abort();
    }, [expanded, nodeId, runId]);
    useEffect(() => {
        if (!expanded || !active)
            return;
        const timer = window.setInterval(load, 1000);
        return () => window.clearInterval(timer);
    }, [expanded, active, page.after, nodeId, runId]);
    return createElement('div', null, createElement('button', { type: 'button', 'aria-expanded': expanded, onClick: () => setExpanded(value => !value) }, expanded ? '隐藏原始输出' : '查看原始输出'), expanded ? createElement('div', null, error ? createElement('p', { role: 'alert' }, error) : null, createElement('pre', null, page.chunks.map(chunk => chunk.text).join('')), page.has_more ? createElement('button', { type: 'button', onClick: load }, '加载更多') : null) : null);
}
function decodeBase64(value) {
    const binary = atob(value);
    return Uint8Array.from(binary, character => character.charCodeAt(0));
}
function ArtifactItem({ remote, workspace, sessionId, artifact }) {
    const [content, setContent] = useState(null);
    const [error, setError] = useState('');
    const contentType = String(artifact.content_type || 'application/octet-stream');
    const dataUrl = content ? `data:${contentType};base64,${content.content}` : '';
    return createElement('section', { style: { borderTop: '1px solid #ddd', padding: '8px 0' } }, createElement('strong', null, String(artifact.filename || artifact.artifact_id)), createElement('span', null, ` · ${contentType} · ${String(artifact.size_bytes || 0)} bytes`), !content ? createElement('button', { type: 'button', onClick: () => {
            const controller = new AbortController();
            remote.orbit.getArtifactContent(workspace, sessionId, artifact.artifact_id, controller.signal).then(setContent).catch(reason => setError(String(reason)));
        } }, '预览') : null, error ? createElement('p', { role: 'alert' }, error) : null, content && contentType.startsWith('image/') ? createElement('img', { src: dataUrl, alt: String(artifact.filename || artifact.artifact_id), style: { maxWidth: '100%' } }) : null, content && (contentType.startsWith('text/') || contentType.includes('json')) ? createElement('pre', null, new TextDecoder().decode(decodeBase64(content.content))) : null, content ? createElement('a', { href: dataUrl, download: String(artifact.filename || 'artifact') }, '下载') : null);
}
function OrbitRunCard({ node, cwd, sessionId }, remote) {
    const restoreKey = `orbit:drawer:${String(sessionId)}`;
    const [open, setOpen] = useState(() => typeof sessionStorage !== 'undefined' && sessionStorage.getItem(restoreKey) === node.data.runId);
    const [detail, setDetail] = useState(null);
    const [error, setError] = useState('');
    const [answer, setAnswer] = useState('');
    const [reconciliationNote, setReconciliationNote] = useState('');
    const [copiedDelegation, setCopiedDelegation] = useState('');
    const triggerRef = useRef(null);
    const drawerRef = useRef(null);
    const closeRef = useRef(null);
    const workspace = { id: node.data.workspaceId, canonicalPath: cwd || '' };
    const close = () => {
        setOpen(false);
        sessionStorage.removeItem(restoreKey);
        requestAnimationFrame(() => triggerRef.current?.focus());
    };
    const openDrawer = () => { sessionStorage.setItem(restoreKey, node.data.runId); setOpen(true); };
    useEffect(() => {
        if (!open)
            return;
        closeRef.current?.focus();
        const escape = (event) => { if (event.key === 'Escape')
            close(); };
        document.addEventListener('keydown', escape);
        return () => document.removeEventListener('keydown', escape);
    }, [open, restoreKey]);
    useEffect(() => {
        if (!open || !cwd)
            return;
        const controller = new AbortController();
        Promise.all([
            remote.orbit.getRun(workspace, String(sessionId), node.data.runId, controller.signal),
            remote.orbit.getSteps(workspace, String(sessionId), node.data.runId, controller.signal),
            remote.orbit.getGraph(workspace, String(sessionId), node.data.runId, controller.signal),
            remote.orbit.getEdges(workspace, String(sessionId), node.data.runId, controller.signal),
            remote.orbit.listArtifacts(workspace, String(sessionId), node.data.runId, controller.signal),
        ]).then(([run, steps, graph, edges, artifacts]) => setDetail({ run, steps, graph, edges, artifacts })).catch(reason => { if (!controller.signal.aborted)
            setError(String(reason)); });
        return () => controller.abort();
    }, [open, cwd, sessionId, node.data.runId, node.data.revision]);
    return createElement('section', { 'data-orbit-run-id': node.data.runId }, createElement('strong', null, node.data.goal || node.data.runId), createElement('span', null, ` ${node.data.status}`), node.data.artifactCount ? createElement('span', null, ` · ${String(node.data.artifactCount)} artifacts`) : null, createElement('button', { ref: triggerRef, type: 'button', onClick: openDrawer, disabled: !cwd }, '查看详情'), open ? createElement('aside', { ref: drawerRef, role: 'dialog', 'aria-modal': true, 'aria-label': 'Orbit Run 详情', style: drawerStyle(), onKeyDown: (event) => {
            if (event.key !== 'Tab' || !drawerRef.current)
                return;
            const focusable = [...drawerRef.current.querySelectorAll('button:not([disabled]),a[href],textarea,input,select,[tabindex]:not([tabindex="-1"])')];
            if (!focusable.length)
                return;
            const first = focusable[0], last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            }
            else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        } }, createElement('button', { ref: closeRef, type: 'button', onClick: close, style: { float: 'right' } }, '关闭'), createElement('h2', null, node.data.goal || node.data.runId), error ? createElement('p', { role: 'alert' }, error) : null, !detail ? createElement('p', null, '加载中…') : createElement('div', null, createElement('p', null, `${detail.run.status} · revision ${String(detail.run.revision)}`), detail.run.allowed_commands.some(command => command.command === 'langgraph_run.resume')
        ? createElement('form', { onSubmit: (event) => {
                event.preventDefault();
                const command = detail.run.allowed_commands.find(item => item.command === 'langgraph_run.resume');
                if (!command)
                    return;
                const controller = new AbortController();
                remote.orbit.executeCommand({ workspace, sessionId: String(sessionId), runId: node.data.runId, command: 'langgraph_run.resume', expectedVersion: command.expected_version, idempotencyKey: crypto.randomUUID(), value: answer }, controller.signal)
                    .then(run => { setDetail({ ...detail, run }); setAnswer(''); })
                    .catch(reason => setError(String(reason)));
            } }, createElement('label', null, '需要人工输入', createElement('textarea', { value: answer, onChange: (event) => setAnswer(event.currentTarget.value) })), createElement('button', { type: 'submit' }, '继续运行')) : null, createElement('h3', null, '步骤'), ...detail.steps.map(step => createElement('section', { key: step.node_id, style: { borderTop: '1px solid #ddd', padding: '10px 0' } }, createElement('strong', null, String(step.label || step.node_id)), createElement('span', null, ` · ${step.status}`), step.resolution?.kind === 'reconciliation_required'
        ? createElement('div', { role: 'status', style: { borderLeft: '4px solid #d97706', paddingLeft: 8 } }, createElement('p', null, '需要人工核对外部 Agent 结果；Orbit 不会自动重试'), step.resolution.delegation_id ? createElement('div', null, createElement('code', null, step.resolution.delegation_id), createElement('button', { type: 'button', onClick: () => {
                void navigator.clipboard.writeText(step.resolution.delegation_id).then(() => setCopiedDelegation(step.resolution.delegation_id)).catch(reason => setError(String(reason)));
            } }, copiedDelegation === step.resolution.delegation_id ? '已复制' : '复制 ID'), createElement('button', { type: 'button', onClick: () => {
                const controller = new AbortController();
                remote.orbit.getSteps(workspace, String(sessionId), node.data.runId, controller.signal)
                    .then(steps => setDetail({ ...detail, steps }))
                    .catch(reason => setError(String(reason)));
            } }, '刷新核对状态')) : null) : null, step.reconciliation
        ? createElement('p', null, `人工判定：${step.reconciliation.outcome === 'confirmed_succeeded' ? '确认成功' : '确认失败'}${step.reconciliation.note ? ` · ${step.reconciliation.note}` : ''}`)
        : step.resolution?.delegation_id ? createElement('div', null, createElement('label', null, '核对说明', createElement('input', { value: reconciliationNote, onChange: (event) => setReconciliationNote(event.currentTarget.value) })), ...['confirmed_succeeded', 'confirmed_failed'].map(outcome => createElement('button', {
            key: outcome, type: 'button', onClick: () => {
                const controller = new AbortController();
                remote.orbit.reconcileDelegation(workspace, String(sessionId), node.data.runId, step.resolution.delegation_id, outcome, reconciliationNote, controller.signal)
                    .then(steps => { setDetail({ ...detail, steps }); setReconciliationNote(''); })
                    .catch(reason => setError(String(reason)));
            },
        }, outcome === 'confirmed_succeeded' ? '确认外部执行成功' : '确认外部执行失败'))) : null, step.prompt ? createElement('pre', null, String(step.prompt)) : null, createElement(StepOutput, { remote, workspace, sessionId: String(sessionId), runId: node.data.runId, nodeId: step.node_id, active: !node.data.terminal }))), createElement('h3', null, `执行图 (${String(Object.keys(detail.graph).length)} fields / ${String(detail.edges.length)} edges)`), createElement('pre', null, JSON.stringify(detail.graph, null, 2)), createElement('h3', null, `产物 (${String(detail.artifacts.length)})`), ...detail.artifacts.map(item => createElement(ArtifactItem, { key: item.artifact_id, remote, workspace, sessionId: String(sessionId), artifact: item })))) : null);
}
function OrbitSettings({ useWorkspaces }, remote) {
    const workspace = useWorkspaces(state => state.items.find(item => item.workspaceId === state.recentWorkspaceId) || state.items[0]);
    const [status, setStatus] = useState('未连接');
    const [runtime, setRuntime] = useState(null);
    const [refresh, setRefresh] = useState(0);
    const startCommand = 'orbit serve --project-root . --mcp-tool-profile harness';
    useEffect(() => {
        if (!workspace) {
            setRuntime(null);
            setStatus('未选择 Workspace');
            return;
        }
        const controller = new AbortController();
        setRuntime(null);
        setStatus('连接中…');
        remote.orbit.getRuntime({ id: String(workspace.workspaceId), canonicalPath: workspace.path }, controller.signal)
            .then(value => { setRuntime(value); setStatus(value.state === 'ready' ? '已连接' : '已停止'); })
            .catch(reason => { if (!controller.signal.aborted)
            setStatus(`连接失败：${String(reason)}`); });
        return () => controller.abort();
    }, [workspace?.workspaceId, workspace?.path, refresh]);
    const capabilities = runtime?.capabilities || {};
    return createElement('section', null, createElement('strong', null, 'Orbit Runtime'), createElement('p', null, `${workspace?.title || 'Workspace'} · ${status}`), runtime ? createElement('dl', null, createElement('dt', null, 'Orbit 版本'), createElement('dd', null, String(capabilities.orbit_version || '未知')), createElement('dt', null, '集成协议'), createElement('dd', null, String(capabilities.integration_protocol || '未知')), createElement('dt', null, 'MCP 协议'), createElement('dd', null, String(capabilities.mcp_protocol || '未知')), createElement('dt', null, '工具 Profile'), createElement('dd', null, String(capabilities.tool_profile || '未知'))) : null, createElement('div', null, createElement('button', { type: 'button', onClick: () => setRefresh(value => value + 1) }, '刷新连接'), createElement('button', { type: 'button', onClick: () => { void navigator.clipboard?.writeText(startCommand); } }, '复制启动命令')), !runtime && workspace ? createElement('code', null, startCommand) : null, createElement('small', null, '连接独立 Orbit Runtime，并使用当前 Harness Session 隔离 MCP 运行身份。'));
}
export const inject = ['conversationEvents', 'slots'];
export function apply(ctx) {
    ctx.conversationEvents.register(orbitRunDefinition);
    const remote = ctx.remote;
    ctx.slots.inject('conversation.chat.node', () => ctx.slots.register({ name: 'conversation.chat.node', key: 'orbit-run' }, (props) => OrbitRunCard(props, remote)));
    ctx.slots.inject('settings.general.item', () => ctx.slots.register({ name: 'settings.general.item', key: 'orbit-runtime', order: 80 }, (props) => OrbitSettings(props, remote)));
}
