/** What went wrong, said to a person rather than to whoever wrote the throw.
 *
 * Everything the panel calls travels Host → Gateway → MCP → Runtime, and each
 * layer wraps the last: what reaches a reader is `Error: {"error":"workflow
 * version not found: workflow:wf_7ba…@2"}` — four layers of packaging around
 * one fact, and the fact is not the one the reader needs. They need to know
 * whether to press the button again, go and start something, or fetch someone.
 *
 * So the raw text is classified rather than printed. It is kept, though, on
 * the element's `title`: a message nobody can quote is a message nobody can
 * get help with, and the classification is a guess that can be wrong.
 */
/**
 * Everything that can be said to have gone wrong.
 *
 * The vocabulary lives here rather than in a host's dictionary because it is
 * decided here: the table below is what turns a raw failure into one of these,
 * and a host that adds a key nothing classifies has written a sentence nobody
 * will ever see. A host supplies the wording — `errNoRuntime` reads differently
 * in a panel and in a terminal — and must supply it for every key.
 */
export declare const ORBIT_ERROR_KEYS: readonly ["errAborted", "errArtifactTooLarge", "errAuthoringActive", "errDiscoveryFailed", "errGoalActive", "errHostGone", "errImageOnly", "errNoAgent", "errNoAnswer", "errNoRuntime", "errNoSession", "errNoWorkspace", "errNotAllowed", "errPromptLength", "errProtocol", "errRequestTooLarge", "errRunElsewhere", "errRunGone", "errRunMoved", "errRuntimeAddress", "errRuntimeConflict", "errStartFailed", "errStopRefused", "errTimeout", "errUnknown", "errUnreachable", "errVersionMismatch", "errWorkflowDeleted", "errWorkflowGone", "errWorkspaceMismatch"];
export type OrbitErrorKey = (typeof ORBIT_ERROR_KEYS)[number];
export interface PanelError {
    /** The sentence to show. */
    readonly key: OrbitErrorKey;
    /** What it was that failed, when the sentence has a place for it. */
    readonly values?: Record<string, string | number>;
    /** The original text, for a tooltip and for quoting into a bug report. */
    readonly detail: string;
}
/**
 * Read one failure into something worth showing.
 *
 * An unrecognised failure is not dressed up as a known one: it says that
 * something went wrong and carries its own text, which is honest and still
 * lets the reader copy it. Guessing here would be worse than saying nothing —
 * a wrong diagnosis sends somebody to fix the wrong thing.
 */
export declare function panelError(reason: unknown): PanelError;
//# sourceMappingURL=error-text.d.ts.map