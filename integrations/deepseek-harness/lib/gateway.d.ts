import type { WorkspaceRef, RunDto } from './types.js';
export declare class OrbitGateway {
    private readonly command;
    private readonly runtimes;
    constructor(command?: string);
    acquire(workspace: WorkspaceRef): Promise<() => Promise<void>>;
    call(workspace: WorkspaceRef, sessionId: string, name: string, args: object): Promise<unknown>;
    run(workspace: WorkspaceRef, sessionId: string, runId: string): Promise<RunDto>;
    private runtime;
    private start;
    private rpc;
    private callRaw;
    private stop;
}
