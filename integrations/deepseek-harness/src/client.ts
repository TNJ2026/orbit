import { createElement, useEffect, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent, KeyboardEvent as ReactKeyboardEvent } from 'react'
import type { ClientContext, ConversationNodeDefinition } from '@deepseek-ai/dsh-client-runtime/client'
import type { SessionListState, WorkspaceListState } from '@deepseek-ai/dsh-client-runtime/client'
import type { ChatNodeViewProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { ArtifactContent, ArtifactSummary, AuthoringJob, EdgeSummary, ImportedArtifact, IntegrationDiagnostics, OrbitCommandRequest, OrbitRunCheckpoint, OrbitRunEnded, OrbitRunStarted, OutputPage, RunDto, RunGraph, RuntimeSummary, StepSummary, WorkflowSummary, WorkspaceRef } from './types.js'

type StartedData = Omit<OrbitRunStarted, 'type'>
type CheckpointData = Omit<OrbitRunCheckpoint, 'type'>
type EndedData = Omit<OrbitRunEnded, 'type'>

declare module '@deepseek-ai/dsh-session/types' {
  interface SessionEventMap {
    'orbit/run-started': StartedData
    'orbit/run-checkpoint': CheckpointData
    'orbit/run-ended': EndedData
  }
}

export interface OrbitRunCardData { runId: string; workspaceId: string; goal: string; status: string; artifactCount: number; revision: number; terminal: boolean }
declare module '@deepseek-ai/dsh-client-ui-conversation/client' { interface ChatNodeDataMap { 'orbit-run': OrbitRunCardData } }

interface State extends OrbitRunCardData { sourcePosition: number }

export const orbitRunDefinition: ConversationNodeDefinition<State> = {
  kind: 'orbit-run', target: 'chat',
  match: event => event.type === 'orbit/run-started'
    ? { id: String(event.data.runId), role: 'start' }
    : event.type === 'orbit/run-checkpoint' || event.type === 'orbit/run-ended'
      ? { id: String(event.data.runId), role: 'update' } : null,
  start: (_reader, match) => {
    if (match.event.type !== 'orbit/run-started') throw new Error('orbit-run requires run-started')
    const data = match.event.data
    return { runId: data.runId, workspaceId: data.workspaceId, goal: data.goal, status: data.status, artifactCount: 0, revision: data.revision, terminal: false, sourcePosition: data.sourcePosition }
  },
  update: (context, match) => {
    if (match.event.type !== 'orbit/run-checkpoint' && match.event.type !== 'orbit/run-ended') return context.state
    const data = match.event.data
    if (data.sourcePosition <= context.state.sourcePosition) return context.state
    return { ...context.state, status: data.status, artifactCount: data.artifactCount, revision: data.revision, terminal: match.event.type === 'orbit/run-ended', sourcePosition: data.sourcePosition }
  },
  publication: match => match.event.type === 'orbit/run-checkpoint' ? 'animation-frame' : 'immediate',
  buildViewNode: context => context.state === undefined ? null : ({
    key: context.key, kind: 'orbit-run', id: context.id, target: 'chat',
    anchorSeq: context.start?.event.seq ?? context.matches[0]?.event.seq ?? 0,
    location: context.start?.location ?? { kind: 'unresolved' }, visibility: 'visible',
    data: { runId: context.state.runId, workspaceId: context.state.workspaceId, goal: context.state.goal, status: context.state.status, artifactCount: context.state.artifactCount, revision: context.state.revision, terminal: context.state.terminal },
  }),
}

interface OrbitClient { orbit: {
  getRuntime(workspace: WorkspaceRef, signal: AbortSignal): Promise<RuntimeSummary>
  getDiagnostics(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<IntegrationDiagnostics>
  listWorkflows(workspace: WorkspaceRef, sessionId: string, signal: AbortSignal): Promise<WorkflowSummary[]>
  listRuns(workspace: WorkspaceRef, sessionId: string, status: string | undefined, signal: AbortSignal): Promise<RunDto[]>
  generateWorkflow(workspace: WorkspaceRef, sessionId: string, prompt: string, signal: AbortSignal): Promise<AuthoringJob>
  modifyWorkflow(workspace: WorkspaceRef, sessionId: string, workflowId: string, prompt: string, regenerate: boolean, signal: AbortSignal): Promise<AuthoringJob>
  getAuthoringJob(workspace: WorkspaceRef, sessionId: string, jobId: string, signal: AbortSignal): Promise<AuthoringJob>
  getRun(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunDto>
  getSteps(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<StepSummary[]>
  getGraph(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<RunGraph>
  getEdges(workspace: WorkspaceRef, sessionId: string, runId: string, signal: AbortSignal): Promise<EdgeSummary[]>
  readOutput(workspace: WorkspaceRef, sessionId: string, runId: string, after: number, nodeId: string | undefined, signal: AbortSignal): Promise<OutputPage>
  listArtifacts(workspace: WorkspaceRef, sessionId: string, runId: string | undefined, signal: AbortSignal): Promise<ArtifactSummary[]>
  getArtifactContent(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ArtifactContent>
  importArtifact(workspace: WorkspaceRef, sessionId: string, artifactId: string, signal: AbortSignal): Promise<ImportedArtifact>
  executeCommand(request: OrbitCommandRequest, signal: AbortSignal): Promise<RunDto>
  reconcileDelegation(workspace: WorkspaceRef, sessionId: string, runId: string, delegationId: string, outcome: 'confirmed_succeeded' | 'confirmed_failed', note: string, signal: AbortSignal): Promise<StepSummary[]>
} }

function drawerStyle(): Record<string, string | number> { return { position: 'fixed', inset: '0 0 0 auto', width: 'min(720px, 92vw)', zIndex: 1000, overflow: 'auto', background: 'var(--background, #fff)', color: 'inherit', borderLeft: '1px solid #ddd', padding: 20, boxShadow: '-12px 0 30px #0002' } }

function mergeOutput(previous: OutputPage, next: OutputPage): OutputPage {
  const chunks = new Map(previous.chunks.map(chunk => [chunk.chunk_id, chunk]))
  for (const chunk of next.chunks) chunks.set(chunk.chunk_id, chunk)
  return { chunks: [...chunks.values()].sort((a, b) => a.chunk_id - b.chunk_id), after: Math.max(previous.after, next.after), has_more: next.has_more }
}

function StepOutput({ remote, workspace, sessionId, runId, nodeId, active }: { remote: OrbitClient; workspace: WorkspaceRef; sessionId: string; runId: string; nodeId: string; active: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const [page, setPage] = useState<OutputPage>({ chunks: [], after: 0, has_more: false })
  const [error, setError] = useState('')
  const load = () => {
    const controller = new AbortController()
    remote.orbit.readOutput(workspace, sessionId, runId, page.after, nodeId, controller.signal)
      .then(next => setPage(current => mergeOutput(current, next)))
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason)) })
    return controller
  }
  useEffect(() => {
    if (!expanded) return
    const controller = load()
    return () => controller.abort()
  }, [expanded, nodeId, runId])
  useEffect(() => {
    if (!expanded || !active) return
    const timer = window.setInterval(load, 1000)
    return () => window.clearInterval(timer)
  }, [expanded, active, page.after, nodeId, runId])
  return createElement('div', null,
    createElement('button', { type: 'button', 'aria-expanded': expanded, onClick: () => setExpanded(value => !value) }, expanded ? '隐藏原始输出' : '查看原始输出'),
    expanded ? createElement('div', null,
      error ? createElement('p', { role: 'alert' }, error) : null,
      createElement('pre', null, page.chunks.map(chunk => chunk.text).join('')),
      page.has_more ? createElement('button', { type: 'button', onClick: load }, '加载更多') : null,
    ) : null,
  )
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value)
  return Uint8Array.from(binary, character => character.charCodeAt(0))
}

function ArtifactItem({ remote, workspace, sessionId, artifact }: { remote: OrbitClient; workspace: WorkspaceRef; sessionId: string; artifact: ArtifactSummary }) {
  const [content, setContent] = useState<ArtifactContent | null>(null)
  const [error, setError] = useState('')
  const contentType = String(artifact.content_type || 'application/octet-stream')
  const dataUrl = content ? `data:${contentType};base64,${content.content}` : ''
  return createElement('section', { style: { borderTop: '1px solid #ddd', padding: '8px 0' } },
    createElement('strong', null, String(artifact.filename || artifact.artifact_id)),
    createElement('span', null, ` · ${contentType} · ${String(artifact.size_bytes || 0)} bytes`),
    !content ? createElement('button', { type: 'button', onClick: () => {
      const controller = new AbortController()
      remote.orbit.getArtifactContent(workspace, sessionId, artifact.artifact_id, controller.signal).then(setContent).catch(reason => setError(String(reason)))
    } }, '预览') : null,
    error ? createElement('p', { role: 'alert' }, error) : null,
    content && contentType.startsWith('image/') ? createElement('img', { src: dataUrl, alt: String(artifact.filename || artifact.artifact_id), style: { maxWidth: '100%' } }) : null,
    content && (contentType.startsWith('text/') || contentType.includes('json')) ? createElement('pre', null, new TextDecoder().decode(decodeBase64(content.content))) : null,
    content ? createElement('a', { href: dataUrl, download: String(artifact.filename || 'artifact') }, '下载') : null,
  )
}

function OrbitRunCard({ node, cwd, sessionId }: ChatNodeViewProps<'orbit-run'>, remote: OrbitClient) {
  const restoreKey = `orbit:drawer:${String(sessionId)}`
  const [open, setOpen] = useState(() => typeof sessionStorage !== 'undefined' && sessionStorage.getItem(restoreKey) === node.data.runId)
  const [detail, setDetail] = useState<{ run: RunDto; steps: StepSummary[]; graph: RunGraph; edges: EdgeSummary[]; artifacts: ArtifactSummary[] } | null>(null)
  const [error, setError] = useState('')
  const [answer, setAnswer] = useState('')
  const [reconciliationNote, setReconciliationNote] = useState('')
  const [copiedDelegation, setCopiedDelegation] = useState('')
  const triggerRef = useRef<HTMLButtonElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const workspace = { id: node.data.workspaceId, canonicalPath: cwd || '' }
  const close = () => {
    setOpen(false)
    sessionStorage.removeItem(restoreKey)
    requestAnimationFrame(() => triggerRef.current?.focus())
  }
  const openDrawer = () => { sessionStorage.setItem(restoreKey, node.data.runId); setOpen(true) }
  useEffect(() => {
    if (!open) return
    closeRef.current?.focus()
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [open, restoreKey])
  useEffect(() => {
    if (!open || !cwd) return
    const controller = new AbortController()
    Promise.all([
      remote.orbit.getRun(workspace, String(sessionId), node.data.runId, controller.signal),
      remote.orbit.getSteps(workspace, String(sessionId), node.data.runId, controller.signal),
      remote.orbit.getGraph(workspace, String(sessionId), node.data.runId, controller.signal),
      remote.orbit.getEdges(workspace, String(sessionId), node.data.runId, controller.signal),
      remote.orbit.listArtifacts(workspace, String(sessionId), node.data.runId, controller.signal),
    ]).then(([run, steps, graph, edges, artifacts]) => setDetail({ run, steps, graph, edges, artifacts })).catch(reason => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => controller.abort()
  }, [open, cwd, sessionId, node.data.runId, node.data.revision])
  return createElement('section', { 'data-orbit-run-id': node.data.runId },
    createElement('strong', null, node.data.goal || node.data.runId),
    createElement('span', null, ` ${node.data.status}`),
    node.data.artifactCount ? createElement('span', null, ` · ${String(node.data.artifactCount)} artifacts`) : null,
    createElement('button', { ref: triggerRef, type: 'button', onClick: openDrawer, disabled: !cwd }, '查看详情'),
    open ? createElement('aside', { ref: drawerRef, role: 'dialog', 'aria-modal': true, 'aria-label': 'Orbit Run 详情', style: drawerStyle(), onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => {
      if (event.key !== 'Tab' || !drawerRef.current) return
      const focusable = [...drawerRef.current.querySelectorAll<HTMLElement>('button:not([disabled]),a[href],textarea,input,select,[tabindex]:not([tabindex="-1"])')]
      if (!focusable.length) return
      const first = focusable[0], last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    } },
      createElement('button', { ref: closeRef, type: 'button', onClick: close, style: { float: 'right' } }, '关闭'),
      createElement('h2', null, node.data.goal || node.data.runId),
      error ? createElement('p', { role: 'alert' }, error) : null,
      !detail ? createElement('p', null, '加载中…') : createElement('div', null,
        createElement('p', null, `${detail.run.status} · revision ${String(detail.run.revision)}`),
        detail.run.allowed_commands.some(command => command.command === 'langgraph_run.resume')
          ? createElement('form', { onSubmit: (event: FormEvent<HTMLFormElement>) => {
            event.preventDefault()
            const command = detail.run.allowed_commands.find(item => item.command === 'langgraph_run.resume')
            if (!command) return
            const controller = new AbortController()
            remote.orbit.executeCommand({ workspace, sessionId: String(sessionId), runId: node.data.runId, command: 'langgraph_run.resume', expectedVersion: command.expected_version, idempotencyKey: crypto.randomUUID(), value: answer }, controller.signal)
              .then(run => { setDetail({ ...detail, run }); setAnswer('') })
              .catch(reason => setError(String(reason)))
          } },
          createElement('label', null, '需要人工输入', createElement('textarea', { value: answer, onChange: (event: ChangeEvent<HTMLTextAreaElement>) => setAnswer(event.currentTarget.value) })),
          createElement('button', { type: 'submit' }, '继续运行')) : null,
        createElement('h3', null, '步骤'),
        ...detail.steps.map(step => createElement('section', { key: step.node_id, style: { borderTop: '1px solid #ddd', padding: '10px 0' } },
          createElement('strong', null, String(step.label || step.node_id)), createElement('span', null, ` · ${step.status}`),
          step.resolution?.kind === 'reconciliation_required'
            ? createElement('div', { role: 'status', style: { borderLeft: '4px solid #d97706', paddingLeft: 8 } },
              createElement('p', null, '需要人工核对外部 Agent 结果；Orbit 不会自动重试'),
              step.resolution.delegation_id ? createElement('div', null,
                createElement('code', null, step.resolution.delegation_id),
                createElement('button', { type: 'button', onClick: () => {
                  void navigator.clipboard.writeText(step.resolution!.delegation_id!).then(() => setCopiedDelegation(step.resolution!.delegation_id!)).catch(reason => setError(String(reason)))
                } }, copiedDelegation === step.resolution.delegation_id ? '已复制' : '复制 ID'),
                createElement('button', { type: 'button', onClick: () => {
                  const controller = new AbortController()
                  remote.orbit.getSteps(workspace, String(sessionId), node.data.runId, controller.signal)
                    .then(steps => setDetail({ ...detail, steps }))
                    .catch(reason => setError(String(reason)))
                } }, '刷新核对状态'),
              ) : null,
            ) : null,
          step.reconciliation
            ? createElement('p', null, `人工判定：${step.reconciliation.outcome === 'confirmed_succeeded' ? '确认成功' : '确认失败'}${step.reconciliation.note ? ` · ${step.reconciliation.note}` : ''}`)
            : step.resolution?.delegation_id ? createElement('div', null,
              createElement('label', null, '核对说明', createElement('input', { value: reconciliationNote, onChange: (event: ChangeEvent<HTMLInputElement>) => setReconciliationNote(event.currentTarget.value) })),
              ...(['confirmed_succeeded', 'confirmed_failed'] as const).map(outcome => createElement('button', {
                key: outcome, type: 'button', onClick: () => {
                  const controller = new AbortController()
                  remote.orbit.reconcileDelegation(workspace, String(sessionId), node.data.runId, step.resolution!.delegation_id!, outcome, reconciliationNote, controller.signal)
                    .then(steps => { setDetail({ ...detail, steps }); setReconciliationNote('') })
                    .catch(reason => setError(String(reason)))
                },
              }, outcome === 'confirmed_succeeded' ? '确认外部执行成功' : '确认外部执行失败')),
            ) : null,
          step.prompt ? createElement('pre', null, String(step.prompt)) : null,
          createElement(StepOutput, { remote, workspace, sessionId: String(sessionId), runId: node.data.runId, nodeId: step.node_id, active: !node.data.terminal }),
        )),
        createElement('h3', null, `执行图 (${String(Object.keys(detail.graph).length)} fields / ${String(detail.edges.length)} edges)`),
        createElement('pre', null, JSON.stringify(detail.graph, null, 2)),
        createElement('h3', null, `产物 (${String(detail.artifacts.length)})`),
        ...detail.artifacts.map(item => createElement(ArtifactItem, { key: item.artifact_id, remote, workspace, sessionId: String(sessionId), artifact: item })),
      ),
    ) : null,
  )
}

function OrbitSettings({ useWorkspaces }: { useWorkspaces: <T>(selector: (state: WorkspaceListState) => T) => T }, remote: OrbitClient) {
  const workspace = useWorkspaces(state => state.items.find(item => item.workspaceId === state.recentWorkspaceId) || state.items[0])
  const [status, setStatus] = useState('未连接')
  const [runtime, setRuntime] = useState<RuntimeSummary | null>(null)
  const [refresh, setRefresh] = useState(0)
  const startCommand = 'orbit serve --project-root . --mcp-tool-profile harness'
  useEffect(() => {
    if (!workspace) { setRuntime(null); setStatus('未选择 Workspace'); return }
    const controller = new AbortController()
    setRuntime(null)
    setStatus('连接中…')
    remote.orbit.getRuntime({ id: String(workspace.workspaceId), canonicalPath: workspace.path }, controller.signal)
      .then(value => { setRuntime(value); setStatus(value.state === 'ready' ? '已连接' : '已停止') })
      .catch(reason => { if (!controller.signal.aborted) setStatus(`连接失败：${String(reason)}`) })
    return () => controller.abort()
  }, [workspace?.workspaceId, workspace?.path, refresh])
  const capabilities = runtime?.capabilities || {}
  return createElement('section', null,
    createElement('strong', null, 'Orbit Runtime'),
    createElement('p', null, `${workspace?.title || 'Workspace'} · ${status}`),
    runtime ? createElement('dl', null,
      createElement('dt', null, 'Orbit 版本'), createElement('dd', null, String(capabilities.orbit_version || '未知')),
      createElement('dt', null, '集成协议'), createElement('dd', null, String(capabilities.integration_protocol || '未知')),
      createElement('dt', null, 'MCP 协议'), createElement('dd', null, String(capabilities.mcp_protocol || '未知')),
      createElement('dt', null, '工具 Profile'), createElement('dd', null, String(capabilities.tool_profile || '未知')),
    ) : null,
    createElement('div', null,
      createElement('button', { type: 'button', onClick: () => setRefresh(value => value + 1) }, '刷新连接'),
      createElement('button', { type: 'button', onClick: () => { void navigator.clipboard?.writeText(startCommand) } }, '复制启动命令'),
    ),
    !runtime && workspace ? createElement('code', null, startCommand) : null,
    createElement('small', null, '连接独立 Orbit Runtime，并使用当前 Harness Session 隔离 MCP 运行身份。'),
  )
}

interface OrbitWorkspaceProps {
  close: () => void
  useSessions: <T>(selector: (state: SessionListState) => T) => T
  useWorkspaces: <T>(selector: (state: WorkspaceListState) => T) => T
}

function OrbitWorkspace({ useSessions, useWorkspaces }: OrbitWorkspaceProps, remote: OrbitClient) {
  const sessionId = useSessions(state => state.current)
  const workspaceView = useWorkspaces(state => state.items.find(item => item.workspaceId === state.recentWorkspaceId) || state.items[0])
  const workspace = workspaceView ? { id: String(workspaceView.workspaceId), canonicalPath: workspaceView.path } : null
  const [tab, setTab] = useState<'runs' | 'workflows' | 'artifacts' | 'diagnostics'>('runs')
  const [runs, setRuns] = useState<RunDto[]>([])
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([])
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([])
  const [selectedRun, setSelectedRun] = useState<{ run: RunDto; steps: StepSummary[]; graph: RunGraph; edges: EdgeSummary[]; artifacts: ArtifactSummary[] } | null>(null)
  const [prompt, setPrompt] = useState('')
  const [workflowToModify, setWorkflowToModify] = useState('')
  const [regenerate, setRegenerate] = useState(false)
  const [job, setJob] = useState<AuthoringJob | null>(null)
  const [imported, setImported] = useState<Record<string, ImportedArtifact>>({})
  const [diagnostics, setDiagnostics] = useState<IntegrationDiagnostics | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const historyDrawerRef = useRef<HTMLElement>(null)
  const historyReturnFocus = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!workspace || !sessionId) return
    const controller = new AbortController()
    setLoading(true); setError('')
    Promise.all([
      remote.orbit.listRuns(workspace, String(sessionId), undefined, controller.signal),
      remote.orbit.listWorkflows(workspace, String(sessionId), controller.signal),
      remote.orbit.listArtifacts(workspace, String(sessionId), undefined, controller.signal),
      remote.orbit.getDiagnostics(workspace, String(sessionId), controller.signal),
    ]).then(([nextRuns, nextWorkflows, nextArtifacts, nextDiagnostics]) => {
      setRuns(nextRuns); setWorkflows(nextWorkflows); setArtifacts(nextArtifacts); setDiagnostics(nextDiagnostics)
    }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [workspace?.id, workspace?.canonicalPath, sessionId, refresh])

  useEffect(() => {
    if (!workspace || !sessionId || !job || !['queued', 'running'].includes(job.status)) return
    const controller = new AbortController()
    const timer = window.setInterval(() => {
      remote.orbit.getAuthoringJob(workspace, String(sessionId), job.job_id, controller.signal)
        .then(setJob).catch(reason => { if (!controller.signal.aborted) setError(String(reason)) })
    }, 1000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [workspace?.id, sessionId, job?.job_id, job?.status])

  useEffect(() => {
    if (!selectedRun) return
    historyReturnFocus.current = document.activeElement as HTMLElement | null
    historyDrawerRef.current?.focus()
    const close = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') setSelectedRun(null) }
    window.addEventListener('keydown', close)
    return () => { window.removeEventListener('keydown', close); historyReturnFocus.current?.focus() }
  }, [selectedRun?.run.run_id])

  const openRun = (runId: string) => {
    if (!workspace || !sessionId) return
    const controller = new AbortController()
    Promise.all([
      remote.orbit.getRun(workspace, String(sessionId), runId, controller.signal),
      remote.orbit.getSteps(workspace, String(sessionId), runId, controller.signal),
      remote.orbit.getGraph(workspace, String(sessionId), runId, controller.signal),
      remote.orbit.getEdges(workspace, String(sessionId), runId, controller.signal),
      remote.orbit.listArtifacts(workspace, String(sessionId), runId, controller.signal),
    ]).then(([run, steps, graph, edges, runArtifacts]) => setSelectedRun({ run, steps, graph, edges, artifacts: runArtifacts })).catch(reason => setError(String(reason)))
  }
  const generate = (event: FormEvent) => {
    event.preventDefault()
    if (!workspace || !sessionId || !prompt.trim()) return
    const controller = new AbortController()
    setError('')
    const request = workflowToModify
      ? remote.orbit.modifyWorkflow(workspace, String(sessionId), workflowToModify, prompt, regenerate, controller.signal)
      : remote.orbit.generateWorkflow(workspace, String(sessionId), prompt, controller.signal)
    request
      .then(setJob).catch(reason => setError(String(reason)))
  }
  const importArtifact = (artifact: ArtifactSummary) => {
    if (!workspace || !sessionId) return
    const controller = new AbortController()
    remote.orbit.importArtifact(workspace, String(sessionId), artifact.artifact_id, controller.signal)
      .then(ref => setImported(current => ({ ...current, [artifact.artifact_id]: ref })))
      .catch(reason => setError(String(reason)))
  }
  const downloadDiagnostics = () => {
    if (!diagnostics) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(diagnostics, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url; link.download = `orbit-harness-diagnostics-${Date.now()}.json`; link.click()
    URL.revokeObjectURL(url)
  }

  if (!workspace || !sessionId) return createElement('section', null,
    createElement('h2', null, 'Orbit'), createElement('p', null, '请选择一个带 Workspace 的 Harness Session。'),
  )
  const buttons = (['runs', 'workflows', 'artifacts', 'diagnostics'] as const).map(value =>
    createElement('button', { key: value, type: 'button', 'aria-pressed': tab === value, onClick: () => setTab(value) },
      ({ runs: '历史', workflows: 'Workflow', artifacts: 'Artifact', diagnostics: '诊断' })[value]),
  )
  return createElement('section', null,
    createElement('h2', null, 'Orbit Workspace'),
    createElement('p', null, `${workspaceView?.title || workspace.canonicalPath} · Session ${String(sessionId)}`),
    createElement('nav', { 'aria-label': 'Orbit Workspace' }, ...buttons,
      createElement('button', { type: 'button', onClick: () => setRefresh(value => value + 1) }, '刷新')),
    error ? createElement('p', { role: 'alert' }, error) : null,
    loading ? createElement('p', null, '加载中…') : null,
    tab === 'runs' ? createElement('div', null,
      createElement('h3', null, `Run 历史 (${String(runs.length)})`),
      ...runs.map(run => createElement('article', { key: run.run_id },
        createElement('button', { type: 'button', onClick: () => openRun(run.run_id) }, run.goal || run.run_id),
        createElement('span', null, ` ${run.status} · ${run.workflow_id}@${String(run.workflow_version)}`),
      )),
      selectedRun ? createElement('aside', { ref: historyDrawerRef, tabIndex: -1, role: 'dialog', 'aria-modal': true, 'aria-label': 'Orbit Run 详情', style: drawerStyle() },
        createElement('h3', null, selectedRun.run.goal || selectedRun.run.run_id),
        createElement('p', null, `${selectedRun.run.status} · revision ${String(selectedRun.run.revision)}`),
        createElement('ol', null, ...selectedRun.steps.map(step => createElement('li', { key: step.node_id },
          createElement('strong', null, `${step.node_id} · ${step.status}`),
          createElement(StepOutput, { remote, workspace, sessionId: String(sessionId), runId: selectedRun.run.run_id, nodeId: step.node_id, active: !['completed', 'failed', 'cancelled', 'unknown'].includes(selectedRun.run.status) }),
        ))),
        createElement('h4', null, `Edges (${String(selectedRun.edges.length)})`),
        createElement('pre', null, JSON.stringify(selectedRun.edges, null, 2)),
        createElement('h4', null, 'Graph'),
        createElement('pre', null, JSON.stringify(selectedRun.graph, null, 2)),
        createElement('h4', null, `Artifact (${String(selectedRun.artifacts.length)})`),
        ...selectedRun.artifacts.map(item => createElement(ArtifactItem, { key: item.artifact_id, remote, workspace, sessionId: String(sessionId), artifact: item })),
        createElement('button', { type: 'button', onClick: () => setSelectedRun(null) }, '关闭详情'),
      ) : null,
    ) : null,
    tab === 'workflows' ? createElement('div', null,
      createElement('h3', null, `Workflow Catalog (${String(workflows.length)})`),
      ...workflows.map(item => createElement('article', { key: item.workflow_id },
        createElement('strong', null, item.name),
        createElement('p', null, item.description || item.workflow_id),
        createElement('small', null, `v${String(item.latest_version)} · ${item.goal_readiness}`),
      )),
      createElement('h3', null, 'Agent 生成或修改 Workflow'),
      createElement('form', { onSubmit: generate },
        createElement('select', { value: workflowToModify, onChange: (event: ChangeEvent<HTMLSelectElement>) => setWorkflowToModify(event.target.value) },
          createElement('option', { value: '' }, '新建 Workflow'),
          ...workflows.map(item => createElement('option', { key: item.workflow_id, value: item.workflow_id }, `修改 ${item.name}`)),
        ),
        workflowToModify ? createElement('label', null,
          createElement('input', { type: 'checkbox', checked: regenerate, onChange: (event: ChangeEvent<HTMLInputElement>) => setRegenerate(event.target.checked) }),
          '重新生成完整定义',
        ) : null,
        createElement('textarea', { value: prompt, maxLength: 20_000, required: true, onChange: (event: ChangeEvent<HTMLTextAreaElement>) => setPrompt(event.target.value), placeholder: '描述目标、步骤、输入输出和限制' }),
        createElement('button', { type: 'submit' }, workflowToModify ? '修改并编译' : '生成并编译'),
      ),
      job ? createElement('div', { role: 'status' },
        createElement('p', null, `任务 ${job.status} · ${job.job_id}`),
        job.error ? createElement('pre', null, JSON.stringify(job.error, null, 2)) : null,
        job.result ? createElement('pre', null, JSON.stringify(job.result, null, 2)) : null,
      ) : null,
    ) : null,
    tab === 'artifacts' ? createElement('div', null,
      createElement('h3', null, `Artifact Catalog (${String(artifacts.length)})`),
      createElement('p', null, '图片可显式导入 Harness Attachment；其他类型保留在 Orbit 中按需查看。'),
      ...artifacts.map(item => createElement('article', { key: item.artifact_id },
        createElement('strong', null, String(item.name || item.filename || item.artifact_id)),
        createElement('span', null, ` ${item.content_type || 'unknown'} · ${String(item.size_bytes || 0)} bytes`),
        imported[item.artifact_id]
          ? createElement('code', null, ` Attachment ${imported[item.artifact_id].attachmentId}`)
          : createElement('button', { type: 'button', onClick: () => importArtifact(item) }, '导入 Attachment'),
      )),
    ) : null,
    tab === 'diagnostics' ? createElement('div', null,
      createElement('h3', null, '诊断与升级'),
      createElement('p', null, '兼容范围：Orbit >=0.4.0 <0.5.0；集成协议 orbit-harness/1；Harness <0.2.0。升级前先运行 Profile 冒烟，回滚不删除 Orbit 数据库。'),
      createElement('button', { type: 'button', disabled: !diagnostics, onClick: downloadDiagnostics }, '下载诊断包'),
      createElement('button', { type: 'button', disabled: !diagnostics, onClick: () => { if (diagnostics) void navigator.clipboard?.writeText(JSON.stringify(diagnostics, null, 2)) } }, '复制诊断信息'),
      createElement('pre', null, JSON.stringify(diagnostics || {
        workspace, sessionId: String(sessionId), state: 'loading',
      }, null, 2)),
    ) : null,
  )
}

export const inject = ['conversationEvents', 'remote', 'slots']
export function apply(ctx: ClientContext): void {
  ctx.conversationEvents.register(orbitRunDefinition)
  const remote = ctx.remote as unknown as OrbitClient
  ctx.slots.inject('conversation.chat.node', () => ctx.slots.register(
    { name: 'conversation.chat.node', key: 'orbit-run' },
    (props: ChatNodeViewProps<'orbit-run'>) => OrbitRunCard(props, remote),
  ))
  ctx.slots.inject('settings.general.item', () => ctx.slots.register(
    { name: 'settings.general.item', id: 'orbit-runtime', order: 80 },
    (props: { useWorkspaces: <T>(selector: (state: WorkspaceListState) => T) => T }) => OrbitSettings(props, remote),
  ))
  ctx.slots.inject('settings.section', () => ctx.slots.register(
    { name: 'settings.section', id: 'orbit', order: 70, label: 'Orbit' },
    (props: OrbitWorkspaceProps) => OrbitWorkspace(props, remote),
  ))
}
