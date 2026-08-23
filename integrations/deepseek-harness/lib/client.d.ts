/**
 * The only thing this bundle puts in front of a person: a way to leave.
 *
 * `/orbit` takes no argument and renders nothing. Orbit's own Runtime UI is
 * where Runs are read and driven, so the command's whole job is to open it —
 * anything more would be a second interface competing with the first.
 *
 * @module @orbit-runtime/dsh-orbit/client
 */
import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client';
export declare function registerOrbitSlashSource(ctx: ClientContext): void;
export declare const inject: string[];
export declare function apply(ctx: ClientContext): void;
