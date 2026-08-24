import type { Context } from '@deepseek-ai/cordis';
import { OrbitGateway } from './gateway.js';
import type { AuthoringJob, WorkspaceRef } from './types.js';
/** Told when an authoring job starts here, so the panel can show it running. */
export type AuthoringWatcher = (workspace: WorkspaceRef, sessionId: string, job: AuthoringJob) => void;
export declare class OrbitToolBridge {
    private readonly ctx;
    private readonly gateway;
    private readonly watch;
    private readonly tools;
    private readonly registry;
    constructor(ctx: Context, gateway: OrbitGateway, watch?: AuthoringWatcher);
    register(): void;
    private definitions;
    private definition;
    private command;
    private call;
    private route;
}
