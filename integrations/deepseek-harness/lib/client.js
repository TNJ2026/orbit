window.__ModuleLoader__.load({
	id: "@orbit-runtime/dsh-orbit",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		//#region lib/client.js
		const orbitRunDefinition = {
			kind: "orbit-run",
			target: "chat",
			match: (event) => event.type === "orbit/run-started" ? {
				id: String(event.data.runId),
				role: "start"
			} : event.type === "orbit/run-checkpoint" || event.type === "orbit/run-ended" ? {
				id: String(event.data.runId),
				role: "update"
			} : null,
			start: (_reader, match) => {
				if (match.event.type !== "orbit/run-started") throw new Error("orbit-run requires run-started");
				const data = match.event.data;
				return {
					runId: data.runId,
					workspaceId: data.workspaceId,
					goal: data.goal,
					status: data.status,
					artifactCount: 0,
					revision: data.revision,
					terminal: false,
					sourcePosition: data.sourcePosition
				};
			},
			update: (context, match) => {
				if (match.event.type !== "orbit/run-checkpoint" && match.event.type !== "orbit/run-ended") return context.state;
				const data = match.event.data;
				if (data.sourcePosition <= context.state.sourcePosition) return context.state;
				return {
					...context.state,
					status: data.status,
					artifactCount: data.artifactCount,
					revision: data.revision,
					terminal: match.event.type === "orbit/run-ended",
					sourcePosition: data.sourcePosition
				};
			},
			publication: (match) => match.event.type === "orbit/run-checkpoint" ? "animation-frame" : "immediate",
			buildViewNode: (context) => context.state === void 0 ? null : {
				key: context.key,
				kind: "orbit-run",
				id: context.id,
				target: "chat",
				anchorSeq: context.start?.event.seq ?? context.matches[0]?.event.seq ?? 0,
				location: context.start?.location ?? { kind: "unresolved" },
				visibility: "visible",
				data: {
					runId: context.state.runId,
					workspaceId: context.state.workspaceId,
					goal: context.state.goal,
					status: context.state.status,
					artifactCount: context.state.artifactCount,
					revision: context.state.revision,
					terminal: context.state.terminal
				}
			}
		};
		function drawerStyle() {
			return {
				position: "fixed",
				inset: "0 0 0 auto",
				width: "min(720px, 92vw)",
				zIndex: 1e3,
				overflow: "auto",
				background: "var(--background, #fff)",
				color: "inherit",
				borderLeft: "1px solid #ddd",
				padding: 20,
				boxShadow: "-12px 0 30px #0002"
			};
		}
		function mergeOutput(previous, next) {
			const chunks = new Map(previous.chunks.map((chunk) => [chunk.chunk_id, chunk]));
			for (const chunk of next.chunks) chunks.set(chunk.chunk_id, chunk);
			return {
				chunks: [...chunks.values()].sort((a, b) => a.chunk_id - b.chunk_id),
				after: Math.max(previous.after, next.after),
				has_more: next.has_more
			};
		}
		function StepOutput({ remote, workspace, sessionId, runId, nodeId, active }) {
			const [expanded, setExpanded] = (0, react.useState)(false);
			const [page, setPage] = (0, react.useState)({
				chunks: [],
				after: 0,
				has_more: false
			});
			const [error, setError] = (0, react.useState)("");
			const load = () => {
				const controller = new AbortController();
				remote.orbit.readOutput(workspace, sessionId, runId, page.after, nodeId, controller.signal).then((next) => setPage((current) => mergeOutput(current, next))).catch((reason) => {
					if (!controller.signal.aborted) setError(String(reason));
				});
				return controller;
			};
			(0, react.useEffect)(() => {
				if (!expanded) return;
				const controller = load();
				return () => controller.abort();
			}, [
				expanded,
				nodeId,
				runId
			]);
			(0, react.useEffect)(() => {
				if (!expanded || !active) return;
				const timer = window.setInterval(load, 1e3);
				return () => window.clearInterval(timer);
			}, [
				expanded,
				active,
				page.after,
				nodeId,
				runId
			]);
			return (0, react.createElement)("div", null, (0, react.createElement)("button", {
				type: "button",
				"aria-expanded": expanded,
				onClick: () => setExpanded((value) => !value)
			}, expanded ? "隐藏原始输出" : "查看原始输出"), expanded ? (0, react.createElement)("div", null, error ? (0, react.createElement)("p", { role: "alert" }, error) : null, (0, react.createElement)("pre", null, page.chunks.map((chunk) => chunk.text).join("")), page.has_more ? (0, react.createElement)("button", {
				type: "button",
				onClick: load
			}, "加载更多") : null) : null);
		}
		function decodeBase64(value) {
			const binary = atob(value);
			return Uint8Array.from(binary, (character) => character.charCodeAt(0));
		}
		function ArtifactItem({ remote, workspace, sessionId, artifact }) {
			const [content, setContent] = (0, react.useState)(null);
			const [error, setError] = (0, react.useState)("");
			const contentType = String(artifact.content_type || "application/octet-stream");
			const dataUrl = content ? `data:${contentType};base64,${content.content}` : "";
			return (0, react.createElement)("section", { style: {
				borderTop: "1px solid #ddd",
				padding: "8px 0"
			} }, (0, react.createElement)("strong", null, String(artifact.filename || artifact.artifact_id)), (0, react.createElement)("span", null, ` · ${contentType} · ${String(artifact.size_bytes || 0)} bytes`), !content ? (0, react.createElement)("button", {
				type: "button",
				onClick: () => {
					const controller = new AbortController();
					remote.orbit.getArtifactContent(workspace, sessionId, artifact.artifact_id, controller.signal).then(setContent).catch((reason) => setError(String(reason)));
				}
			}, "预览") : null, error ? (0, react.createElement)("p", { role: "alert" }, error) : null, content && contentType.startsWith("image/") ? (0, react.createElement)("img", {
				src: dataUrl,
				alt: String(artifact.filename || artifact.artifact_id),
				style: { maxWidth: "100%" }
			}) : null, content && (contentType.startsWith("text/") || contentType.includes("json")) ? (0, react.createElement)("pre", null, new TextDecoder().decode(decodeBase64(content.content))) : null, content ? (0, react.createElement)("a", {
				href: dataUrl,
				download: String(artifact.filename || "artifact")
			}, "下载") : null);
		}
		function OrbitRunCard({ node, cwd, sessionId }, remote) {
			const restoreKey = `orbit:drawer:${String(sessionId)}`;
			const [open, setOpen] = (0, react.useState)(() => typeof sessionStorage !== "undefined" && sessionStorage.getItem(restoreKey) === node.data.runId);
			const [detail, setDetail] = (0, react.useState)(null);
			const [error, setError] = (0, react.useState)("");
			const [answer, setAnswer] = (0, react.useState)("");
			const [reconciliationNote, setReconciliationNote] = (0, react.useState)("");
			const [copiedDelegation, setCopiedDelegation] = (0, react.useState)("");
			const triggerRef = (0, react.useRef)(null);
			const drawerRef = (0, react.useRef)(null);
			const closeRef = (0, react.useRef)(null);
			const workspace = {
				id: node.data.workspaceId,
				canonicalPath: cwd || ""
			};
			const close = () => {
				setOpen(false);
				sessionStorage.removeItem(restoreKey);
				requestAnimationFrame(() => triggerRef.current?.focus());
			};
			const openDrawer = () => {
				sessionStorage.setItem(restoreKey, node.data.runId);
				setOpen(true);
			};
			(0, react.useEffect)(() => {
				if (!open) return;
				closeRef.current?.focus();
				const escape = (event) => {
					if (event.key === "Escape") close();
				};
				document.addEventListener("keydown", escape);
				return () => document.removeEventListener("keydown", escape);
			}, [open, restoreKey]);
			(0, react.useEffect)(() => {
				if (!open || !cwd) return;
				const controller = new AbortController();
				Promise.all([
					remote.orbit.getRun(workspace, String(sessionId), node.data.runId, controller.signal),
					remote.orbit.getSteps(workspace, String(sessionId), node.data.runId, controller.signal),
					remote.orbit.getGraph(workspace, String(sessionId), node.data.runId, controller.signal),
					remote.orbit.getEdges(workspace, String(sessionId), node.data.runId, controller.signal),
					remote.orbit.listArtifacts(workspace, String(sessionId), node.data.runId, controller.signal)
				]).then(([run, steps, graph, edges, artifacts]) => setDetail({
					run,
					steps,
					graph,
					edges,
					artifacts
				})).catch((reason) => {
					if (!controller.signal.aborted) setError(String(reason));
				});
				return () => controller.abort();
			}, [
				open,
				cwd,
				sessionId,
				node.data.runId,
				node.data.revision
			]);
			return (0, react.createElement)("section", { "data-orbit-run-id": node.data.runId }, (0, react.createElement)("strong", null, node.data.goal || node.data.runId), (0, react.createElement)("span", null, ` ${node.data.status}`), node.data.artifactCount ? (0, react.createElement)("span", null, ` · ${String(node.data.artifactCount)} artifacts`) : null, (0, react.createElement)("button", {
				ref: triggerRef,
				type: "button",
				onClick: openDrawer,
				disabled: !cwd
			}, "查看详情"), open ? (0, react.createElement)("aside", {
				ref: drawerRef,
				role: "dialog",
				"aria-modal": true,
				"aria-label": "Orbit Run 详情",
				style: drawerStyle(),
				onKeyDown: (event) => {
					if (event.key !== "Tab" || !drawerRef.current) return;
					const focusable = [...drawerRef.current.querySelectorAll("button:not([disabled]),a[href],textarea,input,select,[tabindex]:not([tabindex=\"-1\"])")];
					if (!focusable.length) return;
					const first = focusable[0], last = focusable[focusable.length - 1];
					if (event.shiftKey && document.activeElement === first) {
						event.preventDefault();
						last.focus();
					} else if (!event.shiftKey && document.activeElement === last) {
						event.preventDefault();
						first.focus();
					}
				}
			}, (0, react.createElement)("button", {
				ref: closeRef,
				type: "button",
				onClick: close,
				style: { float: "right" }
			}, "关闭"), (0, react.createElement)("h2", null, node.data.goal || node.data.runId), error ? (0, react.createElement)("p", { role: "alert" }, error) : null, !detail ? (0, react.createElement)("p", null, "加载中…") : (0, react.createElement)("div", null, (0, react.createElement)("p", null, `${detail.run.status} · revision ${String(detail.run.revision)}`), detail.run.allowed_commands.some((command) => command.command === "langgraph_run.resume") ? (0, react.createElement)("form", { onSubmit: (event) => {
				event.preventDefault();
				const command = detail.run.allowed_commands.find((item) => item.command === "langgraph_run.resume");
				if (!command) return;
				const controller = new AbortController();
				remote.orbit.executeCommand({
					workspace,
					sessionId: String(sessionId),
					runId: node.data.runId,
					command: "langgraph_run.resume",
					expectedVersion: command.expected_version,
					idempotencyKey: crypto.randomUUID(),
					value: answer
				}, controller.signal).then((run) => {
					setDetail({
						...detail,
						run
					});
					setAnswer("");
				}).catch((reason) => setError(String(reason)));
			} }, (0, react.createElement)("label", null, "需要人工输入", (0, react.createElement)("textarea", {
				value: answer,
				onChange: (event) => setAnswer(event.currentTarget.value)
			})), (0, react.createElement)("button", { type: "submit" }, "继续运行")) : null, (0, react.createElement)("h3", null, "步骤"), ...detail.steps.map((step) => (0, react.createElement)("section", {
				key: step.node_id,
				style: {
					borderTop: "1px solid #ddd",
					padding: "10px 0"
				}
			}, (0, react.createElement)("strong", null, String(step.label || step.node_id)), (0, react.createElement)("span", null, ` · ${step.status}`), step.resolution?.kind === "reconciliation_required" ? (0, react.createElement)("div", {
				role: "status",
				style: {
					borderLeft: "4px solid #d97706",
					paddingLeft: 8
				}
			}, (0, react.createElement)("p", null, "需要人工核对外部 Agent 结果；Orbit 不会自动重试"), step.resolution.delegation_id ? (0, react.createElement)("div", null, (0, react.createElement)("code", null, step.resolution.delegation_id), (0, react.createElement)("button", {
				type: "button",
				onClick: () => {
					navigator.clipboard.writeText(step.resolution.delegation_id).then(() => setCopiedDelegation(step.resolution.delegation_id)).catch((reason) => setError(String(reason)));
				}
			}, copiedDelegation === step.resolution.delegation_id ? "已复制" : "复制 ID"), (0, react.createElement)("button", {
				type: "button",
				onClick: () => {
					const controller = new AbortController();
					remote.orbit.getSteps(workspace, String(sessionId), node.data.runId, controller.signal).then((steps) => setDetail({
						...detail,
						steps
					})).catch((reason) => setError(String(reason)));
				}
			}, "刷新核对状态")) : null) : null, step.reconciliation ? (0, react.createElement)("p", null, `人工判定：${step.reconciliation.outcome === "confirmed_succeeded" ? "确认成功" : "确认失败"}${step.reconciliation.note ? ` · ${step.reconciliation.note}` : ""}`) : step.resolution?.delegation_id ? (0, react.createElement)("div", null, (0, react.createElement)("label", null, "核对说明", (0, react.createElement)("input", {
				value: reconciliationNote,
				onChange: (event) => setReconciliationNote(event.currentTarget.value)
			})), ...["confirmed_succeeded", "confirmed_failed"].map((outcome) => (0, react.createElement)("button", {
				key: outcome,
				type: "button",
				onClick: () => {
					const controller = new AbortController();
					remote.orbit.reconcileDelegation(workspace, String(sessionId), node.data.runId, step.resolution.delegation_id, outcome, reconciliationNote, controller.signal).then((steps) => {
						setDetail({
							...detail,
							steps
						});
						setReconciliationNote("");
					}).catch((reason) => setError(String(reason)));
				}
			}, outcome === "confirmed_succeeded" ? "确认外部执行成功" : "确认外部执行失败"))) : null, step.prompt ? (0, react.createElement)("pre", null, String(step.prompt)) : null, (0, react.createElement)(StepOutput, {
				remote,
				workspace,
				sessionId: String(sessionId),
				runId: node.data.runId,
				nodeId: step.node_id,
				active: !node.data.terminal
			}))), (0, react.createElement)("h3", null, `执行图 (${String(Object.keys(detail.graph).length)} fields / ${String(detail.edges.length)} edges)`), (0, react.createElement)("pre", null, JSON.stringify(detail.graph, null, 2)), (0, react.createElement)("h3", null, `产物 (${String(detail.artifacts.length)})`), ...detail.artifacts.map((item) => (0, react.createElement)(ArtifactItem, {
				key: item.artifact_id,
				remote,
				workspace,
				sessionId: String(sessionId),
				artifact: item
			})))) : null);
		}
		function OrbitSettings({ useWorkspaces }, remote) {
			const workspace = useWorkspaces((state) => state.items.find((item) => item.workspaceId === state.recentWorkspaceId) || state.items[0]);
			const [status, setStatus] = (0, react.useState)("未连接");
			const [runtime, setRuntime] = (0, react.useState)(null);
			const [refresh, setRefresh] = (0, react.useState)(0);
			const startCommand = "orbit serve --project-root . --mcp-tool-profile harness";
			(0, react.useEffect)(() => {
				if (!workspace) {
					setRuntime(null);
					setStatus("未选择 Workspace");
					return;
				}
				const controller = new AbortController();
				setRuntime(null);
				setStatus("连接中…");
				remote.orbit.getRuntime({
					id: String(workspace.workspaceId),
					canonicalPath: workspace.path
				}, controller.signal).then((value) => {
					setRuntime(value);
					setStatus(value.state === "ready" ? "已连接" : "已停止");
				}).catch((reason) => {
					if (!controller.signal.aborted) setStatus(`连接失败：${String(reason)}`);
				});
				return () => controller.abort();
			}, [
				workspace?.workspaceId,
				workspace?.path,
				refresh
			]);
			const capabilities = runtime?.capabilities || {};
			return (0, react.createElement)("section", null, (0, react.createElement)("strong", null, "Orbit Runtime"), (0, react.createElement)("p", null, `${workspace?.title || "Workspace"} · ${status}`), runtime ? (0, react.createElement)("dl", null, (0, react.createElement)("dt", null, "Orbit 版本"), (0, react.createElement)("dd", null, String(capabilities.orbit_version || "未知")), (0, react.createElement)("dt", null, "集成协议"), (0, react.createElement)("dd", null, String(capabilities.integration_protocol || "未知")), (0, react.createElement)("dt", null, "MCP 协议"), (0, react.createElement)("dd", null, String(capabilities.mcp_protocol || "未知")), (0, react.createElement)("dt", null, "工具 Profile"), (0, react.createElement)("dd", null, String(capabilities.tool_profile || "未知"))) : null, (0, react.createElement)("div", null, (0, react.createElement)("button", {
				type: "button",
				onClick: () => setRefresh((value) => value + 1)
			}, "刷新连接"), (0, react.createElement)("button", {
				type: "button",
				onClick: () => {
					navigator.clipboard?.writeText(startCommand);
				}
			}, "复制启动命令")), !runtime && workspace ? (0, react.createElement)("code", null, startCommand) : null, (0, react.createElement)("small", null, "连接独立 Orbit Runtime，并使用当前 Harness Session 隔离 MCP 运行身份。"));
		}
		function OrbitWorkspace({ useSessions, useWorkspaces }, remote) {
			const sessionId = useSessions((state) => state.current);
			const workspaceView = useWorkspaces((state) => state.items.find((item) => item.workspaceId === state.recentWorkspaceId) || state.items[0]);
			const workspace = workspaceView ? {
				id: String(workspaceView.workspaceId),
				canonicalPath: workspaceView.path
			} : null;
			const [tab, setTab] = (0, react.useState)("runs");
			const [runs, setRuns] = (0, react.useState)([]);
			const [workflows, setWorkflows] = (0, react.useState)([]);
			const [artifacts, setArtifacts] = (0, react.useState)([]);
			const [selectedRun, setSelectedRun] = (0, react.useState)(null);
			const [prompt, setPrompt] = (0, react.useState)("");
			const [workflowToModify, setWorkflowToModify] = (0, react.useState)("");
			const [regenerate, setRegenerate] = (0, react.useState)(false);
			const [job, setJob] = (0, react.useState)(null);
			const [imported, setImported] = (0, react.useState)({});
			const [diagnostics, setDiagnostics] = (0, react.useState)(null);
			const [error, setError] = (0, react.useState)("");
			const [loading, setLoading] = (0, react.useState)(false);
			const [refresh, setRefresh] = (0, react.useState)(0);
			const historyDrawerRef = (0, react.useRef)(null);
			const historyReturnFocus = (0, react.useRef)(null);
			(0, react.useEffect)(() => {
				if (!workspace || !sessionId) return;
				const controller = new AbortController();
				setLoading(true);
				setError("");
				Promise.all([
					remote.orbit.listRuns(workspace, String(sessionId), void 0, controller.signal),
					remote.orbit.listWorkflows(workspace, String(sessionId), controller.signal),
					remote.orbit.listArtifacts(workspace, String(sessionId), void 0, controller.signal),
					remote.orbit.getDiagnostics(workspace, String(sessionId), controller.signal)
				]).then(([nextRuns, nextWorkflows, nextArtifacts, nextDiagnostics]) => {
					setRuns(nextRuns);
					setWorkflows(nextWorkflows);
					setArtifacts(nextArtifacts);
					setDiagnostics(nextDiagnostics);
				}).catch((reason) => {
					if (!controller.signal.aborted) setError(String(reason));
				}).finally(() => {
					if (!controller.signal.aborted) setLoading(false);
				});
				return () => controller.abort();
			}, [
				workspace?.id,
				workspace?.canonicalPath,
				sessionId,
				refresh
			]);
			(0, react.useEffect)(() => {
				if (!workspace || !sessionId || !job || !["queued", "running"].includes(job.status)) return;
				const controller = new AbortController();
				const timer = window.setInterval(() => {
					remote.orbit.getAuthoringJob(workspace, String(sessionId), job.job_id, controller.signal).then(setJob).catch((reason) => {
						if (!controller.signal.aborted) setError(String(reason));
					});
				}, 1e3);
				return () => {
					controller.abort();
					window.clearInterval(timer);
				};
			}, [
				workspace?.id,
				sessionId,
				job?.job_id,
				job?.status
			]);
			(0, react.useEffect)(() => {
				if (!selectedRun) return;
				historyReturnFocus.current = document.activeElement;
				historyDrawerRef.current?.focus();
				const close = (event) => {
					if (event.key === "Escape") setSelectedRun(null);
				};
				window.addEventListener("keydown", close);
				return () => {
					window.removeEventListener("keydown", close);
					historyReturnFocus.current?.focus();
				};
			}, [selectedRun?.run.run_id]);
			const openRun = (runId) => {
				if (!workspace || !sessionId) return;
				const controller = new AbortController();
				Promise.all([
					remote.orbit.getRun(workspace, String(sessionId), runId, controller.signal),
					remote.orbit.getSteps(workspace, String(sessionId), runId, controller.signal),
					remote.orbit.getGraph(workspace, String(sessionId), runId, controller.signal),
					remote.orbit.getEdges(workspace, String(sessionId), runId, controller.signal),
					remote.orbit.listArtifacts(workspace, String(sessionId), runId, controller.signal)
				]).then(([run, steps, graph, edges, runArtifacts]) => setSelectedRun({
					run,
					steps,
					graph,
					edges,
					artifacts: runArtifacts
				})).catch((reason) => setError(String(reason)));
			};
			const generate = (event) => {
				event.preventDefault();
				if (!workspace || !sessionId || !prompt.trim()) return;
				const controller = new AbortController();
				setError("");
				(workflowToModify ? remote.orbit.modifyWorkflow(workspace, String(sessionId), workflowToModify, prompt, regenerate, controller.signal) : remote.orbit.generateWorkflow(workspace, String(sessionId), prompt, controller.signal)).then(setJob).catch((reason) => setError(String(reason)));
			};
			const importArtifact = (artifact) => {
				if (!workspace || !sessionId) return;
				const controller = new AbortController();
				remote.orbit.importArtifact(workspace, String(sessionId), artifact.artifact_id, controller.signal).then((ref) => setImported((current) => ({
					...current,
					[artifact.artifact_id]: ref
				}))).catch((reason) => setError(String(reason)));
			};
			const downloadDiagnostics = () => {
				if (!diagnostics) return;
				const url = URL.createObjectURL(new Blob([JSON.stringify(diagnostics, null, 2)], { type: "application/json" }));
				const link = document.createElement("a");
				link.href = url;
				link.download = `orbit-harness-diagnostics-${Date.now()}.json`;
				link.click();
				URL.revokeObjectURL(url);
			};
			if (!workspace || !sessionId) return (0, react.createElement)("section", null, (0, react.createElement)("h2", null, "Orbit"), (0, react.createElement)("p", null, "请选择一个带 Workspace 的 Harness Session。"));
			const buttons = [
				"runs",
				"workflows",
				"artifacts",
				"diagnostics"
			].map((value) => (0, react.createElement)("button", {
				key: value,
				type: "button",
				"aria-pressed": tab === value,
				onClick: () => setTab(value)
			}, {
				runs: "历史",
				workflows: "Workflow",
				artifacts: "Artifact",
				diagnostics: "诊断"
			}[value]));
			return (0, react.createElement)("section", null, (0, react.createElement)("h2", null, "Orbit Workspace"), (0, react.createElement)("p", null, `${workspaceView?.title || workspace.canonicalPath} · Session ${String(sessionId)}`), (0, react.createElement)("nav", { "aria-label": "Orbit Workspace" }, ...buttons, (0, react.createElement)("button", {
				type: "button",
				onClick: () => setRefresh((value) => value + 1)
			}, "刷新")), error ? (0, react.createElement)("p", { role: "alert" }, error) : null, loading ? (0, react.createElement)("p", null, "加载中…") : null, tab === "runs" ? (0, react.createElement)("div", null, (0, react.createElement)("h3", null, `Run 历史 (${String(runs.length)})`), ...runs.map((run) => (0, react.createElement)("article", { key: run.run_id }, (0, react.createElement)("button", {
				type: "button",
				onClick: () => openRun(run.run_id)
			}, run.goal || run.run_id), (0, react.createElement)("span", null, ` ${run.status} · ${run.workflow_id}@${String(run.workflow_version)}`))), selectedRun ? (0, react.createElement)("aside", {
				ref: historyDrawerRef,
				tabIndex: -1,
				role: "dialog",
				"aria-modal": true,
				"aria-label": "Orbit Run 详情",
				style: drawerStyle()
			}, (0, react.createElement)("h3", null, selectedRun.run.goal || selectedRun.run.run_id), (0, react.createElement)("p", null, `${selectedRun.run.status} · revision ${String(selectedRun.run.revision)}`), (0, react.createElement)("ol", null, ...selectedRun.steps.map((step) => (0, react.createElement)("li", { key: step.node_id }, (0, react.createElement)("strong", null, `${step.node_id} · ${step.status}`), (0, react.createElement)(StepOutput, {
				remote,
				workspace,
				sessionId: String(sessionId),
				runId: selectedRun.run.run_id,
				nodeId: step.node_id,
				active: ![
					"completed",
					"failed",
					"cancelled",
					"unknown"
				].includes(selectedRun.run.status)
			})))), (0, react.createElement)("h4", null, `Edges (${String(selectedRun.edges.length)})`), (0, react.createElement)("pre", null, JSON.stringify(selectedRun.edges, null, 2)), (0, react.createElement)("h4", null, "Graph"), (0, react.createElement)("pre", null, JSON.stringify(selectedRun.graph, null, 2)), (0, react.createElement)("h4", null, `Artifact (${String(selectedRun.artifacts.length)})`), ...selectedRun.artifacts.map((item) => (0, react.createElement)(ArtifactItem, {
				key: item.artifact_id,
				remote,
				workspace,
				sessionId: String(sessionId),
				artifact: item
			})), (0, react.createElement)("button", {
				type: "button",
				onClick: () => setSelectedRun(null)
			}, "关闭详情")) : null) : null, tab === "workflows" ? (0, react.createElement)("div", null, (0, react.createElement)("h3", null, `Workflow Catalog (${String(workflows.length)})`), ...workflows.map((item) => (0, react.createElement)("article", { key: item.workflow_id }, (0, react.createElement)("strong", null, item.name), (0, react.createElement)("p", null, item.description || item.workflow_id), (0, react.createElement)("small", null, `v${String(item.latest_version)} · ${item.goal_readiness}`))), (0, react.createElement)("h3", null, "Agent 生成或修改 Workflow"), (0, react.createElement)("form", { onSubmit: generate }, (0, react.createElement)("select", {
				value: workflowToModify,
				onChange: (event) => setWorkflowToModify(event.target.value)
			}, (0, react.createElement)("option", { value: "" }, "新建 Workflow"), ...workflows.map((item) => (0, react.createElement)("option", {
				key: item.workflow_id,
				value: item.workflow_id
			}, `修改 ${item.name}`))), workflowToModify ? (0, react.createElement)("label", null, (0, react.createElement)("input", {
				type: "checkbox",
				checked: regenerate,
				onChange: (event) => setRegenerate(event.target.checked)
			}), "重新生成完整定义") : null, (0, react.createElement)("textarea", {
				value: prompt,
				maxLength: 2e4,
				required: true,
				onChange: (event) => setPrompt(event.target.value),
				placeholder: "描述目标、步骤、输入输出和限制"
			}), (0, react.createElement)("button", { type: "submit" }, workflowToModify ? "修改并编译" : "生成并编译")), job ? (0, react.createElement)("div", { role: "status" }, (0, react.createElement)("p", null, `任务 ${job.status} · ${job.job_id}`), job.error ? (0, react.createElement)("pre", null, JSON.stringify(job.error, null, 2)) : null, job.result ? (0, react.createElement)("pre", null, JSON.stringify(job.result, null, 2)) : null) : null) : null, tab === "artifacts" ? (0, react.createElement)("div", null, (0, react.createElement)("h3", null, `Artifact Catalog (${String(artifacts.length)})`), (0, react.createElement)("p", null, "图片可显式导入 Harness Attachment；其他类型保留在 Orbit 中按需查看。"), ...artifacts.map((item) => (0, react.createElement)("article", { key: item.artifact_id }, (0, react.createElement)("strong", null, String(item.name || item.filename || item.artifact_id)), (0, react.createElement)("span", null, ` ${item.content_type || "unknown"} · ${String(item.size_bytes || 0)} bytes`), imported[item.artifact_id] ? (0, react.createElement)("code", null, ` Attachment ${imported[item.artifact_id].attachmentId}`) : (0, react.createElement)("button", {
				type: "button",
				onClick: () => importArtifact(item)
			}, "导入 Attachment")))) : null, tab === "diagnostics" ? (0, react.createElement)("div", null, (0, react.createElement)("h3", null, "诊断与升级"), (0, react.createElement)("p", null, "兼容范围：Orbit >=0.4.0 <0.5.0；集成协议 orbit-harness/1；Harness <0.2.0。升级前先运行 Profile 冒烟，回滚不删除 Orbit 数据库。"), (0, react.createElement)("button", {
				type: "button",
				disabled: !diagnostics,
				onClick: downloadDiagnostics
			}, "下载诊断包"), (0, react.createElement)("button", {
				type: "button",
				disabled: !diagnostics,
				onClick: () => {
					if (diagnostics) navigator.clipboard?.writeText(JSON.stringify(diagnostics, null, 2));
				}
			}, "复制诊断信息"), (0, react.createElement)("pre", null, JSON.stringify(diagnostics || {
				workspace,
				sessionId: String(sessionId),
				state: "loading"
			}, null, 2))) : null);
		}
		const inject = [
			"conversationEvents",
			"remote",
			"slots"
		];
		function apply(ctx) {
			ctx.conversationEvents.register(orbitRunDefinition);
			const remote = ctx.remote;
			ctx.slots.inject("conversation.chat.node", () => ctx.slots.register({
				name: "conversation.chat.node",
				key: "orbit-run"
			}, (props) => OrbitRunCard(props, remote)));
			ctx.slots.inject("settings.general.item", () => ctx.slots.register({
				name: "settings.general.item",
				id: "orbit-runtime",
				order: 80
			}, (props) => OrbitSettings(props, remote)));
			ctx.slots.inject("settings.section", () => ctx.slots.register({
				name: "settings.section",
				id: "orbit",
				order: 70,
				label: "Orbit"
			}, (props) => OrbitWorkspace(props, remote)));
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		exports.orbitRunDefinition = orbitRunDefinition;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map