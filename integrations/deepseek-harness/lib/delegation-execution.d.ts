import type { Agent } from '@deepseek-ai/dsh-agent';
import type { SubagentRuntime } from '@deepseek-ai/dsh-subagent';
import { snapshotWorkspace, type WorkspaceSnapshot } from './effects.js';
import type { DelegationDto, WorkspaceRef } from './types.js';
export interface DelegationExecutionPorts {
    settle(result?: unknown, error?: string): Promise<void>;
    renew(): Promise<DelegationDto>;
    snapshot(path: string): Promise<WorkspaceSnapshot | null>;
    renewalMilliseconds: number;
}
/** Execute one already-leased job. A rejected published result is deliberately
 * left unsettled: Orbit expires the Job Lease to unknown and never replays it. */
export declare function executeDelegation(workspace: WorkspaceRef, delegation: DelegationDto, parent: Agent, subagents: SubagentRuntime, outerSignal: AbortSignal, ports: DelegationExecutionPorts): Promise<void>;
export declare const defaultDelegationExecutionPorts: {
    snapshot: typeof snapshotWorkspace;
    renewalMilliseconds: number;
};
