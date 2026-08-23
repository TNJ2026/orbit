window.__ModuleLoader__.load({
	id: "@orbit-runtime/dsh-orbit",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region \0dsh-css:/Users/cxd/develop/orbit/integrations/deepseek-harness/src/client/OrbitPanel.module.css.mjs
		const css = ".JeOz9W_panel{border:1px solid var(--dsw-alias-line-normal);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary);pointer-events:auto;border-radius:12px;flex-direction:column;display:flex;position:absolute;overflow:hidden;box-shadow:0 12px 40px #0000002e}.JeOz9W_bar{border-bottom:1px solid var(--dsw-alias-line-normal);background:var(--dsw-alias-bg-module);cursor:grab;user-select:none;align-items:center;gap:8px;padding:8px 10px 8px 12px;display:flex}.JeOz9W_bar:active{cursor:grabbing}.JeOz9W_title{font-size:13px;font-weight:600}.JeOz9W_count{color:var(--dsw-alias-label-tertiary);flex:1;font-size:12px}.JeOz9W_body{flex:1;min-height:0;overflow-y:auto}.JeOz9W_row{border-bottom:1px solid var(--dsw-alias-line-normal);grid-template-columns:8px 1fr auto;align-items:start;gap:8px;padding:10px 12px;display:grid}.JeOz9W_row:last-child{border-bottom:0}.JeOz9W_dot{border-radius:50%;width:8px;height:8px;margin-top:5px}.JeOz9W_live{background:var(--dsw-alias-state-business-primary)}.JeOz9W_done{background:var(--dsw-alias-state-success)}.JeOz9W_failed{background:var(--dsw-alias-state-danger)}.JeOz9W_unknown{background:var(--dsw-alias-state-warning,var(--dsw-alias-label-tertiary))}.JeOz9W_goal{-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:13px;line-height:1.4;display:-webkit-box;overflow:hidden}.JeOz9W_meta{color:var(--dsw-alias-label-tertiary);font-size:11px}.JeOz9W_status{color:var(--dsw-alias-label-secondary);white-space:nowrap;font-size:11px}.JeOz9W_empty,.JeOz9W_error{color:var(--dsw-alias-label-tertiary);text-align:center;padding:20px 14px;font-size:12px}.JeOz9W_error{color:var(--dsw-alias-state-error-primary);text-align:left}.JeOz9W_badge{border:1px solid var(--dsw-alias-line-normal);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-secondary);cursor:pointer;pointer-events:auto;border-radius:999px;align-items:center;gap:6px;padding:6px 12px;font-size:12px;display:flex;position:absolute;box-shadow:0 6px 20px #00000024}.JeOz9W_badge:hover{color:var(--dsw-alias-label-primary)}.JeOz9W_resize{cursor:nwse-resize;width:14px;height:14px;position:absolute;inset:auto 0 0 auto}";
		const tagId = "@orbit-runtime/dsh-orbit/OrbitPanel.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "@orbit-runtime/dsh-orbit";
			tag.dataset.pluginCss = tagId;
			tag.textContent = css;
			document.head.appendChild(tag);
		}
		var OrbitPanel_module_css_default = {
			"badge": "JeOz9W_badge",
			"bar": "JeOz9W_bar",
			"body": "JeOz9W_body",
			"count": "JeOz9W_count",
			"done": "JeOz9W_done",
			"dot": "JeOz9W_dot",
			"empty": "JeOz9W_empty",
			"error": "JeOz9W_error",
			"failed": "JeOz9W_failed",
			"goal": "JeOz9W_goal",
			"live": "JeOz9W_live",
			"meta": "JeOz9W_meta",
			"panel": "JeOz9W_panel",
			"resize": "JeOz9W_resize",
			"row": "JeOz9W_row",
			"status": "JeOz9W_status",
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
			height: 620
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
				height: Math.max(finite(stored.height, 620), 280)
			};
		}
		/** The CSS box for a layout inside the bounds it has to live in. */
		function placePanel(layout, bounds) {
			const compact = bounds.width < 960;
			const maxWidth = Math.max(320, Math.min(720, bounds.width - 24));
			const width = compact ? Math.max(320, bounds.width - 24) : clamp(layout.width, 320, maxWidth);
			if (layout.mode === "docked" || compact) {
				const top = 64;
				return {
					left: Math.max(12, bounds.width - width - 18),
					top,
					width,
					height: Math.max(280, bounds.height - top - 24)
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
				updatedAt: run.updated_at
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
		//#endregion
		//#region src/client/OrbitPanel.tsx
		/** The resident Orbit panel: what is running, and a way into Orbit itself. */
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
		function statusClass(row) {
			if (row.live) return OrbitPanel_module_css_default.live;
			if (row.status === "completed") return OrbitPanel_module_css_default.done;
			if (row.status === "unknown") return OrbitPanel_module_css_default.unknown;
			return OrbitPanel_module_css_default.failed;
		}
		function OrbitPanel({ t, sessionId }) {
			const [layout, setLayout] = (0, react.useState)(() => {
				try {
					return readLayout(localStorage.getItem(PANEL_STORAGE_KEY));
				} catch {
					return DEFAULT_PANEL_LAYOUT;
				}
			});
			const [rows, setRows] = (0, react.useState)(null);
			const [uiUrl, setUiUrl] = (0, react.useState)("");
			const [error, setError] = (0, react.useState)("");
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
				if (layout.collapsed && rows !== null) return;
				if (!sessionId) {
					setRows([]);
					return;
				}
				let timer;
				const controller = new AbortController();
				const tick = async () => {
					try {
						const state = await hostCall("getPanelState", [sessionId], controller.signal);
						if (controller.signal.aborted) return;
						const next = orderRows(state.runs.map(toRow));
						setRows(next);
						setUiUrl(state.uiUrl);
						setError("");
						timer = setTimeout(() => {
							tick();
						}, nextInterval(next));
					} catch (reason) {
						if (controller.signal.aborted) return;
						setError(String(reason));
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
				rows === null
			]);
			const counts = summarise(rows ?? []);
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
							uiUrl ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("a", {
								href: uiUrl,
								target: "_blank",
								rel: "noopener",
								children: t("openRuntime")
							}) : null,
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								onClick: () => update({
									...layout,
									collapsed: true
								}),
								"aria-label": t("collapse"),
								children: "×"
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.body,
						children: [
							error ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.error,
								children: error
							}) : null,
							!error && rows === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("loading")
							}) : null,
							!error && rows?.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("empty")
							}) : null,
							rows?.map((row) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", {
								className: OrbitPanel_module_css_default.row,
								children: [
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: `${OrbitPanel_module_css_default.dot} ${statusClass(row)}` }),
									/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
										className: OrbitPanel_module_css_default.goal,
										children: row.goal
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
										className: OrbitPanel_module_css_default.meta,
										children: row.workflow
									})] }),
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: OrbitPanel_module_css_default.status,
										children: row.status
									})
								]
							}, row.runId))
						]
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
			openRuntime: "Open in Orbit",
			dock: "Dock to the side",
			float: "Detach",
			empty: "No runs in this Workspace yet.",
			loading: "Asking Orbit…",
			disconnected: "No Orbit Runtime is serving this Workspace.",
			liveCount: "{live} running of {total}",
			idleCount: "{total} runs",
			status: "Status",
			refresh: "Refresh"
		};
		const zh = {
			title: "Orbit",
			expand: "显示 Orbit 运行",
			collapse: "收起 Orbit 运行",
			openRuntime: "在 Orbit 中打开",
			dock: "停靠到侧边",
			float: "浮动",
			empty: "这个 Workspace 还没有运行记录。",
			loading: "正在询问 Orbit…",
			disconnected: "没有 Orbit Runtime 在服务这个 Workspace。",
			liveCount: "{total} 个运行中有 {live} 个进行中",
			idleCount: "{total} 个运行",
			status: "状态",
			refresh: "刷新"
		};
		//#endregion
		//#region src/client/index.tsx
		/** `/orbit` folds the resident panel open or shut; it never opens a second one. */
		function registerOrbitSlashSource(ctx) {
			const inputTriggers = ctx.get("inputTriggers");
			if (!inputTriggers) throw new Error("Orbit /orbit requires the Harness inputTriggers service");
			const claim = () => ({
				token: "/orbit",
				submit: async (args) => {
					if (args.trim()) return {
						kind: "error",
						text: "/orbit takes no argument; it shows or hides the Orbit panel"
					};
					window.dispatchEvent(new Event("orbit:toggle-panel"));
					return { kind: "success" };
				}
			});
			const candidate = {
				name: "orbit",
				description: "show or hide the Orbit panel"
			};
			ctx.effect(() => inputTriggers.registerSource({
				trigger: "/",
				name: "orbit",
				order: -10,
				showGroupTitle: false,
				candidates: async (_session, request) => "orbit".includes(request.query.toLowerCase()) ? [candidate] : [],
				onPick: () => ({ claim: claim() }),
				matchSpace: (_session, token) => token === "/orbit" ? { claim: claim() } : void 0,
				matchEnter: async (_session, line) => /^\/orbit(?:\s|$)/u.test(line.trim()) ? { claim: claim() } : void 0
			}), "orbit: slash command folding the panel");
		}
		const inject = [
			"inputTriggers",
			"slots",
			"locale"
		];
		function apply(ctx) {
			ctx.effect(() => ctx.locale.register(ORBIT_LOCALE_NAMESPACE, {
				zh,
				en
			}), "orbit: dictionaries");
			registerOrbitSlashSource(ctx);
			const Panel = ({ t, sessionId }) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitPanel, {
				t,
				sessionId
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