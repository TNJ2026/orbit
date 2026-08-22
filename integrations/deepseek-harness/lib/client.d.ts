import type { ClientContext, ConversationNodeDefinition } from '@deepseek-ai/dsh-client-runtime/client';
import type { OrbitRunCheckpoint, OrbitRunEnded, OrbitRunStarted } from './types.js';
type StartedData = Omit<OrbitRunStarted, 'type'>;
type CheckpointData = Omit<OrbitRunCheckpoint, 'type'>;
type EndedData = Omit<OrbitRunEnded, 'type'>;
declare module '@deepseek-ai/dsh-session/types' {
    interface SessionEventMap {
        'orbit/run-started': StartedData;
        'orbit/run-checkpoint': CheckpointData;
        'orbit/run-ended': EndedData;
    }
}
export interface OrbitRunCardData {
    runId: string;
    workspaceId: string;
    goal: string;
    status: string;
    artifactCount: number;
    revision: number;
    terminal: boolean;
}
declare module '@deepseek-ai/dsh-client-ui-conversation/client' {
    interface ChatNodeDataMap {
        'orbit-run': OrbitRunCardData;
    }
}
interface State extends OrbitRunCardData {
    sourcePosition: number;
}
export declare const orbitRunDefinition: ConversationNodeDefinition<State>;
export declare const inject: string[];
export declare function apply(ctx: ClientContext): void;
export {};
