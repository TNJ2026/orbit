/** Orbit, to any integration that is not this one.
 *
 * What lives here is everything an Orbit integration needs that is not about
 * a particular host: finding or starting a Runtime, speaking to it, reading
 * what it says back, and turning a failure into a sentence. What does not live
 * here is anything that knows what a panel, a composer, or a slash command is
 * — those are a host's shape, and every host has a different one.
 *
 * The test is mechanical: a module belongs here if it imports nothing from a
 * host SDK and touches no DOM. Two consumers keep that honest — the
 * DeepSeek-Harness plugin and the Claude MCP server — because a module that
 * only ever had one caller was never shown to be reusable.
 */
export * from './artifact-export.js';
export * from './authoring-claim.js';
export * from './authoring-progress.js';
export * from './codecs.js';
export * from './commands.js';
export * from './error-text.js';
export * from './gateway.js';
export * from './orbit-model.js';
export * from './run-progress.js';
export * from './session-bridge.js';
export * from './types.js';
export * from './workflow-catalog.js';
