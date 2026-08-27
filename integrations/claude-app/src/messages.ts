/** What each Orbit failure is called, in the words a terminal wants.
 *
 * The shared core decides *what* can go wrong — `ORBIT_ERROR_KEYS` is its
 * list, and its classifier is what turns a raw failure into one of them. What
 * it does not decide is how to say so, because that is not the same sentence
 * everywhere: the DeepSeek-Harness panel can say "Reopen the panel to start
 * it" because it has a panel, and here there is no panel to reopen. A host
 * that is a background MCP server tells the model what happened and what would
 * fix it, in one line, with no gestures in it.
 *
 * Every key has a sentence. A missing one would surface as an identifier, and
 * a test fails rather than letting that reach anybody.
 */

import { ORBIT_ERROR_KEYS, type OrbitErrorKey } from '@orbit-runtime/integration-core'

export const MESSAGES: Readonly<Record<OrbitErrorKey, string>> = {
  errHostGone: 'The connection to Orbit was refused. It is not listening on that address.',
  errNoRuntime: 'No Orbit Runtime is serving this project. Start one with `orbit serve`.',
  errStartFailed: 'Orbit would not start for this project.',
  errDiscoveryFailed: 'The `orbit` command did not answer. Check that it is installed and on PATH.',
  errRuntimeAddress: 'Orbit is running but published no address this client can use.',
  errRuntimeConflict: 'More than one Orbit Runtime claims this project. Stop the extra one.',
  errVersionMismatch: 'This Orbit speaks a version of the integration protocol this server does not.',
  errTimeout: 'Orbit did not answer in time. It may still be working.',
  errUnreachable: 'Could not reach Orbit. It may have stopped or be restarting.',
  errNoWorkspace: 'No project directory was given, so there is nothing for Orbit to work on.',
  errNoSession: 'This request arrived without a session.',
  errWorkspaceMismatch: 'That request names a different project than this server is serving.',
  errWorkflowDeleted: 'That workflow has been deleted.',
  errWorkflowGone: 'That workflow is no longer published. List the workflows again.',
  errRunMoved: 'That run moved on. Read it again before acting on it.',
  errRunGone: 'That run is no longer here.',
  errRunElsewhere: 'That run was started elsewhere. Act on it where it began, or in Orbit.',
  errGoalActive: 'A goal is already running here. Wait for it, or cancel it first.',
  errAuthoringActive: 'A workflow is already being written here. Wait for it to finish.',
  errNoAgent: 'No agent is available to do that.',
  errNoAnswer: 'The agent finished without an answer to submit.',
  errNotAllowed: 'Orbit refused that: this caller is not allowed to do it.',
  errStopRefused: 'Orbit would not stop. It may be finishing something first.',
  errArtifactTooLarge: 'That artifact is too large to return inline. Read it from Orbit.',
  errRequestTooLarge: 'That request is too large to send.',
  errPromptLength: 'The prompt has to be between 1 and 20000 characters.',
  errImageOnly: 'Only images can be carried this way.',
  errAborted: 'That request was cancelled.',
  errProtocol: 'Orbit sent something this server could not read. This one is worth reporting.',
  errUnknown: 'Orbit failed in a way this server does not recognise.',
}

/** The sentence for a key, and never an identifier: an unlisted key is a bug
 *  here, not something a reader should be shown. */
export function sentenceFor(key: OrbitErrorKey): string {
  return MESSAGES[key] ?? MESSAGES.errUnknown
}

/** The vocabulary this dictionary must cover, re-exported so the check has one
 *  thing to compare against. */
export const COVERED: readonly OrbitErrorKey[] = ORBIT_ERROR_KEYS
