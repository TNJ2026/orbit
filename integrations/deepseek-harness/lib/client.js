window.__ModuleLoader__.load({
	id: "@orbit-runtime/dsh-orbit",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		let _deepseek_ai_dsh_client_ui_primitives = require("@deepseek-ai/dsh-client-ui-primitives");
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region ../../integration-core/lib/authoring-progress.js
		/** How far a Workflow being written has got, read from what it printed.
		*
		* Authoring has stages the way a Run has steps — it drafts, it compiles what
		* came back, it goes round again when the compiler refuses, and it publishes —
		* and the Runtime has always said so: `AuthoringJobService` writes a marker
		* into the job's console at each turn. Nothing read them. The panel matched
		* them only to drop them, so a job that spent a minute on its second attempt
		* showed one unchanging line, and the one question a person watching has —
		* *is it stuck or is it working* — had no answer on the page.
		*
		* A marker is a whole chunk whose text is the sentinel followed by JSON, so
		* this reads chunks rather than scanning text: an Agent that prints the
		* sentinel itself is printing inside a chunk of its own output, not writing a
		* marker, and must not be able to move the ladder.
		*/
		const SENTINEL = "orbit-progress:";
		/** The three things authoring does. Repairing is not among them — see below. */
		const AUTHORING_STAGES = [
			"generating",
			"validating",
			"publishing"
		];
		const LANDS_ON = {
			generating: 0,
			repairing: 0,
			validating: 1,
			validated: 2,
			publishing: 2
		};
		/** Whether this chunk is a progress marker rather than Agent output. */
		function isProgressMarker(chunk) {
			return chunk.text.startsWith(SENTINEL);
		}
		function marker(chunk) {
			if (!isProgressMarker(chunk)) return null;
			try {
				const value = JSON.parse(chunk.text.slice(16));
				const stage = typeof value.stage === "string" ? value.stage : "";
				if (!(stage in LANDS_ON)) return null;
				return {
					stage,
					attempt: typeof value.attempt === "number" ? value.attempt : 0,
					maxAttempts: typeof value.max_attempts === "number" ? value.max_attempts : 0
				};
			} catch {
				return null;
			}
		}
		/**
		* The ladder, from the markers a job has printed and the state it is in.
		*
		* The job's own status has the last word on the stages: a job that failed
		* failed at whatever rung it had reached, and one that is done reached all of
		* them — the markers stop when the process does, so a job killed mid-stage
		* would otherwise show that stage running for as long as anyone looked at it.
		*/
		function authoringProgress(chunks, jobStatus) {
			let reached = -1;
			let attempt = 0;
			let maxAttempts = 0;
			for (const chunk of chunks) {
				const found = marker(chunk);
				if (found === null) continue;
				reached = LANDS_ON[found.stage] ?? reached;
				if (found.attempt) attempt = found.attempt;
				if (found.maxAttempts) maxAttempts = found.maxAttempts;
			}
			const settled = jobStatus === "done" || jobStatus === "failed" || jobStatus === "cancelled";
			return {
				stages: AUTHORING_STAGES.map((stage, index) => {
					if (jobStatus === "done") return {
						stage,
						status: "succeeded"
					};
					if (index < reached) return {
						stage,
						status: "succeeded"
					};
					if (index > reached) return {
						stage,
						status: "not_reached"
					};
					return {
						stage,
						status: settled ? "failed" : "running"
					};
				}),
				attempt,
				maxAttempts
			};
		}
		//#endregion
		//#region ../../integration-core/lib/error-text.js
		const READINGS = [
			[/No independent Orbit Runtime is serving/i, "errNoRuntime"],
			[/auto-start (failed|timed out)/i, "errStartFailed"],
			[/Runtime discovery (failed|returned invalid JSON|must return an array)/i, "errDiscoveryFailed"],
			[/Multiple Orbit Runtimes claim/i, "errRuntimeConflict"],
			[/not reachable over HTTP MCP|published no HTTP address|did not publish a browser address/i, "errRuntimeAddress"],
			[/incompatible Orbit integration protocol/i, "errVersionMismatch"],
			[/only a Runtime operator|valid actor credentials|HTTP 40[13]/i, "errNotAllowed"],
			[/refused to stop/i, "errStopRefused"],
			[/timed out/i, "errTimeout"],
			[/transport failed|MCP HTTP|HTTP 5\d\d|failed with HTTP/i, "errUnreachable"],
			[/Failed to fetch|fetch failed|NetworkError|Load failed|ECONNREFUSED|HTTP 40[04]/i, "errHostGone"],
			[/Workspace cwd/i, "errNoWorkspace"],
			[/requires? a live Harness|invalid Harness session id/i, "errNoSession"],
			[/Workspace does not match the Harness Session|has not registered/i, "errWorkspaceMismatch"],
			[/workflow was deleted/i, "errWorkflowDeleted"],
			[/workflow (version )?not found/i, "errWorkflowGone"],
			[/no longer (offers|advertis(es|ed))/i, "errRunMoved"],
			[/run not found|LangGraph run not found/i, "errRunGone"],
			[/ActiveGoalExists|active_goal_exists/i, "errGoalActive"],
			[/workflow_generation_already_active/i, "errAuthoringActive"],
			[/too large for MCP content proxy/i, "errArtifactTooLarge"],
			[/no live Agent for Session|exposes no Agent registry/i, "errNoAgent"],
			[/was started elsewhere/i, "errRunElsewhere"],
			[/exceeds 256 KiB/i, "errRequestTooLarge"],
			[/must be 1-20000 characters/i, "errPromptLength"],
			[/supports images only/i, "errImageOnly"],
			[/request aborted|operation was aborted|AbortError/i, "errAborted"],
			[/produced no answer to submit/i, "errNoAnswer"],
			[/invalid Orbit DTO|not canonical base64|arguments must be an object/i, "errProtocol"],
			[/Unknown Orbit client action|requires action and args|Workflow id is required/i, "errProtocol"],
			[/invalid authoring output (cursor|address)|authoring output returned invalid JSON/i, "errProtocol"],
			[/returned an invalid authoring output address/i, "errProtocol"]
		];
		/**
		* Read one failure into something worth showing.
		*
		* An unrecognised failure is not dressed up as a known one: it says that
		* something went wrong and carries its own text, which is honest and still
		* lets the reader copy it. Guessing here would be worse than saying nothing —
		* a wrong diagnosis sends somebody to fix the wrong thing.
		*/
		function panelError(reason) {
			const detail = textOf(reason);
			for (const [pattern, key] of READINGS) if (pattern.test(detail)) return {
				key,
				detail
			};
			return {
				key: "errUnknown",
				detail
			};
		}
		function textOf(reason) {
			if (typeof reason === "string") return reason;
			if (reason instanceof Error) return reason.message;
			if (reason !== null && typeof reason === "object") {
				const held = reason;
				for (const value of [held.error, held.message]) if (typeof value === "string" && value) return value;
			}
			return String(reason);
		}
		//#endregion
		//#region ../../integration-core/lib/run-progress.js
		/** How far a Run has got, derived once for everywhere that says it.
		*
		* The panel answers "where is this now" in three places — the Host, when it
		* decides which Runs are worth reading steps for; the list line under a Run;
		* the bar above its steps — and two of those disagreeing is worse than either
		* being absent, because a reader has no way to tell which one lied. So the
		* vocabulary of statuses lives here, once, beside the arithmetic that reads it.
		*/
		/** Run statuses there is no coming back from. */
		const TERMINAL = /* @__PURE__ */ new Set([
			"completed",
			"failed",
			"cancelled",
			"unknown"
		]);
		/** Whether a Run could still do something. */
		function isLive(status) {
			return !TERMINAL.has(status);
		}
		/** The step's authored name, or the node id it was authored without one. */
		function labelOf(step) {
			return typeof step.label === "string" && step.label ? step.label : step.node_id;
		}
		/**
		* The Runs the Goal page shows: everything still moving, or the one that
		* moved last when nothing is.
		*
		* A Goal reaching its end is the moment its result matters most, and dropping
		* it from the page right then answered "what happened" with an empty page —
		* the reader watched four steps go green and was left looking at "nothing is
		* running here". So a finished Goal stays, with its steps and its outcome,
		* until the next one starts and takes the page.
		*
		* Shared because the Host reads steps for exactly the Runs this page draws. A
		* Host that kept its own idea of that would go on serving a settled Run's last
		* *running* step forever, since the step read stops with the Run.
		*/
		function goalRuns(rows) {
			const live = rows.filter((row) => row.live);
			if (live.length) return live;
			const latest = rows.reduce((best, row) => best === void 0 || row.updatedAt > best.updatedAt ? row : best, void 0);
			return latest === void 0 ? [] : [latest];
		}
		//#endregion
		//#region ../../integration-core/lib/orbit-model.js
		/** What the panel shows, and how often it asks.
		*
		* Separated from the view because the interesting decisions here are not
		* visual: which Runs count as live, how fast to ask while any of them is, and
		* how to stop asking when none is. React only renders the answer.
		*/
		/** Cadence while a Run is moving. */
		const ORBIT_POLL_MS = 2e3;
		/** Cadence while nothing is. A resident panel costs nothing when idle. */
		const ORBIT_IDLE_MS = 15e3;
		/** Read the interrupts a Run advertises, keeping only the ones answerable here.
		*
		* An interrupt with no output port is a question this panel cannot form an
		* answer to — the reply would have to name a port, and guessing one produces a
		* node that "returned undeclared outputs" minutes later. */
		function toInterrupts(items) {
			const found = [];
			for (const item of items ?? []) {
				if (item === null || typeof item !== "object") continue;
				const held = item;
				const value = held.value;
				if (value === null || typeof value !== "object") continue;
				const asked = value;
				const first = (Array.isArray(asked.output_ports) ? asked.output_ports : [])[0];
				const config = asked.config ?? {};
				if (typeof held.id !== "string" || typeof first?.id !== "string") continue;
				found.push({
					id: held.id,
					nodeId: typeof asked.node_id === "string" ? asked.node_id : "",
					taskKind: typeof config.task_kind === "string" ? config.task_kind : "",
					outputPort: first.id
				});
			}
			return found;
		}
		/**
		* The answer to a yes/no question, in the shape the next step will read.
		*
		* A Mapping handed to `resume` *is* the node's outputs, so the port has to be
		* named — replying with a bare `{decision: …}` would look for a port called
		* `decision` and fail as a node returning something it never declared. And
		* `decision` is the field the branches test: `source.result.decision == …` is
		* how every approval workflow here routes.
		*
		* Which is why this is built rather than typed. A person asked to approve
		* something was being handed a text box and expected to know both of those
		* facts — and to spell them without a typo, minutes after the question.
		*/
		function approvalValue(interrupt, decision) {
			return { [interrupt.outputPort]: { decision } };
		}
		function toRow(run, workflowName) {
			return {
				runId: run.run_id,
				goal: run.goal || run.run_id,
				workflow: `${run.workflow_id}@${String(run.workflow_version)}`,
				workflowName: workflowName || run.workflow_id,
				status: run.status,
				live: isLive(run.status),
				revision: run.revision,
				artifactCount: run.artifact_count,
				updatedAt: run.updated_at,
				prompt: promptText(run.inputs),
				result: run.result,
				...typeof run.error === "string" && run.error ? { error: run.error } : {},
				commands: run.allowed_commands,
				interrupts: toInterrupts(run.interrupts)
			};
		}
		/**
		* What a Run was asked to work on, as something a person can read.
		*
		* The same shape of question as `resultText` and answered the same way: a lone
		* string input is the request, so it is shown as written; anything else is
		* printed, because a workflow taking three inputs has no one of them that is
		* "the prompt" and picking one would hide the other two.
		*
		* Not the goal. A goal is a label somebody put on the work — for a Run an
		* Agent started it is usually a sentence about the request rather than the
		* request — and a reader shown only the label cannot see what was asked.
		*/
		function promptText(inputs) {
			if (inputs === null || typeof inputs !== "object" || Array.isArray(inputs)) return "";
			const entries = Object.entries(inputs);
			if (!entries.length) return "";
			const [only] = entries;
			if (entries.length === 1 && only !== void 0 && typeof only[1] === "string") return only[1];
			try {
				return JSON.stringify(inputs, null, 2) ?? "";
			} catch {
				return "";
			}
		}
		/**
		* What a Run produced, as something a person can read.
		*
		* A workflow's answer is the reason someone started it, and it arrives as
		* whatever the terminal step emitted — most often a string, sometimes an
		* object with one string in it, sometimes a document. A plain string is shown
		* as it was written rather than as a quoted JSON scalar; a single-field object
		* is unwrapped, because `{"translation": "…"}` is a container, not the answer.
		* Anything else is printed, since guessing further would start hiding fields.
		*/
		function resultText(value) {
			if (value === null || value === void 0) return "";
			if (typeof value === "string") return value;
			if (typeof value === "number" || typeof value === "boolean") return String(value);
			if (typeof value === "object" && !Array.isArray(value)) {
				const entries = Object.entries(value);
				const [only] = entries;
				if (entries.length === 1 && only !== void 0 && typeof only[1] === "string") return only[1];
			}
			try {
				return JSON.stringify(value, null, 2) ?? "";
			} catch {
				return String(value);
			}
		}
		/** What an Artifact id looks like wherever a result happens to carry one. */
		const ARTIFACT = /^langgraph_artifact:[A-Za-z0-9]+$/;
		/**
		* Split a result into what a person can read and what they have to open.
		*
		* A workflow that writes a file answers `{"artifact_id":
		* "langgraph_artifact:4c1e5281…"}`, and printing that put a 64-character hash
		* on the page where the answer should have been — the one thing in the result
		* that means nothing at all to a reader. It is a door, so it is drawn as one.
		*
		* The rest of the result still shows. A workflow that returns a summary *and*
		* a file has both, and dropping either would be answering a different
		* question than the one that was asked.
		*/
		function resultOutcome(value) {
			const artifacts = [];
			const remainder = strip(value, artifacts);
			return {
				artifacts,
				text: remainder === void 0 ? "" : resultText(remainder)
			};
		}
		/** The value with its Artifact references taken out, or undefined if that is
		*  all it was. Containers left empty by the removal go too: `{"artifact_id":
		*  …}` is a wrapper around a door, not an answer with a door in it. */
		function strip(value, into) {
			if (typeof value === "string") {
				if (!ARTIFACT.test(value)) return value;
				into.push(value);
				return;
			}
			if (Array.isArray(value)) {
				const kept = value.map((item) => strip(item, into)).filter((item) => item !== void 0);
				return kept.length ? kept : void 0;
			}
			if (value !== null && typeof value === "object") {
				const kept = {};
				for (const [key, item] of Object.entries(value)) {
					const left = strip(item, into);
					if (left !== void 0) kept[key] = left;
				}
				return Object.keys(kept).length ? kept : void 0;
			}
			return value;
		}
		/**
		* Where an Artifact can be opened: through this Host, as the Session that owns
		* it.
		*
		* Not Orbit's own address for it. Artifacts belong to the actor that produced
		* them, a browser reaching `/api/v1` on loopback is `local`, and a Run this
		* panel started belongs to `harness:session:<id>` — so Orbit's link is a 404
		* for every Artifact this Harness ever made, and so is Orbit's own UI. The
		* Host holds the identity that can read it, and hands the bytes to the
		* browser unchanged.
		*
		* Empty without a Session, which is what a reader gets instead of a link that
		* goes nowhere.
		*/
		function artifactHref(sessionId, artifactId) {
			if (!sessionId || !artifactId) return "";
			return `/plugins/dsh-orbit/artifact?${new URLSearchParams({
				session: sessionId,
				id: artifactId
			}).toString()}`;
		}
		/** The short name an Artifact is offered under: its id without the kind. */
		function artifactLabel(artifactId) {
			const bare = artifactId.replace(/^langgraph_artifact:/, "");
			return bare.length > 12 ? `${bare.slice(0, 12)}…` : bare;
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
			if (status === "unknown" || status === "waiting") return "warning";
			if (status === "failed" || status === "cancelled") return "error";
			return "ongoing";
		}
		/** A step card uses a still dot: history records outcomes, not activity. */
		function stepDotState(status) {
			if (status === "succeeded") return "success";
			if (status === "failed" || status === "cancelled") return "error";
			if (status === "not_reached") return "skipped";
			if (status === "unknown" || status === "waiting") return "warning";
			return "ongoing";
		}
		function toStepRow(step) {
			return {
				nodeId: step.node_id,
				label: labelOf(step),
				status: step.status,
				hasOutput: step.has_output === true,
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
		//#region \0dsh-css:/Users/cxd/develop/orbit/integrations/deepseek-harness/src/client/OrbitPanel.module.css.mjs
		const css = ".JeOz9W_panel,.JeOz9W_panel *,.JeOz9W_panel :before,.JeOz9W_panel :after{box-sizing:border-box}.JeOz9W_panel{border:1px solid var(--dsw-alias-border-l4,#80808033);background:var(--dsw-alias-bg-layer-1,Canvas);color:var(--dsw-alias-label-primary);pointer-events:auto;border-radius:12px;flex-direction:column;display:flex;position:absolute;overflow:hidden;box-shadow:0 12px 40px #0000002e}.JeOz9W_bar{border-bottom:1px solid var(--dsw-alias-border-l2,#8080801f);background:var(--dsw-alias-bg-module-platform,#80808014);cursor:grab;user-select:none;align-items:center;gap:8px;padding:8px 10px 8px 12px;display:flex}.JeOz9W_bar:active{cursor:grabbing}.JeOz9W_title{font-size:13px;font-weight:600}.JeOz9W_count{color:var(--dsw-alias-label-tertiary);flex:1;font-size:12px}.JeOz9W_body{flex:1;min-height:0;overflow-y:auto}.JeOz9W_row{border-bottom:1px solid var(--dsw-alias-border-l2,#8080801f);grid-template-columns:8px 1fr auto;align-items:start;gap:8px;padding:10px 12px;display:grid}.JeOz9W_row:last-child{border-bottom:0}.JeOz9W_dot{border-radius:50%;width:8px;height:8px;margin-top:5px}.JeOz9W_live{background:var(--dsw-alias-state-business-primary,#679efe)}.JeOz9W_done{background:var(--dsw-alias-state-success-primary,#22c55e)}.JeOz9W_failed{background:var(--dsw-alias-state-error-primary,#f25a5a)}.JeOz9W_unknown{background:var(--dsw-alias-state-warn-primary,#f59e0b)}.JeOz9W_goal{-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:13px;line-height:1.4;display:-webkit-box;overflow:hidden}.JeOz9W_meta{color:var(--dsw-alias-label-tertiary);font-size:11px}.JeOz9W_status{color:var(--dsw-alias-label-secondary,GrayText);white-space:nowrap;margin-left:8px;font-size:11px}.JeOz9W_empty,.JeOz9W_error{color:var(--dsw-alias-label-tertiary);text-align:center;padding:20px 14px;font-size:12px}.JeOz9W_error{color:var(--dsw-alias-state-error-primary);text-align:left}.JeOz9W_connecting{color:var(--dsw-alias-label-secondary,GrayText);justify-content:center;align-items:center;gap:9px;padding:24px 14px;font-size:12px;display:flex}.JeOz9W_connectSpinner{border:2px solid var(--dsw-alias-border-l3,#80808029);border-top-color:var(--dsw-alias-state-business-primary,Highlight);border-radius:50%;width:14px;height:14px;animation:.7s linear infinite JeOz9W_orbit-spin}.JeOz9W_badge{border:1px solid var(--dsw-alias-border-l4,#80808033);background:var(--dsw-alias-bg-layer-1,Canvas);width:42px;height:42px;color:var(--dsw-alias-label-secondary);cursor:pointer;pointer-events:auto;border-radius:999px;place-items:center;margin-top:-21px;padding:0;display:grid;position:absolute;top:50%;right:18px;box-shadow:0 6px 20px #00000024}.JeOz9W_badge:hover{transform:translateY(-1px)}.JeOz9W_orbitMark{width:30px;height:30px}.JeOz9W_orbitBackground{fill:var(--dsw-alias-label-primary)}.JeOz9W_orbitRing{fill:none;stroke:var(--dsw-alias-bg-layer-1,Canvas);stroke-width:6px}.JeOz9W_orbitSatellite{fill:var(--dsw-alias-state-business-primary,Highlight)}.JeOz9W_resize{cursor:nwse-resize;width:14px;height:14px;position:absolute;inset:auto 0 0 auto}.JeOz9W_stepDisclosure{width:100%;min-width:0}.JeOz9W_stepRow{width:100%;min-width:0;height:32px;color:inherit;text-align:left;cursor:pointer;background:0 0;border:0;align-items:center;gap:8px;padding:4px 12px 4px 22px;font-size:12px;display:flex}.JeOz9W_stepRow:disabled{cursor:default}.JeOz9W_stepTitle{text-overflow:ellipsis;white-space:nowrap;flex:auto;min-width:0;overflow:hidden}.JeOz9W_stepChevron{color:var(--dsw-alias-label-tertiary,GrayText);flex:none;transition:transform .12s}.JeOz9W_stepChevronOpen{transform:rotate(180deg)}.JeOz9W_stepContent{padding:0 12px 4px 38px}.JeOz9W_stepDot{border-radius:50%;flex:none;width:8px;height:8px;display:block}.JeOz9W_stepDot_success{background:var(--dsw-alias-state-success-primary,#22c55e)}.JeOz9W_stepDot_error{background:var(--dsw-alias-state-error-primary,#f25a5a)}.JeOz9W_stepDot_skipped{background:var(--dsw-alias-label-tertiary,#adb2b8)}.JeOz9W_stepDot_warning{background:var(--dsw-alias-state-warn-primary,#f59e0b)}.JeOz9W_stepDot_ongoing{background:var(--dsw-alias-state-business-primary,#679efe)}.JeOz9W_attention{border-left:2px solid var(--dsw-alias-state-warn-primary,#f59e0b);color:var(--dsw-alias-label-secondary);margin:4px 0;padding:6px 8px;font-size:11px}.JeOz9W_actions{flex-wrap:wrap;align-items:center;gap:6px;margin:6px 0 8px;display:flex}.JeOz9W_runActions{justify-content:center;padding:0 12px}.JeOz9W_actions input{border:1px solid var(--dsw-alias-border-l3,#80808029);background:var(--dsw-alias-bg-layer-1,Canvas);min-width:0;color:var(--dsw-alias-label-primary);border-radius:6px;flex:140px;padding:4px 8px;font-size:11px}.JeOz9W_iconButton{color:var(--dsw-alias-label-tertiary);cursor:pointer;background:0 0;border:0;border-radius:4px;justify-content:center;align-items:center;padding:2px;display:inline-flex}.JeOz9W_iconButton:hover{color:var(--dsw-alias-label-primary)}.JeOz9W_iconButton:disabled{cursor:default}.JeOz9W_iconButton:disabled svg{animation:.7s linear infinite JeOz9W_orbit-spin}@keyframes JeOz9W_orbit-spin{to{transform:rotate(360deg)}}@media (prefers-reduced-motion:reduce){.JeOz9W_iconButton:disabled svg,.JeOz9W_connectSpinner{opacity:.45;animation:none}}.JeOz9W_stopButton:hover{color:var(--dsw-alias-state-error-primary,LinkText)}.JeOz9W_confirmBar{border-bottom:1px solid var(--dsw-alias-border-l2,#8080801f);background:var(--dsw-alias-bg-module-platform,#80808014);gap:8px;padding:10px 12px;display:grid}.JeOz9W_confirmText{color:var(--dsw-alias-label-secondary,GrayText);font-size:12px;line-height:1.5}.JeOz9W_confirmActions{justify-content:flex-end;gap:8px;display:flex}.JeOz9W_confirmCancel,.JeOz9W_confirmGo{font:inherit;cursor:pointer;border-radius:6px;padding:4px 10px;font-size:12px}.JeOz9W_confirmCancel{border:1px solid var(--dsw-alias-border-l3,#80808029);background:var(--dsw-alias-bg-layer-1,Canvas);color:var(--dsw-alias-label-primary)}.JeOz9W_confirmGo{background:var(--dsw-alias-state-error-primary,Highlight);color:var(--dsw-alias-label-primary-foreground,#fff);border:0}.JeOz9W_confirmGo:disabled{opacity:.6;cursor:default}.JeOz9W_catalogRow{justify-content:space-between;align-items:baseline;gap:8px;padding:4px 0 4px 12px;display:flex}.JeOz9W_catalogRow>*{text-overflow:ellipsis;white-space:nowrap;min-width:0;overflow:hidden}.JeOz9W_catalogRow>span{flex:auto}.JeOz9W_catalogRow>code{flex:0 auto}.JeOz9W_tabs{border-bottom:1px solid var(--dsw-alias-border-l2,#8080801f);background:var(--dsw-alias-bg-module-platform,#80808014);flex:none;gap:2px;padding:6px 8px 0;display:flex}.JeOz9W_tab{color:var(--dsw-alias-label-tertiary,GrayText);cursor:pointer;background:0 0;border:0;border-bottom:2px solid #0000;flex:1 1 0;padding:6px 4px 8px;font-size:12px}.JeOz9W_tab:hover{color:var(--dsw-alias-label-primary,CanvasText)}.JeOz9W_tabActive{color:var(--dsw-alias-label-primary,CanvasText);border-bottom-color:var(--dsw-alias-state-business-primary,Highlight);font-weight:600}.JeOz9W_agentsGrid{gap:10px;padding:10px 12px;display:grid}.JeOz9W_agentCard{border:1px solid var(--dsw-alias-border-l3,#80808029);background:var(--dsw-alias-bg-layer-1,Canvas);border-radius:8px;gap:11px;padding:12px;transition:border-color .12s,background-color .12s;display:grid}.JeOz9W_agentCard:hover{border-color:var(--dsw-alias-state-business-primary,Highlight);background:var(--dsw-alias-bg-module-platform,#80808014)}.JeOz9W_agentHead{align-items:flex-start;gap:10px;display:flex}.JeOz9W_avatar{letter-spacing:.5px;text-transform:uppercase;border-radius:7px;flex:none;place-items:center;width:36px;height:36px;font-size:10px;font-weight:700;display:grid}.JeOz9W_agentIdentity{gap:2px;min-width:0;display:grid}.JeOz9W_agentName{color:var(--dsw-alias-label-primary,CanvasText);text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600;overflow:hidden}.JeOz9W_agentVersion{color:var(--dsw-alias-label-tertiary,GrayText);font-family:ui-monospace,monospace;font-size:11px}.JeOz9W_agentStat{border-top:1px solid var(--dsw-alias-border-l2,#8080801f);justify-content:space-between;align-items:center;padding-top:9px;display:flex}.JeOz9W_agentStatLabel{color:var(--dsw-alias-label-tertiary,GrayText);font-size:11px}.JeOz9W_agentStatPill{background:var(--dsw-alias-bg-module-platform,#80808014);color:var(--dsw-alias-label-secondary,GrayText);border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600}.JeOz9W_agentStatError{background:var(--dsw-alias-state-error-secondary,#f25a5a1f);color:var(--dsw-alias-state-error-primary,LinkText)}.JeOz9W_flowRow{padding:9px 12px}.JeOz9W_flowName{font-size:12.5px;font-weight:600;line-height:1.45}.JeOz9W_listRow{width:100%;color:inherit;text-align:left;cursor:pointer;background:0 0;border:0;align-items:flex-start;gap:9px;padding:9px 12px;display:flex}.JeOz9W_listRow:hover{background:var(--dsw-alias-bg-module-platform,#80808014)}.JeOz9W_listRow,.JeOz9W_flowRow,.JeOz9W_defnRow{position:relative}.JeOz9W_listRow:after,.JeOz9W_flowRow:after,.JeOz9W_defnRow:after{content:\"\";background:var(--dsw-alias-border-l2,#8080801f);height:1px;position:absolute;bottom:0;left:12px;right:12px}.JeOz9W_defnRow:after{left:0}.JeOz9W_listRow:last-child:after,.JeOz9W_flowRow:last-child:after,.JeOz9W_defnRow:last-child:after{display:none}.JeOz9W_listDot{flex:none;margin-top:4px}.JeOz9W_listMain{flex:auto;grid-template-columns:minmax(0,1fr);gap:1px;min-width:0;display:grid}.JeOz9W_listGoal{-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:12.5px;line-height:1.4;display:-webkit-box;overflow:hidden}.JeOz9W_listPrompt{-webkit-line-clamp:2;color:var(--dsw-alias-label-tertiary);overflow-wrap:anywhere;white-space:normal;-webkit-box-orient:vertical;font-size:11px;line-height:1.45;display:-webkit-box;overflow:hidden}.JeOz9W_back{color:var(--dsw-alias-state-business-primary,Highlight);cursor:pointer;background:0 0;border:0;align-items:center;gap:5px;padding:8px 12px 4px;font-size:14px;font-weight:500;display:flex}.JeOz9W_backArrow{font-size:16px;line-height:1}.JeOz9W_back:hover .JeOz9W_backLabel{text-decoration:underline}.JeOz9W_goalTitle{text-overflow:ellipsis;white-space:nowrap;font-size:12.5px;font-weight:600;line-height:1.4;overflow:hidden}.JeOz9W_goalPromptCard{background:var(--dsw-alias-bg-module-platform,#80808014);border-radius:6px;min-width:0;max-width:100%;margin:3px 0 0;padding:6px 8px}.JeOz9W_goalPrompt{min-width:0;max-width:100%;color:var(--dsw-alias-label-secondary,GrayText);white-space:pre-wrap;overflow-wrap:anywhere;-webkit-box-orient:vertical;margin:0;font-family:inherit;font-size:11.5px;line-height:1.5;display:-webkit-box;overflow:hidden}.JeOz9W_goalPrompt[data-open]{display:block}.JeOz9W_goalPromptToggle{color:var(--dsw-alias-state-business-primary,Highlight);font:inherit;cursor:pointer;background:0 0;border:0;align-items:center;gap:3px;margin-top:4px;padding:0;font-size:11px;display:flex}.JeOz9W_goalPromptToggle:hover{text-decoration:underline}.JeOz9W_goalPromptChevronOpen{transform:rotate(180deg)}.JeOz9W_authoringStages{--orbit-step-line:26px;padding:2px 0 6px}.JeOz9W_authoringStages .JeOz9W_stepDisclosure{position:relative}.JeOz9W_authoringStages .JeOz9W_stepDisclosure:before{content:\"\";top:0;bottom:0;left:var(--orbit-step-line);background:var(--dsw-alias-label-tertiary,#808080b3);width:1px;margin-left:-.5px;position:absolute}.JeOz9W_authoringStages .JeOz9W_stepDisclosure:first-child:before{top:16px}.JeOz9W_authoringStages .JeOz9W_stepDisclosure:last-child:before{bottom:calc(100% - 16px)}.JeOz9W_authoringStages .JeOz9W_stepDot{z-index:1;position:relative}.JeOz9W_authoringStages .JeOz9W_stepRow{cursor:default}.JeOz9W_resultBlock{gap:3px;padding:4px 12px 8px 22px;display:grid}.JeOz9W_resultLabel{color:var(--dsw-alias-label-tertiary,GrayText);letter-spacing:.02em;font-size:10.5px}.JeOz9W_outcome{font-size:12px;font-weight:600}.JeOz9W_outcome_done{color:var(--dsw-alias-state-success-primary,#22c55e)}.JeOz9W_outcome_error{color:var(--dsw-alias-state-error-primary,#f25a5a)}.JeOz9W_outcome_warning{color:var(--dsw-alias-state-warn-primary,#f59e0b)}.JeOz9W_outcome_ongoing{color:var(--dsw-alias-state-business-primary,#679efe)}.JeOz9W_artifactRow{gap:4px;display:grid}.JeOz9W_artifactText{background:var(--dsw-alias-bg-module-platform,#80808014);max-width:100%;max-height:240px;color:var(--dsw-alias-label-primary);white-space:pre-wrap;overflow-wrap:anywhere;border-radius:6px;margin:0;padding:6px 8px;font-family:inherit;font-size:11.5px;line-height:1.5;overflow:hidden auto}.JeOz9W_artifacts{flex-wrap:wrap;gap:6px;display:flex}.JeOz9W_artifactPath{background:var(--dsw-alias-bg-module-platform,#80808014);max-width:100%;color:var(--dsw-alias-label-secondary,GrayText);overflow-wrap:anywhere;user-select:all;border-radius:6px;padding:4px 8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;line-height:1.5}.JeOz9W_artifact{border:1px solid var(--dsw-alias-border-l3,#80808029);max-width:100%;color:var(--dsw-alias-state-business-primary,Highlight);text-overflow:ellipsis;white-space:nowrap;border-radius:999px;padding:3px 8px;font-size:11px;text-decoration:none;overflow:hidden}a.JeOz9W_artifact:hover{text-decoration:underline}.JeOz9W_result{background:var(--dsw-alias-bg-module-platform,#80808014);max-height:220px;color:var(--dsw-alias-label-primary);white-space:pre-wrap;overflow-wrap:anywhere;border-radius:6px;margin:0;padding:6px 8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;line-height:1.5;overflow:auto}.JeOz9W_resultError{color:var(--dsw-alias-state-error-primary)}.JeOz9W_goalCard{border-bottom:1px solid var(--dsw-alias-border-l2,#8080801f)}.JeOz9W_goalCard:last-child{border-bottom:0}.JeOz9W_goalHead{align-items:flex-start;gap:9px;width:100%;padding:9px 12px 6px;display:flex}.JeOz9W_goalSteps{--orbit-step-line:26px;padding-bottom:6px}.JeOz9W_goalSteps .JeOz9W_stepDisclosure{position:relative}.JeOz9W_goalSteps .JeOz9W_stepDisclosure:before{content:\"\";top:0;bottom:0;left:var(--orbit-step-line);background:var(--dsw-alias-label-tertiary,#808080b3);width:1px;margin-left:-.5px;position:absolute}.JeOz9W_goalSteps .JeOz9W_stepDisclosure:first-child:before{top:16px}.JeOz9W_goalSteps .JeOz9W_stepDisclosure:last-child:before{bottom:calc(100% - 16px)}.JeOz9W_goalSteps .JeOz9W_stepDisclosure:only-child:before{display:none}.JeOz9W_goalSteps .JeOz9W_stepDot{z-index:1;position:relative}.JeOz9W_detailHead{align-items:baseline;gap:8px;padding:0 12px;display:flex}.JeOz9W_detailGoal{font-size:13px;font-weight:650;line-height:1.4}.JeOz9W_detailMeta{border-bottom:1px solid var(--dsw-alias-border-l2,#8080801f);color:var(--dsw-alias-label-tertiary,GrayText);padding:2px 12px 8px;font-size:11px}.JeOz9W_flowButton{width:100%;color:inherit;text-align:left;cursor:pointer;background:0 0;border:0;display:block}.JeOz9W_flowButton:hover{background:var(--dsw-alias-bg-module-platform,#80808014)}.JeOz9W_prose{color:var(--dsw-alias-label-secondary,GrayText);margin:0;padding:8px 12px;font-size:12px;line-height:1.55}.JeOz9W_outLink{padding:10px 12px;font-size:11.5px;display:block}.JeOz9W_sectionLabel{border-top:1px solid var(--dsw-alias-border-l2,#8080801f);color:var(--dsw-alias-label-tertiary,GrayText);padding:8px 12px 4px;font-size:11px}.JeOz9W_defnRow{border-left:2px solid var(--dsw-alias-label-tertiary,#adb2b8b3);padding:8px 12px 9px}.JeOz9W_kind_action{border-left-color:var(--dsw-alias-state-business-primary,#679efed9)}.JeOz9W_kind_human{border-left-color:var(--dsw-alias-state-warn-primary,#f59e0bd9)}.JeOz9W_kind_terminal{border-left-color:var(--dsw-alias-state-success-primary,#22c55ed9)}.JeOz9W_kind_decision{border-left-color:var(--dsw-alias-label-secondary,#cfd3d6d9)}.JeOz9W_kind_join{border-left-color:var(--dsw-alias-label-tertiary,#adb2b8b3)}.JeOz9W_defnHead{align-items:baseline;gap:6px;min-width:0;display:flex}.JeOz9W_defnName{text-overflow:ellipsis;white-space:nowrap;flex:0 auto;min-width:0;font-size:12.5px;font-weight:600;overflow:hidden}.JeOz9W_defnKind{color:var(--dsw-alias-label-tertiary,GrayText);letter-spacing:.04em;text-transform:uppercase;flex:none;font-size:10px}.JeOz9W_defnHandler{text-overflow:ellipsis;white-space:nowrap;min-width:0;color:var(--dsw-alias-label-tertiary,GrayText);flex:0 auto;margin-left:auto;font-family:ui-monospace,monospace;font-size:11px;overflow:hidden}.JeOz9W_defnPrompt,.JeOz9W_defnNoPrompt{color:var(--dsw-alias-label-secondary,GrayText);-webkit-line-clamp:3;-webkit-box-orient:vertical;margin:3px 0 0;font-size:11.5px;line-height:1.5;display:-webkit-box;overflow:hidden}.JeOz9W_defnNoPrompt{color:var(--dsw-alias-label-tertiary,GrayText);font-style:italic}.JeOz9W_shape{align-items:center;margin-top:5px;display:flex}.JeOz9W_shapeNode{border:1px solid var(--dsw-alias-border-l3,#80808029);width:15px;height:15px;color:var(--dsw-alias-state-business-primary,#5078ffe6);border-radius:50%;flex:0 0 15px;place-items:center;font-family:ui-monospace,monospace;font-size:7.5px;font-weight:700;line-height:1;display:grid;position:relative}.JeOz9W_shapeNode+.JeOz9W_shapeNode{margin-left:8px}.JeOz9W_shapeNode+.JeOz9W_shapeNode:before{content:\"\";border-top:1px solid var(--dsw-alias-border-l3,#80808029);width:8px;position:absolute;top:50%;right:100%}.JeOz9W_node_human{color:var(--dsw-alias-state-warn-primary,#c88c28f2)}.JeOz9W_node_terminal{color:var(--dsw-alias-state-success-primary,#3ca05af2)}.JeOz9W_node_decision{color:var(--dsw-alias-label-secondary,GrayText)}.JeOz9W_node_more{color:var(--dsw-alias-label-tertiary,GrayText);font-size:7px}.JeOz9W_flowBlocked{color:var(--dsw-alias-state-warn-primary,#f59e0b);margin-left:6px;font-size:10px;font-weight:500}.JeOz9W_authoringRow{border-bottom:1px solid var(--dsw-alias-border-l2,#8080801f);background:var(--dsw-alias-bg-module-platform,#80808014);padding:9px 12px}.JeOz9W_authoringSummary{align-items:flex-start;gap:8px;display:flex}.JeOz9W_authoringMain{flex:auto;gap:2px;min-width:0;display:grid}.JeOz9W_authoringLabel{font-size:12px;font-weight:600;line-height:1.4}.JeOz9W_authoringPrompt{color:var(--dsw-alias-label-secondary,GrayText);-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:11px;line-height:1.45;display:-webkit-box;overflow:hidden}.JeOz9W_authoringOutputToggle{width:24px;height:24px;color:var(--dsw-alias-label-secondary,GrayText);cursor:pointer;background:0 0;border:0;flex:none;place-items:center;padding:0;display:grid}.JeOz9W_authoringOutputToggle svg{transition:transform .15s}.JeOz9W_authoringChevronOpen{transform:rotate(180deg)}.JeOz9W_authoringOutput{border:1px solid var(--dsw-alias-border-l3,#80808029);background:var(--dsw-alias-bg-base,Canvas);border-radius:6px;max-height:180px;margin:8px 0 0 16px;padding:8px;overflow:auto}.JeOz9W_authoringOutput pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}";
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
			"agentCard": "JeOz9W_agentCard",
			"agentHead": "JeOz9W_agentHead",
			"agentIdentity": "JeOz9W_agentIdentity",
			"agentName": "JeOz9W_agentName",
			"agentStat": "JeOz9W_agentStat",
			"agentStatError": "JeOz9W_agentStatError",
			"agentStatLabel": "JeOz9W_agentStatLabel",
			"agentStatPill": "JeOz9W_agentStatPill",
			"agentVersion": "JeOz9W_agentVersion",
			"agentsGrid": "JeOz9W_agentsGrid",
			"artifact": "JeOz9W_artifact",
			"artifactPath": "JeOz9W_artifactPath",
			"artifactRow": "JeOz9W_artifactRow",
			"artifactText": "JeOz9W_artifactText",
			"artifacts": "JeOz9W_artifacts",
			"attention": "JeOz9W_attention",
			"authoringChevronOpen": "JeOz9W_authoringChevronOpen",
			"authoringLabel": "JeOz9W_authoringLabel",
			"authoringMain": "JeOz9W_authoringMain",
			"authoringOutput": "JeOz9W_authoringOutput",
			"authoringOutputToggle": "JeOz9W_authoringOutputToggle",
			"authoringPrompt": "JeOz9W_authoringPrompt",
			"authoringRow": "JeOz9W_authoringRow",
			"authoringStages": "JeOz9W_authoringStages",
			"authoringSummary": "JeOz9W_authoringSummary",
			"avatar": "JeOz9W_avatar",
			"back": "JeOz9W_back",
			"backArrow": "JeOz9W_backArrow",
			"backLabel": "JeOz9W_backLabel",
			"badge": "JeOz9W_badge",
			"bar": "JeOz9W_bar",
			"body": "JeOz9W_body",
			"catalogRow": "JeOz9W_catalogRow",
			"confirmActions": "JeOz9W_confirmActions",
			"confirmBar": "JeOz9W_confirmBar",
			"confirmCancel": "JeOz9W_confirmCancel",
			"confirmGo": "JeOz9W_confirmGo",
			"confirmText": "JeOz9W_confirmText",
			"connectSpinner": "JeOz9W_connectSpinner",
			"connecting": "JeOz9W_connecting",
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
			"failed": "JeOz9W_failed",
			"flowBlocked": "JeOz9W_flowBlocked",
			"flowButton": "JeOz9W_flowButton",
			"flowName": "JeOz9W_flowName",
			"flowRow": "JeOz9W_flowRow",
			"goal": "JeOz9W_goal",
			"goalCard": "JeOz9W_goalCard",
			"goalHead": "JeOz9W_goalHead",
			"goalPrompt": "JeOz9W_goalPrompt",
			"goalPromptCard": "JeOz9W_goalPromptCard",
			"goalPromptChevronOpen": "JeOz9W_goalPromptChevronOpen",
			"goalPromptToggle": "JeOz9W_goalPromptToggle",
			"goalSteps": "JeOz9W_goalSteps",
			"goalTitle": "JeOz9W_goalTitle",
			"iconButton": "JeOz9W_iconButton",
			"kind_action": "JeOz9W_kind_action",
			"kind_decision": "JeOz9W_kind_decision",
			"kind_human": "JeOz9W_kind_human",
			"kind_join": "JeOz9W_kind_join",
			"kind_terminal": "JeOz9W_kind_terminal",
			"listDot": "JeOz9W_listDot",
			"listGoal": "JeOz9W_listGoal",
			"listMain": "JeOz9W_listMain",
			"listPrompt": "JeOz9W_listPrompt",
			"listRow": "JeOz9W_listRow",
			"live": "JeOz9W_live",
			"meta": "JeOz9W_meta",
			"node_decision": "JeOz9W_node_decision",
			"node_human": "JeOz9W_node_human",
			"node_more": "JeOz9W_node_more",
			"node_terminal": "JeOz9W_node_terminal",
			"orbit-spin": "JeOz9W_orbit-spin",
			"orbitBackground": "JeOz9W_orbitBackground",
			"orbitMark": "JeOz9W_orbitMark",
			"orbitRing": "JeOz9W_orbitRing",
			"orbitSatellite": "JeOz9W_orbitSatellite",
			"outLink": "JeOz9W_outLink",
			"outcome": "JeOz9W_outcome",
			"outcome_done": "JeOz9W_outcome_done",
			"outcome_error": "JeOz9W_outcome_error",
			"outcome_ongoing": "JeOz9W_outcome_ongoing",
			"outcome_warning": "JeOz9W_outcome_warning",
			"panel": "JeOz9W_panel",
			"prose": "JeOz9W_prose",
			"resize": "JeOz9W_resize",
			"result": "JeOz9W_result",
			"resultBlock": "JeOz9W_resultBlock",
			"resultError": "JeOz9W_resultError",
			"resultLabel": "JeOz9W_resultLabel",
			"row": "JeOz9W_row",
			"runActions": "JeOz9W_runActions",
			"sectionLabel": "JeOz9W_sectionLabel",
			"shape": "JeOz9W_shape",
			"shapeNode": "JeOz9W_shapeNode",
			"status": "JeOz9W_status",
			"stepChevron": "JeOz9W_stepChevron",
			"stepChevronOpen": "JeOz9W_stepChevronOpen",
			"stepContent": "JeOz9W_stepContent",
			"stepDisclosure": "JeOz9W_stepDisclosure",
			"stepDot": "JeOz9W_stepDot",
			"stepDot_error": "JeOz9W_stepDot_error",
			"stepDot_ongoing": "JeOz9W_stepDot_ongoing",
			"stepDot_skipped": "JeOz9W_stepDot_skipped",
			"stepDot_success": "JeOz9W_stepDot_success",
			"stepDot_warning": "JeOz9W_stepDot_warning",
			"stepRow": "JeOz9W_stepRow",
			"stepTitle": "JeOz9W_stepTitle",
			"stopButton": "JeOz9W_stopButton",
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
			dismissed: false,
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
				dismissed: stored.dismissed === true,
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
		//#region src/client/OrbitRunRow.tsx
		/** A Run as a row in a list, and the same Run as the panel's whole body.
		*
		* Two components rather than one disclosure, because a Run's detail does not
		* fit beside its siblings: opening one inline pushed the rest of the list out
		* of a 400px panel, which is the same as losing it.
		*/
		function StepDisclosure({ call, t, sessionId, runId, step, live, onSettled }) {
			const expandable = step.hasOutput || step.needsPerson;
			const [override, setOverride] = (0, react.useState)(void 0);
			const open = override ?? expandable;
			const [note, setNote] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [chunks, setChunks] = (0, react.useState)([]);
			const [error, setError] = (0, react.useState)(null);
			const working = live && step.status === "running";
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
						if (working || page.has_more) timer = setTimeout(() => {
							tick();
						}, 2e3);
					} catch (reason) {
						if (!controller.signal.aborted) setError(panelError(reason));
					}
				};
				tick();
				return () => {
					controller.abort();
					if (timer !== void 0) clearTimeout(timer);
				};
			}, [
				open,
				working,
				sessionId,
				runId,
				step.nodeId,
				call
			]);
			const text = outputText(chunks);
			const indicator = stepDotState(step.status);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: OrbitPanel_module_css_default.stepDisclosure,
				"data-open": open || void 0,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
					type: "button",
					className: OrbitPanel_module_css_default.stepRow,
					disabled: !expandable,
					"aria-expanded": expandable ? open : void 0,
					onClick: () => {
						if (expandable) setOverride(!open);
					},
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: `${OrbitPanel_module_css_default.stepDot} ${OrbitPanel_module_css_default[`stepDot_${indicator}`]}`,
							"aria-hidden": "true"
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.stepTitle,
							children: step.label
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.status,
							children: step.status
						}),
						expandable ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconChevronDownOutline14, { className: `${OrbitPanel_module_css_default.stepChevron} ${open ? OrbitPanel_module_css_default.stepChevronOpen : ""}` }) : null
					]
				}), open ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: OrbitPanel_module_css_default.stepContent,
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
									}).catch((reason) => setError(panelError(reason))).finally(() => setBusy(false));
								},
								children: busy ? t("working") : t(outcome === "confirmed_succeeded" ? "confirmSucceeded" : "confirmFailed")
							}, outcome))]
						}) : null] }) : null,
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
							t,
							error
						}),
						text ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(FoldedText, {
							t,
							text,
							lines: OUTPUT_LINES
						}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
							className: OrbitPanel_module_css_default.empty,
							children: t("noOutput")
						})
					]
				}) : null]
			});
		}
		/**
		* A failure, said in words, with its own text kept where it can be quoted.
		*
		* Every page shows failures the same way because they arrive the same way —
		* through the Host, from whatever layer actually broke. The reading is on the
		* page; the original is on the element, so a person can hover it, copy it into
		* a bug report, and correct the reading when it is wrong.
		*/
		function PanelErrorText({ t, error }) {
			if (error === null) return null;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
				className: OrbitPanel_module_css_default.error,
				title: error.detail,
				children: t(error.key, error.values)
			});
		}
		/**
		* The way out of a detail page, on every page that has one.
		*
		* Shared rather than spelled twice: the two detail pages are reached the same
		* way and left the same way, and a back control that differs between them
		* reads as two different affordances for one idea. The arrow is its own
		* element so it can carry its own size — glued into the label it could only be
		* as big as the word, and the word is not what the eye lands on.
		*/
		function BackButton({ t, onBack }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
				type: "button",
				className: OrbitPanel_module_css_default.back,
				onClick: onBack,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: OrbitPanel_module_css_default.backArrow,
					"aria-hidden": "true",
					children: "←"
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: OrbitPanel_module_css_default.backLabel,
					children: t("back")
				})]
			});
		}
		/**
		* A Run's steps, top to bottom, each opening onto what it printed.
		*
		* One component for both places a Run is read. The Goal page draws it under a
		* running Run so the steps are simply there — that page answers "what is
		* happening", and an answer a reader has to click for is not on the page. The
		* detail page draws the same list under the Run's controls.
		*/
		function OrbitStepList({ call, t, sessionId, runId, steps, live, onSettled }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(react_jsx_runtime.Fragment, { children: steps.map((step) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StepDisclosure, {
				call,
				t,
				sessionId,
				runId,
				step: toStepRow(step),
				live,
				onSettled
			}, step.node_id)) });
		}
		/**
		* A Run in a list: what it was for, and how it went.
		*
		* The History page and a Workflow's run list, both of which list Runs that are
		* over. It briefly grew a progress line for the Goal page; the Goal page draws
		* the whole Run now, and no caller here ever had steps to give it.
		*/
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
						}), run.prompt ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.listPrompt,
							children: run.prompt
						}) : null]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: OrbitPanel_module_css_default.status,
						children: run.status
					})
				]
			});
		}
		/**
		* A Run on the Goal page: what it is for, how far it is, and every step of it.
		*
		* The steps are not behind a click here. This page exists to answer what is
		* happening now, and a Run that is happening now has one thing worth reading —
		* which step it is on and what that step is saying. The list row this replaces
		* could only say that in a summary line, and the reader had to leave the page
		* to see the rest.
		*
		* The heading is a heading and not a link. There is nowhere left for it to
		* go: everything the detail page had — the steps, the output, the answer, the
		* buttons that cancel or resume — is on this card. A control that navigates to
		* a copy of what the reader is already looking at is a control that wastes the
		* one click they were willing to spend.
		*/
		function OrbitRunGoalCard({ call, t, sessionId, run, steps, onSettled }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: OrbitPanel_module_css_default.goalCard,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.goalHead,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
							state: dotState(run.status),
							size: 9,
							className: OrbitPanel_module_css_default.listDot
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: OrbitPanel_module_css_default.listMain,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: OrbitPanel_module_css_default.goalTitle,
								children: run.goal
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(FoldedText, {
								t,
								text: run.prompt,
								lines: PROMPT_LINES
							})]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(RunControls, {
						call,
						t,
						sessionId,
						run
					}),
					steps?.length ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: OrbitPanel_module_css_default.goalSteps,
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitStepList, {
							call,
							t,
							sessionId,
							runId: run.runId,
							steps,
							live: run.live,
							onSettled
						})
					}) : null,
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(RunResult, {
						t,
						run,
						sessionId,
						call
					})
				]
			});
		}
		/** Past this many lines a request stops being a heading and becomes a wall. */
		const PROMPT_LINES = 5;
		/** A step's console is read further than a heading is, and reached on purpose. */
		const OUTPUT_LINES = 10;
		/**
		* Text from somewhere else, folded to a few lines with a way to see the rest.
		*
		* One widget for the two places the panel shows writing it did not produce —
		* the request a Run was given, and what a step printed. They are the same
		* thing to a reader, and they were the same thing here twice: a scroll box for
		* one and a fold for the other, so the same gesture meant different things a
		* few pixels apart.
		*
		* Folded by line box rather than by counting characters, so it folds where the
		* reader's panel actually wraps it — the same paste is two lines wide and six
		* lines narrow. The toggle is drawn only when there is something folded, which
		* cannot be known until the block has been laid out: a measurement taken on
		* first render compares 0 against 0 and hides a control the reader needs. A
		* resize observer answers on first layout and again whenever a narrowing panel
		* could have changed the answer.
		*/
		function FoldedText({ t, text, lines }) {
			const [open, setOpen] = (0, react.useState)(false);
			const [folded, setFolded] = (0, react.useState)(false);
			const clamp = (0, react.useRef)(null);
			(0, react.useEffect)(() => {
				const node = clamp.current;
				if (node === null) return;
				const measure = () => {
					setFolded(node.scrollHeight > node.clientHeight + 1);
				};
				measure();
				if (typeof ResizeObserver === "undefined") return;
				const observer = new ResizeObserver(measure);
				observer.observe(node);
				return () => observer.disconnect();
			}, [text, open]);
			if (!text) return null;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: OrbitPanel_module_css_default.goalPromptCard,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
					ref: clamp,
					className: OrbitPanel_module_css_default.goalPrompt,
					style: open ? void 0 : { WebkitLineClamp: lines },
					"data-open": open || void 0,
					children: text
				}), folded || open ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
					type: "button",
					className: OrbitPanel_module_css_default.goalPromptToggle,
					onClick: () => setOpen((value) => !value),
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconChevronDownOutline14, { className: open ? OrbitPanel_module_css_default.goalPromptChevronOpen : void 0 }), t(open ? "promptCollapse" : "promptExpand")]
				}) : null]
			});
		}
		/**
		* Cancel it, or answer what it is waiting for.
		*
		* Beside the Run wherever the Run is read. It used to be on the detail page
		* only, which was reachable from the Goal page; when that stopped being a
		* link, these were the thing that would have gone with it — a running Goal
		* with no way to stop it.
		*
		* Draws nothing when Orbit advertises neither command, which is most of the
		* time: a Run that is not interrupted cannot be resumed, and a finished one
		* cannot be cancelled.
		*/
		function RunControls({ call, t, sessionId, run }) {
			const [answer, setAnswer] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [error, setError] = (0, react.useState)(null);
			const cancelAt = commandRevision(run, "langgraph_run.cancel");
			const resumeAt = commandRevision(run, "langgraph_run.resume");
			const approval = run.interrupts.find((item) => item.taskKind === "approval");
			const act = (command, revision, value, interruptId) => {
				setBusy(true);
				setError(null);
				call("runCommand", [
					sessionId,
					run.runId,
					command,
					revision,
					value,
					interruptId
				], new AbortController().signal).then(() => setAnswer("")).catch((reason) => setError(panelError(reason))).finally(() => setBusy(false));
			};
			if (cancelAt === void 0 && resumeAt === void 0) return null;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: `${OrbitPanel_module_css_default.actions} ${OrbitPanel_module_css_default.runActions}`,
				children: [cancelAt !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
					size: "sm",
					variant: "primary",
					disabled: busy,
					onClick: () => act("langgraph_run.cancel", cancelAt, void 0),
					children: busy ? t("working") : t("cancel")
				}) : null, resumeAt === void 0 ? null : approval !== void 0 ? ["approve", "reject"].map((decision) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
					size: "sm",
					variant: decision === "approve" ? "primary" : "outline",
					disabled: busy,
					onClick: () => act("langgraph_run.resume", resumeAt, approvalValue(approval, decision), approval.id),
					children: busy ? t("working") : t(decision === "approve" ? "approve" : "reject")
				}, decision)) : /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
					value: answer,
					placeholder: t("answer"),
					onChange: (event) => setAnswer(event.currentTarget.value)
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
					size: "sm",
					variant: "primary",
					disabled: busy,
					onClick: () => act("langgraph_run.resume", resumeAt, answer),
					children: busy ? t("working") : t("resume")
				})] })]
			}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
				t,
				error
			})] });
		}
		/**
		* What the Run produced, under the steps that produced it.
		*
		* The reason somebody started a Goal is its answer, and until now the panel
		* was the one surface that never showed one — it could say a Run succeeded and
		* not what it succeeded at. Drawn only once there is something to draw: a
		* running Run has no answer yet, and an empty box promising one is worse than
		* no box.
		*/
		/**
		* One Artifact: a quick look, and a copy of it on the filesystem.
		*
		* Two different needs. The link opens the bytes in a tab, which answers "what
		* does it say" for anything a browser renders. The export answers "give me the
		* file" — because the path Orbit already has for it is a content-addressed
		* blob named by its own sha256, shared with every Artifact holding the same
		* bytes and collected when nothing references it. Editing that in place would
		* corrupt the store, so what a person gets is a copy that belongs to them.
		*/
		function ArtifactRow({ t, call, sessionId, artifactId }) {
			const [path, setPath] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [failed, setFailed] = (0, react.useState)(null);
			/** Its text, when the Host judged its text to be the answer. */
			const [inline, setInline] = (0, react.useState)(null);
			const href = artifactHref(sessionId, artifactId);
			(0, react.useEffect)(() => {
				const controller = new AbortController();
				call("readArtifactText", [sessionId, artifactId], controller.signal).then((held) => {
					if (!controller.signal.aborted) setInline(held.text);
				}).catch(() => {});
				return () => controller.abort();
			}, [
				call,
				sessionId,
				artifactId
			]);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: OrbitPanel_module_css_default.artifactRow,
				children: [
					inline === null ? null : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
						className: OrbitPanel_module_css_default.artifactText,
						children: inline
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.artifacts,
						children: [inline !== null ? null : href ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("a", {
							className: OrbitPanel_module_css_default.artifact,
							href,
							target: "_blank",
							rel: "noopener",
							title: artifactId,
							children: t("artifactOpen", { name: artifactLabel(artifactId) })
						}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: OrbitPanel_module_css_default.artifact,
							title: artifactId,
							children: artifactLabel(artifactId)
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
							type: "button",
							className: OrbitPanel_module_css_default.artifact,
							disabled: busy,
							onClick: () => {
								setBusy(true);
								setFailed(null);
								call("exportArtifact", [sessionId, artifactId], new AbortController().signal).then((saved) => setPath(saved.path)).catch((reason) => setFailed(panelError(reason))).finally(() => setBusy(false));
							},
							children: busy ? t("working") : t("artifactExport")
						})]
					}),
					path ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("code", {
						className: OrbitPanel_module_css_default.artifactPath,
						title: path,
						children: path
					}) : null,
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
						t,
						error: failed
					})
				]
			});
		}
		function RunResult({ t, run, sessionId, call }) {
			if (run.live) return null;
			const failure = run.error ?? "";
			const { text: answer, artifacts } = failure ? {
				text: "",
				artifacts: []
			} : resultOutcome(run.result);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: OrbitPanel_module_css_default.resultBlock,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: OrbitPanel_module_css_default.resultLabel,
						children: t(failure ? "resultFailed" : "result")
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: `${OrbitPanel_module_css_default.outcome} ${OrbitPanel_module_css_default[`outcome_${dotState(run.status)}`]}`,
						children: t(`outcome_${run.status}`, { status: run.status })
					}),
					failure ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
						className: `${OrbitPanel_module_css_default.result} ${OrbitPanel_module_css_default.resultError}`,
						children: failure
					}) : null,
					artifacts.map((id) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ArtifactRow, {
						t,
						call,
						sessionId,
						artifactId: id
					}, id)),
					answer ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
						className: OrbitPanel_module_css_default.result,
						children: answer
					}) : null
				]
			});
		}
		function OrbitRunDetail({ call, t, sessionId, run, onBack }) {
			const open = true;
			const [steps, setSteps] = (0, react.useState)(null);
			const [error, setError] = (0, react.useState)(null);
			const load = (0, react.useCallback)((signal) => {
				call("getRunDetail", [sessionId, run.runId], signal).then((detail) => {
					if (!signal.aborted) {
						setSteps(detail.steps);
						setError(null);
					}
				}).catch((reason) => {
					if (!signal.aborted) setError(panelError(reason));
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
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(BackButton, {
				t,
				onBack
			}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: OrbitPanel_module_css_default.goalCard,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.goalHead,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
							state: dotState(run.status),
							size: 9,
							className: OrbitPanel_module_css_default.listDot
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: OrbitPanel_module_css_default.listMain,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: OrbitPanel_module_css_default.goalTitle,
								children: run.workflowName
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(FoldedText, {
								t,
								text: run.prompt,
								lines: PROMPT_LINES
							})]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(RunControls, {
						call,
						t,
						sessionId,
						run
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
						t,
						error
					}),
					!error && steps === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: OrbitPanel_module_css_default.empty,
						children: t("loading")
					}) : null,
					steps?.length ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: OrbitPanel_module_css_default.goalSteps,
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitStepList, {
							call,
							t,
							sessionId,
							runId: run.runId,
							steps,
							live: run.live,
							onSettled: setSteps
						})
					}) : null,
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(RunResult, {
						t,
						run,
						sessionId,
						call
					})
				]
			})] });
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
		/** The kinds that carry work, and so are the ones a missing prompt is news about. */
		const PROMPTED = /* @__PURE__ */ new Set(["action", "human"]);
		function StepRow({ t, step }) {
			const prompt = step.prompt.trim();
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: `${OrbitPanel_module_css_default.defnRow} ${OrbitPanel_module_css_default[`kind_${step.kind}`] ?? ""}`,
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
			const ran = runs.filter((run) => run.workflow.startsWith(`${workflow.workflow_id}@`));
			const [steps, setSteps] = (0, react.useState)(null);
			const [stepsError, setStepsError] = (0, react.useState)(null);
			(0, react.useEffect)(() => {
				const controller = new AbortController();
				setSteps(null);
				setStepsError(null);
				call("getWorkflowDefinition", [sessionId, workflow.workflow_id], controller.signal).then((detail) => {
					if (!controller.signal.aborted) setSteps(detail.nodes);
				}).catch((reason) => {
					if (!controller.signal.aborted) setStepsError(panelError(reason));
				});
				return () => controller.abort();
			}, [
				call,
				sessionId,
				workflow.workflow_id
			]);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)(BackButton, {
					t,
					onBack
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
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
					t,
					error: stepsError
				}),
				stepsError === null && steps === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
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
		/** Orbit's ring-and-satellite mark, kept inline so the folded control is self-contained. */
		function OrbitMark() {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("svg", {
				className: OrbitPanel_module_css_default.orbitMark,
				viewBox: "0 0 64 64",
				"aria-hidden": "true",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("circle", {
						className: OrbitPanel_module_css_default.orbitBackground,
						cx: "32",
						cy: "32",
						r: "32"
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("circle", {
						className: OrbitPanel_module_css_default.orbitRing,
						cx: "32",
						cy: "32",
						r: "18"
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("circle", {
						className: OrbitPanel_module_css_default.orbitSatellite,
						cx: "48",
						cy: "22",
						r: "6"
					})
				]
			});
		}
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
		const SHAPE_LIMIT = 8;
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
		/**
		* One authoring job, on the page where its result will land.
		*
		* Not the Goal page: authoring is not a Run, and a job among the Runs would
		* make "what is this Workspace working on" answer with two different kinds of
		* thing. It sits above the catalog it is about to change.
		*/
		function AuthoringRow({ t, job, sessionId }) {
			const [open, setOpen] = (0, react.useState)(false);
			const [chunks, setChunks] = (0, react.useState)([]);
			/** The markers, kept apart: they are the ladder, not console text. */
			const [markers, setMarkers] = (0, react.useState)([]);
			const [outputError, setOutputError] = (0, react.useState)(null);
			const settled = job.status === "done" || job.status === "failed";
			const state = job.status === "done" ? "done" : job.status === "failed" ? "error" : "ongoing";
			const progress = authoringProgress(markers, job.status);
			const label = job.status === "done" ? t("authoringDone") : job.status === "failed" ? t("authoringFailed") : job.status === "queued" ? t("authoringQueued") : t("authoringRunning");
			(0, react.useEffect)(() => {
				const outputHref = job.output_href;
				if (!outputHref) return;
				const controller = new AbortController();
				let timer;
				let after = 0;
				const tick = async () => {
					try {
						const page = await hostCall$1("getAuthoringOutput", [
							sessionId,
							outputHref,
							after
						], controller.signal);
						if (controller.signal.aborted) return;
						if (page.chunks.length) after = Math.max(...page.chunks.map((chunk) => chunk.chunk_id));
						const merge = (into, added) => {
							const byId = new Map(into.map((chunk) => [chunk.chunk_id, chunk]));
							for (const chunk of added) byId.set(chunk.chunk_id, chunk);
							return [...byId.values()].sort((a, b) => a.chunk_id - b.chunk_id);
						};
						const [found, visible] = [page.chunks.filter(isProgressMarker), page.chunks.filter((chunk) => !isProgressMarker(chunk))];
						setChunks((current) => merge(current, visible));
						if (found.length) setMarkers((current) => merge(current, found));
						setOutputError(null);
						if (!settled || page.has_more) timer = setTimeout(() => {
							tick();
						}, 1e3);
					} catch (reason) {
						if (!controller.signal.aborted) setOutputError(panelError(reason));
					}
				};
				tick();
				return () => {
					controller.abort();
					if (timer !== void 0) clearTimeout(timer);
				};
			}, [
				settled,
				sessionId,
				job.output_href
			]);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: OrbitPanel_module_css_default.authoringRow,
				"data-open": open || void 0,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.authoringSummary,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
								state,
								size: 8
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: OrbitPanel_module_css_default.authoringMain,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: OrbitPanel_module_css_default.authoringLabel,
									children: [label, job.requested_agent ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
										className: OrbitPanel_module_css_default.meta,
										children: [" · ", t("authoringBy", { agent: job.requested_agent })]
									}) : null]
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
									className: OrbitPanel_module_css_default.authoringPrompt,
									children: settled && job.status === "failed" && job.error ? job.error : job.prompt
								})]
							}),
							job.output_href ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: OrbitPanel_module_css_default.authoringOutputToggle,
								"aria-expanded": open,
								title: t(open ? "hideAgentOutput" : "showAgentOutput"),
								onClick: () => setOpen((value) => !value),
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconChevronDownOutline14, { className: open ? OrbitPanel_module_css_default.authoringChevronOpen : "" })
							}) : null
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: OrbitPanel_module_css_default.authoringStages,
						children: progress.stages.map((stage) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: OrbitPanel_module_css_default.stepDisclosure,
							children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: OrbitPanel_module_css_default.stepRow,
								children: [
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: `${OrbitPanel_module_css_default.stepDot} ${OrbitPanel_module_css_default[`stepDot_${stepDotState(stage.status)}`]}`,
										"aria-hidden": "true"
									}),
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: OrbitPanel_module_css_default.stepTitle,
										children: t(`stage_${stage.stage}`)
									}),
									stage.status === "running" && progress.attempt > 1 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: OrbitPanel_module_css_default.status,
										children: t("stageAttempt", {
											attempt: progress.attempt,
											max: progress.maxAttempts
										})
									}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: OrbitPanel_module_css_default.status,
										children: stage.status
									})
								]
							})
						}, stage.stage))
					}),
					open ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.authoringOutput,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
							t,
							error: outputError
						}), chunks.length ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", { children: chunks.map((chunk) => chunk.text).join("") }) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
							className: OrbitPanel_module_css_default.empty,
							children: t("agentOutputWaiting")
						})]
					}) : null
				]
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
			const [authoring, setAuthoring] = (0, react.useState)([]);
			/** Whether the close button has asked, and whether the answer is in flight. */
			const [confirmingStop, setConfirmingStop] = (0, react.useState)(false);
			const [stopping, setStopping] = (0, react.useState)(false);
			const [stopError, setStopError] = (0, react.useState)(null);
			/** Steps of the Runs still moving, so a list line can say where each one is. */
			const [steps, setSteps] = (0, react.useState)({});
			const [tab, setTab] = (0, react.useState)("goal");
			const [selected, setSelected] = (0, react.useState)(null);
			const [selectedFlow, setSelectedFlow] = (0, react.useState)(null);
			const [error, setError] = (0, react.useState)(null);
			const [connecting, setConnecting] = (0, react.useState)(false);
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
				const toggle = () => update(layout.dismissed ? {
					...readLayoutSafely(layout),
					dismissed: false,
					collapsed: false
				} : {
					...readLayoutSafely(layout),
					collapsed: !layout.collapsed
				});
				window.addEventListener("orbit:toggle-panel", toggle);
				return () => window.removeEventListener("orbit:toggle-panel", toggle);
			}, [layout, update]);
			(0, react.useEffect)(() => {
				const show = (event) => {
					const detail = event.detail;
					update({
						...readLayoutSafely(layout),
						dismissed: false,
						collapsed: false
					});
					if (detail?.tab === "workflows") {
						setSelected(null);
						setSelectedFlow(null);
						setTab("workflows");
					}
				};
				window.addEventListener("orbit:show-panel", show);
				return () => window.removeEventListener("orbit:show-panel", show);
			}, [layout, update]);
			(0, react.useEffect)(() => {
				if (!sessionId || layout.dismissed) {
					setRows([]);
					return;
				}
				if (!layout.collapsed && rows === null) {
					setConnecting(true);
					setError(null);
				}
				let timer;
				const controller = new AbortController();
				const tick = async () => {
					try {
						const forced = forceNext.current;
						forceNext.current = false;
						const state = await hostCall$1("getPanelState", [
							sessionId,
							forced,
							!layout.collapsed
						], controller.signal);
						if (controller.signal.aborted) return;
						const workflowNames = new Map((state.workflows ?? []).map((item) => [item.workflow_id, item.name || item.workflow_id]));
						const next = orderRows(state.runs.map((run) => toRow(run, workflowNames.get(run.workflow_id))));
						setRows(next);
						setUiUrl(state.uiUrl);
						setError(null);
						setWorkflows(state.workflows ?? []);
						setAgents(state.agents ?? []);
						setSteps((current) => {
							const held = {};
							for (const row of goalRuns(next)) {
								const kept = current[row.runId];
								if (kept) held[row.runId] = kept;
							}
							return {
								...held,
								...state.steps
							};
						});
						setAuthoring(state.authoring ?? []);
						setAsking(false);
						setConnecting(false);
						const authoringLive = state.authoring.some((job) => job.status === "queued" || job.status === "running");
						timer = setTimeout(() => {
							tick();
						}, layout.collapsed ? ORBIT_IDLE_MS : authoringLive ? ORBIT_POLL_MS : nextInterval(next));
					} catch (reason) {
						if (controller.signal.aborted) return;
						setConnecting(false);
						setError(panelError(reason));
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
				layout.dismissed,
				asked
			]);
			const counts = summarise(rows ?? []);
			const chosen = (rows ?? []).find((row) => row.runId === selected);
			const chosenFlow = workflows.find((item) => item.workflow_id === selectedFlow);
			const goal = goalRuns(rows ?? []);
			const settled = (rows ?? []).filter((row) => !row.live);
			const box = placePanel(layout, bounds);
			if (layout.dismissed) return null;
			if (layout.collapsed) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
				type: "button",
				className: OrbitPanel_module_css_default.badge,
				onClick: () => update({
					...layout,
					collapsed: false
				}),
				"aria-label": t("expand"),
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitMark, {})
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
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconPanelLeftOutline16, { size: 14 })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: `${OrbitPanel_module_css_default.iconButton} ${OrbitPanel_module_css_default.stopButton}`,
								disabled: stopping,
								onClick: () => setConfirmingStop(true),
								"aria-label": t("stopRuntime"),
								title: t("stopRuntime"),
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconCloseOutline16, { size: 14 })
							})
						]
					}),
					confirmingStop ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: OrbitPanel_module_css_default.confirmBar,
						role: "alertdialog",
						"aria-label": t("stopRuntime"),
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: OrbitPanel_module_css_default.confirmText,
								children: t("stopRuntimeAsk")
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: OrbitPanel_module_css_default.confirmActions,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									className: OrbitPanel_module_css_default.confirmCancel,
									onClick: () => setConfirmingStop(false),
									children: t("stopCancel")
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									className: OrbitPanel_module_css_default.confirmGo,
									disabled: stopping,
									onClick: () => {
										setStopping(true);
										setStopError(null);
										hostCall$1("stopRuntime", [sessionId], new AbortController().signal).then(() => {
											setConfirmingStop(false);
											setRows([]);
											setSteps({});
											update({
												...readLayoutSafely(layout),
												dismissed: true
											});
										}).catch((reason) => setStopError(panelError(reason))).finally(() => setStopping(false));
									},
									children: stopping ? t("working") : t("stopConfirm")
								})]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
								t,
								error: stopError
							})
						]
					}) : null,
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
							connecting ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: OrbitPanel_module_css_default.connecting,
								role: "status",
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: OrbitPanel_module_css_default.connectSpinner,
									"aria-hidden": "true"
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: t("connectingRuntime") })]
							}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)(PanelErrorText, {
								t,
								error
							}),
							!connecting && error === null && rows === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("loading")
							}) : null,
							!connecting && error === null && rows !== null && sessionId !== void 0 && tab === "goal" ? goal.length ? goal.map((row) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitRunGoalCard, {
								call: hostCall$1,
								t,
								sessionId,
								run: row,
								steps: steps[row.runId],
								onSettled: (settled) => setSteps((current) => ({
									...current,
									[row.runId]: settled
								}))
							}, row.runId)) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("emptyGoal")
							}) : null,
							!connecting && error === null && rows !== null && tab === "history" ? settled.length ? settled.map((row) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(OrbitRunListRow, {
								t,
								run: row,
								onOpen: () => setSelected(row.runId)
							}, row.runId)) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: OrbitPanel_module_css_default.empty,
								children: t("emptyHistory")
							}) : null,
							!connecting && error === null && sessionId && tab === "workflows" ? authoring.map((job) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AuthoringRow, {
								t,
								job,
								sessionId
							}, job.job_id)) : null,
							!connecting && error === null && tab === "workflows" ? workflows.length ? workflows.map((item) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
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
							!connecting && error === null && tab === "agents" ? agents.length ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
								className: OrbitPanel_module_css_default.agentsGrid,
								children: agents.map((item) => {
									const mark = agentMark(item.name);
									const attempts = item.attempt_count ?? 0;
									const failed = item.failed_count ?? 0;
									return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", {
										className: OrbitPanel_module_css_default.agentCard,
										children: [
											/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
												className: OrbitPanel_module_css_default.agentHead,
												children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: OrbitPanel_module_css_default.avatar,
													style: mark.style,
													"aria-hidden": true,
													children: mark.initials
												}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
													className: OrbitPanel_module_css_default.agentIdentity,
													children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
														className: OrbitPanel_module_css_default.agentName,
														children: item.name.replace(/^agent\./u, "")
													}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
														className: OrbitPanel_module_css_default.agentVersion,
														children: item.version
													})]
												})]
											}),
											/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
												className: OrbitPanel_module_css_default.agentStat,
												children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: OrbitPanel_module_css_default.agentStatLabel,
													children: t("agentRunsLabel")
												}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: OrbitPanel_module_css_default.agentStatPill,
													children: t("agentRuns", { count: attempts })
												})]
											}),
											failed > 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
												className: OrbitPanel_module_css_default.agentStat,
												children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: OrbitPanel_module_css_default.agentStatLabel,
													children: t("agentFailedLabel")
												}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: `${OrbitPanel_module_css_default.agentStatPill} ${OrbitPanel_module_css_default.agentStatError}`,
													children: t("agentFailed", { count: failed })
												})]
											}) : null
										]
									}, item.name);
								})
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
			stopRuntime: "Stop Orbit",
			stopRuntimeAsk: "Stop Orbit for this Workspace? Runs in flight are interrupted, and Orbit’s own UI and any other Session lose it too.",
			stopCancel: "Keep it running",
			stopConfirm: "Stop Orbit",
			dock: "Dock to the side",
			float: "Detach",
			empty: "No runs in this Workspace yet.",
			loading: "Asking Orbit…",
			connectingRuntime: "Starting Orbit and connecting to MCP…",
			disconnected: "No Orbit Runtime is serving this Workspace.",
			liveCount: "{live} running of {total}",
			idleCount: "{total} runs",
			status: "Status",
			refresh: "Refresh",
			back: "Back",
			needsUpgrade: "Upgrade needed",
			needsMigration: "Cannot upgrade",
			authoringRunning: "Writing a workflow…",
			authoringQueued: "Queued to be written…",
			authoringDone: "Published",
			authoringFailed: "Could not be written",
			authoringBy: "by {agent}",
			stage_generating: "Drafting",
			stage_validating: "Compiling",
			stage_publishing: "Publishing",
			stageAttempt: "try {attempt} of {max}",
			showAgentOutput: "Show Agent output",
			hideAgentOutput: "Hide Agent output",
			agentOutputWaiting: "Waiting for Agent output…",
			factRuns: "Runs ({total})",
			neverRun: "This workflow has never run here.",
			factSteps: "Steps ({total})",
			stepsLoading: "Reading the definition…",
			noPrompt: "This step was authored without a prompt.",
			openThisInOrbit: "Open in Orbit for the graph →",
			noOutput: "No output from this step.",
			errHostGone: "This page has lost the app behind it. Restart it, then reload.",
			errNoRuntime: "Orbit is not running for this Workspace. Reopen the panel to start it.",
			errStartFailed: "Orbit would not start here. Hover for what it said on the way out.",
			errDiscoveryFailed: "The orbit command did not answer properly. Check that it is installed and on PATH.",
			errRuntimeAddress: "Orbit is running but published no address this panel can use.",
			errRuntimeConflict: "More than one Orbit Runtime claims this Workspace. Stop the extra one.",
			errNoWorkspace: "This Session has no project folder open, so there is nothing for Orbit to work on.",
			errNoSession: "The panel lost its Harness Session. Reopen it.",
			errWorkspaceMismatch: "That request is for a different project than this Session has open.",
			errRunElsewhere: "This run was started somewhere else. Act on it there, or in Orbit.",
			errStopRefused: "Orbit would not stop. It may be finishing something first.",
			errRequestTooLarge: "That request is too big to send from here.",
			errPromptLength: "The prompt has to be between 1 and 20000 characters.",
			errImageOnly: "Only images can be brought into the conversation this way.",
			errNoAnswer: "The Agent finished without an answer to submit.",
			errAborted: "That request was cancelled.",
			errProtocol: "Orbit sent something this panel could not read. This one is worth reporting.",
			errVersionMismatch: "This Orbit speaks a version of the integration this panel does not.",
			errTimeout: "Orbit did not answer in time. It may still be busy — try again.",
			errUnreachable: "Could not reach Orbit. It may have stopped or be restarting.",
			errWorkflowDeleted: "That workflow has been deleted.",
			errWorkflowGone: "That workflow is no longer published. The list may be out of date — refresh.",
			errRunMoved: "This run moved on while the page was open. Refresh and try again.",
			errRunGone: "That run is no longer here.",
			errGoalActive: "A goal is already running here. Wait for it, or cancel it first.",
			errAuthoringActive: "A workflow is already being written here. Wait for it to finish.",
			errNoAgent: "No Agent is available in this Session to do that.",
			errNotAllowed: "Orbit refused that. This Session is not allowed to do it.",
			errArtifactTooLarge: "That file is too large to hand over here. Open it from Orbit.",
			errUnknown: "Something went wrong. Hover for the details.",
			result: "Result",
			resultFailed: "Why it failed",
			outcome_completed: "Finished",
			outcome_failed: "Failed",
			outcome_cancelled: "Cancelled",
			outcome_unknown: "Outcome unknown — nobody has ruled on it",
			outcome_running: "Running",
			outcome_waiting: "Waiting",
			outcome_interrupted: "Waiting for an answer",
			artifactOpen: "Open {name}",
			artifactExport: "Save as a file",
			promptExpand: "Expand",
			promptCollapse: "Collapse",
			progressCount: "{done}/{total}",
			progressOn: "· {step}",
			progressBlockedOn: "· waiting at {step}",
			needsPerson: "Waiting for a person to confirm what the external Agent did.",
			cancel: "Cancel run",
			resume: "Continue",
			approve: "Approve",
			reject: "Reject",
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
			emptyWorkflows: "No workflow has been published here yet.",
			emptyHistory: "No run has finished here yet.",
			emptyAgents: "This Runtime registered no Agent handlers.",
			agentRunsLabel: "Executions",
			agentRuns: "{count} times",
			agentFailedLabel: "Failed",
			agentFailed: "{count} times",
			togglePanel: "Show or hide the Orbit panel",
			askWhatRuns: "List the workflows that can run here",
			generateCommandDescription: "Generate an Orbit workflow from a description",
			generateUsage: "Usage: /orbit-generate <workflow description>",
			runHead: "Run with ",
			runTail: ": "
		};
		const zh = {
			title: "Orbit",
			expand: "显示 Orbit 运行",
			collapse: "收起 Orbit 运行",
			openRuntime: "在新标签页打开 Orbit",
			stopRuntime: "停止 Orbit",
			stopRuntimeAsk: "停止这个 Workspace 的 Orbit？进行中的 Run 会被中断，Orbit 自己的 UI 和其他会话也会一起失去它。",
			stopCancel: "继续运行",
			stopConfirm: "停止 Orbit",
			dock: "停靠到侧边",
			float: "浮动",
			empty: "这个 Workspace 还没有运行记录。",
			loading: "正在询问 Orbit…",
			connectingRuntime: "正在启动 Orbit 并连接 MCP…",
			disconnected: "没有 Orbit Runtime 在服务这个 Workspace。",
			liveCount: "{total} 个运行中有 {live} 个进行中",
			idleCount: "{total} 个运行",
			status: "状态",
			refresh: "刷新",
			back: "返回",
			needsUpgrade: "需要升级",
			needsMigration: "无法升级",
			authoringRunning: "正在生成工作流…",
			authoringQueued: "已排队，等待生成…",
			authoringDone: "已发布",
			authoringFailed: "生成失败",
			authoringBy: "由 {agent} 执行",
			stage_generating: "起草",
			stage_validating: "编译校验",
			stage_publishing: "发布",
			stageAttempt: "第 {attempt}/{max} 次",
			showAgentOutput: "显示 Agent 输出",
			hideAgentOutput: "收起 Agent 输出",
			agentOutputWaiting: "正在等待 Agent 输出…",
			factRuns: "运行记录（{total}）",
			neverRun: "这个工作流还没有在这里跑过。",
			factSteps: "步骤（{total}）",
			stepsLoading: "正在读取定义…",
			noPrompt: "这一步没有写提示词。",
			openThisInOrbit: "在 Orbit 中查看流程图 →",
			noOutput: "这个步骤没有输出。",
			errHostGone: "这个页面背后的服务已经停止。重启它，然后刷新页面。",
			errNoRuntime: "Orbit 没有在这个 Workspace 上运行。重新打开面板会启动它。",
			errStartFailed: "Orbit 在这里启动失败了。把鼠标移上去可以看到它退出前说了什么。",
			errDiscoveryFailed: "orbit 命令没有正常返回。检查它是否已安装、是否在 PATH 中。",
			errRuntimeAddress: "Orbit 在运行，但没有公布面板可以使用的地址。",
			errRuntimeConflict: "有多个 Orbit 运行时都声称管理这个 Workspace。请停掉多余的那个。",
			errNoWorkspace: "当前会话没有打开项目目录，Orbit 没有可以工作的对象。",
			errNoSession: "面板与 Harness 会话断开了。重新打开面板。",
			errWorkspaceMismatch: "这个请求指向的项目，和当前会话打开的不是同一个。",
			errRunElsewhere: "这个运行是在别处启动的。请回到启动它的地方操作，或者去 Orbit 里操作。",
			errStopRefused: "Orbit 拒绝停止。它可能正在收尾。",
			errRequestTooLarge: "这个请求太大，没法从这里发出去。",
			errPromptLength: "提示词长度必须在 1 到 20000 个字符之间。",
			errImageOnly: "只有图片可以用这种方式带进对话。",
			errNoAnswer: "Agent 结束了，但没有产出可以提交的回答。",
			errAborted: "这个请求被取消了。",
			errProtocol: "Orbit 返回了面板无法解析的内容。这一类值得反馈。",
			errVersionMismatch: "这个 Orbit 的集成协议版本和面板对不上。",
			errTimeout: "Orbit 没有及时回应。它可能还在忙，可以再试一次。",
			errUnreachable: "连不上 Orbit。它可能已经停了，或者正在重启。",
			errWorkflowDeleted: "这个工作流已经被删除了。",
			errWorkflowGone: "这个工作流已不在发布列表里。列表可能过期了，刷新一下。",
			errRunMoved: "这个运行在页面打开期间发生了变化。刷新后再试。",
			errRunGone: "这个运行已经不在了。",
			errGoalActive: "这里已经有一个目标在执行。等它结束，或者先取消。",
			errAuthoringActive: "这里已经有一个工作流在生成。等它完成。",
			errNoAgent: "这个会话里没有可用的 Agent 来做这件事。",
			errNotAllowed: "Orbit 拒绝了这个操作。这个会话没有权限。",
			errArtifactTooLarge: "这个文件太大，没法从这里递出来。请在 Orbit 里打开。",
			errUnknown: "出错了。把鼠标移上去可以看到原始信息。",
			result: "结果",
			resultFailed: "失败原因",
			outcome_completed: "已完成",
			outcome_failed: "失败",
			outcome_cancelled: "已取消",
			outcome_unknown: "结果未知 —— 还没有人裁定",
			outcome_running: "执行中",
			outcome_waiting: "等待中",
			outcome_interrupted: "等待回答",
			artifactOpen: "打开 {name}",
			artifactExport: "导出为文件",
			promptExpand: "展开",
			promptCollapse: "收起",
			progressCount: "{done}/{total}",
			progressOn: "· {step}",
			progressBlockedOn: "· 卡在 {step}",
			needsPerson: "等待人工确认外部 Agent 的执行结果。",
			cancel: "取消运行",
			resume: "继续",
			approve: "通过",
			reject: "拒绝",
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
			emptyWorkflows: "这个 Workspace 还没有发布过工作流。",
			emptyHistory: "这里还没有结束的运行。",
			emptyAgents: "这个 Runtime 没有注册任何 Agent handler。",
			agentRunsLabel: "执行次数",
			agentRuns: "{count} 次",
			agentFailedLabel: "失败",
			agentFailed: "{count} 次",
			togglePanel: "显示或收起 Orbit 面板",
			askWhatRuns: "列出这里可运行的工作流",
			generateCommandDescription: "根据描述生成 Orbit 工作流",
			generateUsage: "用法：/orbit-generate <工作流描述>",
			runHead: "用",
			runTail: "执行："
		};
		//#endregion
		//#region src/client/composer-caret.ts
		/** Putting the caret where the person is about to type.
		*
		* Picking a Workflow writes a half-finished sentence into the draft — "用
		* 「名字」执行：" — and leaves the goal for the person to add. So the caret
		* belongs at the end of it. It was not going there: the draft is written
		* through the input machine, and the machine has no caret. The composer's own
		* `restoreCaret` is scoped to the cut and paste handlers that already hold the
		* element, and nothing on the plugin-facing facade — `setDraft`,
		* `insertReference`, `insertText`, `track` — carries a caret position. The
		* caret lives in a `<textarea>` in the shell's DOM, and reaching it is the
		* only way there is.
		*
		* Which is a real cost, so the reach is kept small and honest: one attribute
		* selector, a rule that refuses to guess between two candidates, and no
		* assumption that the write has landed by the time we look.
		*/
		/** The composer's textarea. `data-phase` is the input machine's own phase,
		*  written on the element it belongs to, so it marks a composer rather than
		*  any textarea a page happens to contain. */
		const COMPOSER_SELECTOR = "textarea[data-phase]";
		/**
		* The composer to act on, or nothing.
		*
		* Focus first: with more than one Session rendered, the focused composer is
		* the one the person is looking at. Otherwise the only one, if there is only
		* one. Two unfocused candidates is a genuine ambiguity, and moving the caret
		* in the wrong conversation is worse than leaving it where it was.
		*/
		function pickComposer(candidates, focused) {
			if (candidates.length === 0) return null;
			const active = candidates.find((candidate) => candidate === focused);
			if (active !== void 0) return active;
			return candidates.length === 1 ? candidates[0] ?? null : null;
		}
		/**
		* Whether this element is showing the draft we just wrote.
		*
		* Guards against two races at once: a render that has not happened yet, and a
		* person who has already started typing. The first shows the old draft, the
		* second shows a longer one — and in both cases the answer is to leave the
		* caret alone rather than to move it somewhere that made sense a moment ago.
		*/
		function draftHasLanded(value, expected) {
			return value === expected;
		}
		/**
		* Put the caret at the end of the draft the machine says it has.
		*
		* `expected` comes from the input's own snapshot rather than from rebuilding
		* the string here: inserting a reference splices a placeholder and sometimes a
		* space, and a second opinion about what that produced would be wrong the day
		* the shell changes it.
		*
		* Focus comes with it. The pick happened in a popup, which had focus; leaving
		* the caret correct in an unfocused box would be a caret nobody is typing at.
		*/
		function caretToEnd(expected, attempts = 6) {
			if (typeof document === "undefined" || typeof requestAnimationFrame !== "function") return;
			const attempt = (left) => {
				const composer = pickComposer([...document.querySelectorAll(COMPOSER_SELECTOR)].filter((node) => node instanceof HTMLTextAreaElement), document.activeElement);
				if (composer !== null && draftHasLanded(composer.value, expected)) {
					const end = composer.value.length;
					composer.focus({ preventScroll: true });
					composer.setSelectionRange(end, end);
					return;
				}
				if (left > 0) requestAnimationFrame(() => {
					attempt(left - 1);
				});
			};
			requestAnimationFrame(() => {
				attempt(attempts);
			});
		}
		//#endregion
		//#region src/client/index.tsx
		const PANEL_COMMAND = "orbit";
		const LIST_COMMAND = "orbit-workflows";
		const GENERATE_COMMAND = "orbit-generate";
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
					clipboardText: (ref) => `${MARK_OPEN}${ref}${MARK_CLOSE}`,
					serialize: async (ref) => ref
				}
			}), "orbit: slash command folding the panel");
		}
		/** `/orbit-generate` starts the existing authoring flow and reveals its row. */
		function registerGenerateSlashSource(ctx, t) {
			const inputTriggers = ctx.get("inputTriggers");
			if (!inputTriggers) throw new Error("Orbit /orbit-generate requires the Harness inputTriggers service");
			const claim = (session) => ({
				token: `/${GENERATE_COMMAND} `,
				submit: async (args) => {
					const prompt = args.trim();
					if (!prompt) return {
						kind: "error",
						text: t("generateUsage")
					};
					showOrbitPanel("workflows");
					try {
						await hostCall("generateWorkflowForSession", [session.sessionId, prompt], new AbortController().signal);
						return { kind: "success" };
					} catch (reason) {
						const failure = panelError(reason);
						return {
							kind: "error",
							text: t(failure.key, failure.values)
						};
					}
				}
			});
			ctx.effect(() => inputTriggers.registerSource({
				trigger: "/",
				name: GENERATE_COMMAND,
				order: -9,
				showGroupTitle: false,
				candidates: async (session, request) => GENERATE_COMMAND.includes(request.query.toLowerCase()) ? [{
					name: GENERATE_COMMAND,
					description: t("generateCommandDescription")
				}] : [],
				onPick: (pick) => ({ claim: claim(pick.session) }),
				matchSpace: (session, token) => token === `/${GENERATE_COMMAND}` ? { claim: claim(session) } : void 0,
				matchEnter: async (session, line) => new RegExp(`^/${GENERATE_COMMAND}(?:\\s|$)`, "u").test(line.trim()) ? { claim: claim(session) } : void 0
			}), "orbit: slash command generating a workflow");
		}
		/**
		* Bring the panel out, wherever it was put.
		*
		* Distinct from `orbit:toggle-panel`, which flips: a command that toggles is a
		* command that hides the panel for anyone who already had it open. This one
		* only ever shows, so running an Orbit command twice is not a way to lose
		* sight of what it did.
		*
		* The panel is where an Orbit command's result actually appears — a Run's
		* steps, a Workflow being written — so a command that starts work behind a
		* hidden panel has reported nothing. Called before the work rather than after
		* it, so a failure is met by an open panel too.
		*/
		function showOrbitPanel(tab) {
			window.dispatchEvent(new CustomEvent("orbit:show-panel", { detail: tab === void 0 ? {} : { tab } }));
		}
		const MARK_OPEN = "「";
		const MARK_CLOSE = "」";
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
						showOrbitPanel();
						return ((await hostCall("getPanelState", [
							session.sessionId,
							false,
							true
						], signal)).workflows ?? []).map((item) => {
							const label = item.name || item.workflow_id;
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
						input.setDraft(`${head}${MARK_OPEN}${option.label}${MARK_CLOSE}${t("runTail")}`);
						caretToEnd(input.state.getSnapshot().draft);
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
			registerGenerateSlashSource(ctx, t);
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