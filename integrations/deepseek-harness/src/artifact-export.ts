/** Handing an Artifact over as a file somebody can open.
 *
 * Orbit keeps Artifacts in a content-addressed store: the file on disk is
 * named by the sha256 of its own bytes, carries no extension, is shared by
 * every Artifact with identical content, and is collected when nothing
 * references it. It is a real path, and it is the wrong path to hand anybody —
 * a person told "that is your file" will open it in an editor and save, and
 * saving corrupts every Artifact that shares those bytes.
 *
 * So the bytes are copied out to an ordinary file instead: named for the
 * Artifact, extended for its type, and belonging to the person rather than to
 * the store. What they get back is a path they can double-click and a copy
 * they are free to edit.
 */

/** What each recorded content type is called on a filesystem.
 *
 * Recorded, not sniffed. Orbit wrote down what the workflow produced, and a
 * guess made here would be a second opinion about the same bytes. */
const EXTENSIONS: Readonly<Record<string, string>> = {
  'text/markdown': '.md',
  'text/plain': '.txt',
  'text/html': '.html',
  'text/csv': '.csv',
  'application/json': '.json',
  'application/pdf': '.pdf',
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/gif': '.gif',
  'image/svg+xml': '.svg',
}

/**
 * The extension to save one Artifact under.
 *
 * The recorded name wins when there is one: a workflow that said what it was
 * writing knows better than a table does. Then the content type. Then `.bin`,
 * which is not a guess but a statement that nobody said.
 */
export function artifactExtension(
  contentType: string | null | undefined, filename?: string | null,
): string {
  const named = typeof filename === 'string' ? /\.[A-Za-z0-9]{1,8}$/.exec(filename) : null
  if (named) return named[0].toLowerCase()
  const type = (contentType ?? '').split(';')[0]?.trim().toLowerCase() ?? ''
  return EXTENSIONS[type] ?? '.bin'
}

/**
 * What the copy is called: the Artifact's own digest, shortened, plus a type.
 *
 * Named for the Artifact rather than for the Run or the moment, so exporting
 * the same Artifact twice writes the same file rather than a second copy with
 * a number after it — the bytes are identical by construction, since the name
 * they came from *is* their hash.
 */
export function artifactFilename(
  artifactId: string, contentType: string | null | undefined, filename?: string | null,
): string {
  const digest = artifactId.replace(/^langgraph_artifact:/, '').replace(/[^A-Za-z0-9]/g, '')
  const stem = digest.slice(0, 12) || 'artifact'
  return `orbit-${stem}${artifactExtension(contentType, filename)}`
}

/* Types whose bytes are the answer, rather than a file about it. A workflow
   that writes its reply as markdown has written the reply — making a reader
   click through to it is charging them a click for the thing they asked for. */
const READABLE_TYPES: readonly string[] = ['text/markdown', 'text/plain']

/** Past this it stops being something to read on a panel and starts being a
 *  document. Small enough that fetching it costs nothing worth measuring. */
export const READABLE_MAX_BYTES = 2048

/**
 * Whether an Artifact should be read here or handed over as a file.
 *
 * Decided from what Orbit recorded, before any bytes move: asking for a 2 MiB
 * PDF in order to discover it is a 2 MiB PDF is the round trip this exists to
 * avoid. Anything not plainly text, or not small, is a file — including the
 * types a browser could render, because rendering someone else's HTML inside
 * the panel's own page is not reading, it is hosting.
 */
export function readableAsText(
  contentType: string | null | undefined, sizeBytes: number | null | undefined,
): boolean {
  const type = (contentType ?? '').split(';')[0]?.trim().toLowerCase() ?? ''
  const size = typeof sizeBytes === 'number' && Number.isFinite(sizeBytes) ? sizeBytes : Infinity
  return READABLE_TYPES.includes(type) && size >= 0 && size < READABLE_MAX_BYTES
}
