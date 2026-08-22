import type { OrbitSessionEvent } from './types.js';
export interface OrbitRunCard {
    runId: string;
    sourcePosition: number;
    goal?: string;
    workflowId?: string;
    workflowVersion?: number;
    revision: number;
    status: string;
    artifactCount: number;
    updatedAt: string;
    terminal: boolean;
}
export type OrbitRunCardState = Record<string, OrbitRunCard>;
export declare function reduceOrbitRunCards(state: OrbitRunCardState, event: OrbitSessionEvent): OrbitRunCardState;
