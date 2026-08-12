/* The only place the UI talks to the Runtime.
 *
 * There is no endpoint table here beyond the read paths the UI navigates to.
 * Every mutation is executed from an `allowed_commands[]` entry the server
 * handed us — method, href, target aggregate and expected version included —
 * so the client can never invent an action the server would refuse.
 */

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message || code || `HTTP ${status}`);
    this.status = status;
    this.code = code;
    this.details = details || {};
  }

  /** The message key the UI shows for this failure. */
  get messageKey() {
    if (this.status === 401) return "error.unauthenticated";
    if (this.status === 403) return "error.forbidden";
    if (this.status === 429) return "error.rateLimited";
    if (this.code === "command_in_progress") return "error.commandInProgress";
    // Also a 409, but a different fact: nobody raced us — the Runtime will
    // not do this at all. Saying "it changed, try again" would send the
    // operator round a loop that cannot end.
    if (this.code === "invalid_command") return "error.rejected";
    if (this.status === 409) return "error.conflict";
    if (this.status === 0) return "error.network";
    return "error.generic";
  }

  /** A 409 means our copy is stale; the caller must refetch before retrying. */
  get requiresRefresh() {
    return this.status === 409;
  }
}

function newIdempotencyKey() {
  return crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random());
}

export class Api {
  constructor(base = "") {
    this.base = base;
    // One key per user intent, kept across retries of the same intent so a
    // network retry can never start a second run.
    this.pendingKeys = new Map();
  }

  async request(method, path, { body, idempotencyKey } = {}) {
    const headers = {};
    if (body !== undefined) headers["content-type"] = "application/json";
    if (idempotencyKey) headers["idempotency-key"] = idempotencyKey;

    let response;
    try {
      response = await fetch(`${this.base}${path}`, {
        method,
        headers,
        credentials: "same-origin",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new ApiError(0, "network_error", String(cause));
    }

    const text = await response.text();
    // Not every response is our own envelope: a framework 404 or a proxy's
    // error page is plain text, and parsing it blindly turns a handled HTTP
    // status into an unhandled SyntaxError.
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      if (response.ok) throw new ApiError(response.status, "invalid_response", text.slice(0, 200));
      payload = null;
    }
    if (!response.ok) {
      const error = (payload && payload.error) || {};
      throw new ApiError(
        response.status, error.code, error.message || text.slice(0, 200), error.details,
      );
    }
    return payload;
  }

  get(path) {
    return this.request("GET", path);
  }

  /** Execute a server-advertised command. `intent` scopes the idempotency key. */
  async execute(allowed, payload, intent) {
    const scope = intent || `${allowed.command}:${allowed.target_aggregate_id}`;
    if (!this.pendingKeys.has(scope)) this.pendingKeys.set(scope, newIdempotencyKey());
    const key = this.pendingKeys.get(scope);
    try {
      const result = await this.request(allowed.method, allowed.href, {
        body: { expected_version: allowed.expected_version, ...payload },
        idempotencyKey: key,
      });
      this.pendingKeys.delete(scope);
      return result;
    } catch (error) {
      // A stale version means the next attempt is a different intent against a
      // different state, so it must not reuse this key.
      if (error instanceof ApiError && error.requiresRefresh) this.pendingKeys.delete(scope);
      throw error;
    }
  }

  listRuns({
    cursor, limit = 25, activeOnly = false, terminalOnly = false,
    q = "", status = "", responsibility = "",
  } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    if (activeOnly) params.set("active", "true");
    if (terminalOnly) params.set("terminal", "true");
    if (q.trim()) params.set("q", q.trim());
    if (status) params.set("status", status);
    if (responsibility) params.set("responsibility", responsibility);
    return this.get(`/api/v1/runs?${params}`);
  }

  dashboard() {
    return this.get("/api/v1/dashboard");
  }

  runSummary(runId) {
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}`);
  }

  langGraphRuns({ limit = 25, status = "" } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (status) params.set("status", status);
    return this.get(`/api/v1/langgraph-runs?${params}`);
  }

  langGraphRun(runId) {
    return this.get(`/api/v1/langgraph-runs/${encodeURIComponent(runId)}`);
  }

  responsibilities(runId) {
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/responsibilities`);
  }

  outcome(runId) {
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/outcome`);
  }

  runOutput(runId, after = 0, limit = 200, nodeRunId = "") {
    const params = new URLSearchParams({ limit: String(limit) });
    if (after) params.set("after", String(after));
    if (nodeRunId) params.set("node_run_id", nodeRunId);
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/output?${params}`);
  }


  artifacts({ cursor, q = "", runId = "", contentType = "", limit = 25 } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    if (q.trim()) params.set("q", q.trim());
    if (runId.trim()) params.set("run_id", runId.trim());
    if (contentType.trim()) params.set("content_type", contentType.trim());
    return this.get(`/api/v1/artifacts?${params}`);
  }

  artifact(artifactId) {
    return this.get(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`);
  }

  async artifactPreview(artifactId) {
    const path = `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`;
    let response;
    try {
      response = await fetch(`${this.base}${path}`, { credentials: "same-origin" });
    } catch (cause) {
      throw new ApiError(0, "network_error", String(cause));
    }
    if (!response.ok) {
      let payload = null;
      try { payload = await response.json(); } catch { /* handled below */ }
      const failure = payload?.error || {};
      throw new ApiError(response.status, failure.code, failure.message);
    }
    return response.text();
  }

  /** Inline content, for an <img> the browser fetches itself. */
  artifactContentUrl(artifactId) {
    return `${this.base}/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`;
  }

  artifactDownloadUrl(artifactId) {
    return `${this.artifactContentUrl(artifactId)}?download=true`;
  }

  /* Plan reads are three calls on purpose. The server keeps definition,
     overlay and diff apart, and merging them here would put the distinction
     back at the mercy of the client. */








  graph(runId, planVersion) {
    const suffix = planVersion === undefined ? "" : `?plan_version=${planVersion}`;
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/graph${suffix}`);
  }


  capabilities() {
    return this.get("/api/v1/capabilities");
  }


  handlerCatalog() {
    return this.get("/api/v1/handler-catalog");
  }

  workflowCatalog() {
    return this.get("/api/v1/workflows");
  }

  workflowTemplates() {
    return this.get("/api/v1/workflow-templates");
  }

  workflowDraft(draftId) {
    return this.get(`/api/v1/workflow-drafts/${encodeURIComponent(draftId)}`);
  }

  /** The current definition. There is no older one to ask for. */
  workflowDetail(workflowId) {
    return this.get(`/api/v1/workflows/${encodeURIComponent(workflowId)}`);
  }

  authoringJobs({ active = false, type = "" } = {}) {
    const params = new URLSearchParams({ mine: "true" });
    if (active) params.set("active", "true");
    if (type) params.set("type", type);
    return this.get(`/api/v1/workflow-authoring-jobs?${params}`);
  }



  live(cursor) {
    const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    return this.get(`/api/v1/live${suffix}`);
  }

  /* `/health/ready` answers outside the envelope and uses 503 for "not
     ready" — a degraded runtime is a valid answer here, not an exception. */
  async health() {
    let response;
    try {
      response = await fetch(`${this.base}/health/ready`, { credentials: "same-origin" });
    } catch (cause) {
      throw new ApiError(0, "network_error", String(cause));
    }
    let payload = null;
    try { payload = await response.json(); } catch { /* treated as unknown */ }
    return { ok: response.ok, status: payload?.status || "unknown", checks: payload?.checks || {} };
  }

}
