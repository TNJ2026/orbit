window.__ModuleLoader__.load({
	id: "@orbit-runtime/dsh-orbit",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		let _deepseek_ai_dsh_client_ui_primitives = require("@deepseek-ai/dsh-client-ui-primitives");
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region \0dsh-css:/Users/cxd/develop/orbit/integrations/deepseek-harness/src/client/OrbitPanel.module.css.mjs
		const css = ".JeOz9W_panel{border:1px solid var(--dsw-alias-line-normal,#80808047);background:var(--dsw-alias-bg-layer-1,Canvas);color:var(--dsw-alias-label-primary);pointer-events:auto;border-radius:12px;flex-direction:column;display:flex;position:absolute;overflow:hidden;box-shadow:0 12px 40px #0000002e}.JeOz9W_bar{border-bottom:1px solid var(--dsw-alias-line-normal,#80808047);background:var(--dsw-alias-bg-module,Canvas);cursor:grab;user-select:none;align-items:center;gap:8px;padding:8px 10px 8px 12px;display:flex}.JeOz9W_bar:active{cursor:grabbing}.JeOz9W_title{font-size:13px;font-weight:600}.JeOz9W_count{color:var(--dsw-alias-label-tertiary);flex:1;font-size:12px}.JeOz9W_body{flex:1;min-height:0;overflow-y:auto}.JeOz9W_row{border-bottom:1px solid var(--dsw-alias-line-normal,#80808047);grid-template-columns:8px 1fr auto;align-items:start;gap:8px;padding:10px 12px;display:grid}.JeOz9W_row:last-child{border-bottom:0}.JeOz9W_dot{border-radius:50%;width:8px;height:8px;margin-top:5px}.JeOz9W_live{background:var(--dsw-alias-state-business-primary)}.JeOz9W_done{background:var(--dsw-alias-state-success)}.JeOz9W_failed{background:var(--dsw-alias-state-danger)}.JeOz9W_unknown{background:var(--dsw-alias-state-warning,var(--dsw-alias-label-tertiary))}.JeOz9W_goal{-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:13px;line-height:1.4;display:-webkit-box;overflow:hidden}.JeOz9W_meta{color:var(--dsw-alias-label-tertiary);font-size:11px}.JeOz9W_status{color:var(--dsw-alias-label-secondary,GrayText);white-space:nowrap;margin-left:8px;font-size:11px}.JeOz9W_empty,.JeOz9W_error{color:var(--dsw-alias-label-tertiary);text-align:center;padding:20px 14px;font-size:12px}.JeOz9W_error{color:var(--dsw-alias-state-error-primary);text-align:left}.JeOz9W_badge{border:1px solid var(--dsw-alias-line-normal,#80808047);background:var(--dsw-alias-bg-layer-1,Canvas);color:var(--dsw-alias-label-secondary);cursor:pointer;pointer-events:auto;border-radius:999px;align-items:center;gap:6px;padding:6px 12px;font-size:12px;display:flex;position:absolute;box-shadow:0 6px 20px #00000024}.JeOz9W_badge:hover{color:var(--dsw-alias-label-primary)}.JeOz9W_resize{cursor:nwse-resize;width:14px;height:14px;position:absolute;inset:auto 0 0 auto}.JeOz9W_stepRow{padding:4px 12px 4px 22px;font-size:12px}.JeOz9W_attention{border-left:2px solid var(--dsw-alias-state-warning,var(--dsw-alias-label-tertiary));color:var(--dsw-alias-label-secondary);margin:4px 0;padding:6px 8px;font-size:11px}.JeOz9W_output{background:var(--dsw-alias-bg-module-platform,var(--dsw-alias-bg-module,Canvas));max-height:220px;color:var(--dsw-alias-label-secondary);white-space:pre-wrap;overflow-wrap:anywhere;border-radius:6px;margin:4px 0 8px;padding:8px;font-size:11px;line-height:1.5;overflow:auto}.JeOz9W_actions{flex-wrap:wrap;align-items:center;gap:6px;margin:6px 0 8px;display:flex}.JeOz9W_actions input{border:1px solid var(--dsw-alias-line-normal,#80808047);background:var(--dsw-alias-bg-layer-1,Canvas);min-width:0;color:var(--dsw-alias-label-primary);border-radius:6px;flex:140px;padding:4px 8px;font-size:11px}.JeOz9W_iconButton{color:var(--dsw-alias-label-tertiary);cursor:pointer;background:0 0;border:0;border-radius:4px;justify-content:center;align-items:center;padding:2px;display:inline-flex}.JeOz9W_iconButton:hover{color:var(--dsw-alias-label-primary)}.JeOz9W_iconButton:disabled{cursor:default}.JeOz9W_iconButton:disabled svg{animation:.7s linear infinite JeOz9W_orbit-spin}@keyframes JeOz9W_orbit-spin{to{transform:rotate(360deg)}}@media (prefers-reduced-motion:reduce){.JeOz9W_iconButton:disabled svg{opacity:.45;animation:none}}.JeOz9W_catalogRow{justify-content:space-between;align-items:baseline;gap:8px;padding:4px 0 4px 12px;display:flex}.JeOz9W_catalogRow>*{text-overflow:ellipsis;white-space:nowrap;min-width:0;overflow:hidden}.JeOz9W_catalogRow>span{flex:auto}.JeOz9W_catalogRow>code{flex:0 auto}.JeOz9W_tabs{border-bottom:1px solid var(--dsw-alias-line-normal,#80808047);background:var(--dsw-alias-bg-module,Canvas);flex:none;gap:2px;padding:6px 8px 0;display:flex}.JeOz9W_tab{color:var(--dsw-alias-label-tertiary,GrayText);cursor:pointer;background:0 0;border:0;border-bottom:2px solid #0000;flex:1 1 0;padding:6px 4px 8px;font-size:12px}.JeOz9W_tab:hover{color:var(--dsw-alias-label-primary,CanvasText)}.JeOz9W_tabActive{color:var(--dsw-alias-label-primary,CanvasText);border-bottom-color:var(--dsw-alias-state-business-primary,Highlight);font-weight:600}.JeOz9W_agentRow{align-items:center;gap:10px;padding:8px 12px;display:flex}.JeOz9W_avatar{letter-spacing:.5px;text-transform:uppercase;border-radius:7px;flex:none;place-items:center;width:28px;height:28px;font-size:10px;font-weight:700;display:grid}.JeOz9W_agentName{font-size:12px;font-weight:600}.JeOz9W_agentVersion{color:var(--dsw-alias-label-tertiary,GrayText);font-family:ui-monospace,monospace;font-size:11px}.JeOz9W_flowRow{border-bottom:1px solid var(--dsw-alias-line-normal,#80808047);padding:9px 12px}.JeOz9W_flowRow:last-child{border-bottom:0}.JeOz9W_flowName{font-size:12.5px;font-weight:600;line-height:1.45}.JeOz9W_listRow{border:0;border-bottom:1px solid var(--dsw-alias-line-normal,#80808047);width:100%;color:inherit;text-align:left;cursor:pointer;background:0 0;align-items:flex-start;gap:9px;padding:9px 12px;display:flex}.JeOz9W_listRow:last-child{border-bottom:0}.JeOz9W_listRow:hover{background:var(--dsw-alias-bg-module,Canvas)}.JeOz9W_listDot{flex:none;margin-top:4px}.JeOz9W_listMain{flex:auto;gap:1px;min-width:0;display:grid}.JeOz9W_listGoal{-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:12.5px;line-height:1.4;display:-webkit-box;overflow:hidden}.JeOz9W_back{color:var(--dsw-alias-label-tertiary,GrayText);cursor:pointer;background:0 0;border:0;padding:8px 12px 4px;font-size:11px;display:block}.JeOz9W_back:hover{color:var(--dsw-alias-label-primary,CanvasText)}.JeOz9W_detailHead{align-items:baseline;gap:8px;padding:0 12px;display:flex}.JeOz9W_detailGoal{font-size:13px;font-weight:650;line-height:1.4}.JeOz9W_detailMeta{border-bottom:1px solid var(--dsw-alias-line-normal,#80808047);color:var(--dsw-alias-label-tertiary,GrayText);padding:2px 12px 8px;font-size:11px}.JeOz9W_flowButton{width:100%;color:inherit;text-align:left;cursor:pointer;background:0 0;border-top:0;border-left:0;border-right:0;display:block}.JeOz9W_flowButton:hover{background:var(--dsw-alias-bg-module,Canvas)}.JeOz9W_prose{color:var(--dsw-alias-label-secondary,GrayText);margin:0;padding:8px 12px;font-size:12px;line-height:1.55}.JeOz9W_facts{grid-template-columns:auto 1fr;gap:3px 10px;margin:0;padding:4px 12px 10px;font-size:11.5px;display:grid}.JeOz9W_facts dt{color:var(--dsw-alias-label-tertiary,GrayText)}.JeOz9W_facts dd{margin:0}.JeOz9W_outLink{padding:0 12px 10px;font-size:11.5px;display:block}.JeOz9W_sectionLabel{border-top:1px solid var(--dsw-alias-line-normal,#80808047);color:var(--dsw-alias-label-tertiary,GrayText);padding:8px 12px 4px;font-size:11px}.JeOz9W_defnRow{border-bottom:1px solid var(--dsw-alias-line-normal,#80808047);border-left:2px solid #0000;padding:8px 12px 9px}.JeOz9W_defnRow:last-child{border-bottom:0}.JeOz9W_kind_action{border-left-color:var(--dsw-alias-state-business-primary,#5078ffb3)}.JeOz9W_kind_human{border-left-color:var(--dsw-alias-state-warning,#d29628b3)}.JeOz9W_kind_decision{border-left-color:var(--dsw-alias-label-tertiary,GrayText)}.JeOz9W_defnHead{align-items:baseline;gap:6px;min-width:0;display:flex}.JeOz9W_defnName{text-overflow:ellipsis;white-space:nowrap;flex:0 auto;min-width:0;font-size:12.5px;font-weight:600;overflow:hidden}.JeOz9W_defnKind{color:var(--dsw-alias-label-tertiary,GrayText);letter-spacing:.04em;text-transform:uppercase;flex:none;font-size:10px}.JeOz9W_defnHandler{text-overflow:ellipsis;white-space:nowrap;min-width:0;color:var(--dsw-alias-label-tertiary,GrayText);flex:0 auto;margin-left:auto;font-family:ui-monospace,monospace;font-size:11px;overflow:hidden}.JeOz9W_defnPrompt,.JeOz9W_defnNoPrompt{color:var(--dsw-alias-label-secondary,GrayText);-webkit-line-clamp:3;-webkit-box-orient:vertical;margin:3px 0 0;font-size:11.5px;line-height:1.5;display:-webkit-box;overflow:hidden}.JeOz9W_defnNoPrompt{color:var(--dsw-alias-label-tertiary,GrayText);font-style:italic}.JeOz9W_shape{align-items:center;margin-top:5px;display:flex}.JeOz9W_shapeNode{border:1px solid var(--dsw-alias-line-normal,#80808057);width:15px;height:15px;color:var(--dsw-alias-state-business-primary,#5078ffe6);border-radius:50%;flex:0 0 15px;place-items:center;font-family:ui-monospace,monospace;font-size:7.5px;font-weight:700;line-height:1;display:grid;position:relative}.JeOz9W_shapeNode+.JeOz9W_shapeNode{margin-left:8px}.JeOz9W_shapeNode+.JeOz9W_shapeNode:before{content:\"\";border-top:1px solid var(--dsw-alias-line-normal,#80808057);width:8px;position:absolute;top:50%;right:100%}.JeOz9W_node_human{color:var(--dsw-alias-state-warning,#c88c28f2)}.JeOz9W_node_terminal{color:var(--dsw-alias-state-success,#3ca05af2)}.JeOz9W_node_decision{color:var(--dsw-alias-label-secondary,GrayText)}.JeOz9W_node_more{color:var(--dsw-alias-label-tertiary,GrayText);font-size:7px}.JeOz9W_flowBlocked{color:var(--dsw-alias-state-warning,#b47d23f2);margin-left:6px;font-size:10px;font-weight:500}";
		const tagId = "@orbit-runtime/dsh-orbit/OrbitPanel.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "@orbit-runtime/dsh-orbit";
			tag.dataset.pluginCss = tagId;
			tag.textContent = css;
			document.head.appendChild(tag);
		}
		var OrbitPanel_module_css_default = {
			"actions": "JeOz9W_actions",
			"agentName": "JeOz9W_agentName",
			"agentRow": "JeOz9W_agentRow",
			"agentVersion": "JeOz9W_agentVersion",
			"attention": "JeOz9W_attention",
			"avatar": "JeOz9W_avatar",
			"back": "JeOz9W_back",
			"badge": "JeOz9W_badge",
			"bar": "JeOz9W_bar",
			"body": "JeOz9W_body",
			"catalogRow": "JeOz9W_catalogRow",
			"count": "JeOz9W_count",
			"defnHandler": "JeOz9W_defnHandler",
			"defnHead": "JeOz9W_defnHead",
			"defnKind": "JeOz9W_defnKind",
			"defnName": "JeOz9W_defnName",
			"defnNoPrompt": "JeOz9W_defnNoPrompt",
			"defnPrompt": "JeOz9W_defnPrompt",
			"defnRow": "JeOz9W_defnRow",
			"detailGoal": "JeOz9W_detailGoal",
			"detailHead": "JeOz9W_detailHead",
			"detailMeta": "JeOz9W_detailMeta",
			"done": "JeOz9W_done",
			"dot": "JeOz9W_dot",
			"empty": "JeOz9W_empty",
			"error": "JeOz9W_error",
			"facts": "JeOz9W_facts",
			"failed": "JeOz9W_failed",
			"flowBlocked": "JeOz9W_flowBlocked",
			"flowButton": "JeOz9W_flowButton",
			"flowName": "JeOz9W_flowName",
			"flowRow": "JeOz9W_flowRow",
			"goal": "JeOz9W_goal",
			"iconButton": "JeOz9W_iconButton",
			"kind_action": "JeOz9W_kind_action",
			"kind_decision": "JeOz9W_kind_decision",
			"kind_human": "JeOz9W_kind_human",
			"listDot": "JeOz9W_listDot",
			"listGoal": "JeOz9W_listGoal",
			"listMain": "JeOz9W_listMain",
			"listRow": "JeOz9W_listRow",
			"live": "JeOz9W_live",
			"meta": "JeOz9W_meta",
			"node_decision": "JeOz9W_node_decision",
			"node_human": "JeOz9W_node_human",
			"node_more": "JeOz9W_node_more",
			"node_terminal": "JeOz9W_node_terminal",
			"orbit-spin": "JeOz9W_orbit-spin",
			"outLink": "JeOz9W_outLink",
			"output": "JeOz9W_output",
			"panel": "JeOz9W_panel",
			"prose": "JeOz9W_prose",
			"resize": "JeOz9W_resize",
			"row": "JeOz9W_row",
			"sectionLabel": "JeOz9W_sectionLabel",
			"shape": "JeOz9W_shape",
			"shapeNode": "JeOz9W_shapeNode",
			"status": "JeOz9W_status",
			"stepRow": "JeOz9W_stepRow",
			"tab": "JeOz9W_tab",
			"tabActive": "JeOz9W_tabActive",
			"tabs": "JeOz9W_tabs",
			"title": "JeOz9W_title",
			"unknown": "JeOz9W_unknown"
		};
		//#endregion
		//#region src/client/panel-geometry.ts
		const PANEL_STORAGE_KEY = "orbit:panel:v1";
		const DEFAULT_PANEL_LAYOUT = Object.freeze({
			mode: "docked",
			collapsed: true,
			x: 0,
			y: 64,
			width: 400,
			height: 420
		});
		function clamp(value, low, high) {
			return value < low ? low : value > high ? high : value;
		}
		function finite(value, fallback) {
			return typeof value === "number" && Number.isFinite(value) ? value : fallback;
		}
		/**
		* Read a stored layout, keeping only what is still meaningful.
		*
		* A layout is written by a version of this panel and read by whatever version
		* runs next, so every field is treated as a suggestion: unreadable storage, a
		* renamed mode, or a width from a wider monitor all resolve to something the
		* current window can actually show rather than to a panel nobody can reach.
		*/
		function readLayout(raw) {
			if (!raw) return DEFAULT_PANEL_LAYOUT;
			let value;
			try {
				value = JSON.parse(raw);
			} catch {
				return DEFAULT_PANEL_LAYOUT;
			}
			if (value === null || typeof value !== "object" || Array.isArray(value)) return DEFAULT_PANEL_LAYOUT;
			const stored = value;
			return {
				mode: stored.mode === "floating" ? "floating" : "docked",
				collapsed: stored.collapsed !== false,
				x: finite(stored.x, DEFAULT_PANEL_LAYOUT.x),
				y: finite(stored.y, DEFAULT_PANEL_LAYOUT.y),
				width: clamp(finite(stored.width, 400), 320, 720),
				height: Math.max(finite(stored.height, 420), 280)
			};
		}
		/** The CSS box for a layout inside the bounds it has to live in. */
		function placePanel(layout, bounds) {
			const compact = bounds.width < 960;
			const maxWidth = Math.max(320, Math.min(720, bounds.width - 24));
			const width = compact ? Math.max(320, bounds.width - 24) : clamp(layout.width, 320, maxWidth);
			if (layout.mode === "docked" || compact) {
				const top = 64;
				const available = Math.max(280, bounds.height - top - 24);
				return {
					left: Math.max(12, bounds.width - width - 18),
					top,
					width,
					height: Math.min(Math.max(layout.height, 280), available)
				};
			}
			const height = clamp(layout.height, 280, Math.max(280, bounds.height - 24));
			return {
				left: clamp(layout.x, 12, Math.max(12, bounds.width - width - 12)),
				top: clamp(layout.y, 12, Math.max(12, bounds.height - height - 12)),
				width,
				height
			};
		}
		/** Move a floating panel by a drag delta, keeping it inside the overlay. */
		function dragPanel(layout, dx, dy, bounds) {
			const placed = placePanel({
				...layout,
				mode: "floating"
			}, bounds);
			return {
				...layout,
				mode: "floating",
				x: clamp(placed.left + dx, 12, Math.max(12, bounds.width - placed.width - 12)),
				y: clamp(placed.top + dy, 12, Math.max(12, bounds.height - placed.height - 12))
			};
		}
		/** Resize from the panel's left or bottom edge. */
		function resizePanel(layout, dWidth, dHeight, bounds) {
			const maxWidth = Math.max(320, Math.min(720, bounds.width - 24));
			return {
				...layout,
				width: clamp(layout.width + dWidth, 320, maxWidth),
				height: clamp(layout.height + dHeight, 280, Math.max(280, bounds.height - 24))
			};
		}
		//#endregion
		//#region src/client/orbit-model.ts
		/** Cadence while a Run is moving. */
		const ORBIT_POLL_MS = 2e3;
		/** Cadence while nothing is. A resident panel costs nothing when idle. */
		const ORBIT_IDLE_MS = 15e3;
		const TERMINAL = /* @__PURE__ */ new Set([
			"completed",
			"failed",
			"cancelled",
			"unknown"
		]);
		function isLive(status) {
			return !TERMINAL.has(status);
		}
		function toRow(run) {
			return {
				runId: run.run_id,
				goal: run.goal || run.run_id,
				workflow: `${run.workflow_id}@${String(run.workflow_version)}`,
				status: run.status,
				live: isLive(run.status),
				revision: run.revision,
				artifactCount: run.artifact_count,
				updatedAt: run.updated_at,
				commands: run.allowed_commands
			};
		}
		/** How soon to ask again, given what the last answer contained. */
		function nextInterval(rows) {
			return rows.some((row) => row.live) ? ORBIT_POLL_MS : ORBIT_IDLE_MS;
		}
		/** Newest first, with anything still running ahead of anything finished.
		*
		* A resident panel is read at a glance, and the glance is almost always about
		* what is happening now — so recency alone would bury a running Run under a
		* pile of Runs that already have their answer.
		*/
		function orderRows(rows) {
			return [...rows].sort((a, b) => {
				if (a.live !== b.live) return a.live ? -1 : 1;
				return a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0;
			});
		}
		/** A one-line count for the collapsed badge. */
		function summarise(rows) {
			return {
				live: rows.filter((row) => row.live).length,
				total: rows.length
			};
		}
		/** The four states the shell's StateDot draws, from an Orbit status.
		*
		* `unknown` is amber rather than red on purpose: it is the outcome nobody has
		* ruled on yet, and colouring it as a failure would answer a question the
		* Runtime deliberately left open.
		*/
		function dotState(status) {
			if (status === "completed") return "done";
			if (status === "unknown") return "warning";
			if (status === "failed" || status === "cancelled") return "error";
			return "ongoing";
		}
		function toStepRow(step) {
			const label = typeof step.label === "string" && step.label ? step.label : step.node_id;
			return {
				nodeId: step.node_id,
				label,
				status: step.status,
				needsPerson: step.resolution?.kind === "reconciliation_required" && step.reconciliation === void 0,
				...step.resolution?.delegation_id ? { delegationId: step.resolution.delegation_id } : {}
			};
		}
		/** Join an output page into displayable text, oldest chunk first. */
		function outputText(chunks) {
			return [...chunks].sort((a, b) => a.chunk_id - b.chunk_id).map((chunk) => chunk.text).join("");
		}
		/** Merge a new page into what is already shown without duplicating a chunk. */
		function mergeChunks(previous, next) {
			const byId = new Map(previous.map((chunk) => [chunk.chunk_id, chunk]));
			for (const chunk of next) byId.set(chunk.chunk_id, chunk);
			return [...byId.values()].sort((a, b) => a.chunk_id - b.chunk_id);
		}
		/** The revision a command may be issued at, or undefined if it may not be.
		*
		* Read from what the Run advertises rather than from what the panel last drew:
		* a button offered for a command Orbit has since withdrawn is a button that
		* fails, and one offered at a stale revision is worse — it succeeds against a
		* Run the reader was not looking at.
		*/
		function commandRevision(row, command) {
			return row.commands.find((item) => item.command === command)?.expected_version;
		}
		//#endregion
		//#region src/client/OrbitRunRow.tsx
		/** A Run as a row in a list, and the same Run as the panel's whole body.
		*
		* Two components rather than one disclosure, because a Run's detail does not
		* fit beside its siblings: opening one inline pushed the rest of the list out
		* of a 400px panel, which is the same as losing it.
		*/
		function StepDisclosure({ call, t, sessionId, runId, step, live, onSettled }) {
			const [open, setOpen] = (0, react.useState)(false);
			const [note, setNote] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [chunks, setChunks] = (0, react.useState)([]);
			const [error, setError] = (0, react.useState)("");
			(0, react.useEffect)(() => {
				if (!open) return;
				const controller = new AbortController();
				let timer;
				let after = 0;
				const tick = async () => {
					try {
						const page = await call("getStepOutput", [
							sessionId,
							runId,
							step.nodeId,
							after
						], controller.signal);
						if (controller.signal.aborted) return;
						after = page.after;
						setChunks((current) => mergeChunks(current, page.chunks));
						if (live || page.has_more) timer = setTimeout(() => {
							tick();
						}, 2e3);
					} catch (reason) {
						if (!controller.signal.aborted) setError(String(reason));
					}
				};
				tick();
				return () => {
					controller.abort();
					if (timer !== void 0) clearTimeout(timer);
				};
			}, [
				open,
				live,
				sessionId,
				runId,
				step.nodeId,
				call
			]);
			const text = outputText(chunks);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.DisclosureRow, {
				icon: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
					state: step.needsPerson ? "warning" : dotState(step.status),
					size: 8
				}),
				title: step.label,
				open,
				expandable: true,
				expandOnRowClick: true,
				onToggle: () => setOpen((value) => !value),
				collapsedContent: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: OrbitPanel_module_css_default.status,
					children: step.status
				}),
				rowClassName: OrbitPanel_module_css_default.stepRow,
				children: [
					step.needsPerson ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: OrbitPanel_module_css_default.attention,
						children: t("needsPerson")
					}), step.delegationId ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.actions,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
							value: note,
							placeholder: t("note"),
							onChange: (event) => setNote(event.currentTarget.value)
						}), ["confirmed_succeeded", "confirmed_failed"].map((outcome) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
							size: "sm",
							variant: outcome === "confirmed_succeeded" ? "primary" : "outline",
							disabled: busy,
							onClick: () => {
								setBusy(true);
								call("reconcileStep", [
									sessionId,
									runId,
									step.delegationId,
									outcome,
									note
								], new AbortController().signal).then((detail) => {
									setNote("");
									onSettled(detail.steps);
								}).catch((reason) => setError(String(reason))).finally(() => setBusy(false));
							},
							children: busy ? t("working") : t(outcome === "confirmed_succeeded" ? "confirmSucceeded" : "confirmFailed")
						}, outcome))]
					}) : null] }) : null,
					error ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: OrbitPanel_module_css_default.error,
						children: error
					}) : null,
					text ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
						className: OrbitPanel_module_css_default.output,
						children: text
					}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: OrbitPanel_module_css_default.empty,
						children: t("noOutput")
					})
				]
			});
		}
		/** A Run in a list: what it was for, and how it went. */
		function OrbitRunListRow({ t, run, onOpen }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
				type: "button",
				className: OrbitPanel_module_css_default.listRow,
				onClick: onOpen,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
						state: dotState(run.status),
						size: 9,
						className: OrbitPanel_module_css_default.listDot
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
						className: OrbitPanel_module_css_default.listMain,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.listGoal,
							children: run.goal
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.meta,
							children: run.workflow
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: OrbitPanel_module_css_default.status,
						children: run.status
					})
				]
			});
		}
		function OrbitRunDetail({ call, t, sessionId, run, onBack }) {
			const open = true;
			const [steps, setSteps] = (0, react.useState)(null);
			const [error, setError] = (0, react.useState)("");
			const [answer, setAnswer] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const cancelAt = commandRevision(run, "langgraph_run.cancel");
			const resumeAt = commandRevision(run, "langgraph_run.resume");
			const act = (command, revision, value) => {
				setBusy(true);
				setError("");
				call("runCommand", [
					sessionId,
					run.runId,
					command,
					revision,
					value,
					void 0
				], new AbortController().signal).then(() => setAnswer("")).catch((reason) => setError(String(reason))).finally(() => setBusy(false));
			};
			const load = (0, react.useCallback)((signal) => {
				call("getRunDetail", [sessionId, run.runId], signal).then((detail) => {
					if (!signal.aborted) {
						setSteps(detail.steps);
						setError("");
					}
				}).catch((reason) => {
					if (!signal.aborted) setError(String(reason));
				});
			}, [
				call,
				sessionId,
				run.runId
			]);
			(0, react.useEffect)(() => {
				const controller = new AbortController();
				load(controller.signal);
				const timer = run.live ? setInterval(() => load(controller.signal), 3e3) : void 0;
				return () => {
					controller.abort();
					if (timer !== void 0) clearInterval(timer);
				};
			}, [
				open,
				run.live,
				load
			]);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: OrbitPanel_module_css_default.back,
					onClick: onBack,
					children: t("back")
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: OrbitPanel_module_css_default.detailHead,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
						state: dotState(run.status),
						size: 9
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: OrbitPanel_module_css_default.detailGoal,
						children: run.goal
					})]
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: OrbitPanel_module_css_default.detailMeta,
					children: [
						run.workflow,
						" · ",
						run.status
					]
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: OrbitPanel_module_css_default.actions,
					children: [cancelAt !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						size: "sm",
						variant: "outline",
						disabled: busy,
						onClick: () => act("langgraph_run.cancel", cancelAt, void 0),
						children: busy ? t("working") : t("cancel")
					}) : null, resumeAt !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
						value: answer,
						placeholder: t("answer"),
						onChange: (event) => setAnswer(event.currentTarget.value)
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						size: "sm",
						variant: "primary",
						disabled: busy,
						onClick: () => act("langgraph_run.resume", resumeAt, answer),
						children: busy ? t("working") : t("resume")
					})] }) : null]
				}),
				error ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.error,
					children: error
				}) : null,
				!error && steps === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.empty,
					children: t("loading")
				}) : null,
				steps?.map((step) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StepDisclosure, {
					call,
					t,
					sessionId,
					runId: run.runId,
					step: toStepRow(step),
					live: run.live,
					onSettled: setSteps
				}, step.node_id))
			] });
		}
		//#endregion
		//#region src/client/OrbitWorkflowDetail.tsx
		/** One Workflow as the panel's body: what it needs, what it does, what it did.
		*
		* The steps are listed, not drawn. A graph answers "how do these connect",
		* which needs room this panel does not have; a list answers "what happens and
		* who does it", which is the question a reader has before starting a goal. The
		* header still links out for the picture.
		*/
		/** The input ids a caller has to supply, in the order the Workflow declares them. */
		function inputIds(workflow) {
			return Array.isArray(workflow.inputs) ? workflow.inputs.map((input) => input.id).filter((id) => typeof id === "string") : [];
		}
		/** The kinds that get their own accent; anything else reads as structure. */
		const ACCENTED = /* @__PURE__ */ new Set([
			"action",
			"human",
			"decision"
		]);
		/** The kinds that carry work, and so are the ones a missing prompt is news about. */
		const PROMPTED = /* @__PURE__ */ new Set(["action", "human"]);
		function StepRow({ t, step }) {
			const accent = ACCENTED.has(step.kind) ? step.kind : "plain";
			const prompt = step.prompt.trim();
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: `${OrbitPanel_module_css_default.defnRow} ${OrbitPanel_module_css_default[`kind_${accent}`] ?? ""}`,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: OrbitPanel_module_css_default.defnHead,
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.defnName,
							children: step.label
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.defnKind,
							children: step.kind
						}),
						step.handler ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("code", {
							className: OrbitPanel_module_css_default.defnHandler,
							children: step.handler.replace(/^agent\./, "")
						}) : null
					]
				}), prompt ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.defnPrompt,
					children: prompt
				}) : PROMPTED.has(step.kind) ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.defnNoPrompt,
					children: t("noPrompt")
				}) : null]
			});
		}
		function OrbitWorkflowDetail({ call, t, sessionId, workflow, runs, uiUrl, onBack, onOpenRun }) {
			const inputs = inputIds(workflow);
			const ran = runs.filter((run) => run.workflow.startsWith(`${workflow.workflow_id}@`));
			const ready = workflow.goal_readiness === "ready";
			const [steps, setSteps] = (0, react.useState)(null);
			const [stepsError, setStepsError] = (0, react.useState)("");
			(0, react.useEffect)(() => {
				const controller = new AbortController();
				setSteps(null);
				setStepsError("");
				call("getWorkflowDefinition", [sessionId, workflow.workflow_id], controller.signal).then((detail) => {
					if (!controller.signal.aborted) setSteps(detail.nodes);
				}).catch((reason) => {
					if (!controller.signal.aborted) setStepsError(String(reason));
				});
				return () => controller.abort();
			}, [
				call,
				sessionId,
				workflow.workflow_id
			]);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: OrbitPanel_module_css_default.back,
					onClick: onBack,
					children: t("back")
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: OrbitPanel_module_css_default.detailHead,
					children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: OrbitPanel_module_css_default.detailGoal,
						children: workflow.name || workflow.workflow_id
					})
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: OrbitPanel_module_css_default.detailMeta,
					children: [
						workflow.workflow_id,
						"@",
						String(workflow.latest_version)
					]
				}),
				workflow.description ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.prose,
					children: workflow.description
				}) : null,
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("dl", {
					className: OrbitPanel_module_css_default.facts,
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("dt", { children: t("factReadiness") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("dd", { children: [ready ? t("readyYes") : t("readyNo"), !ready && workflow.readiness_reason ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: OrbitPanel_module_css_default.meta,
							children: [" · ", workflow.readiness_reason]
						}) : null] }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("dt", { children: t("factInputs") }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("dd", { children: inputs.length ? inputs.join(", ") : t("factNone") })
					]
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("a", {
					className: OrbitPanel_module_css_default.outLink,
					href: `${uiUrl}#/workflows/${encodeURIComponent(workflow.workflow_id)}`,
					target: "_blank",
					rel: "noopener",
					children: t("openThisInOrbit")
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: OrbitPanel_module_css_default.sectionLabel,
					children: t("factSteps", { total: steps?.length ?? 0 })
				}),
				stepsError ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.error,
					children: stepsError
				}) : null,
				!stepsError && steps === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.empty,
					children: t("stepsLoading")
				}) : null,
				steps?.map((step) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StepRow, {
					t,
					step
				}, step.node_id)),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: OrbitPanel_module_css_default.sectionLabel,
					children: t("factRuns", { total: ran.length })
				}),
				ran.length ? ran.map((run) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitRunListRow, {
					t,
					run,
					onOpen: () => onOpenRun(run.runId)
				}, run.runId)) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: OrbitPanel_module_css_default.empty,
					children: t("neverRun")
				})
			] });
		}
		//#endregion
		//#region src/client/OrbitPanel.tsx
		/** The resident Orbit panel: what is running, and a way into Orbit itself. */
		async function hostCall$1(action, args, signal) {
			const response = await fetch("/plugins/dsh-orbit/api", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					action,
					args
				}),
				signal
			});
			const payload = await response.json();
			if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${String(response.status)}`);
			return payload.result;
		}
		function storeLayout(layout) {
			try {
				localStorage.setItem(PANEL_STORAGE_KEY, JSON.stringify(layout));
			} catch {}
		}
		function useBounds() {
			const [bounds, setBounds] = (0, react.useState)(() => ({
				width: typeof window === "undefined" ? 1280 : window.innerWidth,
				height: typeof window === "undefined" ? 800 : window.innerHeight
			}));
			(0, react.useEffect)(() => {
				const measure = () => setBounds({
					width: window.innerWidth,
					height: window.innerHeight
				});
				window.addEventListener("resize", measure);
				return () => window.removeEventListener("resize", measure);
			}, []);
			return bounds;
		}
		/** Two letters and a colour, derived so the same Agent always looks the same.
		*
		* Orbit gives each Agent a coloured mark; this reproduces the idea without
		* shipping a palette that would drift from it. The hue is the name's own, and
		* the colours stay inside the shell's theme by being expressed as one.
		*/
		function agentMark(name) {
			const bare = name.replace(/^agent\./u, "");
			let hash = 0;
			for (const ch of bare) hash = (hash * 31 + ch.codePointAt(0)) % 360;
			return {
				initials: bare.slice(0, 2),
				style: {
					background: `color-mix(in oklab, hsl(${String(hash)} 70% 55%) 22%, transparent)`,
					color: `hsl(${String(hash)} 70% 45%)`
				}
			};
		}
		/** One page's heading, the line under it, and anything it counts. */
		/** The letter a step wears in the chain, as Orbit's own card assigns it. */
		function glyph(kind) {
			if (kind === "terminal") return "✓";
			if (kind === "human") return "H";
			if (kind === "decision") return "?";
			return kind.slice(0, 1).toUpperCase();
		}
		const SHAPE_LIMIT = 5;
		const SHAPE_ORDER = {
			action: 0,
			human: 1,
			decision: 2,
			join: 3,
			terminal: 5
		};
		const shapeRank = (kind) => SHAPE_ORDER[kind] ?? 4;
		/**
		* A workflow's shape: its first steps as a connected chain, then how many more.
		*
		* Grouped by kind rather than laid out in graph order, which is what the
		* listing knows — a tally of kinds costs one small object, and the order would
		* cost the graph. It reads as "three actions, a decision, an end", which is
		* the size and character a reader is choosing by; the real order is one click
		* away on the detail page, where the steps are listed as they happen.
		*/
		function WorkflowShape({ kinds, total }) {
			const shown = [];
			const ranked = Object.entries(kinds).sort(([left], [right]) => shapeRank(left) - shapeRank(right));
			for (const [kind, count] of ranked) {
				for (let index = 0; index < count && shown.length < SHAPE_LIMIT; index += 1) shown.push(kind);
				if (shown.length === SHAPE_LIMIT) break;
			}
			if (!shown.length) return null;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: OrbitPanel_module_css_default.shape,
				"aria-hidden": "true",
				children: [shown.map((kind, index) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: `${OrbitPanel_module_css_default.shapeNode} ${OrbitPanel_module_css_default[`node_${kind}`] ?? ""}`,
					children: glyph(kind)
				}, index)), total > shown.length ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
					className: `${OrbitPanel_module_css_default.shapeNode} ${OrbitPanel_module_css_default.node_more}`,
					children: ["+", total - shown.length]
				}) : null]
			});
		}
		function OrbitPanel({ t, useSessions }) {
			const sessionId = useSessions((state) => state.current);
			const [layout, setLayout] = (0, react.useState)(() => {
				try {
					return readLayout(localStorage.getItem(PANEL_STORAGE_KEY));
				} catch {
					return DEFAULT_PANEL_LAYOUT;
				}
			});
			const [rows, setRows] = (0, react.useState)(null);
			const [uiUrl, setUiUrl] = (0, react.useState)("");
			const [workflows, setWorkflows] = (0, react.useState)([]);
			const [agents, setAgents] = (0, react.useState)([]);
			const [tab, setTab] = (0, react.useState)("goal");
			const [selected, setSelected] = (0, react.useState)(null);
			const [selectedFlow, setSelectedFlow] = (0, react.useState)(null);
			const [error, setError] = (0, react.useState)("");
			const [asked, setAsked] = (0, react.useState)(0);
			const [asking, setAsking] = (0, react.useState)(false);
			const forceNext = (0, react.useRef)(false);
			const bounds = useBounds();
			const drag = (0, react.useRef)(null);
			const update = (0, react.useCallback)((next) => {
				setLayout(next);
				storeLayout(next);
			}, []);
			(0, react.useEffect)(() => {
				const toggle = () => update({
					...readLayoutSafely(layout),
					collapsed: !layout.collapsed
				});
				window.addEventListener("orbit:toggle-panel", toggle);
				return () => window.removeEventListener("orbit:toggle-panel", toggle);
			}, [layout, update]);
			(0, react.useEffect)(() => {
				if (!sessionId) {
					setRows([]);
					return;
				}
				let timer;
				const controller = new AbortController();
				const tick = async () => {
					try {
						const forced = forceNext.current;
						forceNext.current = false;
						const state = await hostCall$1("getPanelState", [sessionId, forced], controller.signal);
						if (controller.signal.aborted) return;
						const next = orderRows(state.runs.map(toRow));
						setRows(next);
						setUiUrl(state.uiUrl);
						setError("");
						setWorkflows(state.workflows ?? []);
						setAgents(state.agents ?? []);
						setAsking(false);
						timer = setTimeout(() => {
							tick();
						}, layout.collapsed ? ORBIT_IDLE_MS : nextInterval(next));
					} catch (reason) {
						if (controller.signal.aborted) return;
						setError(String(reason));
						setAsking(false);
						timer = setTimeout(() => {
							tick();
						}, 15e3);
					}
				};
				tick();
				return () => {
					controller.abort();
					if (timer !== void 0) clearTimeout(timer);
				};
			}, [
				sessionId,
				layout.collapsed,
				asked
			]);
			const counts = summarise(rows ?? []);
			const chosen = (rows ?? []).find((row) => row.runId === selected);
			const chosenFlow = workflows.find((item) => item.workflow_id === selectedFlow);
			const live = (rows ?? []).filter((row) => row.live);
			const settled = (rows ?? []).filter((row) => !row.live);
			const box = placePanel(layout, bounds);
			if (layout.collapsed) return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
				type: "button",
				className: OrbitPanel_module_css_default.badge,
				style: {
					right: 18,
					bottom: 24
				},
				onClick: () => update({
					...layout,
					collapsed: false
				}),
				"aria-label": t("expand"),
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: `${OrbitPanel_module_css_default.dot} ${counts.live ? OrbitPanel_module_css_default.live : OrbitPanel_module_css_default.done}` }), counts.live ? t("liveCount", counts) : t("idleCount", counts)]
			});
			const onPointerDown = (event) => {
				if (event.target.closest("button, a, input")) return;
				drag.current = {
					x: event.clientX,
					y: event.clientY
				};
				event.currentTarget.setPointerCapture(event.pointerId);
			};
			const onPointerMove = (event) => {
				const from = drag.current;
				if (!from) return;
				drag.current = {
					x: event.clientX,
					y: event.clientY
				};
				setLayout((current) => dragPanel(current, event.clientX - from.x, event.clientY - from.y, bounds));
			};
			const onPointerUp = () => {
				drag.current = null;
				storeLayout(layout);
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: OrbitPanel_module_css_default.panel,
				style: {
					left: box.left,
					top: box.top,
					width: box.width,
					height: box.height
				},
				role: "region",
				"aria-label": t("title"),
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.bar,
						onPointerDown,
						onPointerMove,
						onPointerUp,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: OrbitPanel_module_css_default.title,
								children: t("title")
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: OrbitPanel_module_css_default.count,
								children: counts.live ? t("liveCount", counts) : t("idleCount", counts)
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: OrbitPanel_module_css_default.iconButton,
								disabled: asking,
								onClick: () => {
									forceNext.current = true;
									setAsking(true);
									setAsked((count) => count + 1);
								},
								"aria-label": t("refresh"),
								title: t("refresh"),
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconRefreshOutline16, { size: 14 })
							}),
							uiUrl ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("a", {
								className: OrbitPanel_module_css_default.iconButton,
								href: uiUrl,
								target: "_blank",
								rel: "noopener",
								"aria-label": t("openRuntime"),
								title: t("openRuntime"),
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconShareOutline16, { size: 14 })
							}) : null,
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: OrbitPanel_module_css_default.iconButton,
								onClick: () => update({
									...layout,
									collapsed: true
								}),
								"aria-label": t("collapse"),
								title: t("collapse"),
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconCloseOutline16, { size: 14 })
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("nav", {
						className: OrbitPanel_module_css_default.tabs,
						"aria-label": t("title"),
						children: [
							["goal", "tabGoal"],
							["workflows", "tabWorkflows"],
							["history", "tabHistory"],
							["agents", "tabAgents"]
						].map(([key, label]) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
							type: "button",
							className: key === tab ? `${OrbitPanel_module_css_default.tab} ${OrbitPanel_module_css_default.tabActive}` : OrbitPanel_module_css_default.tab,
							"aria-pressed": key === tab,
							onClick: () => {
								setSelected(null);
								setSelectedFlow(null);
								setTab(key);
							},
							children: t(label)
						}, key))
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: OrbitPanel_module_css_default.body,
						children: chosen !== void 0 && sessionId !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitRunDetail, {
							call: hostCall$1,
							t,
							sessionId,
							run: chosen,
							onBack: () => setSelected(null)
						}) : chosenFlow !== void 0 && sessionId !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitWorkflowDetail, {
							call: hostCall$1,
							t,
							sessionId,
							workflow: chosenFlow,
							runs: rows ?? [],
							uiUrl,
							onBack: () => setSelectedFlow(null),
							onOpenRun: (runId) => setSelected(runId)
						}) : /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [
							error ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.error,
								children: error
							}) : null,
							!error && rows === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("loading")
							}) : null,
							!error && rows !== null && tab === "goal" ? live.length ? live.map((row) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitRunListRow, {
								t,
								run: row,
								onOpen: () => setSelected(row.runId)
							}, row.runId)) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("emptyGoal")
							}) : null,
							!error && rows !== null && tab === "history" ? settled.length ? settled.map((row) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitRunListRow, {
								t,
								run: row,
								onOpen: () => setSelected(row.runId)
							}, row.runId)) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("emptyHistory")
							}) : null,
							!error && tab === "workflows" ? workflows.length ? workflows.map((item) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
								type: "button",
								className: `${OrbitPanel_module_css_default.flowRow} ${OrbitPanel_module_css_default.flowButton}`,
								onClick: () => setSelectedFlow(item.workflow_id),
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: OrbitPanel_module_css_default.flowName,
									children: [item.name || item.workflow_id, item.goal_readiness === "ready" ? null : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: OrbitPanel_module_css_default.flowBlocked,
										children: t(item.goal_readiness === "needs_migration" ? "needsMigration" : "needsUpgrade")
									})]
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(WorkflowShape, {
									kinds: item.node_kinds ?? {},
									total: item.node_count ?? 0
								})]
							}, item.workflow_id)) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("emptyWorkflows")
							}) : null,
							!error && tab === "agents" ? agents.length ? agents.map((item) => {
								const mark = agentMark(item.name);
								return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: OrbitPanel_module_css_default.agentRow,
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: OrbitPanel_module_css_default.avatar,
										style: mark.style,
										"aria-hidden": true,
										children: mark.initials
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
										className: OrbitPanel_module_css_default.agentName,
										children: item.name.replace(/^agent\./u, "")
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
										className: OrbitPanel_module_css_default.agentVersion,
										children: item.version
									})] })]
								}, item.name);
							}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("emptyAgents")
							}) : null
						] })
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: OrbitPanel_module_css_default.resize,
						onPointerDown,
						onPointerMove: (event) => {
							const from = drag.current;
							if (!from) return;
							drag.current = {
								x: event.clientX,
								y: event.clientY
							};
							setLayout((current) => resizePanel(current, event.clientX - from.x, event.clientY - from.y, bounds));
						},
						onPointerUp
					})
				]
			});
		}
		function readLayoutSafely(fallback) {
			try {
				return readLayout(localStorage.getItem(PANEL_STORAGE_KEY));
			} catch {
				return fallback;
			}
		}
		//#endregion
		//#region src/client/locales.ts
		/** Panel copy, registered with the Harness locale service. */
		const ORBIT_LOCALE_NAMESPACE = "orbit";
		const en = {
			title: "Orbit",
			expand: "Show Orbit runs",
			collapse: "Hide Orbit runs",
			openRuntime: "Open Orbit in a new tab",
			dock: "Dock to the side",
			float: "Detach",
			empty: "No runs in this Workspace yet.",
			loading: "Asking Orbit…",
			disconnected: "No Orbit Runtime is serving this Workspace.",
			liveCount: "{live} running of {total}",
			idleCount: "{total} runs",
			status: "Status",
			refresh: "Refresh",
			back: "← Back",
			factReadiness: "Can start a goal",
			factInputs: "Inputs",
			factNone: "none",
			readyYes: "Yes",
			readyNo: "Not yet",
			needsUpgrade: "Upgrade needed",
			needsMigration: "Cannot upgrade",
			factRuns: "Runs ({total})",
			neverRun: "This workflow has never run here.",
			factSteps: "Steps ({total})",
			stepsLoading: "Reading the definition…",
			noPrompt: "This step was authored without a prompt.",
			openThisInOrbit: "Open in Orbit for the graph →",
			noOutput: "No output from this step.",
			needsPerson: "Waiting for a person to confirm what the external Agent did.",
			cancel: "Cancel run",
			resume: "Continue",
			answer: "What should it use?",
			confirmSucceeded: "It succeeded",
			confirmFailed: "It failed",
			note: "What you checked",
			working: "Working…",
			tabGoal: "Goal",
			tabWorkflows: "Workflows",
			tabHistory: "History",
			tabAgents: "Agents",
			emptyGoal: "Nothing is running here.",
			emptyWorkflows: "No workflow here can start a goal yet.",
			emptyHistory: "No run has finished here yet.",
			emptyAgents: "This Runtime registered no Agent handlers.",
			togglePanel: "Show or hide the Orbit panel",
			askWhatRuns: "List the workflows that can run here",
			runHead: "Run with ",
			runTail: ": "
		};
		const zh = {
			title: "Orbit",
			expand: "显示 Orbit 运行",
			collapse: "收起 Orbit 运行",
			openRuntime: "在新标签页打开 Orbit",
			dock: "停靠到侧边",
			float: "浮动",
			empty: "这个 Workspace 还没有运行记录。",
			loading: "正在询问 Orbit…",
			disconnected: "没有 Orbit Runtime 在服务这个 Workspace。",
			liveCount: "{total} 个运行中有 {live} 个进行中",
			idleCount: "{total} 个运行",
			status: "状态",
			refresh: "刷新",
			back: "← 返回",
			factReadiness: "能否启动目标",
			factInputs: "输入",
			factNone: "无",
			readyYes: "可以",
			readyNo: "还不能",
			needsUpgrade: "需要升级",
			needsMigration: "无法升级",
			factRuns: "运行记录（{total}）",
			neverRun: "这个工作流还没有在这里跑过。",
			factSteps: "步骤（{total}）",
			stepsLoading: "正在读取定义…",
			noPrompt: "这一步没有写提示词。",
			openThisInOrbit: "在 Orbit 中查看流程图 →",
			noOutput: "这个步骤没有输出。",
			needsPerson: "等待人工确认外部 Agent 的执行结果。",
			cancel: "取消运行",
			resume: "继续",
			answer: "要用什么值继续？",
			confirmSucceeded: "确认执行成功",
			confirmFailed: "确认执行失败",
			note: "你核对了什么",
			working: "处理中…",
			tabGoal: "目标",
			tabWorkflows: "工作流",
			tabHistory: "历史",
			tabAgents: "Agents",
			emptyGoal: "这里没有正在执行的目标。",
			emptyWorkflows: "这个 Workspace 还没有能启动目标的工作流。",
			emptyHistory: "这里还没有结束的运行。",
			emptyAgents: "这个 Runtime 没有注册任何 Agent handler。",
			togglePanel: "显示或收起 Orbit 面板",
			askWhatRuns: "列出这里可运行的工作流",
			runHead: "用 ",
			runTail: " 执行："
		};
		//#endregion
		//#region src/client/index.tsx
		const PANEL_COMMAND = "orbit";
		const LIST_COMMAND = "orbit-workflows";
		/** `/orbit` folds the resident panel; it never opens a second one. */
		function registerOrbitSlashSource(ctx, t) {
			const inputTriggers = ctx.get("inputTriggers");
			if (!inputTriggers) throw new Error("Orbit /orbit requires the Harness inputTriggers service");
			const claim = () => ({
				token: `/${PANEL_COMMAND}`,
				submit: async (args) => {
					if (args.trim()) return {
						kind: "error",
						text: "/orbit takes no argument; it shows or hides the Orbit panel"
					};
					window.dispatchEvent(new Event("orbit:toggle-panel"));
					return { kind: "success" };
				}
			});
			ctx.effect(() => inputTriggers.registerSource({
				trigger: "/",
				name: "orbit",
				order: -10,
				showGroupTitle: false,
				candidates: async (_session, request) => PANEL_COMMAND.includes(request.query.toLowerCase()) ? [{
					name: PANEL_COMMAND,
					description: t("togglePanel")
				}] : [],
				onPick: () => ({ claim: claim() }),
				matchSpace: (_session, token) => token === `/${PANEL_COMMAND}` ? { claim: claim() } : void 0,
				matchEnter: async (_session, line) => new RegExp(`^/${PANEL_COMMAND}(?:\\s|$)`, "u").test(line.trim()) ? { claim: claim() } : void 0,
				codec: {
					clipboardText: (ref) => `「${namesById.get(ref) ?? ref}」`,
					serialize: async (ref) => {
						const name = namesById.get(ref);
						return name === void 0 ? ref : `${ref}（${name}）`;
					}
				}
			}), "orbit: slash command folding the panel");
		}
		/**
		* Names for the ids a reference carries, learned when the popup lists them.
		*
		* The codec is handed a `ref` and nothing else, and a `ref` is the id — which
		* is the right thing to send the model and the wrong thing to show a person.
		* Remembering the pair at list time is what lets the chip be both.
		*/
		const namesById = /* @__PURE__ */ new Map();
		async function hostCall(action, args, signal) {
			const response = await fetch("/plugins/dsh-orbit/api", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({
					action,
					args
				}),
				signal
			});
			const payload = await response.json();
			if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${String(response.status)}`);
			return payload.result;
		}
		/**
		* `/orbit-workflows` opens the shell's own popup — the one `/model` uses.
		*
		* Selecting writes the request into the draft for the person to finish. A
		* popupSelect is one list and one pick, with nowhere to put the goal these
		* Workflows declare an input for — and the Run has to be the Agent's, or it
		* cannot report on it afterwards.
		*/
		function registerWorkflowPopup(ctx, t) {
			const commandUi = ctx.get("commandUi");
			if (!commandUi) return;
			ctx.effect(() => commandUi.register({
				name: LIST_COMMAND,
				description: t("askWhatRuns"),
				available: () => true,
				ui: {
					kind: "popupSelect",
					options: async (session, signal) => {
						return ((await hostCall("getPanelState", [session.sessionId], signal)).workflows ?? []).map((item) => {
							const label = item.name || item.workflow_id;
							namesById.set(item.workflow_id, label);
							return {
								id: item.workflow_id,
								label,
								detail: `${item.workflow_id}@${String(item.latest_version)}`
							};
						});
					},
					onSelect: (option, session) => {
						const conversation = ctx.get("conversation");
						const actx = ctx.get("sessions")?.scope(session.sessionId);
						if (!conversation || actx === void 0) return;
						const input = conversation.input.for(actx);
						const head = t("runHead");
						input.setDraft(`${head}${t("runTail")}`);
						if (!input.insertReference({
							source: "orbit",
							ref: option.id,
							label: option.label,
							clipboardText: `「${option.label}」`
						}, {
							start: head.length,
							end: head.length,
							draftRev: input.state.getSnapshot().draftRev
						})) input.setDraft(`${head}「${option.label}」${t("runTail")}`);
					}
				}
			}), "orbit: workflow popup");
		}
		const inject = [
			"inputTriggers",
			"slots",
			"locale",
			"commandUi"
		];
		function apply(ctx) {
			ctx.effect(() => ctx.locale.register(ORBIT_LOCALE_NAMESPACE, {
				zh,
				en
			}), "orbit: dictionaries");
			const t = ctx.locale.bind(ORBIT_LOCALE_NAMESPACE);
			registerOrbitSlashSource(ctx, t);
			registerWorkflowPopup(ctx, t);
			const Panel = ({ t, useSessions }) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitPanel, {
				t,
				useSessions
			});
			ctx.slots.inject("shell.overlay", () => ctx.slots.register({
				name: "shell.overlay",
				id: "orbit-runs",
				order: 80,
				label: "Orbit runs",
				locale: ORBIT_LOCALE_NAMESPACE
			}, Panel));
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map