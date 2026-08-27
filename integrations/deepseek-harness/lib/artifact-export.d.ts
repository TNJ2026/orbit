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
/**
 * The extension to save one Artifact under.
 *
 * The recorded name wins when there is one: a workflow that said what it was
 * writing knows better than a table does. Then the content type. Then `.bin`,
 * which is not a guess but a statement that nobody said.
 */
export declare function artifactExtension(contentType: string | null | undefined, filename?: string | null): string;
/**
 * What the copy is called: the Artifact's own digest, shortened, plus a type.
 *
 * Named for the Artifact rather than for the Run or the moment, so exporting
 * the same Artifact twice writes the same file rather than a second copy with
 * a number after it — the bytes are identical by construction, since the name
 * they came from *is* their hash.
 */
export declare function artifactFilename(artifactId: string, contentType: string | null | undefined, filename?: string | null): string;
/** Past this it stops being something to read on a panel and starts being a
 *  document. Small enough that fetching it costs nothing worth measuring. */
export declare const READABLE_MAX_BYTES = 2048;
/**
 * Whether an Artifact should be read here or handed over as a file.
 *
 * Decided from what Orbit recorded, before any bytes move: asking for a 2 MiB
 * PDF in order to discover it is a 2 MiB PDF is the round trip this exists to
 * avoid. Anything not plainly text, or not small, is a file — including the
 * types a browser could render, because rendering someone else's HTML inside
 * the panel's own page is not reading, it is hosting.
 */
export declare function readableAsText(contentType: string | null | undefined, sizeBytes: number | null | undefined): boolean;
