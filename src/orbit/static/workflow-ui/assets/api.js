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

  listRuns({ cursor, limit = 25, activeOnly = false } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    if (activeOnly) params.set("active", "true");
    return this.get(`/api/v1/runs?${params}`);
  }

  runSummary(runId) {
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}`);
  }

  responsibilities(runId) {
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/responsibilities`);
  }

  runPage(runId, kind, cursor, limit = 25) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/${kind}?${params}`);
  }

  /* Plan reads are three calls on purpose. The server keeps definition,
     overlay and diff apart, and merging them here would put the distinction
     back at the mercy of the client. */

  planDefinition(runId, planVersion) {
    const suffix = planVersion === undefined ? "" : `?plan_version=${planVersion}`;
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/plan${suffix}`);
  }

  planOverlay(runId, planVersion) {
    const suffix = planVersion === undefined ? "" : `?plan_version=${planVersion}`;
    return this.get(
      `/api/v1/runs/${encodeURIComponent(runId)}/plan/overlay${suffix}`,
    );
  }

  planDiff(runId, baseVersion, targetVersion) {
    const params = new URLSearchParams({
      base_version: String(baseVersion), target_version: String(targetVersion),
    });
    return this.get(`/api/v1/runs/${encodeURIComponent(runId)}/plan/diff?${params}`);
  }

  inbox(cursor) {
    const params = new URLSearchParams({ limit: "25" });
    if (cursor) params.set("cursor", cursor);
    return this.get(`/api/v1/inbox?${params}`);
  }

  handlerCatalog() {
    return this.get("/api/v1/handler-catalog");
  }

  recovery() {
    return this.get("/api/v1/recovery");
  }

  health() {
    return this.get("/health/ready");
  }

  /** Starting a run is the one command with no prior aggregate to reference. */
  startRun(body, intent) {
    const scope = intent || "run.start";
    if (!this.pendingKeys.has(scope)) this.pendingKeys.set(scope, newIdempotencyKey());
    return this.request("POST", "/api/v1/runs", {
      body,
      idempotencyKey: this.pendingKeys.get(scope),
    }).then((result) => {
      this.pendingKeys.delete(scope);
      return result;
    });
  }
}
