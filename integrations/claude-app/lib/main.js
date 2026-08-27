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
import { realpath } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { OrbitGateway } from '@orbit-runtime/integration-core';
import { DEFAULT_ACTOR, OrbitBridge } from './bridge.js';
import { LineReader, PARSE_ERROR, errorReply, expectsReply, frame, parse } from './stdio.js';
/** The project to serve, from the flag or from where this was launched. */
export function projectRootFrom(argv, cwd) {
    return flagFrom(argv, '--project-root') ?? cwd;
}
/** A flag's value, or nothing. Shared so the flags read alike and an empty
 *  value is treated as absent everywhere rather than in most places. */
export function flagFrom(argv, name) {
    const at = argv.indexOf(name);
    const given = at >= 0 ? argv[at + 1] : undefined;
    return given !== undefined && given !== '' && !given.startsWith('--') ? given : undefined;
}
/** The `orbit` executable to drive, so a checkout can point at its own. */
export function orbitCommandFrom(argv, env) {
    return flagFrom(argv, '--orbit-command') ?? env.ORBIT_COMMAND ?? 'orbit';
}
/** Who Orbit records as having done this; see `BridgeOptions.actor`. */
export function actorFrom(argv, env) {
    return flagFrom(argv, '--actor') ?? env.ORBIT_ACTOR ?? DEFAULT_ACTOR;
}
async function main() {
    const argv = process.argv.slice(2);
    const root = await realpath(projectRootFrom(argv, process.cwd()));
    // `full`, not the Harness's subset. A Runtime this server starts exists for
    // a general-purpose client, which has no use for the authoring claim loop
    // and every use for the tools that subset leaves out. A Runtime somebody
    // else already started keeps whatever it was started with — the profile
    // belongs to the Runtime, not to whoever connects.
    const gateway = new OrbitGateway(orbitCommandFrom(argv, process.env), [], undefined, undefined, 'full');
    const actor = actorFrom(argv, process.env);
    const bridge = new OrbitBridge({
        workspace: { id: root, canonicalPath: root },
        gateway,
        actor,
    });
    process.stderr.write(`claude-orbit: serving ${root} as ${actor}\n`);
    const reader = new LineReader();
    process.stdin.setEncoding('utf8');
    // Messages are answered in the order they arrive. MCP allows concurrency,
    // but a Runtime that is still starting is the common case here, and letting
    // a second request start a second Runtime is worse than making it wait.
    let queue = Promise.resolve();
    const handle = (line) => {
        queue = queue.then(async () => {
            const message = parse(line);
            if ('parseError' in message) {
                process.stdout.write(frame(errorReply(null, PARSE_ERROR, message.parseError)));
                return;
            }
            const reply = await bridge.forward(message);
            // A notification asked for nothing; sending it an answer is a message
            // the peer has no record of requesting.
            if (expectsReply(message))
                process.stdout.write(frame(reply));
        });
    };
    process.stdin.on('data', chunk => { for (const line of reader.push(String(chunk)))
        handle(line); });
    // A client that goes away closes the pipe; a client that is killed may not.
    // Either way this exits when it has nothing left to answer — and never kills
    // the Runtime, which is independent and may be serving somebody else.
    await new Promise(resolve => {
        process.stdin.on('end', resolve);
        for (const signal of ['SIGINT', 'SIGTERM']) {
            process.on(signal, () => { resolve(); });
        }
    });
    const tail = reader.rest();
    if (tail)
        handle(tail);
    await queue;
}
/** Whether this module is the program, rather than something a test imported.
 *  Compared as resolved paths: a suffix match would also be true of any file
 *  that happens to end the same way. */
export function isEntryPoint(moduleUrl, invokedAs) {
    if (invokedAs === undefined || invokedAs === '')
        return false;
    return fileURLToPath(moduleUrl) === resolve(invokedAs);
}
if (isEntryPoint(import.meta.url, process.argv[1])) {
    main().catch((error) => {
        process.stderr.write(`claude-orbit: ${String(error)}\n`);
        process.exit(1);
    });
}
