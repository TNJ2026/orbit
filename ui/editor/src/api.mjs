/**
 * The Runtime endpoints the editor uses. Nothing here decides anything about a
 * workflow; the server does, and this only carries the question and the answer.
 */

class ApiError extends Error {
  constructor(code, message, detail) {
    super(message);
    this.code = code;
    this.detail = detail;
  }
}

async function request(path, { method = "GET", body, key } = {}) {
  const headers = {};
  if (body !== undefined) {
    headers["content-type"] = "application/json";
    // Every write on this API is idempotent by key. The editor is one person
    // clicking, so a fresh key per call is right: two clicks mean two intents.
    headers["idempotency-key"] = key ?? crypto.randomUUID();
  }
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError("invalid_response", `${response.status} ${path}`, text);
  }
  if (!response.ok) {
    const error = payload?.error ?? {};
    throw new ApiError(
      error.code ?? "request_failed",
      error.message ?? `${response.status} ${path}`,
      error,
    );
  }
  return payload?.data ?? payload;
}

/** The authoring contract: what an editor may draw.
 *
 * Fetched rather than hard-coded. A canvas carrying its own copy of the node
 * kinds is a second definition of the boundary, and the two drift.
 */
export const authoringSchema = () => request("/api/v1/workflows/authoring-schema");

export const listWorkflows = () => request("/api/v1/workflows");

export const readWorkflow = (workflowId) =>
  request(`/api/v1/workflows/${encodeURIComponent(workflowId)}`);

/** Compile a document without publishing it.
 *
 * The only opinion about validity the editor ever holds is this answer: the
 * compiler knows whether a Handler is registered, whether two schemas are
 * compatible and whether a loop is bounded, and reproducing any of that here
 * would be a second compiler that is wrong at a different time.
 */
export const validate = (document, expectedVersion) =>
  request("/api/v1/workflows/validate", {
    method: "POST",
    // `expected_version` is the version the editor was opened against. Without
    // it a stale tab would validate — and later publish — over a revision
    // somebody else already made.
    body: {
      source: JSON.stringify(document),
      expected_version: expectedVersion ?? 0,
    },
  });

export { ApiError };
