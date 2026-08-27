#!/usr/bin/env node
/** Orbit as a stdio MCP server.
 *
 * Point a client at it with the project it should serve:
 *
 *     claude-orbit --project-root /path/to/project
 *
 * or leave the flag off and it serves the directory it was launched in, which
 * is what a client that starts servers per project already gives it.
 *
 * stdout carries JSON-RPC and nothing else — a stray `console.log` here is a
 * protocol violation, which is why every diagnostic goes to stderr.
 */
/** The project to serve, from the flag or from where this was launched. */
export declare function projectRootFrom(argv: readonly string[], cwd: string): string;
/** A flag's value, or nothing. Shared so the flags read alike and an empty
 *  value is treated as absent everywhere rather than in most places. */
export declare function flagFrom(argv: readonly string[], name: string): string | undefined;
/** The `orbit` executable to drive, so a checkout can point at its own. */
export declare function orbitCommandFrom(argv: readonly string[], env: Record<string, string | undefined>): string;
/** Who Orbit records as having done this; see `BridgeOptions.actor`. */
export declare function actorFrom(argv: readonly string[], env: Record<string, string | undefined>): string;
/** Whether this module is the program, rather than something a test imported.
 *  Compared as resolved paths: a suffix match would also be true of any file
 *  that happens to end the same way. */
export declare function isEntryPoint(moduleUrl: string, invokedAs: string | undefined): boolean;
