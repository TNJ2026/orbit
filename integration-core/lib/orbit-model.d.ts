/** What the panel shows, and how often it asks.
 *
 * Separated from the view because the interesting decisions here are not
 * visual: which Runs count as live, how fast to ask while any of them is, and
 * how to stop asking when none is. React only renders the answer.
 */
import type { OutputChunk, RunDto, StepSummary } from './types.js';
import { isLive } from './run-progress.js';
export { isLive };
export { goalRuns, progressOf, type RunProgress } from './run-progress.js';
/** Cadence while a Run is moving. */
export declare const ORBIT_POLL_MS = 2000;
/** Cadence while nothing is. A resident panel costs nothing when idle. */
export declare const ORBIT_IDLE_MS = 15000;
export interface OrbitRunRow {
    readonly runId: string;
    readonly goal: string;
    readonly workflow: string;
    /** The catalog's human-facing name, falling back to the workflow id. */
    readonly workflowName: string;
    readonly status: string;
    readonly live: boolean;
    readonly revision: number;
    readonly artifactCount: number;
    readonly updatedAt: string;
    /** What it was asked to work on — the request behind the goal's label. */
    readonly prompt: string;
    /** What it produced, once it has produced anything. */
    readonly result: unknown;
    /** Why it failed, when it did. */
    readonly error?: string;
    /** What Orbit says may be done to this Run right now, at this revision. */
    readonly commands: readonly {
        command: string;
        expected_version: number;
    }[];
    /** What it is stopped waiting for, when it is stopped waiting for a person. */
    readonly interrupts: readonly RunInterrupt[];
}
/** A question a Run has stopped to ask, in the terms needed to answer it. */
export interface RunInterrupt {
    readonly id: string;
    readonly nodeId: string;
    /** What kind of answer the node was authored to want; `approval` is a yes/no. */
    readonly taskKind: string;
    /** The port the answer belongs on, which the question carries so a caller
     *  never has to fetch the definition to learn where to put it. */
    readonly outputPort: string;
}
/** Read the interrupts a Run advertises, keeping only the ones answerable here.
 *
 * An interrupt with no output port is a question this panel cannot form an
 * answer to — the reply would have to name a port, and guessing one produces a
 * node that "returned undeclared outputs" minutes later. */
export declare function toInterrupts(items: readonly unknown[] | undefined): RunInterrupt[];
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
export declare function approvalValue(interrupt: RunInterrupt, decision: 'approve' | 'reject'): Record<string, unknown>;
export declare function toRow(run: RunDto, workflowName?: string): OrbitRunRow;
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
export declare function promptText(inputs: unknown): string;
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
export declare function resultText(value: unknown): string;
export interface RunOutcome {
    /** What the Run produced, once the Artifact references are out of it. */
    readonly text: string;
    /** The Artifacts it produced, in the order the result named them. */
    readonly artifacts: readonly string[];
}
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
export declare function resultOutcome(value: unknown): RunOutcome;
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
export declare function artifactHref(sessionId: string, artifactId: string): string;
/** The short name an Artifact is offered under: its id without the kind. */
export declare function artifactLabel(artifactId: string): string;
/** How soon to ask again, given what the last answer contained. */
export declare function nextInterval(rows: readonly OrbitRunRow[]): number;
/** Newest first, with anything still running ahead of anything finished.
 *
 * A resident panel is read at a glance, and the glance is almost always about
 * what is happening now — so recency alone would bury a running Run under a
 * pile of Runs that already have their answer.
 */
export declare function orderRows(rows: readonly OrbitRunRow[]): OrbitRunRow[];
/** A one-line count for the collapsed badge. */
export declare function summarise(rows: readonly OrbitRunRow[]): {
    live: number;
    total: number;
};
/** The four states the shell's StateDot draws, from an Orbit status.
 *
 * `unknown` is amber rather than red on purpose: it is the outcome nobody has
 * ruled on yet, and colouring it as a failure would answer a question the
 * Runtime deliberately left open.
 */
export declare function dotState(status: string): 'done' | 'warning' | 'ongoing' | 'error';
/** A step card uses a still dot: history records outcomes, not activity. */
export declare function stepDotState(status: string): 'success' | 'error' | 'skipped' | 'warning' | 'ongoing';
export interface OrbitStepRow {
    readonly nodeId: string;
    readonly label: string;
    readonly status: string;
    readonly hasOutput: boolean;
    readonly needsPerson: boolean;
    readonly delegationId?: string;
}
export declare function toStepRow(step: StepSummary): OrbitStepRow;
/** Join an output page into displayable text, oldest chunk first. */
export declare function outputText(chunks: readonly OutputChunk[]): string;
/** Merge a new page into what is already shown without duplicating a chunk. */
export declare function mergeChunks(previous: readonly OutputChunk[], next: readonly OutputChunk[]): OutputChunk[];
/** The revision a command may be issued at, or undefined if it may not be.
 *
 * Read from what the Run advertises rather than from what the panel last drew:
 * a button offered for a command Orbit has since withdrawn is a button that
 * fails, and one offered at a stale revision is worse — it succeeds against a
 * Run the reader was not looking at.
 */
export declare function commandRevision(row: OrbitRunRow, command: string): number | undefined;
//# sourceMappingURL=orbit-model.d.ts.map