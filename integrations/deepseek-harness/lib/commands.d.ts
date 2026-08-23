/** Which command a Run will accept, and at which revision. */
import type { RunDto } from './types.js';
export type OrbitRunCommand = 'langgraph_run.cancel' | 'langgraph_run.resume';
/**
 * The advertised entry for a command at the revision the caller was reading,
 * or undefined if there is none.
 *
 * Both halves matter. A command Orbit never offered is a call that would fail
 * at the Runtime anyway; a command offered at a *different* revision is worse,
 * because it would succeed — against a Run that moved after the caller looked
 * at it, doing the thing they asked to a state they never saw.
 */
export declare function advertisedAt(run: RunDto, command: OrbitRunCommand, expectedRevision: number): {
    command: string;
    expected_version: number;
} | undefined;
/** The wire tool one command is carried by. */
export declare function commandTool(command: OrbitRunCommand): 'cancel_run' | 'resume_run';
