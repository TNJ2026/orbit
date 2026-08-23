/**
 * `/orbit` opens Orbit's own Runtime UI inside the Harness window.
 *
 * The panel holds an iframe and nothing else. Every earlier version of this
 * module drew Orbit's data itself — a Run drawer, a Workflow picker, a
 * settings page — and each was a second account of something Orbit already
 * renders. Showing Orbit's own page instead means there is one interface, and
 * this module's whole job is deciding when it is visible.
 *
 * @module @orbit-runtime/dsh-orbit/client
 */
import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client';
export declare function registerOrbitSlashSource(ctx: ClientContext): void;
export declare const inject: string[];
export declare function apply(ctx: ClientContext): void;
