/** What the panel shows, and how often it asks.
 *
 * Separated from the view because the interesting decisions here are not
 * visual: which Runs count as live, how fast to ask while any of them is, and
 * how to stop asking when none is. React only renders the answer.
 */
import { isLive, labelOf } from './run-progress.js';
// Re-exported rather than re-implemented: the Host decides which Runs to read
// steps for using the same predicate the panel sorts and polls by, and a second
// list of terminal statuses is a second answer waiting to drift.
export { isLive };
export { goalRuns, progressOf } from './run-progress.js';
/** Cadence while a Run is moving. */
export const ORBIT_POLL_MS = 2_000;
/** Cadence while nothing is. A resident panel costs nothing when idle. */
export const ORBIT_IDLE_MS = 15_000;
/** Read the interrupts a Run advertises, keeping only the ones answerable here.
 *
 * An interrupt with no output port is a question this panel cannot form an
 * answer to — the reply would have to name a port, and guessing one produces a
 * node that "returned undeclared outputs" minutes later. */
export function toInterrupts(items) {
    const found = [];
    for (const item of items ?? []) {
        if (item === null || typeof item !== 'object')
            continue;
        const held = item;
        const value = held.value;
        if (value === null || typeof value !== 'object')
            continue;
        const asked = value;
        const ports = Array.isArray(asked.output_ports) ? asked.output_ports : [];
        const first = ports[0];
        const config = (asked.config ?? {});
        if (typeof held.id !== 'string' || typeof first?.id !== 'string')
            continue;
        found.push({
            id: held.id,
            nodeId: typeof asked.node_id === 'string' ? asked.node_id : '',
            taskKind: typeof config.task_kind === 'string' ? config.task_kind : '',
            outputPort: first.id,
        });
    }
    return found;
}
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
export function approvalValue(interrupt, decision) {
    return { [interrupt.outputPort]: { decision } };
}
export function toRow(run, workflowName) {
    return {
        runId: run.run_id,
        goal: run.goal || run.run_id,
        workflow: `${run.workflow_id}@${String(run.workflow_version)}`,
        workflowName: workflowName || run.workflow_id,
        status: run.status,
        live: isLive(run.status),
        revision: run.revision,
        artifactCount: run.artifact_count,
        updatedAt: run.updated_at,
        prompt: promptText(run.inputs),
        result: run.result,
        ...(typeof run.error === 'string' && run.error ? { error: run.error } : {}),
        commands: run.allowed_commands,
        interrupts: toInterrupts(run.interrupts),
    };
}
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
export function promptText(inputs) {
    if (inputs === null || typeof inputs !== 'object' || Array.isArray(inputs))
        return '';
    const entries = Object.entries(inputs);
    if (!entries.length)
        return '';
    const [only] = entries;
    if (entries.length === 1 && only !== undefined && typeof only[1] === 'string')
        return only[1];
    try {
        return JSON.stringify(inputs, null, 2) ?? '';
    }
    catch {
        return '';
    }
}
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
export function resultText(value) {
    if (value === null || value === undefined)
        return '';
    if (typeof value === 'string')
        return value;
    if (typeof value === 'number' || typeof value === 'boolean')
        return String(value);
    if (typeof value === 'object' && !Array.isArray(value)) {
        const entries = Object.entries(value);
        const [only] = entries;
        if (entries.length === 1 && only !== undefined && typeof only[1] === 'string')
            return only[1];
    }
    try {
        return JSON.stringify(value, null, 2) ?? '';
    }
    catch {
        return String(value);
    }
}
/** What an Artifact id looks like wherever a result happens to carry one. */
const ARTIFACT = /^langgraph_artifact:[A-Za-z0-9]+$/;
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
export function resultOutcome(value) {
    const artifacts = [];
    const remainder = strip(value, artifacts);
    return { artifacts, text: remainder === undefined ? '' : resultText(remainder) };
}
/** The value with its Artifact references taken out, or undefined if that is
 *  all it was. Containers left empty by the removal go too: `{"artifact_id":
 *  …}` is a wrapper around a door, not an answer with a door in it. */
function strip(value, into) {
    if (typeof value === 'string') {
        if (!ARTIFACT.test(value))
            return value;
        into.push(value);
        return undefined;
    }
    if (Array.isArray(value)) {
        const kept = value.map(item => strip(item, into)).filter(item => item !== undefined);
        return kept.length ? kept : undefined;
    }
    if (value !== null && typeof value === 'object') {
        const kept = {};
        for (const [key, item] of Object.entries(value)) {
            const left = strip(item, into);
            if (left !== undefined)
                kept[key] = left;
        }
        return Object.keys(kept).length ? kept : undefined;
    }
    return value;
}
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
export function artifactHref(sessionId, artifactId) {
    if (!sessionId || !artifactId)
        return '';
    const query = new URLSearchParams({ session: sessionId, id: artifactId });
    return `/plugins/dsh-orbit/artifact?${query.toString()}`;
}
/** The short name an Artifact is offered under: its id without the kind. */
export function artifactLabel(artifactId) {
    const bare = artifactId.replace(/^langgraph_artifact:/, '');
    return bare.length > 12 ? `${bare.slice(0, 12)}…` : bare;
}
/** How soon to ask again, given what the last answer contained. */
export function nextInterval(rows) {
    return rows.some(row => row.live) ? ORBIT_POLL_MS : ORBIT_IDLE_MS;
}
/** Newest first, with anything still running ahead of anything finished.
 *
 * A resident panel is read at a glance, and the glance is almost always about
 * what is happening now — so recency alone would bury a running Run under a
 * pile of Runs that already have their answer.
 */
export function orderRows(rows) {
    return [...rows].sort((a, b) => {
        if (a.live !== b.live)
            return a.live ? -1 : 1;
        return a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0;
    });
}
/** A one-line count for the collapsed badge. */
export function summarise(rows) {
    return { live: rows.filter(row => row.live).length, total: rows.length };
}
/** The four states the shell's StateDot draws, from an Orbit status.
 *
 * `unknown` is amber rather than red on purpose: it is the outcome nobody has
 * ruled on yet, and colouring it as a failure would answer a question the
 * Runtime deliberately left open.
 */
export function dotState(status) {
    if (status === 'completed')
        return 'done';
    if (status === 'unknown' || status === 'waiting')
        return 'warning';
    if (status === 'failed' || status === 'cancelled')
        return 'error';
    return 'ongoing';
}
/** A step card uses a still dot: history records outcomes, not activity. */
export function stepDotState(status) {
    if (status === 'succeeded')
        return 'success';
    if (status === 'failed' || status === 'cancelled')
        return 'error';
    if (status === 'not_reached')
        return 'skipped';
    if (status === 'unknown' || status === 'waiting')
        return 'warning';
    return 'ongoing';
}
export function toStepRow(step) {
    return {
        nodeId: step.node_id,
        label: labelOf(step),
        status: step.status,
        hasOutput: step.has_output === true,
        // The one step state a panel must not render as ordinary progress: the
        // Runtime is waiting for a person, and nothing moves until one answers.
        needsPerson: step.resolution?.kind === 'reconciliation_required'
            && step.reconciliation === undefined,
        ...(step.resolution?.delegation_id ? { delegationId: step.resolution.delegation_id } : {}),
    };
}
/** Join an output page into displayable text, oldest chunk first. */
export function outputText(chunks) {
    return [...chunks].sort((a, b) => a.chunk_id - b.chunk_id).map(chunk => chunk.text).join('');
}
/** Merge a new page into what is already shown without duplicating a chunk. */
export function mergeChunks(previous, next) {
    const byId = new Map(previous.map(chunk => [chunk.chunk_id, chunk]));
    for (const chunk of next)
        byId.set(chunk.chunk_id, chunk);
    return [...byId.values()].sort((a, b) => a.chunk_id - b.chunk_id);
}
/** The revision a command may be issued at, or undefined if it may not be.
 *
 * Read from what the Run advertises rather than from what the panel last drew:
 * a button offered for a command Orbit has since withdrawn is a button that
 * fails, and one offered at a stale revision is worse — it succeeds against a
 * Run the reader was not looking at.
 */
export function commandRevision(row, command) {
    return row.commands.find(item => item.command === command)?.expected_version;
}
