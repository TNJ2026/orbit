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

/** A 32-bit FNV-1a of the source, as hex.
 *
 * Only a discriminator for the idempotency key, never an integrity claim —
 * `definition_hash` is the server's business and is a real digest. This one
 * has to be synchronous, which SubtleCrypto is not.
 */
export function sourceDiscriminator(text) {
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

/** The idempotency key for one publish attempt.
 *
 * Stable across a retry of the same attempt, so a request that was sent and
 * lost replays its receipt rather than colliding with itself. It changes when
 * the content does, because the author fixing a rejected document and
 * publishing again is a *different* attempt — reusing the key there would
 * answer them with `idempotency_conflict` instead of publishing their fix.
 */
export const publishKey = (workflowId, expectedVersion, source) =>
  `publish:${workflowId}:${expectedVersion}:${sourceDiscriminator(source)}`;

/** Publish the document as the next version of this workflow.
 *
 * The server compiles it again — publishing is never a promise the editor can
 * make on the compiler's behalf — and refuses if `expected_version` is no
 * longer the latest, which is what stops a stale tab from overwriting
 * somebody else's revision.
 */
export function publish(workflowId, document, expectedVersion) {
  const source = JSON.stringify(document);
  return request(
    `/api/v1/workflows/${encodeURIComponent(workflowId)}/versions`,
    {
      method: "POST",
      body: { source, expected_version: expectedVersion },
      key: publishKey(workflowId, expectedVersion, source),
    },
  );
}

/** The compiler's findings inside a rejection, or none if it was not that.
 *
 * A publish is refused for two quite different reasons and both arrive as one
 * status: the document did not compile, or somebody else published first. The
 * author can act on the first and only on the second by reloading, so the
 * editor has to tell them apart rather than say "rejected".
 */
export function diagnosticsOf(error) {
  try {
    return JSON.parse(error.message)?.diagnostics ?? null;
  } catch {
    return null;
  }
}

export const isConflict = (error) =>
  typeof error?.message === "string" && error.message.includes("publish conflict");

export { ApiError };
