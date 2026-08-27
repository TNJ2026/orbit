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
import { type OrbitErrorKey } from '@orbit-runtime/integration-core';
export declare const MESSAGES: Readonly<Record<OrbitErrorKey, string>>;
/** The sentence for a key, and never an identifier: an unlisted key is a bug
 *  here, not something a reader should be shown. */
export declare function sentenceFor(key: OrbitErrorKey): string;
/** The vocabulary this dictionary must cover, re-exported so the check has one
 *  thing to compare against. */
export declare const COVERED: readonly OrbitErrorKey[];
