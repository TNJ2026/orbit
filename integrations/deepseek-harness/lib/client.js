window.__ModuleLoader__.load({
	id: "@orbit-runtime/dsh-orbit",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		//#region lib/client.js
		/**
		* The only thing this bundle puts in front of a person: a way to leave.
		*
		* `/orbit` takes no argument and renders nothing. Orbit's own Runtime UI is
		* where Runs are read and driven, so the command's whole job is to open it —
		* anything more would be a second interface competing with the first.
		*
		* @module @orbit-runtime/dsh-orbit/client
		*/
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
					const controller = new AbortController();
					try {
						const url = await orbitHostCall("getRuntimeUi", [sessionId], controller.signal);
						if (!window.open(url, "_blank", "noopener")) return {
							kind: "error",
							text: `Allow pop-ups for this site, or open ${url}`
						};
						return { kind: "success" };
					} catch (reason) {
						return {
							kind: "error",
							text: String(reason)
						};
					}
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
		const inject = ["inputTriggers"];
		function apply(ctx) {
			registerOrbitSlashSource(ctx);
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		exports.registerOrbitSlashSource = registerOrbitSlashSource;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map