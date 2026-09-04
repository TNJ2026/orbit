/* Client-side behaviour that no server test can reach.
 *
 * api.js and i18n.js are pure modules — no DOM — so they run directly under
 * node with a stubbed fetch. What is covered here is exactly what broke during
 * manual verification: error-status mapping, idempotency-key reuse across
 * retries, and locale-aware formatting.
 *
 * Run by tests/test_ui_client_modules.py, which skips when node is absent.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const assets = resolve(here, "../../src/orbit/static/workflow-ui/assets");

const { Api, ApiError } = await import(`${assets}/api.js`);
const { I18n, preferredLocale, LOCALES } = await import(`${assets}/i18n.js`);
const { readRoute, routeHash } = await import(`${assets}/router.js`);
const { dataState } = await import(`${assets}/components/data-state.js`);
const { semanticWorkflowDiff } = await import(`${assets}/workflow-diff.js`);
const { humanResponseValue, resumeActions } = await import(`${assets}/run-resume.js`);

function catalog(locale) {
  return JSON.parse(readFileSync(`${assets}/i18n.${locale}.json`, "utf8"));
}

function stubFetch(responses) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, ...options });
    const next = responses.shift() ?? { status: 200, body: {} };
    if (next.throws) throw new Error("connection refused");
    return {
      ok: next.status < 400,
      status: next.status,
      text: async () =>
        typeof next.body === "string" ? next.body : JSON.stringify(next.body),
    };
  };
  return calls;
}

// node exposes crypto as a getter-only property; define over it.
function stubUuid(next) {
  Object.defineProperty(globalThis, "crypto", {
    value: { randomUUID: next }, configurable: true, writable: true,
  });
}
stubUuid(() => "uuid-1");

/* -- Workflow semantic diff ---------------------------------------------- */

test("workflow diff ignores formatting and object-key order", () => {
  const before = JSON.stringify({
    metadata: { name: "Review", id: "review" }, nodes: [{ id: "done", kind: "terminal" }], edges: [],
  });
  const after = JSON.stringify({
    edges: [], nodes: [{ kind: "terminal", id: "done" }], metadata: { id: "review", name: "Review" },
  }, null, 2);
  assert.deepEqual(semanticWorkflowDiff(before, after), {
    addedNodes: [], removedNodes: [], changedNodes: [],
    addedEdges: [], removedEdges: [], changedEdges: [], workflowFields: [], changeCount: 0,
  });
});

test("workflow diff reports graph and workflow changes by stable identity", () => {
  const before = JSON.stringify({
    metadata: { id: "review", name: "Review" }, entry: ["draft"],
    nodes: [
      { id: "draft", kind: "action", handler: { name: "agent.a", version: "1.0.0" } },
      { id: "done", kind: "terminal" },
    ],
    edges: [{ id: "finish", from: { node: "draft", port: "out" }, to: { node: "done", port: "in" } }],
  });
  const after = JSON.stringify({
    metadata: { id: "review", name: "Review with approval" }, entry: ["draft"],
    nodes: [
      { id: "draft", kind: "action", handler: { name: "agent.b", version: "1.0.0" } },
      { id: "approve", kind: "human" },
    ],
    edges: [{ id: "finish", from: { node: "draft", port: "out" }, to: { node: "approve", port: "in" } }],
  });
  const diff = semanticWorkflowDiff(before, after);
  assert.deepEqual(diff.addedNodes, ["approve"]);
  assert.deepEqual(diff.removedNodes, ["done"]);
  assert.deepEqual(diff.changedNodes, [{ id: "draft", fields: ["handler"] }]);
  assert.deepEqual(diff.changedEdges, [{ id: "finish", fields: ["to"] }]);
  assert.deepEqual(diff.workflowFields, ["metadata"]);
  assert.equal(diff.changeCount, 5);
});

test("workflow diff preserves raw-source fallback for non-JSON DSL", () => {
  assert.equal(semanticWorkflowDiff("nodes: []", "nodes: [changed]"), null);
});

/* -- interrupted runs ---------------------------------------------------- */

test("resume actions identify every pending interrupt", () => {
  const command = { command: "langgraph_run.resume", label: "Resume" };
  const actions = resumeActions({ interrupts: [
    { id: "interrupt:left", value: { node_id: "left" } },
    { id: "interrupt:right", value: { node_id: "right" } },
  ] }, command);

  assert.deepEqual(actions.map((item) => item.label), ["Resume · left", "Resume · right"]);
  assert.deepEqual(actions.map((item) => item.payload.interrupt_id), [
    "interrupt:left", "interrupt:right",
  ]);
});

test("human resume defaults to the declared result port", () => {
  const [action] = resumeActions({ interrupts: [{
    id: "interrupt:review",
    value: {
      node_id: "review",
      output_ports: [{ id: "result", schema_id: "schema:review" }],
    },
  }] }, { command: "langgraph_run.resume", label: "Resume" });

  assert.deepEqual(action.payload, {
    interrupt_id: "interrupt:review",
    value: { result: { decision: "approve", value: null } },
  });
  // An approval is answered by choosing, so the view draws two buttons for
  // it rather than a field of JSON to edit.
  assert.equal(action.approval, true);
});

test("an interrupt that is not an approval is left to the raw answer", () => {
  // Not a broken approval — a question with no single port to reply on is a
  // different question, and pretending it is a yes/no would send nonsense.
  const [action] = resumeActions({ interrupts: [{
    id: "interrupt:form",
    value: { node_id: "form", output_ports: [{ id: "a" }, { id: "b" }] },
  }] }, { command: "langgraph_run.resume", label: "Resume" });

  assert.equal(action.approval, false);
});

test("human decisions share one canonical approve and reject encoder", () => {
  const interrupt = { value: { output_ports: [{ id: "result" }] } };
  assert.deepEqual(humanResponseValue(interrupt, "approve"), {
    result: { decision: "approve", value: null },
  });
  assert.deepEqual(humanResponseValue(interrupt, "reject"), {
    result: { decision: "reject", value: null },
  });
  assert.throws(() => humanResponseValue(interrupt, "approved"), /approve or reject/);
});

test("a rejection carries the reason it was rejected for", () => {
  const interrupt = { value: { output_ports: [{ id: "result" }] } };
  assert.deepEqual(humanResponseValue(interrupt, "reject", "缺少版本号"), {
    result: { decision: "reject", value: "缺少版本号" },
  });
  // Nothing typed is `null` rather than an empty string: an approval's
  // declared schema is checked against the answer given when there is no
  // reason, and `null` is the one it is checked with.
  assert.deepEqual(humanResponseValue(interrupt, "reject", "   "), {
    result: { decision: "reject", value: null },
  });
  // Approving takes no reason at all, and never grows a key for one.
  assert.deepEqual(humanResponseValue(interrupt, "approve"), {
    result: { decision: "approve", value: null },
  });
});

/* -- error mapping -------------------------------------------------------- */

test("http statuses map to distinct message keys", async () => {
  const cases = [
    [401, null, "error.unauthenticated"],
    [403, null, "error.forbidden"],
    [404, null, "error.generic"],
    [409, null, "error.conflict"],
    [409, "command_in_progress", "error.commandInProgress"],
    [422, null, "error.generic"],
    [429, null, "error.rateLimited"],
    [503, null, "error.generic"],
    [500, null, "error.generic"],
  ];
  for (const [status, code, expected] of cases) {
    stubFetch([{ status, body: { error: { code, message: "no" } } }]);
    const failed = await new Api().get("/api/v1/runs").catch((error) => error);
    assert.ok(failed instanceof ApiError, `${status} did not raise ApiError`);
    assert.equal(failed.messageKey, expected, `status ${status}`);
  }
});

test("a dead server is a network error, not a crash", async () => {
  stubFetch([{ throws: true }]);
  const failed = await new Api().get("/api/v1/runs").catch((error) => error);
  assert.ok(failed instanceof ApiError);
  assert.equal(failed.messageKey, "error.network");
});

test("a non-JSON error body does not become a SyntaxError", async () => {
  // A framework 404 is plain text. Parsing it blindly blanked the whole page.
  stubFetch([{ status: 404, body: "Not Found" }]);
  const failed = await new Api().get("/api/v1/runs/x/plan").catch((error) => error);
  assert.ok(failed instanceof ApiError);
  assert.equal(failed.status, 404);
});

test("only a conflict asks the caller to refresh", async () => {
  const conflict = new ApiError(409, null, "stale");
  const forbidden = new ApiError(403, null, "no");
  assert.equal(conflict.requiresRefresh, true);
  assert.equal(forbidden.requiresRefresh, false);
});

/* -- idempotency ---------------------------------------------------------- */

const command = {
  command: "run.cancel",
  method: "POST",
  href: "/api/v1/runs/run:1/cancel",
  target_aggregate_id: "run:1",
  expected_version: 3,
  payload_schema: "run-cancel/1.0",
};

test("a retried command reuses its idempotency key", async () => {
  let counter = 0;
  stubUuid(() => `uuid-${++counter}`);
  const api = new Api();
  const calls = stubFetch([
    { status: 500, body: { error: { code: "boom" } } },
    { status: 200, body: { data: {} } },
  ]);

  await api.execute(command, {}).catch(() => {});
  await api.execute(command, {});

  assert.equal(calls.length, 2);
  assert.equal(
    calls[0].headers["idempotency-key"],
    calls[1].headers["idempotency-key"],
    "a retry of the same intent must not start a second command",
  );
});

test("a conflict retires the key so the next attempt is a new intent", async () => {
  let counter = 0;
  stubUuid(() => `uuid-${++counter}`);
  const api = new Api();
  const calls = stubFetch([
    { status: 409, body: { error: { code: "version_conflict" } } },
    { status: 200, body: { data: {} } },
  ]);

  await api.execute(command, {}).catch(() => {});
  await api.execute({ ...command, expected_version: 4 }, {});

  assert.notEqual(
    calls[0].headers["idempotency-key"],
    calls[1].headers["idempotency-key"],
    "acting on refreshed state is a different intent and needs a fresh key",
  );
});

test("a command posts the server's href and expected version verbatim", async () => {
  const calls = stubFetch([{ status: 200, body: { data: {} } }]);
  await new Api().execute(command, { reason: "because" });
  assert.equal(calls[0].url, "/api/v1/runs/run:1/cancel");
  assert.equal(calls[0].method, "POST");
  assert.deepEqual(JSON.parse(calls[0].body), {
    expected_version: 3,
    reason: "because",
  });
});

test("capabilities reads the shell identity and deployment facts endpoint", async () => {
  const calls = stubFetch([{ status: 200, body: { data: { actor: "local" } } }]);
  const response = await new Api().capabilities();
  assert.equal(calls[0].url, "/api/v1/capabilities");
  assert.equal(response.data.actor, "local");
});

test("workflow detail encodes identity and asks for no particular version", async () => {
  const calls = stubFetch([{ status: 200, body: { data: {} } }]);
  await new Api().workflowDetail("workflow:launch plan", 3);
  // Extra arguments cannot bring version selection back: the catalog serves
  // the current definition and only that.
  assert.equal(calls[0].url, "/api/v1/workflows/workflow%3Alaunch%20plan");
});

test("authoring jobs follow the operator's whole workspace", async () => {
  const calls = stubFetch([{ status: 200, body: { data: { jobs: [] } } }]);
  await new Api().authoringJobs({ active: true, type: "generate" });
  assert.equal(
    calls[0].url,
    "/api/v1/workflow-authoring-jobs?mine=false&active=true&type=generate",
  );
});


test("live reads use their versioned API view", async () => {
  const calls = stubFetch([
    { status: 200, body: { data: {} } },
  ]);
  const api = new Api();
  await api.live("opaque+/=");
  assert.equal(calls[0].url, "/api/v1/live?cursor=opaque%2B%2F%3D");
});

/* -- shell primitives ---------------------------------------------------- */

test("router parses deep links and serialises navigation", () => {
  assert.deepEqual(readRoute("#/runs/run%3A7"), { view: "run", runId: "run:7" });
  assert.deepEqual(readRoute("#/history/run%3A7"), { view: "goal", runId: "run:7" });
  assert.deepEqual(readRoute("#/goals/run%3A7"), { view: "goal", runId: "run:7" });
  assert.deepEqual(readRoute("#/history"), { view: "goals", runId: null });
  assert.deepEqual(readRoute("#/goals"), { view: "goals", runId: null });
  assert.deepEqual(readRoute("#/workflows"), { view: "workflows", runId: null });
  assert.deepEqual(
    readRoute("#/workflows/workflow%3Ashared/edit"),
    { view: "workflowEdit", workflowId: "workflow:shared", runId: null },
  );
  assert.deepEqual(
    readRoute("#/workflows/workflow%3Aold/edit/draft%3Aold"),
    { view: "workflow", workflowId: "workflow:old", runId: null },
  );
  // Retired views still parse (render() redirects them to the workspace,
  // normalising the hash); artifacts deep links fall through to home;
  // unknown ones land on the workspace too.
  assert.deepEqual(readRoute("#/agents"), { view: "agents", runId: null });
  assert.deepEqual(readRoute("#/inbox"), { view: "inbox", runId: null });
  assert.deepEqual(readRoute("#/settings"), { view: "settings", runId: null });
  assert.deepEqual(readRoute("#/artifacts"), { view: "artifacts", runId: null });
  assert.deepEqual(readRoute("#/not-a-view"), { view: "home", runId: null });
  assert.equal(routeHash({ view: "run", runId: "run:7" }), "#/runs/run%3A7");
  assert.equal(routeHash({ view: "goal", runId: "run:7" }), "#/history/run%3A7");
  assert.equal(routeHash({ view: "goals", runId: null }), "#/history");
  assert.equal(
    routeHash({ view: "workflowEdit", workflowId: "workflow:shared" }),
    "#/workflows/workflow%3Ashared/edit",
  );
});

test("generic data states carry text roles and retry actions", () => {
  const el = (tag, props = {}, children = []) => ({ tag, props, children });
  const i18n = { t: (key) => key };
  for (const kind of ["loading", "empty", "error", "stale", "pending"]) {
    const state = dataState(el, i18n, kind);
    assert.equal(state.props.class, `data-state ${kind}`);
    assert.equal(state.props.role, kind === "error" ? "alert" : "status");
    assert.equal(state.children[0].props.text, `state.${kind}`);
  }
  const retriable = dataState(el, i18n, "error", { onRetry: () => {} });
  assert.equal(retriable.children[1].children[0].props.text, "state.retry");
});

/* -- i18n ----------------------------------------------------------------- */

test("both catalogs load and share every key", () => {
  const zh = new I18n("zh-CN", catalog("zh-CN"));
  const en = new I18n("en-US", catalog("en-US"));
  assert.deepEqual(Object.keys(zh.messages).sort(), Object.keys(en.messages).sort());
});

test("a missing key is reported loudly, not silently blank", () => {
  const i18n = new I18n("en-US", {});
  assert.equal(i18n.t("nope.missing"), "nope.missing");
  assert.ok(i18n.missing.has("nope.missing"));
});

test("placeholders are substituted in both locales", () => {
  for (const locale of LOCALES) {
    const i18n = new I18n(locale, catalog(locale));
    const rendered = i18n.t("newRun.active.exists", { goal: "run:7" });
    assert.ok(rendered.includes("run:7"), `${locale}: ${rendered}`);
    assert.ok(!rendered.includes("{goal}"));
  }
});

test("numbers and dates follow the locale", () => {
  const zh = new I18n("zh-CN", catalog("zh-CN"));
  const en = new I18n("en-US", catalog("en-US"));
  const when = "2026-07-18T14:05:23Z";
  assert.notEqual(zh.dateTime(when), en.dateTime(when));
  assert.equal(en.number(1234567), "1,234,567");
});

test("an unparseable timestamp is shown as-is rather than as Invalid Date", () => {
  const i18n = new I18n("en-US", catalog("en-US"));
  assert.equal(i18n.dateTime("not-a-date"), "not-a-date");
  assert.equal(i18n.dateTime(null), "");
});

test("a command label falls back to the server's own label", () => {
  const i18n = new I18n("en-US", catalog("en-US"));
  assert.equal(i18n.command({ command: "run.cancel", label: "X" }), "Cancel run");
  assert.equal(
    i18n.command({ command: "brand.new", label: "Brand new" }),
    "Brand new",
    "a command shipped ahead of its translation must still be clickable",
  );
});

test("an unknown status shows the raw server value", () => {
  const i18n = new I18n("en-US", catalog("en-US"));
  assert.equal(i18n.status("succeeded"), "Succeeded");
  assert.equal(i18n.status("quiescing"), "quiescing");
});

test("the browser's language picks the initial locale", () => {
  assert.equal(preferredLocale(null, ["zh-CN"]), "zh-CN");
  assert.equal(preferredLocale(null, ["zh-TW"]), "zh-CN");
  assert.equal(preferredLocale(null, ["fr-FR"]), "en-US");
  assert.equal(preferredLocale("en-US", ["zh-CN"]), "en-US", "a stored choice wins");
});

test("resume actions take their label from the caller", () => {
  const command = { command: "langgraph_run.resume", label: "Resume LangGraph workflow" };
  const run = { interrupts: [{ id: "i1", value: { node_id: "review" } }] };

  // The Runtime names a command for every client at once; this UI says it in
  // the reader's language, and the node suffix is still appended to whatever
  // it says.
  const [action] = resumeActions(run, command, "继续");
  assert.equal("继续", action.label);

  const two = resumeActions(
    { interrupts: [{ id: "a", value: { node_id: "one" } },
                   { id: "b", value: { node_id: "two" } }] },
    command, "继续",
  );
  assert.deepEqual(["继续 · one", "继续 · two"], two.map((item) => item.label));

  // Omitted, it still falls back to the command's own label.
  assert.equal(command.label, resumeActions({ interrupts: [] }, command)[0].label);
});
