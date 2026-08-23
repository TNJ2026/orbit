window.__ModuleLoader__.load({
	id: "@orbit-runtime/dsh-orbit",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		//#region lib/client.js
		/**
		* `/orbit` opens Orbit's own Runtime UI inside the Harness window.
		*
		* The panel holds an iframe and nothing else. Every earlier version of this
		* module drew Orbit's data itself — a Run drawer, a Workflow picker, a
		* settings page — and each was a second account of something Orbit already
		* renders. Showing Orbit's own page instead means there is one interface, and
		* this module's whole job is deciding when it is visible.
		*
		* @module @orbit-runtime/dsh-orbit/client
		*/
		/** How long a frame may stay blank before we assume the Host refused to embed it. */
		const EMBED_TIMEOUT_MS = 6e3;
		async function orbitHostCall(action, args, signal) {
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
			if (!response.ok || payload.error) throw new Error(payload.error || `Orbit Host API failed with HTTP ${String(response.status)}`);
			return payload.result;
		}
		const openEvent = "orbit:panel";
		const openKey = (sessionId) => `orbit.panel.${sessionId}`;
		function readOpen(sessionId) {
			try {
				return sessionStorage.getItem(openKey(sessionId)) === "1";
			} catch {
				return false;
			}
		}
		function writeOpen(sessionId, open) {
			try {
				if (open) sessionStorage.setItem(openKey(sessionId), "1");
				else sessionStorage.removeItem(openKey(sessionId));
			} catch {}
			window.dispatchEvent(new CustomEvent(openEvent, { detail: { sessionId } }));
		}
		const overlayStyle = {
			position: "fixed",
			inset: "0 0 0 auto",
			width: "min(1100px, 70vw)",
			zIndex: 1200,
			display: "flex",
			flexDirection: "column",
			background: "var(--background, #fff)",
			borderLeft: "1px solid #8884",
			boxShadow: "-12px 0 30px #0003"
		};
		const barStyle = {
			display: "flex",
			alignItems: "center",
			justifyContent: "space-between",
			gap: 12,
			padding: "10px 16px",
			borderBottom: "1px solid #8884",
			flex: "0 0 auto"
		};
		const frameStyle = {
			flex: "1 1 auto",
			width: "100%",
			border: 0
		};
		function OrbitPanel({ sessionId, url, onClose }) {
			const [loaded, setLoaded] = (0, react.useState)(false);
			const [blocked, setBlocked] = (0, react.useState)(false);
			const closeRef = (0, react.useRef)(null);
			(0, react.useEffect)(() => {
				closeRef.current?.focus();
				const escape = (event) => {
					if (event.key === "Escape") onClose();
				};
				document.addEventListener("keydown", escape);
				return () => document.removeEventListener("keydown", escape);
			}, [onClose]);
			(0, react.useEffect)(() => {
				if (loaded) return;
				const timer = setTimeout(() => setBlocked(true), EMBED_TIMEOUT_MS);
				return () => clearTimeout(timer);
			}, [loaded, url]);
			return (0, react.createElement)("section", {
				role: "dialog",
				"aria-label": "Orbit Runtime",
				style: overlayStyle
			}, (0, react.createElement)("div", { style: barStyle }, (0, react.createElement)("strong", null, "Orbit Runtime"), (0, react.createElement)("span", { style: {
				flex: 1,
				opacity: .6,
				fontSize: 12
			} }, url), blocked && !loaded ? (0, react.createElement)("a", {
				href: url,
				target: "_blank",
				rel: "noopener"
			}, "在新标签页打开") : null, (0, react.createElement)("button", {
				ref: closeRef,
				type: "button",
				onClick: onClose
			}, "关闭")), (0, react.createElement)("iframe", {
				key: sessionId,
				src: url,
				style: frameStyle,
				title: "Orbit Runtime",
				onLoad: () => {
					setLoaded(true);
					setBlocked(false);
				}
			}));
		}
		function OrbitOverlay({ sessionId }) {
			const id = String(sessionId);
			const [open, setOpen] = (0, react.useState)(() => readOpen(id));
			const [url, setUrl] = (0, react.useState)("");
			const [error, setError] = (0, react.useState)("");
			(0, react.useEffect)(() => {
				const refresh = (event) => {
					const detail = event.detail;
					if (!detail?.sessionId || detail.sessionId === id) setOpen(readOpen(id));
				};
				window.addEventListener(openEvent, refresh);
				window.addEventListener("storage", refresh);
				return () => {
					window.removeEventListener(openEvent, refresh);
					window.removeEventListener("storage", refresh);
				};
			}, [id]);
			(0, react.useEffect)(() => {
				if (!open || url) return;
				const controller = new AbortController();
				orbitHostCall("getRuntimeUi", [id], controller.signal).then(setUrl).catch((reason) => {
					if (!controller.signal.aborted) setError(String(reason));
				});
				return () => controller.abort();
			}, [
				open,
				url,
				id
			]);
			const close = () => {
				writeOpen(id, false);
				setError("");
			};
			if (!open) return null;
			if (error) return (0, react.createElement)("section", {
				role: "alert",
				style: overlayStyle
			}, (0, react.createElement)("div", { style: barStyle }, (0, react.createElement)("strong", null, "Orbit Runtime"), (0, react.createElement)("button", {
				type: "button",
				onClick: close
			}, "关闭")), (0, react.createElement)("p", { style: { padding: 16 } }, error));
			if (!url) return null;
			return (0, react.createElement)(OrbitPanel, {
				sessionId: id,
				url,
				onClose: close
			});
		}
		function registerOrbitSlashSource(ctx) {
			const inputTriggers = ctx.get("inputTriggers");
			if (!inputTriggers) throw new Error("Orbit /orbit requires the Harness inputTriggers service");
			const claim = (sessionId) => ({
				token: "/orbit",
				submit: async (args) => {
					if (args.trim()) return {
						kind: "error",
						text: "/orbit takes no argument; it opens the Orbit Runtime UI"
					};
					writeOpen(String(sessionId), true);
					return { kind: "success" };
				}
			});
			const candidate = {
				name: "orbit",
				description: "open the Orbit Runtime UI for this Workspace"
			};
			ctx.effect(() => inputTriggers.registerSource({
				trigger: "/",
				name: "orbit",
				order: -10,
				showGroupTitle: false,
				candidates: async (_session, request) => "orbit".includes(request.query.toLowerCase()) ? [candidate] : [],
				onPick: (pick) => ({ claim: claim(String(pick.session.sessionId)) }),
				matchSpace: (session, token) => token === "/orbit" ? { claim: claim(String(session.sessionId)) } : void 0,
				matchEnter: async (session, line) => /^\/orbit(?:\s|$)/u.test(line.trim()) ? { claim: claim(String(session.sessionId)) } : void 0
			}), "orbit: slash command opening the Runtime UI");
		}
		const inject = ["inputTriggers", "slots"];
		function apply(ctx) {
			registerOrbitSlashSource(ctx);
			ctx.slots.inject("conversation.input.overlay", () => ctx.slots.register({
				name: "conversation.input.overlay",
				id: "orbit-runtime-panel",
				order: 100
			}, (props) => (0, react.createElement)(OrbitOverlay, props)));
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		exports.registerOrbitSlashSource = registerOrbitSlashSource;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map