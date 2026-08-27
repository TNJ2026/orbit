/** How far a Workflow being written has got, read from what it printed.
 *
 * Authoring has stages the way a Run has steps — it drafts, it compiles what
 * came back, it goes round again when the compiler refuses, and it publishes —
 * and the Runtime has always said so: `AuthoringJobService` writes a marker
 * into the job's console at each turn. Nothing read them. The panel matched
 * them only to drop them, so a job that spent a minute on its second attempt
 * showed one unchanging line, and the one question a person watching has —
 * *is it stuck or is it working* — had no answer on the page.
 *
 * A marker is a whole chunk whose text is the sentinel followed by JSON, so
 * this reads chunks rather than scanning text: an Agent that prints the
 * sentinel itself is printing inside a chunk of its own output, not writing a
 * marker, and must not be able to move the ladder.
 */
/** The three things authoring does. Repairing is not among them — see below. */
export declare const AUTHORING_STAGES: readonly ["generating", "validating", "publishing"];
export type AuthoringStageName = (typeof AUTHORING_STAGES)[number];
export type AuthoringStageStatus = 'succeeded' | 'running' | 'failed' | 'not_reached';
export interface AuthoringStage {
    readonly stage: AuthoringStageName;
    readonly status: AuthoringStageStatus;
}
export interface AuthoringProgress {
    readonly stages: readonly AuthoringStage[];
    /** Which try this is, or 0 when the job has not said yet. */
    readonly attempt: number;
    /** How many it is allowed, or 0 when unknown. */
    readonly maxAttempts: number;
}
interface Chunk {
    readonly text: string;
}
/** Whether this chunk is a progress marker rather than Agent output. */
export declare function isProgressMarker(chunk: Chunk): boolean;
/**
 * The ladder, from the markers a job has printed and the state it is in.
 *
 * The job's own status has the last word on the stages: a job that failed
 * failed at whatever rung it had reached, and one that is done reached all of
 * them — the markers stop when the process does, so a job killed mid-stage
 * would otherwise show that stage running for as long as anyone looked at it.
 */
export declare function authoringProgress(chunks: readonly Chunk[], jobStatus: string): AuthoringProgress;
export {};
