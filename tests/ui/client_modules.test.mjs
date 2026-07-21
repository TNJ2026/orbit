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
const {
  compatibleHandlers, diagnosticTarget, formatWorkflowSource, parseWorkflowSource,
  removeNode, removePolicy, replaceMetadata, replaceNode, replacePolicy,
} = await import(`${assets}/workflow-editor.js`);

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

test("workflow detail encodes identity and pins the requested version", async () => {
  const calls = stubFetch([{ status: 200, body: { data: {} } }]);
  await new Api().workflowDetail("workflow:launch plan", 3);
  assert.equal(calls[0].url, "/api/v1/workflows/workflow%3Alaunch%20plan?version=3");
});

/* -- workflow editor transforms ----------------------------------------- */

test("structured workflow edits produce a candidate without mutating the source", () => {
  const original = {
    dsl_version: "1.2",
    metadata: { id: "demo", name: "Demo", labels: {} },
    nodes: [{ id: "work", kind: "action", inputs: [], outputs: [] }],
    edges: [], entry: ["work"], terminals: ["work"],
  };
  const metadata = replaceMetadata(original, {
    name: "Renamed", description: "Edited", labels: { team: "local" },
  });
  const withNode = replaceNode(metadata, 0, {
    ...metadata.nodes[0], config: { prompt: "hello" },
  });
  assert.equal(original.metadata.name, "Demo");
  assert.equal(original.nodes[0].config, undefined);
  assert.equal(parseWorkflowSource(formatWorkflowSource(withNode)).metadata.name, "Renamed");
  assert.deepEqual(withNode.nodes[0].config, { prompt: "hello" });
});

test("node rename and removal update every graph reference", () => {
  const original = {
    nodes: [
      { id: "work", kind: "action" },
      { id: "done", kind: "terminal" },
    ],
    edges: [{
      id: "flow", from: { node: "work", port: "out" },
      to: { node: "done", port: "in" },
    }],
    entry: ["work"], terminals: ["done"],
  };
  const renamed = replaceNode(original, 0, { id: "start", kind: "action" });
  assert.deepEqual(renamed.entry, ["start"]);
  assert.equal(renamed.edges[0].from.node, "start");
  const removed = removeNode(renamed, 1);
  assert.deepEqual(removed.terminals, []);
  assert.deepEqual(removed.edges, []);
  assert.equal(original.nodes[0].id, "work");
});

test("handler choices require node kind and an exact input/output contract", () => {
  const node = {
    kind: "action",
    inputs: [{ id: "prompt", schema_id: "schema://object/1.0" }],
    outputs: [{ id: "result", schema_id: "schema://object/1.0" }],
  };
  const handlers = [
    {
      name: "agent.codex", version: "1.0.0", node_kinds: ["action"],
      inputs: { prompt: "schema://object/1.0" },
      outputs: { result: "schema://object/1.0" },
    },
    {
      name: "wrong-port", version: "1.0.0", node_kinds: ["action"],
      inputs: { value: "schema://object/1.0" }, outputs: {},
    },
    {
      name: "wrong-kind", version: "1.0.0", node_kinds: ["human"],
      inputs: { prompt: "schema://object/1.0" },
      outputs: { result: "schema://object/1.0" },
    },
  ];
  assert.deepEqual(
    compatibleHandlers(node, handlers).map((handler) => handler.name),
    ["agent.codex"],
  );
});

test("policy transforms update and remove every node and edge reference", () => {
  const original = {
    nodes: [{ id: "work", policies: ["retry"] }],
    edges: [{ id: "again", policy: "retry" }],
    policies: [{ id: "retry", kind: "retry", config: { max_attempts: 2 } }],
  };
  const renamed = replacePolicy(original, 0, {
    id: "bounded", kind: "retry", config: { max_attempts: 3 },
  });
  assert.deepEqual(renamed.nodes[0].policies, ["bounded"]);
  assert.equal(renamed.edges[0].policy, "bounded");
  const removed = removePolicy(renamed, 0);
  assert.deepEqual(removed.policies, []);
  assert.deepEqual(removed.nodes[0].policies, []);
  assert.equal("policy" in removed.edges[0], false);
});

test("diagnostics resolve structured entities and source coordinates", () => {
  assert.deepEqual(diagnosticTarget({
    json_path: "$.edges[2].mapping",
    source_range: { start: { line: 18, column: 5 } },
  }), {
    path: "$.edges[2].mapping", pane: "edges", index: 2, line: 18, column: 5,
  });
  assert.equal(diagnosticTarget({ path: "$.metadata.name" }).pane, "source");
});

test("ops and live reads use their versioned API views", async () => {
  const calls = stubFetch([
    { status: 200, body: { data: {} } },
    { status: 200, body: { data: {} } },
  ]);
  const api = new Api();
  await api.opsStatus();
  await api.live("opaque+/=");
  assert.equal(calls[0].url, "/api/v1/ops/status");
  assert.equal(calls[1].url, "/api/v1/live?cursor=opaque%2B%2F%3D");
});

/* -- shell primitives ---------------------------------------------------- */

test("router parses deep links and serialises navigation", () => {
  assert.deepEqual(readRoute("#/runs/run%3A7"), { view: "run", runId: "run:7" });
  assert.deepEqual(readRoute("#/goals/run%3A7"), { view: "goal", runId: "run:7" });
  assert.deepEqual(readRoute("#/artifacts/artifact%3A7"), {
    view: "artifact", artifactId: "artifact:7", runId: null,
  });
  assert.deepEqual(readRoute("#/inbox"), { view: "inbox", runId: null });
  assert.deepEqual(readRoute("#/workflows"), { view: "workflows", runId: null });
  assert.deepEqual(readRoute("#/agents"), { view: "agents", runId: null });
  assert.deepEqual(readRoute("#/settings"), { view: "settings", runId: null });
  assert.deepEqual(readRoute("#/not-a-view"), { view: "home", runId: null });
  assert.equal(routeHash({ view: "run", runId: "run:7" }), "#/runs/run%3A7");
  assert.equal(routeHash({ view: "goal", runId: "run:7" }), "#/goals/run%3A7");
  assert.equal(
    routeHash({ view: "artifact", artifactId: "artifact:7" }),
    "#/artifacts/artifact%3A7",
  );
  assert.equal(routeHash({ view: "ops", runId: null }), "#/ops");
  assert.equal(routeHash({ view: "agents", runId: null }), "#/agents");
  assert.equal(routeHash({ view: "settings", runId: null }), "#/settings");
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
    const rendered = i18n.t("newRun.started", { runId: "run:7" });
    assert.ok(rendered.includes("run:7"), `${locale}: ${rendered}`);
    assert.ok(!rendered.includes("{runId}"));
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
